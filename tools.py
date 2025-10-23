# langchain_app/tools.py
from __future__ import annotations
import os, sqlite3, time
from typing import List, Dict, Any
import pandas as pd
from langchain_core.tools import tool

ROOT = os.path.abspath(os.getcwd())
DB_PATH = os.path.join(ROOT, "store", "market.sqlite")

#extrat tools start


def _call_custom_search_api(query: str, recency_days: int = 30, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Optional path: hit your own search microservice if present.
    Expected response shape (example):
      [{"title": "...", "url": "...", "snippet": "...",
        "source":"...","published":"YYYY-MM-DD"}]
    """
    import requests

    endpoint = os.getenv("CUSTOM_SEARCH_ENDPOINT")
    api_key  = os.getenv("CUSTOM_SEARCH_API_KEY")
    if not endpoint:
        return []

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "q": query,
        "recency_days": recency_days,
        "top_k": top_k
    }
    r = requests.post(endpoint, json=payload, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data[:top_k]

def _duckduckgo_search(query: str, recency_days: int = 30, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Fallback path using duckduckgo_search (no API key).
    pip install duckduckgo-search
    """
    from duckduckgo_search import DDGS

    # DDGS().text supports a 'timelimit' like "d", "w", "m", "y"
    # Map days → the coarsest reasonable granularity
    if recency_days <= 1:    timelimit = "d"
    elif recency_days <= 7:  timelimit = "w"
    elif recency_days <= 31: timelimit = "m"
    else:                    timelimit = "y"

    results = []
    with DDGS() as ddg:
        for r in ddg.text(query, region="wt-wt", safesearch="moderate",
                          timelimit=timelimit, max_results=top_k):
            # r keys: title, href, body, date (date sometimes missing)
            results.append({
                "title": r.get("title"),
                "url": r.get("href"),
                "snippet": r.get("body"),
                "source": "duckduckgo",
                "published": r.get("date")  # may be None
            })
    return results[:top_k]


@tool
def web_search(query: str, recency_days: int = 30, top_k: int = 5) -> Dict[str, Any]:
    """
    Search the web for up-to-date information. Returns a dict:
    {
      "query": str,
      "results": [{"title","url","snippet","source","published"}, ...],
      "took_ms": int,
      "backend": "custom" | "duckduckgo"
    }

    Notes:
    - If CUSTOM_SEARCH_ENDPOINT is set, will call it first.
    - Otherwise falls back to DuckDuckGo (duckduckgo-search).
    """
    start = time.time()
    try:
        results = _call_custom_search_api(query, recency_days, top_k)
        backend = "custom"
        if not results:
            results = _duckduckgo_search(query, recency_days, top_k)
            backend = "duckduckgo"
        took_ms = int((time.time() - start) * 1000)
        return {
            "query": query,
            "results": results,
            "took_ms": took_ms,
            "backend": backend
        }
    except Exception as e:
        return {
            "query": query,
            "results": [],
            "took_ms": int((time.time() - start) * 1000),
            "backend": "error",
            "error": str(e)
        }

#extra tools end

def _read_last_n(symbol: str, n: int) -> pd.DataFrame:
    con = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql(
            "SELECT ts_utc, o,h,l,c,v FROM market_bars WHERE symbol=? ORDER BY ts_utc DESC LIMIT ?",
            con, params=[symbol.upper().strip(), n]
        )
        if not df.empty:
            df = df.sort_values("ts_utc").reset_index(drop=True)
        return df
    finally:
        con.close()

@tool("market_snapshot", return_direct=False)
def market_snapshot(symbols: List[str], window: int = 60) -> Dict[str, Any]:
    """
    Return the last `window` bars from SQLite for each symbol as JSON usable by an LLM.
    """
    out: Dict[str, Any] = {"window": window, "series": {}, "meta": {"db_path": DB_PATH}}
    for s in symbols:
        df = _read_last_n(s, window)
        out["series"][s.upper()] = [] if df.empty else [
            {"t": str(r.ts_utc), "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c), "v": float(r.v)}
            for _, r in df.iterrows()
        ]
    return out
