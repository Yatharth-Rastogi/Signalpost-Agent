"""
search.py — Tavily web search layer with judge-key fallback + depth control.
"""
import os
from dotenv import load_dotenv

try:
    import judge_keys
except Exception:
    judge_keys = None

load_dotenv()

def _clean(key):
    key = (key or "").strip()
    if not key or "PLACEHOLDER" in key.upper() or "PASTE" in key.upper():
        return None
    return key

_env_key = _clean(os.getenv("TAVILY_API_KEY") or "")
_judge_key = _clean(getattr(judge_keys, "TAVILY_JUDGE_KEY", "") if judge_keys else "")

if _env_key:
    TAVILY_API_KEY = _env_key
    KEY_SOURCE = "env"
elif _judge_key:
    TAVILY_API_KEY = _judge_key
    KEY_SOURCE = "judge"
else:
    TAVILY_API_KEY = None
    KEY_SOURCE = None

_client = None
if TAVILY_API_KEY:
    try:
        from tavily import TavilyClient
        _client = TavilyClient(api_key=TAVILY_API_KEY)
        print(f"[SEARCH] Tavily enabled via {KEY_SOURCE} key.")
    except Exception as e:
        print(f"[SEARCH] Tavily unavailable: {e}")
        _client = None
else:
    print("[SEARCH] No Tavily key found in .env or judge_keys.py — web search disabled.")

def get_web_context(query, max_results=5, depth="basic"):
    """max_results = how many websites Tavily scans; depth 'basic' (1 credit/result)
    or 'advanced' (2 credits/result, full-page extraction = deeper research)."""
    if _client is None:
        return "Web search is currently unavailable."
    query = (query or "").strip()
    if not query:
        return "No relevant web results found."
    print(f"[SEARCH] Querying Tavily for: {query[:90]}... ({max_results} results, {depth})")
    try:
        res = _client.search(query[:400], max_results=max_results,
                             search_depth=depth, include_answer=False)
        items = res.get("results") or []
        if not items:
            return "No relevant web results found."
        lines = []
        for i, r in enumerate(items, 1):
            title = (r.get("title") or "").strip()
            snippet = (r.get("content") or "").strip()
            url = (r.get("url") or "").strip()
            lines.append(f"[{i}] {title}\n{snippet}\nSource: {url}\n")
        return "\n".join(lines).strip()
    except Exception as e:
        return f"Web search unavailable: {e}"