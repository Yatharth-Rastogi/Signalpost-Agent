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
LAST_ITEMS = []
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

# Note: tavily.com/knowledge and tavily.com/research are allowed if content is relevant
# (user preference: these sources are fine as long as they provide correct information)

def get_web_context(query, max_results=5, depth="basic", days=None):
    """max_results = how many websites Tavily scans; depth 'basic' (1 credit/result)
    or 'advanced' (2 credits/result, full-page extraction = deeper research).
    days = restrict to articles published within last N days (None = no filter).
    When days is set, uses topic='news' for better recency targeting."""
    global LAST_ITEMS
    LAST_ITEMS = []
    if _client is None:
        return "Web search is currently unavailable."
    query = (query or "").strip()
    if not query:
        return "No relevant web results found."
    recency_note = f", last {days}d" if days else ""
    print(f"[SEARCH] Querying Tavily for: {query[:90]}... ({max_results} results, {depth}{recency_note})")
    try:
        params = dict(max_results=max_results * 2,   # fetch extra to allow filtering
                      search_depth=depth, include_answer=False)
        if days is not None:
            params["topic"] = "news"
            params["days"] = days
        res = _client.search(query[:400], **params)
        items = res.get("results") or []
        if not items:
            LAST_ITEMS = []
            return "No relevant web results found."

        # Filter: dedupe by base URL (strip ?query and #fragment)
        seen_bases = set()
        filtered = []
        for r in items:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            base = url.split("?")[0].split("#")[0].rstrip("/")
            if base in seen_bases:
                continue
            seen_bases.add(base)
            filtered.append(r)
            if len(filtered) >= max_results:
                break

        if not filtered:
            LAST_ITEMS = []
            return "No relevant web results found."

        LAST_ITEMS = [{"title": (r.get("title") or "").strip(),
                       "snippet": (r.get("content") or "").strip(),
                       "url": (r.get("url") or "").strip(),
                       "published_date": r.get("published_date") or ""} for r in filtered]
        lines = []
        for i, r in enumerate(filtered, 1):
            title = (r.get("title") or "").strip()
            snippet = (r.get("content") or "").strip()[:700]
            url = (r.get("url") or "").strip()
            pub = r.get("published_date") or ""
            date_line = f"Published: {pub}" if pub else "Published: (date not in source)"
            lines.append(f"[{i}] {title}\n{date_line}\n{snippet}\nSource: {url}\n")
        return "\n".join(lines).strip()
    except Exception as e:
        LAST_ITEMS = []
        return f"Web search unavailable: {e}"