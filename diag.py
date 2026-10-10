"""Provider diagnostic — run: py diag.py"""

# Copyright (c) 2026 <Yatharth Rastogi> — MIT License.

import llm, search

print("== Tavily search ==")
print(search.get_web_context("Equinor latest news", max_results=2)[:300])

print("\n== OpenRouter model probe ==")
for m in llm.MODEL_CHAIN + llm.FAST_CHAIN:
    try:
        r = llm.client.chat.completions.create(
            model=m, messages=[{"role": "user", "content": "Ping"}], max_tokens=5)
        print(f"OK   {m}: {(r.choices[0].message.content or '')[:30]}")
    except Exception as e:
        print(f"FAIL {m}: {str(e)[:110]}")

print("\n== ask_llm end-to-end ==")
print(llm.ask_llm("Reply with exactly: PONG", model_tier="smart"))