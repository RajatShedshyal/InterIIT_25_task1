# streamlit_app.py (revamped UI)
import os
import time
import json
import pandas as pd
import streamlit as st
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = None

# ⬇️ Imports: trading hypotheses + research chat
from agent import generate_hypotheses_llm, chat_research_llm

# ---------- Small helper: nice IST timestamp ----------
def now_ist_str():
    dt = datetime.now(IST or timezone.utc).astimezone(IST or timezone.utc)
    return dt.strftime("%d %b %Y, %H:%M:%S IST")

# ---------- Simple runtime logger (kept silent by default) ----------
def _ts():
    return datetime.now(IST or timezone.utc).astimezone(IST or timezone.utc).strftime("%H:%M:%S")

def log(msg):
    # No UI toggle; only appended for optional expander below
    st.session_state.setdefault("_logs", [])
    line = f"[{_ts()}] {msg}"
    st.session_state["_logs"].append(line)
    if len(st.session_state["_logs"]) > 500:
        st.session_state["_logs"] = st.session_state["_logs"][-500:]
    # Also print to console for devs
    print(line)

# A nonce we bump when the user clicks the fetch button
st.session_state.setdefault("_refresh_nonce", 0)

# Carousel index for hypotheses
st.session_state.setdefault("_hypo_idx", 0)

# Cache for last-fetched hypotheses payload (so we can page locally)
st.session_state.setdefault("_hypo_payload", {"hypotheses": [], "meta": {}})

# ---------- Page config ----------
st.set_page_config(page_title="Agentic Market Research & Live Hypotheses", layout="wide")
st.title("📈 Agentic Market Research & Live Hypotheses")
st.caption(f"Session started: {now_ist_str()}")

