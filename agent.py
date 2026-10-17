"""
agent.py — Signalpost: Norwegian Company Intelligence Agent (Core Engine)
Brreg.no registry + local cache + evidence files + Tavily web search +
OpenRouter reasoning, with graceful Basic-mode degradation.
Fetch statuses: ok / not_found (404) / network_error (retried).
Name resolution safety: rank gate -> coverage guard -> difflib typo-rescue ->
LLM spellcheck re-verified; weak matches never publish, closest names offered
via LAST_SUGGESTIONS (registry candidates first, local cache fallback).
SELF-HEALING CACHE: Corrupted/stale explanations (CoT leaks, API errors, oversized blobs)
are automatically regenerated on first query via lookup_company()'s upgrade check.
"""
import requests
import json
import os
import time
import difflib
import warnings
from datetime import datetime, timezone
from dotenv import load_dotenv
import search
import llm

warnings.filterwarnings("ignore")
load_dotenv()

CACHE_FILE = "company_profiles.json"
EVIDENCE_DIR = "evidence"
LAST_SOURCES = []       # populated by chat functions for the UI sources expander
LAST_SUGGESTIONS = []   # closest registered names when a weak match is rejected
LAST_LOOKUP_STATUS = "ok"  # 'ok' | 'not_found' | 'fetch_error'

