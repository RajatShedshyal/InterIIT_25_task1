# langchain_app/tools.py
from __future__ import annotations
import os, sqlite3, time
from typing import List, Dict, Any
import pandas as pd
from langchain_core.tools import tool
from duckduckgo_search import DDGS
import re, datetime as dt

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

    # Heuristic: is this a finance/stock query?
    def _is_finance(q: str) -> bool:
        q_up = q.upper()
        has_ticker = bool(re.search(r"\b[A-Z]{1,5}\b", q_up))
        finance_terms = ["stock", "stocks", "share", "price", "earnings", "revenue",
                         "guidance", "eps", "results", "analyst", "rating"]
        has_term = any(w in q_up for w in (t.upper() for t in finance_terms))
        company_names = ["TESLA", "APPLE", "MICROSOFT", "GOOGLE", "ALPHABET", "META", "AMAZON", "NVIDIA"]
        return has_ticker or has_term or any(n in q_up for n in company_names)

    # Map recency → DDG timelimit
    if recency_days <= 1:    timelimit = "d"
    elif recency_days <= 7:  timelimit = "w"
    elif recency_days <= 31: timelimit = "m"
    else:                    timelimit = "y"

    def _iso(d):
        if not d: return None
        s = str(d)
        return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else dt.date.today().isoformat()

    def _dedupe_sort(items):
        seen, out = set(), []
        for it in items:
            u = (it.get("url") or it.get("href") or "").split("?", 1)[0].rstrip("/").lower()
            if u and u not in seen:
                seen.add(u); out.append(it)
        def key(it):
            d = it.get("published") or it.get("date")
            try: return dt.date.fromisoformat(d) if d else dt.date(1970,1,1)
            except Exception: return dt.date(1970,1,1)
        out.sort(key=key, reverse=True)
        return out

    is_finance = _is_finance(query)
    results = []

    try:
        with DDGS() as ddg:
            if is_finance:
                # 1) NEWS first
                for r in ddg.news(query, region="wt-wt", safesearch="moderate",
                                  timelimit=timelimit, max_results=max(top_k, 6)):
                    results.append({
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "snippet": r.get("body") or r.get("excerpt"),
                        "source": "duckduckgo_news",
                        "published": _iso(r.get("date")),
                    })
                # 2) Fallback to WEB if thin
                if len(results) < top_k:
                    for r in ddg.text(query, region="wt-wt", safesearch="moderate",
                                      timelimit=timelimit, max_results=max(top_k, 6)):
                        results.append({
                            "title": r.get("title"),
                            "url": r.get("href"),
                            "snippet": r.get("body"),
                            "source": "duckduckgo_text",
                            "published": _iso(r.get("date")),
                        })
            else:
                # General queries (like "who is elon musk") → WEB first
                for r in ddg.text(query, region="wt-wt", safesearch="moderate",
                                  timelimit=timelimit, max_results=max(top_k, 6)):
                    results.append({
                        "title": r.get("title"),
                        "url": r.get("href"),
                        "snippet": r.get("body"),
                        "source": "duckduckgo_text",
                        "published": _iso(r.get("date")),
                    })
                # Fallback to NEWS if needed
                if len(results) < top_k:
                    for r in ddg.news(query, region="wt-wt", safesearch="moderate",
                                      timelimit=timelimit, max_results=max(top_k, 6)):
                        results.append({
                            "title": r.get("title"),
                            "url": r.get("url"),
                            "snippet": r.get("body") or r.get("excerpt"),
                            "source": "duckduckgo_news",
                            "published": _iso(r.get("date")),
                        })
    except Exception as e:
        print(f"[web_search] DDG error: {e}")
        raise

    # If still empty, broaden: remove timelimit and add finance site hints
    if not results and is_finance:
        hints = " site:reuters.com OR site:finance.yahoo.com OR site:investors.tesla.com OR site:seekingalpha.com"
        with DDGS() as ddg:
            for r in ddg.news(query + hints, region="wt-wt", safesearch="moderate",
                              max_results=max(top_k, 6)):
                results.append({
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "snippet": r.get("body") or r.get("excerpt"),
                    "source": "duckduckgo_news",
                    "published": _iso(r.get("date")),
                })

    return _dedupe_sort(results)[:top_k]



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
