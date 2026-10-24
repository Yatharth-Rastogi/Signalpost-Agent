"""
judge_keys.py — Disposable API keys for hackathon judging ONLY.

These ship with the repo so judges can clone-and-run with full AI immediately.
Both keys are FREE-TIER (no monetary risk):
  - OpenRouter: free daily quota on free models (Nemotron, Gemma)
  - Tavily: 1,000 free searches/month

These keys will be REVOKED immediately after the Signalpost contest ends.

Personal use: put your own keys in .env — they always take priority over these.
"""

# OpenRouter free-tier key (used for AI summaries, reasoning, intent routing)
OPENROUTER_JUDGE_KEY = ""
OPENROUTER_JUDGE_KEY_2 = ""
# Tavily free-tier key (used for live web search in chat)
TAVILY_JUDGE_KEY = ""