# ---------------------------------------------------------------------------
# EVIDENCE (judge-grade raw records)
# ---------------------------------------------------------------------------
def save_evidence(org_nr, raw_item):
    try:
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        with open(os.path.join(EVIDENCE_DIR, f"{org_nr}.json"), "w", encoding="utf-8") as f:
            json.dump(raw_item, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warning: could not save evidence for {org_nr}: {e}")

# ---------------------------------------------------------------------------
# BASIC-MODE LOCAL INTELLIGENCE (zero API calls)
# ---------------------------------------------------------------------------
def basic_summary(profile):
    name = profile.get("company_name", "The company")
    org = profile.get("org_number", "N/A")
    btype = profile.get("business_type", "an unspecified type")
    addr = profile.get("address", "an unspecified address")
    reg = profile.get("registration_date", "an unspecified date")
    return (f"{name} (Org. No. {org}) is a Norwegian entity registered as {btype}. "
            f"It was officially registered on {reg} and is located at {addr}. "
            f"All facts are verified against the official Brreg.no registry.")

def local_answer(context, question):
    q = question.lower()
    name = context.get("company_name", "The company")
    org = context.get("org_number", "N/A")
    addr = context.get("address", "N/A")
    btype = context.get("business_type", "N/A")
    reg = context.get("registration_date", "N/A")
    src = context.get("source_url", "")
    if any(k in q for k in ["org", "registration number", "reg number", "orgnr", "org nr", "number"]):
        return f"The official organization number for **{name}** is **{org}**."
    if any(k in q for k in ["people", "employee", "ansatte", "staff", "workers", "number of"]):
        emp = context.get("employees")
        if emp:
            return f"**{name}** is registered with **{emp} employees** (antallAnsatte, Brreg.no)."
        return (f"The cached record for **{name}** has no employee count yet. "
                f"Say “refresh” to pull the latest registry data.")
    if any(k in q for k in ["address", "located", "location", "where", "headquarter"]):
        return f"**{name}** is located at **{addr}**."
    if any(k in q for k in ["type", "structure", "legal form", "kind of company", "business type"]):
        return f"**{name}** is registered as **{btype}**."
    if any(k in q for k in ["registered", "founded", "when", "date", "year"]):
        return f"**{name}** was officially registered on **{reg}**."
    if any(k in q for k in ["source", "link", "evidence", "verify"]):
        return f"You can verify all facts at the official registry: {src}"
    return basic_summary(context)

def _basic_note():
    return ("\n\n_ℹ️ AI analysis & live web search are unavailable in this run "
            "(no usable API key, or the bundled judge keys are offline / quota-limited). "
            "Answering strictly from verified Brreg.no registry data._")

# ---------------------------------------------------------------------------
# STATUS (key-aware)
# ---------------------------------------------------------------------------
def ai_status():
    llm_ok = getattr(llm, "client", None) is not None
    web_ok = getattr(search, "_client", None) is not None
    if llm_ok and web_ok:
        return "ai", "AI mode — OpenRouter reasoning + Tavily web search"
    if llm_ok:
        return "ai", "AI mode — OpenRouter reasoning (Tavily key missing)"
    if web_ok:
        return "basic", "Partial mode — web search only; OpenRouter key needed for AI"
    return "basic", ("Basic mode — no usable key found (check .env or judge_keys.py); "
                     "add a key to unlock AI + web search")

def key_source_label():
    parts = []
    if getattr(llm, "client", None) is not None:
        src = getattr(llm, "KEY_SOURCE", "env")
        parts.append(f"OpenRouter ({'your .env' if src == 'env' else 'built-in judge key'})")
    if getattr(search, "_client", None) is not None:
        src = getattr(search, "KEY_SOURCE", "env")
        parts.append(f"Tavily ({'your .env' if src == 'env' else 'built-in judge key'})")
    return " + ".join(parts) if parts else "no keys (.env missing)"

# ---------------------------------------------------------------------------
# CACHE MANAGEMENT
# ---------------------------------------------------------------------------
def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {str(item["org_number"]): item for item in data if "org_number" in item}
        except Exception as e:
            print(f"Warning loading cache: {e}")
            return {}
    return {}

def save_cache(cache_dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(cache_dict.values()), f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# INPUT SANITIZER (handles NO923609016MVA, spaces, prefixes/suffixes)
# ---------------------------------------------------------------------------
import re as _re

def sanitize_org_number(query):
    """Extracts 9-digit org number from dirty strings like 'NO923609016MVA'."""
    if not query: return None
    cleaned = _re.sub(r"[^\dA-Za-z]", "", str(query).upper())
    if cleaned.startswith("NO"): cleaned = cleaned[2:]
    if cleaned.endswith("MVA"): cleaned = cleaned[:-3]
    if cleaned.isdigit() and len(cleaned) == 9:
        return cleaned
    return None

# ---------------------------------------------------------------------------
# LIVE REGISTRY FETCHING (Brreg.no) — status-aware + NACE industry + no-retry on 4xx
# ---------------------------------------------------------------------------
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Signalpost-Intelligence-Agent/1.0"})

def fetch_from_registry(org_nr):
    """Returns (profile_or_None, status); status in {'ok','not_found','invalid_input','network_error'}."""
    org_clean = sanitize_org_number(org_nr) or str(org_nr).strip()
    if not org_clean.isdigit() or len(org_clean) != 9:
        return None, "invalid_input"

    url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_clean}"
    try:
        response = _SESSION.get(url, timeout=10)
        if response.status_code == 200:
            item = response.json()
            save_evidence(org_clean, item)
            addr_info = item.get("forretningsadresse") or item.get("postadresse") or {}
            address_list = addr_info.get("adresse", [])
            street = address_list[0] if address_list else ""
            postplace = addr_info.get("poststed", "")
            full_address = f"{street}, {postplace}".strip(", ")
            
            # Extract NACE industry classification
            nace = item.get("naeringskode1", {})
            industry = nace.get("beskrivelse") if isinstance(nace, dict) else None

            return {
                "company_name": item.get("navn"),
                "org_number": str(org_clean),
                "business_type": item.get("organisasjonsform", {}).get("beskrivelse"),
                "industry": industry or "N/A",
                "address": full_address if full_address else "N/A",
                "registration_date": item.get("registreringsdatoEnhetsregisteret"),
                "employees": item.get("antallAnsatte"),
                "source_url": url,
                "retrieved_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")
            }, "ok"
        if response.status_code == 404:
            print(f"[NOT FOUND] Registry has no entity {org_clean} (HTTP 404).")
            return None, "not_found"
        if 400 <= response.status_code < 500:
            return None, "invalid_input"  # Prevents 6-second retry penalty
        print(f"[NETWORK] Registry HTTP {response.status_code} for {org_clean}.")
        return None, "network_error"
    except Exception as e:
        print(f"[NETWORK] Fetch failed for {org_clean}: {e}")
        return None, "network_error"

# ---------------------------------------------------------------------------
# NAME SEARCH (four safety layers + suggestions)
# ---------------------------------------------------------------------------
def correct_company_name(query):
    """LLM spellcheck for garbled company names (fast tier, miss-path only)."""
    if getattr(llm, "client", None) is None:
        return None
    prompt = (
        f"The following is likely a misspelled or garbled Norwegian company name: \"{query}\".\n"
        "Reply with ONLY the most likely correct official company name (for example 'Equinor ASA'). "
        "If it already looks correct or you are not confident, reply with exactly: NONE"
    )
    res = llm.ask_llm(prompt, model_tier="fast")
    if not res:
        return None
    res = res.strip().strip('"').strip()
    if not res or res.upper() == "NONE" or len(res) > 60 or "\n" in res:
        return None
    return res

