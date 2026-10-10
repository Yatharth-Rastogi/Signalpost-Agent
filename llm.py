"""
llm.py — OpenRouter LLM engine (final).
Verified-active free models only; account-level daily-cap detection with
optional secondary-key failover (OPENROUTER_API_KEY_2 in .env); per-request
hop timeouts; 429/timeout cooldowns; winner memory; reasoning-safe polishing
with expanded chain-of-thought detection; hard per-call budgets.
Keys loaded from .env — never hardcoded.
"""
import os
import re
import time
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
_KEY_1 = (os.getenv("OPENROUTER_API_KEY") or "").strip() or None
_KEY_2 = (os.getenv("OPENROUTER_API_KEY_2") or "").strip() or None

def _build_client(key):
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
        default_headers={"X-Title": "Signalpost Agent"},
        max_retries=0,   # WE manage retries; no hidden client backoff
    )

client = None
_active_key = None
if _KEY_1:
    client = _build_client(_KEY_1)
    _active_key = _KEY_1
else:
    print("[LLM] No OPENROUTER_API_KEY in .env — AI disabled (Basic mode).")

# Verified active free models (agentic-only & delisted models removed)
MODEL_CHAIN = [
    "nvidia/nemotron-3-super-120b-a12b:free",   # 12B-active MoE: fast AND smart
    "nvidia/nemotron-3-ultra-550b-a55b:free",   # deepest reasoning
    "google/gemma-4-31b-it:free",               # backup
]
FAST_CHAIN = [
    "nvidia/nemotron-3.5-lightning:free",       # quickest
    "google/gemma-4-26b-a4b-it:free",           # backup
    "google/gemma-4-31b-it:free",               # last resort
]
_working = {"smart": None, "fast": None}
_cooldown = {}                        # model -> skip-until timestamp
_account_limited_until = 0.0
MAX_TOKENS = {"smart": 4096, "fast": 500}
REQ_TIMEOUT = {"smart": 40, "fast": 12}    # per-request hop limit (seconds)
BUDGET = {"smart": 60, "fast": 20}         # total per-call limit (seconds)
COOLDOWN_429 = 90
COOLDOWN_TIMEOUT = 60
ACCOUNT_QUIET_SECONDS = 6 * 3600

COT_MARKERS = ("the user is asking", "i need to", "first, check", "first check",
               "let me ", "step by step", "now, web findings", "checking the registry",
               "analysis:", "reasoning:", "thinking process", "here's a thinking",
               "analyze the request", "drafting", "iteration", "critique:",
               "check constraints", "final polish", "let's do:", "step 1", "step 2")

def _strip_fences(t):
    t = t.strip()
    if t.startswith("```"):
        parts = t.split("```", 2)
        t = parts[1] if len(parts) > 1 else t[3:]
        if t.startswith("json"):
            t = t[4:]
        t = t.rsplit("```", 1)[0]
    return t.strip()

def _strip_think(t):
    t = re.sub(r"<think.*?</think>", "", t, flags=re.S | re.I)
    t = re.sub(r"<think.*", "", t, flags=re.S | re.I)
    return t.strip()

def _looks_like_cot(t):
    low = t.lower()
    return any(m in low for m in COT_MARKERS)

def looks_like_cot(t):
    """Public wrapper so agent.py can validate cached/old explanations."""
    return _looks_like_cot(t)

def _clean_tail(t):
    """Drops a final truncated footnote like '*Currency conversion approximate; 2'."""
    lines = t.rstrip().split("\n")
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if (last.startswith("*") and "]" not in last and len(last) < 80
                and (last[-1].isdigit() or last.endswith(";") or last.endswith(","))):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()

def _is_account_limit(err):
    return "free-models-per-day" in err or ("429" in err and "per-day" in err)

def _is_rate_limited(err):
    e = err.lower()
    return "429" in e or "rate limit" in e or "temporarily" in e

def _is_timeout(err):
    e = err.lower()
    return "timed out" in e or "timeout" in e

def _polish(raw):
    return _strip_think(_strip_fences(raw or ""))

def _call(model, messages, tier, ctrl):
    kwargs = {"model": model, "messages": messages,
              "max_tokens": MAX_TOKENS[tier], "timeout": REQ_TIMEOUT[tier]}
    if ctrl:
        kwargs["extra_body"] = {"reasoning": {"effort": "low", "exclude": True}}
    return client.chat.completions.create(**kwargs)

def ask_llm(prompt, system_prompt=None, model_tier="smart", json_mode=False):
    """json_mode ignored on purpose (free endpoints reject response_format)."""
    global client, _active_key, _account_limited_until
    if client is None:
        return None
    if _account_limited_until > time.time():
        return None   # account cap active — fail fast, templates downstream

    chain = FAST_CHAIN if model_tier == "fast" else MODEL_CHAIN
    win = _working.get(model_tier)
    if win in chain:
        chain = [win] + [m for m in chain if m != win]
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    deadline = time.time() + BUDGET[model_tier]
    last_err = None

    for model in chain:
        if _cooldown.get(model, 0) > time.time():
            continue                      # still cooling off
        for ctrl in (True, False):
            if time.time() > deadline:
                break
            try:
                content = _polish(_call(model, messages, model_tier, ctrl).choices[0].message.content)
            except Exception as e:
                err = str(e)
                last_err = f"{model}: {err[:120]}"
                print(f"[LLM] FAIL {last_err}")
                if _is_account_limit(err):
                    if _KEY_2 and _active_key != _KEY_2:
                        client = _build_client(_KEY_2)
                        _active_key = _KEY_2
                        print("[LLM] Primary key daily cap hit — failing over to secondary key.")
                        return ask_llm(prompt, system_prompt=system_prompt, model_tier=model_tier)
                    _account_limited_until = time.time() + ACCOUNT_QUIET_SECONDS
                    print("[LLM] OpenRouter daily free cap reached — registry-template "
                          "fallback active until reset or a second key is added.")
                    return None
                if _is_rate_limited(err):
                    _cooldown[model] = time.time() + COOLDOWN_429
                    break
                if _is_timeout(err):
                    _cooldown[model] = time.time() + COOLDOWN_TIMEOUT
                    break
                continue                  # other error -> retry without reasoning ctrl
            if not content:
                continue
            if _looks_like_cot(content) or (model_tier == "fast" and len(content) > 900):
                try:
                    fix = messages + [
                        {"role": "assistant", "content": content[:400]},
                        {"role": "user", "content": "Rewrite your reply as ONLY the final "
                                                    "answer. No planning, no restating the "
                                                    "question, no step-by-step notes."},
                    ]
                    c2 = _polish(_call(model, fix, model_tier, ctrl).choices[0].message.content)
                    if c2 and not _looks_like_cot(c2):
                        content = c2
                except Exception:
                    pass
            _working[model_tier] = model
            print(f"[LLM] OK via {model}")
            return _clean_tail(content)
        if time.time() > deadline:
            break

    print(f"[LLM] No model available for tier={model_tier} (limits/budget). Last: {last_err}")
    return None