# ---------- CSS (Aesthetic cards, dark-mode aware) ----------
st.markdown(
    """
    <style>
      :root{
        --card-bg: linear-gradient(180deg, rgba(255,255,255,0.85) 0%, rgba(248,250,252,0.9) 100%);
        --card-border: rgba(15,23,42,0.08);
        --shadow: 0 6px 18px rgba(2,6,23,0.08);
        --muted: #6b7280;
        --chip: #111827;
        --chip-bg: #f3f4f6;
        --risk-bg: #fff7ed; --risk-text:#9a3412;
        --pill-bg:#f3f4f6; --pill-text:#374151; --pill-border:#e5e7eb;
        --good:#065f46; --good-bg:#ecfdf5; --good-bd:#10b98133;
        --bad:#991b1b;  --bad-bg:#fef2f2; --bad-bd:#ef444433;
        --watch:#3730a3; --watch-bg:#eef2ff; --watch-bd:#6366f133;
      }
      @media (prefers-color-scheme: dark){
        :root{
          --card-bg: linear-gradient(180deg, rgba(30,41,59,0.55) 0%, rgba(15,23,42,0.4) 100%);
          --card-border: rgba(148,163,184,0.2);
          --shadow: 0 8px 20px rgba(0,0,0,0.35);
          --muted:#cbd5e1;
          --chip:#e5e7eb; --chip-bg:#0b1220;
          --risk-bg:#2b1d11; --risk-text:#fbbf24;
          --pill-bg:#0b1220; --pill-text:#e5e7eb; --pill-border:#1f2937;
          --good:#34d399; --good-bg:#064e3b; --good-bd:#065f4633;
          --bad:#f87171;  --bad-bg:#7f1d1d; --bad-bd:#ef444433;
          --watch:#a5b4fc; --watch-bg:#1e1b4b; --watch-bd:#6366f133;
        }
      }
      .hypo-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px; padding: 14px 16px; box-shadow: var(--shadow); backdrop-filter: blur(6px);}
      .hypo-header { display:flex; align-items:center; justify-content:space-between; gap: 12px; margin-bottom: 6px; }
      .hypo-title { font-weight: 700; font-size: 1rem; line-height: 1.2; letter-spacing:.2px; }
      .chip { padding: 4px 10px; border-radius: 999px; font-size: 0.75rem; border: 1px solid rgba(0,0,0,0.08); background: var(--chip-bg); color: var(--chip); text-transform: uppercase; letter-spacing:.4px; }
      .chip-long  { background:var(--good-bg);  color:var(--good);  border-color:var(--good-bd); }
      .chip-short { background:var(--bad-bg);   color:var(--bad);   border-color:var(--bad-bd); }
      .chip-watch { background:var(--watch-bg); color:var(--watch); border-color:var(--watch-bd); }
      .muted { color: var(--muted); font-size: 0.9rem; }
      .metrics-grid { display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 6px 10px; margin-top: 8px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 0.86rem;}
      .metric { padding:6px 8px; border:1px dashed var(--card-border); border-radius: 10px; }
      .metric strong { font-weight: 600; margin-right: 4px; }
      .conf-wrap { margin-top:10px; }
      .conf-bar { height:8px; border-radius:999px; background:rgba(0,0,0,0.08); overflow:hidden; border:1px solid var(--card-border); }
      .conf-fill { height:100%; border-radius:999px; background:linear-gradient(90deg, #22c55e, #3b82f6); }
      .footer { margin-top:10px; display:flex; gap:8px; align-items:center; justify-content:flex-start; }
      .pill { display:inline-block; padding:5px 10px; border-radius:999px; background:var(--pill-bg); color:var(--pill-text); border:1px solid var(--pill-border); margin-right:6px; margin-top:6px; font-size:.8rem; }
      .nav-row { display:flex; align-items:center; justify-content:space-between; gap:10px; }
      .nav-idx { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; opacity:.8; }
      .arrow-btn { width:100%; padding:8px 0; border-radius:10px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------- Cached LLM wrapper ----------
@st.cache_data(ttl=300, show_spinner=False)
def get_hypotheses_cached(symbols_tuple, window_int, nonce):
    # nonce busts the cache when the user clicks the fetch button
    return generate_hypotheses_llm(list(symbols_tuple), int(window_int))

# ---------- Layout: LEFT = Research Chat, RIGHT = Hypotheses Carousel ----------
left, right = st.columns([1.55, 1.45], gap="large")

# ===== LEFT: Research Chat =====
with left:
    st.subheader("🌐 Research Chat")
    st.caption(f"Last opened: {now_ist_str()}")

    # Sidebar contains only Research Chat settings, so we show the helper text here
    if "chat_msgs" not in st.session_state:
        st.session_state["chat_msgs"] = []

    # Controls row (Clear chat)
    colA, colB, colC = st.columns([1, 1, 1])
    with colA:
        if st.button("🧹 Clear chat"):
            st.session_state["chat_msgs"] = []
            st.rerun()
    with colB:
        st.write("")  # spacer
    with colC:
        st.write("")  # spacer

    # Messenger-like window
    chat_window = st.container(height=650, border=True)

    with chat_window:
        for m in st.session_state["chat_msgs"]:
            avatar = "🧑‍💻" if m["role"] == "user" else "🧠"
            with st.chat_message(m["role"], avatar=avatar):
                st.markdown(m["content"])  # Markdown-rendered
                if m.get("when"):
                    st.caption(m["when"])

    # Input box
    prompt = st.chat_input("Type your message…")
    if prompt:
        st.session_state["chat_msgs"].append({
            "role": "user",
            "content": prompt,
            "when": now_ist_str()
        })
        try:
            with st.spinner("Researching…"):
                # Recency + topk taken from sidebar controls
                answer_md = chat_research_llm(prompt, recency_days=st.session_state.get("_recency_days", 30), top_k=st.session_state.get("_topk", 6))
        except Exception as e:
            answer_md = f"Sorry, I ran into an error: `{e}`"
        st.session_state["chat_msgs"].append({
            "role": "assistant",
            "content": answer_md,
            "when": now_ist_str()
        })
        st.rerun()

# ===== RIGHT: Live Hypotheses (Carousel + Fetch button) =====
with right:
    st.subheader("💡 Live Trading Hypotheses")

    # Inline controls + centered nav/fetch row
    with st.container(border=True):
        # Row 1: inputs
        r1c1, r1c2 = st.columns([2, 1])
        with r1c1:
            symbols_text = st.text_input("Symbols (comma‑separated)", os.getenv("SYMBOLS", "AAPL,MSFT,TSLA"))
        with r1c2:
            window = st.number_input("Bars per symbol", 10, 300, 60, 10)

        # Row 2: centered buttons (Prev | Fetch | Next)
        sL, c_prev, c_fetch, c_next, sR = st.columns([1, 1, 2, 1, 1])
        with c_prev:
            if st.button("◀ Prev", use_container_width=True, key="prev_top"):
                total = len(st.session_state["_hypo_payload"].get("hypotheses", []))
                if total:
                    st.session_state["_hypo_idx"] = (st.session_state["_hypo_idx"] - 1) % total
        with c_fetch:
            if st.button("⚡ Get live hypotheses", use_container_width=True, key="fetch_hypos"):
                symbols = [s.strip().upper() for s in symbols_text.split(",") if s.strip()]
                st.session_state["_refresh_nonce"] = time.time()
                log(f"Fetching hypotheses for: {symbols} (win={int(window)})")
                try:
                    with st.spinner("Thinking with tools…"):
                        payload = get_hypotheses_cached(tuple(symbols), int(window), st.session_state["_refresh_nonce"])
                    # Normalize payload
                    cards = payload.get("hypotheses", []) if isinstance(payload, dict) else []
                    meta_info = payload.get("meta", {}) if isinstance(payload, dict) else {}
                    st.session_state["_hypo_payload"] = {"hypotheses": cards, "meta": meta_info}
                    st.session_state["_hypo_idx"] = 0
                except Exception as e:
                    st.error(f"LLM/agent error: {e}")
                    log(f"ERROR in hypotheses fetch: {e}")
        with c_next:
            if st.button("Next ▶", use_container_width=True, key="next_top"):
                total = len(st.session_state["_hypo_payload"].get("hypotheses", []))
                if total:
                    st.session_state["_hypo_idx"] = (st.session_state["_hypo_idx"] + 1) % total

    # Render carousel area
    payload = st.session_state["_hypo_payload"]
    cards = payload.get("hypotheses", [])
    meta_info = payload.get("meta", {}) or {}

    if not cards:
        st.info("No hypotheses yet. Click **Get live hypotheses** above.")
    else:
        idx = st.session_state.get("_hypo_idx", 0)
        idx = max(0, min(idx, len(cards)-1))
        h = cards[idx]

        # Extract fields safely
        direction = (h.get("direction") or "watch").lower()
        chip_cls = "chip-watch" if direction not in ("long", "short") else f"chip-{direction}"

        title = h.get("title", "Hypothesis")
        horizon = h.get("time_horizon", "intraday")
        conf = float(h.get("confidence", 0) or 0.0)
        conf_pct = max(0, min(100, int(round(conf * 100))))

        rationale = h.get("rationale", "")
        metrics = (h.get("metrics", {}) or {})
        risks = (h.get("risk_flags", []) or [])
        related = (h.get("related_symbols", []) or [])
        symbol = h.get("symbol", "")

        last_close = metrics.get("last_close", "—")
        sma5 = metrics.get("sma5", "—")
        sma20 = metrics.get("sma20", "—")
        ret_5 = metrics.get("ret_5", "—")
        vol_mult = metrics.get("vol_mult", "—")

        def colorize(val, is_pct=False):
            try:
                v = float(val)
            except Exception:
                return str(val)
            txt = f"{v:+.2%}" if is_pct else f"{v:.2f}"
            col = "#22c55e" if v > 0 else ("#ef4444" if v < 0 else "inherit")
            return f"<span style='color:{col};'>{txt}</span>"

        ret_5_html = colorize(ret_5, is_pct=True)
        vol_html = colorize(vol_mult, is_pct=False)
        conf_style = f"width:{conf_pct}%;"

        # Top row: index + total
        t1, t2, t3 = st.columns([1, 2, 1])
        with t1:
            st.write("")
        with t2:
            st.markdown(f"<div class='nav-idx' style='text-align:center;'>Card {idx+1} / {len(cards)}</div>", unsafe_allow_html=True)
        with t3:
            st.write("")

        # Card itself
        st.markdown(
            f"""
            <div class="hypo-card">
              <div class="hypo-header">
                <div class="hypo-title">{title}</div>
                <div class="chip {chip_cls}">{'LONG' if direction=='long' else ('SHORT' if direction=='short' else 'WATCH')}</div>
              </div>

              <div class="muted">{(f"<b>{symbol}</b> • " if symbol else "")}{horizon} • confidence: {conf:.2f}</div>

              <div style="margin:10px 0 6px 0;">
                <div class="muted">{rationale}</div>
              </div>

              <div class="metrics-grid">
                <div class="metric"><strong>Close</strong> {last_close}</div>
                <div class="metric"><strong>SMA5</strong> {sma5}</div>
                <div class="metric"><strong>SMA20</strong> {sma20}</div>
                <div class="metric"><strong>Ret₅</strong> {ret_5_html}</div>
                <div class="metric"><strong>Vol×</strong> {vol_html}</div>
              </div>

              {"<div style='margin-top:10px;'>" + "".join([f"<span class='pill'>⚠ {r}</span>" for r in risks]) + "</div>" if risks else ""}
              {"<div style='margin-top:8px;'>" + "".join([f"<span class='pill'>#{s}</span>" for s in related]) + "</div>" if related else ""}

              <div class="conf-wrap">
                <div class="muted" style="margin-bottom:6px;">Confidence</div>
                <div class="conf-bar"><div class="conf-fill" style="{conf_style}"></div></div>
              </div>

              <div class="footer muted">
                <span>🔎 TA: SMA5/SMA20 • Ret5 • Volume spike</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Meta info
        if meta_info:
            provider = meta_info.get("provider", "")
            model = meta_info.get("model", "")
            gen_at = meta_info.get("generated_at", "")
            # keep this subtle; no provider secrets
            stamp = f"Generated: {gen_at}" if gen_at else ""
            if stamp:
                st.caption(stamp)

# ---------- Sidebar: ONLY Research Chat settings ----------
with st.sidebar:
    st.header("Research Chat settings")
    st.session_state["_recency_days"] = st.slider("Recency window (days)", 1, 120, st.session_state.get("_recency_days", 30))
    st.session_state["_topk"] = st.slider("Max results per search", 3, 15, st.session_state.get("_topk", 6))
    st.markdown("---")
    st.caption("Tip: Use the **Get live hypotheses** button to refresh cards.")

# ---------- Optional: Runtime log (collapsed) ----------
with st.expander("🧪 Runtime log (latest first)", expanded=False):
    for line in reversed(st.session_state.get("_logs", [])):
        st.markdown(f"<code>{line}</code>", unsafe_allow_html=True)

st.caption("Built with a tool-calling agent, web search, and your hypotheses generator.")