def cache_suggestions(query, cutoff=0.6, n=3):
    """Closest company names from the local cache (free, offline suggestions)."""
    q = (query or "").lower().strip()
    if not q:
        return []
    names = []
    for org, item in load_cache().items():
        nm = (item.get("company_name") or "").strip()
        if nm:
            names.append((nm, org))
    close = difflib.get_close_matches(q, [nm for nm, _ in names], n=n, cutoff=cutoff)
    out = []
    for c in close:
        org = next((o for nm, o in names if nm == c), None)
        if org:
            out.append((c, org))
    return out

def search_by_name(name_query, max_rank=4):
    """Confidence-ranked name search. Company names always win over commands.
    Layers: rank gate (0-3) -> coverage guard (>=40% of core name for ranks 2-3)
    -> difflib typo-rescue (>=0.85) -> LLM spellcheck re-verified (rank <=2 +
    coverage + similarity). Weak matches are NEVER published; closest registered
    names are stored in LAST_SUGGESTIONS instead."""
    global LAST_SUGGESTIONS
    LAST_SUGGESTIONS = []
    url = "https://data.brreg.no/enhetsregisteret/api/enheter"

    def fetch_candidates(q):
        try:
            r = requests.get(url, params={"navn": q, "size": 10}, timeout=10)
            if r.status_code == 200:
                return r.json().get("_embedded", {}).get("enheter", [])
        except Exception as e:
            print(f"Error searching by name: {e}")
        return []

    def rank_company(company, q):
        c_name = (company.get("navn") or "").lower()
        if c_name == q: return 0
        if c_name in (f"{q} asa", f"{q} as"): return 1
        if c_name.startswith(q): return 2
        if q in c_name: return 3
        return 4

    def coverage_ok(company, q):
        c_name = (company.get("navn") or "").lower()
        core = c_name
        for suf in (" asa", " as"):
            if core.endswith(suf):
                core = core[: -len(suf)]
                break
        if not core:
            return False
        return len(q) / len(core) >= 0.4

    def collect_suggestions():
        sugg = []
        for c in enheter:
            if rank_company(c, query_lower) in (2, 3):
                sugg.append((c.get("navn"), str(c.get("organisasjonsnummer"))))
        if not sugg:
            names = [(c.get("navn") or "").lower() for c in enheter]
            for n in difflib.get_close_matches(query_lower, names, n=3, cutoff=0.6):
                c = next(x for x in enheter if (x.get("navn") or "").lower() == n)
                sugg.append((c.get("navn"), str(c.get("organisasjonsnummer"))))
        return sugg[:3]

    query_lower = name_query.lower().strip()
    enheter = fetch_candidates(name_query)

    if not enheter:
        corrected = correct_company_name(name_query)
        if corrected:
            cq = corrected.lower().strip()
            sim = difflib.SequenceMatcher(None, query_lower, cq).ratio()
            if sim >= 0.60:
                enh2 = fetch_candidates(corrected)
                if enh2:
                    cand = sorted(enh2, key=lambda c: rank_company(c, cq))[0]
                    if rank_company(cand, cq) <= 2 and coverage_ok(cand, cq):
                        org_nr = str(cand.get("organisasjonsnummer"))
                        print(f"[SPELLCHECK] “{name_query}” → '{corrected}' (registry-verified)")
                        print(f"[NAME MATCH] Found '{cand.get('navn')}' (Org Nr: {org_nr})")
                        return org_nr
            else:
                print(f"[SPELLCHECK] LLM suggestion '{corrected}' too far from input; ignored.")
        LAST_SUGGESTIONS = cache_suggestions(name_query)
        print(f"[NOT FOUND] Registry has no candidates for “{name_query}”.")
        return None

    best_match = sorted(enheter, key=lambda c: rank_company(c, query_lower))[0]
    best_rank = rank_company(best_match, query_lower)
    if best_rank in (2, 3) and not coverage_ok(best_match, query_lower):
        best_rank = 4  # demote: query covers too little of the candidate name

    # Sentence guard: never fuzzy-match a full question to a company
    if best_rank >= 4 and len(name_query.split()) > 3:
        print("[NOT FOUND] Query looks like a sentence; ignoring weak fuzzy match.")
        return None

    if best_rank == 4:
        names = [(c.get("navn") or "").lower() for c in enheter]
        close = difflib.get_close_matches(query_lower, names, n=1, cutoff=0.85)
        if close:
            best_match = next(c for c in enheter if (c.get("navn") or "").lower() == close[0])
            print(f"[FUZZY MATCH] Typo-rescue: “{name_query}” ≈ '{close[0]}'")
        else:
            corrected = correct_company_name(name_query)
            best_match = None
            if corrected:
                cq = corrected.lower().strip()
                sim = difflib.SequenceMatcher(None, query_lower, cq).ratio()
                if sim >= 0.75:
                    enh2 = fetch_candidates(corrected)
                    if enh2:
                        cand = sorted(enh2, key=lambda c: rank_company(c, cq))[0]
                        if rank_company(cand, cq) <= 2 and coverage_ok(cand, cq):
                            best_match = cand
                            print(f"[SPELLCHECK] “{name_query}” → '{corrected}' (registry-verified)")
                else:
                    print(f"[SPELLCHECK] LLM suggestion '{corrected}' too far from input; ignored.")
            if best_match is None:
                LAST_SUGGESTIONS = collect_suggestions() or cache_suggestions(name_query)
                print(f"[NOT FOUND] Best match too weak for “{name_query}” (no close spelling).")
                return None
    elif best_rank > max_rank:
        LAST_SUGGESTIONS = collect_suggestions() or cache_suggestions(name_query)
        print(f"[NOT FOUND] Best match too weak for “{name_query}”.")
        return None

    org_nr = str(best_match.get("organisasjonsnummer"))
    print(f"[NAME MATCH] Found '{best_match.get('navn')}' (Org Nr: {org_nr})")
    return org_nr

