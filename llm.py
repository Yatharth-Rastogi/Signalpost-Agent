"""
llm.py — OpenRouter LLM engine (multi-key failover + safe response parsing).
Key pool priority: env OPENROUTER_API_KEY -> env OPENROUTER_API_KEY_2
-> judge_keys.OPENROUTER_JUDGE_KEY -> judge_keys.OPENROUTER_JUDGE_KEY_2.
On an account-level daily free cap the engine advances to the next key and
retries; when every key is exhausted it arms a quiet period and returns None
so downstream code degrades to registry-template explanations. Per-request hop
timeouts, 429/timeout cooldowns, winner memory, reasoning-safe polishing with
expanded chain-of-thought detection, hard per-call budgets, and SAFE extraction
that never raises IndexError on empty choices[]. Personal keys in .env always
take priority over the bundled judge keys.
"""
import os
import re
import time
from dotenv import load_dotenv
from openai import OpenAI

try:
    import judge_keys
except Exception:
    judge_keys = None

load_dotenv()

def _build_client(key):
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
        default_headers={"X-Title": "Signalpost Agent"},
        max_retries=0,
    )

def _clean(k):
    k = (k or "").strip()
    if not k or "PLACEHOLDER" in k.upper() or "PASTE" in k.upper():
        return None
    return k

_CANDIDATES = []
for _raw, _tag in [
    (os.getenv("OPENROUTER_API_KEY") or "", "env"),
    (os.getenv("OPENROUTER_API_KEY_2") or "", "env"),
    (getattr(judge_keys, "OPENROUTER_JUDGE_KEY", "") or "", "judge"),
    (getattr(judge_keys, "OPENROUTER_JUDGE_KEY_2", "") or "", "judge"),
]:
    _c = _clean(_raw)
    if _c and _c not in [x[0] for x in _CANDIDATES]:
        _CANDIDATES.append((_c, _tag))

client = None
KEY_SOURCE = None
_key_index = 0
_account_limited_until = 0.0
if _CANDIDATES:
    client = _build_client(_CANDIDATES[0][0])
    KEY_SOURCE = _CANDIDATES[0][1]
else:
    print("[LLM] No OpenRouter key (.env or judge_keys) — AI disabled (Basic mode).")

# Merged chain: proven-fast Nemotron first, universal stable models as net.
MODEL_CHAIN = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "google/gemma-2-9b-it:free",
]
FAST_CHAIN = [
    "nvidia/nemotron-3.5-lightning:free",
    "google/gemma-2-9b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]
_working = {"smart": None, "fast": None}
_cooldown = {}
MAX_TOKENS = {"smart": 4096, "fast": 500}
REQ_TIMEOUT = {"smart": 40, "fast": 12}
BUDGET = {"smart": 60, "fast": 20}
COOLDOWN_429 = 90
COOLDOWN_TIMEOUT = 60
ACCOUNT_QUIET_SECONDS = 6 * 3600

COT_MARKERS = ("the user is asking", "i need to", "first, check", "first check",
               "let me ", "step by step", "now, web findings", "checking the registry",
               "analysis:", "reasoning:", "thinking process", "here's a thinking",
               "analyze the request", "drafting", "iteration", "critique:",
               "check constraints", "final polish", "let's do:", "step 1", "step 2")

def _safe_extract_content(resp):
    """Never raises: returns '' if choices missing/empty (moderation/throttle)."""
    try:
        if not resp or not getattr(resp, "choices", None):
            return ""
        msg = resp.choices[0].message
        return (msg.content or "") if msg else ""
    except Exception:
        return ""

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
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S | re.I)
    t = re.sub(r"<think>.*", "", t, flags=re.S | re.I)
    return t.strip()

def _looks_like_cot(t):
    low = t.lower()
    return any(m in low for m in COT_MARKERS)

def looks_like_cot(t):
    return _looks_like_cot(t)

def _clean_tail(t):
    lines = t.rstrip().split("\n")
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop(); continue
        if (last.startswith("*") and "]" not in last and len(last) < 80
                and (last[-1].isdigit() or last.endswith(";") or last.endswith(","))):
            lines.pop(); continue
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
    global client, KEY_SOURCE, _key_index, _account_limited_until
    if client is None:
        return None
    if _account_limited_until > time.time():
        return None

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
            continue
        for ctrl in (True, False):
            if time.time() > deadline:
                break
            try:
                resp = _call(model, messages, model_tier, ctrl)
                raw_text = _safe_extract_content(resp)
                if not raw_text:
                    continue  # empty choices -> next hop, no IndexError
                content = _polish(raw_text)
            except Exception as e:
                err = str(e)
                last_err = f"{model}: {err[:120]}"
                print(f"[LLM] FAIL {last_err}")
                if _is_account_limit(err):
                    if _key_index + 1 < len(_CANDIDATES):
                        _key_index += 1
                        client = _build_client(_CANDIDATES[_key_index][0])
                        KEY_SOURCE = _CANDIDATES[_key_index][1]
                        print(f"[LLM] Key #{_key_index} daily cap hit — failing over (source={KEY_SOURCE}).")
                        return ask_llm(prompt, system_prompt=system_prompt, model_tier=model_tier)
                    _account_limited_until = time.time() + ACCOUNT_QUIET_SECONDS
                    print("[LLM] All OpenRouter keys hit the daily free cap — template fallback active.")
                    return None
                if _is_rate_limited(err):
                    _cooldown[model] = time.time() + COOLDOWN_429
                    break
                if _is_timeout(err):
                    _cooldown[model] = time.time() + COOLDOWN_TIMEOUT
                    break
                continue
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
                    fix_resp = _call(model, fix, model_tier, ctrl)
                    raw_fix = _safe_extract_content(fix_resp)
                    c2 = _polish(raw_fix) if raw_fix else None
                    if c2 and not _looks_like_cot(c2):
                        content = c2
                except Exception:
                    pass
            _working[model_tier] = model
            print(f"[LLM] OK via {model}")
            return _clean_tail(content)
        if time.time() > deadline:
            break

    print(f"[LLM] No model available for tier={model_tier}. Last: {last_err}")
    return None