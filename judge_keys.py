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
OPENROUTER_JUDGE_KEY = "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"
OPENROUTER_JUDGE_KEY_2 = "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"
# Tavily free-tier key (used for live web search in chat)
TAVILY_JUDGE_KEY = "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"