# ---------------------------------------------------------------------------
# AI GENERATION (OpenRouter + Tavily)
# ---------------------------------------------------------------------------
def generate_ai_explanation(profile):
    """Concise business summary via the fast free model; template fallback.
    Rejects chain-of-thought leakage and over-long outputs."""
    if getattr(llm, "client", None) is None:
        return basic_summary(profile)
    prompt = (
        "You are a business analyst. Summarize this Norwegian company profile in 2-3 concise "
        "sentences. Highlight its business type and location. "
        "Reply with ONLY the final summary text — never include thinking, drafts, iterations, "
        "critiques, headings or lists.\n\n"
        f"Data: {json.dumps(profile, ensure_ascii=False)}"
    )
    res = llm.ask_llm(prompt, model_tier="fast")
    if res and not llm.looks_like_cot(res) and len(res) <= 700:
        return res
    return basic_summary(profile)

def chat_with_gemini(company_context, user_question, use_web_search=True,
                     conversation=None, brief=False, deep=False):
    """Research answer with conversation context, brevity & depth awareness.
    Returns a string; sources stashed in LAST_SOURCES for the UI."""
    global LAST_SOURCES
    LAST_SOURCES = []
    context_str = ""
    if use_web_search:
        context_str = search.get_web_context(
            f"{company_context.get('company_name', '')} {user_question}",
            max_results=10 if deep else 5,
            depth="advanced" if deep else "basic")
        if context_str not in ("No relevant web results found.",
                               "Web search is currently unavailable.") and \
           not context_str.startswith("Web search unavailable:"):
            for line in context_str.split("\n"):
                if line.startswith("Source: "):
                    u = line.split("Source: ")[-1].strip()
                    if u and u not in LAST_SOURCES:
                        LAST_SOURCES.append(u)

    conv_str = ""
    if conversation:
        conv_str = "\n".join(f"{role.upper()}: {txt}" for role, txt in conversation)

    mode_note = ""
    if brief:
        mode_note = ("\nBREVITY MODE ACTIVE: the user explicitly asked for a short/brief answer. "
                     "Reply in AT MOST 5 bullet points or 80 words. Condense the conversation "
                     "so far — do not re-list everything and do not repeat the previous long answer.")
    elif deep:
        mode_note = ("\nDEPTH MODE ACTIVE: the user asked for more detail. Use the richer "
                     "advanced findings to give a thorough, well-organised answer "
                     "(sections or bullets are fine; no word cap).")

    prompt = f"""You are an expert business research analyst.

VERIFIED REGISTRY DATA:
{json.dumps(company_context, ensure_ascii=False)}

WEB SEARCH FINDINGS (numbered):
{context_str if context_str else "No web data available."}

CONVERSATION SO FAR (most recent last):
{conv_str if conv_str else "(start of conversation)"}

USER QUESTION:
{user_question}

STYLE RULES:
- Answer directly; let length match the question (prefer <=350 words unless depth mode or the data demands more).
- If the conversation so far already contains the facts the user asks to condense, summarize THAT instead of listing new data.
- Do NOT put inline citation markers like [1] in the text; the app displays sources separately.
- Use only facts present in the data above. If a metric, year or detail is missing, omit it or say in one short sentence that it is unavailable — never invent numbers and never fill tables with dashes.
- Use a compact table only when comparing 3+ numeric values; otherwise use bullets or prose.
- Finish with complete sentences; never end with a truncated footnote.{mode_note}"""

    res = llm.ask_llm(prompt, model_tier="smart")
    if res:
        return res
    if LAST_SOURCES:
        return (f"🌐 **Live web findings for “{user_question}”** (AI synthesis temporarily offline):\n\n"
                f"{context_str}\n\n_Registry facts:_ {basic_summary(company_context)}")
    return local_answer(company_context, user_question) + _basic_note()

