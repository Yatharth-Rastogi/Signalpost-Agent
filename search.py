"""
search.py — Tavily web search layer.
Provides get_web_context(query) returning a plain-text numbered summary with
Source: lines, plus a module-level _client for ai_status() detection.
Key loaded from .env — never hardcoded.
"""

# Copyright (c) 2026 <Yatharth Rastogi> — MIT License.

import os
from dotenv import load_dotenv

load_dotenv()
TAVILY_API_KEY = (os.getenv("TAVILY_API_KEY") or "").strip() or None

_client = None
try:
    if TAVILY_API_KEY:
        from tavily import TavilyClient
        _client = TavilyClient(api_key=TAVILY_API_KEY)
    else:
        print("[SEARCH] No TAVILY_API_KEY in .env — web search disabled.")
except Exception as e:
    print(f"[SEARCH] Tavily unavailable: {e}")
    _client = None

def get_web_context(query, max_results=5):
    if _client is None:
        return "Web search is currently unavailable."
    if not query or not query.strip():
        return "No relevant web results found."
    try:
        res = _client.search(query.strip()[:400], max_results=max_results,
                             search_depth="basic", include_answer=False)
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