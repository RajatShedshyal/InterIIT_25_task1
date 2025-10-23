"""
alpaca_poll.py — Minimal Alpaca → SQLite ingestor (1‑min bars)
• IEX (free) optimized: pulls dense Regular Trading Hours (RTH) windows
• Falls back to the most recent completed RTH outside live RTH
• Adds high limit + per‑symbol diagnostics
"""

import os
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import pandas as pd
import pytz

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

# --- 1) Env ---
load_dotenv()
ALPACA_KEY_ID = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT").split(",") if s.strip()]
if not ALPACA_KEY_ID or not ALPACA_SECRET_KEY:
    raise RuntimeError("❌ Set ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env")

# --- 2) Client + DB ---
os.makedirs("store", exist_ok=True)                     # ensure folder exists
DB_PATH = os.getenv("DB_PATH", "store/market.sqlite")
client = StockHistoricalDataClient(ALPACA_KEY_ID, ALPACA_SECRET_KEY)

with sqlite3.connect(DB_PATH) as conn:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_bars (
            ts_utc TEXT NOT NULL,
            symbol TEXT NOT NULL,
            o REAL, h REAL, l REAL, c REAL, v REAL,
            src TEXT DEFAULT 'alpaca',
            PRIMARY KEY (ts_utc, symbol)
        )
        """
    )
    conn.commit()

print(f"✅ Connected to DB at {DB_PATH}")
print(f"✅ Tracking symbols: {SYMBOLS}")

# --- 3) Time helpers (IEX = prefer RTH) ---
NY = pytz.timezone("America/New_York")

def most_recent_rth_session(now_utc: datetime):
    """Return (open_utc, close_utc) for the most recent completed or ongoing RTH.
    If within today's RTH, returns today's 09:30–16:00. If outside, returns previous weekday's RTH.
    Note: This does not check US market holidays; on a holiday it will still pick the previous weekday.
    """
    dt_ny = now_utc.astimezone(NY)
    # If weekend, step back to previous weekday
    while dt_ny.weekday() >= 5:  # 5=Sat, 6=Sun
        dt_ny -= timedelta(days=1)
    # Candidate: today's RTH
    open_ny = dt_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    close_ny = dt_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    now_ny = now_utc.astimezone(NY)

    if now_ny < open_ny:
        # Before today's open → pick previous weekday
        prev = dt_ny - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        open_ny = prev.replace(hour=9, minute=30, second=0, microsecond=0)
        close_ny = prev.replace(hour=16, minute=0, second=0, microsecond=0)
    # If after close or during RTH, keep today's window

    return open_ny.astimezone(timezone.utc), close_ny.astimezone(timezone.utc)

def pick_window_for_iex(now_utc: datetime):
    """For IEX (free), prefer dense RTH. If currently in RTH: open→now. Else: last full RTH.
    Returns (start_ts, end_ts).
    """
    open_utc, close_utc = most_recent_rth_session(now_utc)
    if open_utc <= now_utc <= close_utc:
        return open_utc, now_utc
    return open_utc, close_utc

# --- 4) Poll loop ---
while True:
    try:
        now = datetime.now(timezone.utc)

        # Choose a window that yields dense bars on IEX
        start_ts, end_ts = pick_window_for_iex(now)

        req = StockBarsRequest(
            symbol_or_symbols=SYMBOLS,
            timeframe=TimeFrame.Minute,
            start=start_ts,
            end=end_ts,
            feed=DataFeed.IEX,      # free plan (sparse outside RTH)
            limit=10000,            # avoid silent truncation
        )

        bars = client.get_stock_bars(req)
        rows = []

        def to_utc_iso(ts):
            ts = pd.Timestamp(ts)
            if ts.tzinfo is None or ts.tz is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            return ts.to_pydatetime().isoformat()

        # --- Diagnostics ---
        def debug_bars(obj):
            try:
                if hasattr(obj, "df") and isinstance(obj.df, pd.DataFrame):
                    df = obj.df.reset_index()
                    if df.empty:
                        print("Fetched rows: 0")
                    else:
                        ts_col = "timestamp" if "timestamp" in df.columns else ("time" if "time" in df.columns else None)
                        syms = sorted(df.get("symbol", pd.Series(dtype=str)).astype(str).str.upper().unique().tolist()) if "symbol" in df.columns else []
                        print(f"Fetched rows: {len(df)} | symbols: {syms}")
                        if ts_col:
                            tmin, tmax = pd.to_datetime(df[ts_col]).min(), pd.to_datetime(df[ts_col]).max()
                            print(f"Time range (UTC): {tmin} → {tmax}")
                elif hasattr(obj, "data") and isinstance(obj.data, dict):
                    counts = {str(s).upper(): len(lst) for s, lst in obj.data.items()}
                    print(f"Per-symbol counts: {counts}")
                else:
                    print("Bars shape:", type(getattr(obj, "df", None)), type(getattr(obj, "data", None)))
            except Exception as ee:
                print("Debug error:", ee)

        debug_bars(bars)

        # --- Case 1: DataFrame interface (preferred) ---
        if hasattr(bars, "df") and isinstance(bars.df, pd.DataFrame):
            df = bars.df
            if not df.empty:
                df = df.reset_index()
                ts_col = "timestamp" if "timestamp" in df.columns else ("time" if "time" in df.columns else None)
                if ts_col is None:
                    raise RuntimeError(f"Bars DF missing timestamp column. Columns: {df.columns.tolist()}")
                if "symbol" not in df.columns:
                    raise RuntimeError(f"Bars DF missing 'symbol' column. Columns: {df.columns.tolist()}")

                o_col = "open"   if "open"   in df.columns else ("o" if "o" in df.columns else None)
                h_col = "high"   if "high"   in df.columns else ("h" if "h" in df.columns else None)
                l_col = "low"    if "low"    in df.columns else ("l" if "l" in df.columns else None)
                c_col = "close"  if "close"  in df.columns else ("c" if "c" in df.columns else None)
                v_col = "volume" if "volume" in df.columns else ("v" if "v" in df.columns else None)
                for name, col in dict(o=o_col, h=h_col, l=l_col, c=c_col, v=v_col).items():
                    if col is None:
                        raise RuntimeError(f"Bars DF missing '{name}' column. Columns: {df.columns.tolist()}")

                for _, r in df.iterrows():
                    rows.append((
                        to_utc_iso(r[ts_col]),
                        str(r["symbol"]).upper(),
                        float(r[o_col]), float(r[h_col]), float(r[l_col]), float(r[c_col]),
                        int(r[v_col]),
                        "alpaca",
                    ))

        # --- Case 2: Mapping dict[str, list[Bar]] ---
        elif hasattr(bars, "data") and isinstance(bars.data, dict) and bars.data:
            for sym, bar_list in bars.data.items():
                sym = str(sym).upper()
                for b in bar_list:
                    ts_attr = getattr(b, "timestamp", getattr(b, "time", None))
                    o = getattr(b, "open", getattr(b, "o", None))
                    h = getattr(b, "high", getattr(b, "h", None))
                    l = getattr(b, "low", getattr(b, "l", None))
                    c = getattr(b, "close", getattr(b, "c", None))
                    v = getattr(b, "volume", getattr(b, "v", None))
                    if None in (ts_attr, o, h, l, c, v):
                        continue
                    rows.append((to_utc_iso(ts_attr), sym, float(o), float(h), float(l), float(c), int(v), "alpaca"))

        # --- Case 3: Flat list[list[Bar]] ---
        elif hasattr(bars, "data") and isinstance(bars.data, list) and bars.data:
            for b in bars.data:
                sym = str(getattr(b, "symbol", "")).upper() or (SYMBOLS[0] if len(SYMBOLS) == 1 else None)
                ts_attr = getattr(b, "timestamp", getattr(b, "time", None))
                o = getattr(b, "open", getattr(b, "o", None))
                h = getattr(b, "high", getattr(b, "h", None))
                l = getattr(b, "low", getattr(b, "l", None))
                c = getattr(b, "close", getattr(b, "c", None))
                v = getattr(b, "volume", getattr(b, "v", None))
                if not sym or None in (ts_attr, o, h, l, c, v):
                    continue
                rows.append((to_utc_iso(ts_attr), sym, float(o), float(h), float(l), float(c), int(v), "alpaca"))
        else:
            print(
                f"⚠️ Unexpected BarSet shape. df_type={type(getattr(bars,'df',None))}, "
                f"data_type={type(getattr(bars,'data',None))}, "
                f"len_data={(len(bars.data) if hasattr(bars,'data') and hasattr(bars.data,'__len__') else 'n/a')}"
            )

        if rows:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                conn.executemany(
                    """
                    INSERT INTO market_bars
                    (ts_utc, symbol, o, h, l, c, v, src)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ts_utc, symbol) DO UPDATE SET
                        o=excluded.o,
                        h=excluded.h,
                        l=excluded.l,
                        c=excluded.c,
                        v=excluded.v,
                        src=excluded.src
                    """,
                    rows,
                )
                conn.commit()
            print(f"📥 Upserted {len(rows)} rows at {now.isoformat()} | window: {start_ts.isoformat()} → {end_ts.isoformat()}")
        else:
            print(f"ℹ️ No rows this cycle at {now.isoformat()} | window: {start_ts.isoformat()} → {end_ts.isoformat()}")

    except Exception as e:
        print("⚠️ Error fetching Alpaca data:", e)

    time.sleep(60)