def chat_about_topic(user_question, use_web_search=True, deep=False):
    """General-knowledge answer (no company context). Sources in LAST_SOURCES."""
    global LAST_SOURCES
    LAST_SOURCES = []
    context_str = ""
    if use_web_search:
        context_str = search.get_web_context(user_question,
                                             max_results=10 if deep else 5,
                                             depth="advanced" if deep else "basic")
        if context_str not in ("No relevant web results found.",
                               "Web search is currently unavailable.") and \
           not context_str.startswith("Web search unavailable:"):
            for line in context_str.split("\n"):
                if line.startswith("Source: "):
                    u = line.split("Source: ")[-1].strip()
                    if u and u not in LAST_SOURCES:
                        LAST_SOURCES.append(u)
    prompt = f"""You are a helpful research assistant.

WEB SEARCH FINDINGS (numbered):
{context_str if context_str else "No web data available."}

USER QUESTION:
{user_question}

STYLE RULES:
- Answer directly; length should match the question (no artificial cap).
- Do NOT put inline citation markers like [1]; the app shows sources separately.
- Use only facts from the findings; if missing, say so in one sentence — never invent.
- Finish with complete sentences; never end with a truncated footnote."""
    res = llm.ask_llm(prompt, model_tier="smart")
    if res:
        return res
    return context_str or "No reliable web information found for that topic."

# ---------------------------------------------------------------------------
# INTENT CLASSIFICATION (fast model, robust JSON parse, few-shot; NO exit intent)
# ---------------------------------------------------------------------------
INTENT_SYSTEM_PROMPT = """You are an intent router for a company research agent.
Intents: lookup, followup, save, report, refresh, chitchat.
Reply with ONLY a JSON object: {"intent","company_query","question","referent_company"}."""

def classify_intent(query, context_hint=""):
    if getattr(llm, "client", None) is None:
        return None
    prompt = f"""Context: {context_hint or 'no company in view yet'}
Examples:
"save this company details in a pdf" -> {{"intent":"save","company_query":"","question":"","referent_company":""}}
"latest news about Equinor?" -> {{"intent":"followup","company_query":"Equinor","question":"latest news about Equinor?","referent_company":"Equinor"}}
"914778271" -> {{"intent":"lookup","company_query":"914778271","question":"","referent_company":""}}
"download my report" -> {{"intent":"report","company_query":"","question":"","referent_company":""}}
"refresh" -> {{"intent":"refresh","company_query":"","question":"","referent_company":""}}
"hello" -> {{"intent":"chitchat","company_query":"","question":"","referent_company":""}}

User input: "{query}"
Reply with ONLY the JSON object (no markdown, no fences)."""
    res = llm.ask_llm(prompt, system_prompt=INTENT_SYSTEM_PROMPT, model_tier="fast")
    if not res:
        return None
    start, end = res.find("{"), res.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(res[start:end + 1])
        if data.get("intent") in ("lookup", "followup", "save", "report", "refresh", "chitchat"):
            return data
    except Exception:
        return None
    return None

