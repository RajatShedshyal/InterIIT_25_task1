# langchain_app/agent.py
from __future__ import annotations
from typing import Dict, Any, List
import json, os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate

# Use your existing @tool-decorated tool
from tools import market_snapshot, web_search

# System message: NO braces/JSON here (to avoid PromptTemplate var parsing)
SYSTEM = (
    "You are an agentic trading research assistant. "
    "First call the tool `market_snapshot` with BOTH parameters: "
    "symbols (list of tickers) and window (int). "
    "After receiving the tool observation, produce the FINAL ANSWER "
    "as STRICT JSON exactly in the schema provided in the user input. "
    "If bars < 20, still return best-effort with lower confidence. "
    "Keep 1–3 hypotheses total, sorted by confidence desc. "
    "Output ONLY JSON in the final answer—no extra text."
)

# Tool-calling agents expect this structure
PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM),
    ("human", "{input}"),
    ("placeholder", "{agent_scratchpad}"),
])


def _get_llm() -> ChatGoogleGenerativeAI:
    model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set.")
    return ChatGoogleGenerativeAI(
        model=model,
        temperature=0.2,
        api_key=api_key,
        # safety_settings=None,    # uncomment during debugging if safety blocks output
        # max_output_tokens=1024,  # optional if supported by your version
    )


def build_agent() -> AgentExecutor:
    llm = _get_llm()
    tools = [market_snapshot]  # your @tool-decorated function
    agent = create_tool_calling_agent(llm, tools, PROMPT)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,               # set True to watch tool calls; False once stable
        handle_parsing_errors=True,
        max_iterations=10,
        return_intermediate_steps=False,
    )


def _extract_json(s: str) -> Dict[str, Any]:
    s = (s or "").strip()
    try:
        return json.loads(s)
    except Exception:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start:end+1])
        raise ValueError(f"Agent did not return valid JSON:\n{s}")


def generate_hypotheses_llm(symbols: List[str], window: int = 60) -> Dict[str, Any]:
    """
    Runs a Gemini tool-calling agent: it will call market_snapshot and return JSON hypotheses.
    """
    executor = build_agent()

    # Put the JSON schema in the USER INPUT (no template parsing here, so no escaping issues)
    schema = """
Return ONLY valid JSON with this schema:
{
  "hypotheses": [
    {
      "symbol": "TICKER",
      "title": "...",
      "direction": "long" | "short" | "watch",
      "time_horizon": "intraday" | "1-3d",
      "confidence": number between 0 and 1,
      "rationale": "one paragraph",
      "risk_flags": ["..."],
      "metrics": { "last_close": number, "sma5": number, "sma20": number, "ret_5": number, "vol_mult": number },
      "related_symbols": ["SYM"]
    }
  ],
  "meta": { "window": <int> }
}
Rules:
- ALWAYS call the tool `market_snapshot` FIRST with both parameters: symbols=[...], window=<int>.
- If bars < 20, still produce best-effort with lower confidence.
- Keep 1–3 hypotheses total, sorted by confidence desc.
- Output ONLY JSON. No extra text.
""".strip()

    user_input = (
        f"{schema}\n\n"
        f"Symbols: {', '.join([s.upper().strip() for s in symbols])}\n"
        f"Window: {int(window)}"
    )

    result = executor.invoke({"input": user_input})
    output_text = result.get("output", "")
    payload = _extract_json(output_text)

    # Ensure meta + provider/model for UI
    payload.setdefault("meta", {})
    payload["meta"].setdefault("window", int(window))
    payload["meta"]["provider"] = "gemini-api"
    payload["meta"]["model"] = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    payload["meta"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload.setdefault("hypotheses", [])
    return payload


# ---------------- Research/Chat Agent (new) ----------------

SYSTEM_RESEARCH = (
    "You are a web research copilot.\n"
    "When helpful, call the `web_search` tool to fetch fresh information. "
    "You may call it multiple times with refined queries.\n"
    "Write a concise, factual answer in Markdown with bullet points and short paragraphs. "
    "At the end, include a 'Sources' section listing the URLs you actually used."
)

PROMPT_RESEARCH = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_RESEARCH),
    ("human", "{question}"),
    ("placeholder", "{agent_scratchpad}"),
])

def build_research_agent() -> AgentExecutor:
    llm = _get_llm()
    tools = [web_search, market_snapshot]
    agent = create_tool_calling_agent(llm, tools, PROMPT_RESEARCH)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        handle_parsing_errors=True,
        max_iterations=8,
        return_intermediate_steps=False,
    )

def chat_research_llm(question: str, recency_days: int = 30, top_k: int = 5) -> str:
    """
    Free-form chat/research. The agent can call `web_search` and returns Markdown text.
    """
    executor = build_research_agent()

    # Nudge the model to include recency in its first search
    augmented = (
        f"Task: {question}\n\n"
        f"If you search, prefer items from the last {recency_days} days. "
        f"When calling web_search, pass recency_days={recency_days} and top_k={top_k}."
    )
    result = executor.invoke({"question": augmented})
    return result.get("output", "").strip()