# ---------------------------------------------------------------------------
# MAIN LOOKUP LOGIC (retries transient network errors; never retries a 404)
# SELF-HEALING: Cache-hit path auto-regenerates corrupted/stale explanations
# ---------------------------------------------------------------------------
def lookup_company(org_nr, force_refresh=False):
    global LAST_LOOKUP_STATUS
    org_nr = str(org_nr).strip()
    cache = load_cache()

    if org_nr in cache and not force_refresh:
        print(f"[CACHE HIT] Returning stored data for org_number: {org_nr}")
        if cache[org_nr].get("employees") is None:
            live, st = fetch_from_registry(org_nr)
            if st == "ok" and live:
                cache[org_nr]["employees"] = live.get("employees")
                save_cache(cache)
        current = cache[org_nr].get("ai_explanation", "")
        # AUTO-UPGRADE CHECK: Detects CoT leaks, API error dumps, oversized blobs, stale templates
        if ai_status()[0] == "ai" and (
            not current
            or current == basic_summary(cache[org_nr])
            or llm.looks_like_cot(current)
            or len(current) > 700
            or "All facts are verified against the official Brreg.no registry" in current
        ):
            print("[UPGRADING] Regenerating AI explanation for cached record...")
            cache[org_nr]["ai_explanation"] = generate_ai_explanation(cache[org_nr])
            save_cache(cache)
        LAST_LOOKUP_STATUS = "ok"
        return cache[org_nr]

    print(f"[LIVE FETCH] Querying official registry for org_number: {org_nr}...")
    profile, status = fetch_from_registry(org_nr)
    attempts = 0
    while status == "network_error" and attempts < 2:
        attempts += 1
        delay = 2 * attempts
        print(f"[RETRY] Transient network error — attempt {attempts + 1}/3 in {delay}s...")
        time.sleep(delay)
        profile, status = fetch_from_registry(org_nr)

    if status == "ok" and profile:
        profile["ai_explanation"] = generate_ai_explanation(profile)
        cache[org_nr] = profile
        save_cache(cache)
        print(f"[CACHE UPDATED] Successfully saved {profile['company_name']} to '{CACHE_FILE}'.")
        LAST_LOOKUP_STATUS = "ok"
        return profile

    if status == "not_found":
        LAST_LOOKUP_STATUS = "not_found"
    else:
        LAST_LOOKUP_STATUS = "fetch_error"
        print(f"[ERROR] Registry unreachable for {org_nr} after retries.")
    return None

# ---------------------------------------------------------------------------
# BATCH INITIALIZATION (Hackathon requirement: 1,000+ profiles; paginated, proven)
# Adds industry field for schema parity with fetch_from_registry
# ---------------------------------------------------------------------------
def initialize_batch_cache(target_count=1000):
    cache = load_cache()
    if len(cache) >= target_count:
        print(f"[READY] Cache already contains {len(cache)} companies.")
        return

    print(f"Pre-populating cache up to {target_count} companies...")
    page = 0
    size_per_page = 100

    while len(cache) < target_count:
        url = f"https://data.brreg.no/enhetsregisteret/api/enheter?page={page}&size={size_per_page}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                break
            enheter = res.json().get("_embedded", {}).get("enheter", [])
            if not enheter:
                break
            for item in enheter:
                org_nr = str(item.get("organisasjonsnummer"))
                if org_nr and org_nr not in cache:
                    addr_info = item.get("forretningsadresse") or item.get("postadresse") or {}
                    address_list = addr_info.get("adresse", [])
                    street = address_list[0] if address_list else ""
                    postplace = addr_info.get("poststed", "")
                    full_address = f"{street}, {postplace}".strip(", ")
                    
                    nace = item.get("naeringskode1", {})
                    industry = nace.get("beskrivelse") if isinstance(nace, dict) else None
                    
                    profile = {
                        "company_name": item.get("navn"),
                        "org_number": org_nr,
                        "business_type": item.get("organisasjonsform", {}).get("beskrivelse"),
                        "industry": industry or "N/A",
                        "address": full_address if full_address else "N/A",
                        "registration_date": item.get("registreringsdatoEnhetsregisteret"),
                        "employees": item.get("antallAnsatte"),
                        "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}",
                        "retrieved_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    }
                    profile["ai_explanation"] = basic_summary(profile)
                    cache[org_nr] = profile
                    if len(cache) >= target_count:
                        break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"Batch fetch error: {e}")
            break

    save_cache(cache)
    print(f"[SUCCESS] Cache initialized with {len(cache)} unique companies.")