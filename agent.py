"""
agent.py — Signalpost core engine.
Brreg.no registry + self-healing local cache + preserved evidence snapshots +
Tavily web search + OpenRouter reasoning, with honest six-state degradation.
Internal fetch states: ok / not_found / blocked / failed / invalid_input.
build_envelope() emits the rubric's terminal result envelope (one per input),
mapping internals to: available / not_available / blocked / not_applicable /
ambiguous / failed. Batch path is template-only by default to honour the
official run's fixed time/resource budget (a timeout = unscored).
Name safety: rank gate -> coverage guard -> difflib rescue -> LLM spellcheck
re-verified; uncertain matches set LAST_MATCH_AMBIGUOUS (-> 'ambiguous'),
never a guessed company.
"""
import requests
import json
import os
import re
import time
import hashlib
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
LAST_SOURCES = []
LAST_SUGGESTIONS = []
LAST_LOOKUP_STATUS = "ok"
LAST_MATCH_AMBIGUOUS = False          # True only when a real candidate was rejected as unsure

BRREG_ENHETER = "https://data.brreg.no/enhetsregisteret/api/enheter"
BRREG_REGNSKAP = "https://data.brreg.no/regnskapsregisteret/regnskap"

# org-form codes that file home-country / no Norwegian accounts -> financials 'not_applicable'
NON_FILING_FORMS = {"NUF", "ENHET", "AFDELING", "UTENLANDSK_FORETAK"}

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Signalpost-Intelligence-Agent/1.0",
    "Accept": "application/json"
})

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _field(item, *path):
    cur = item
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur

def _pick(item, *paths):
    """Return the first non-None value among candidate key-paths (Brreg moves
    contact fields between top-level and kontaktposter across versions)."""
    for path in paths:
        cur = item
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return None

def _fwd(p):
    """Normalise OS path separators to POSIX for the Linux evaluation harness."""
    return str(p).replace("\\", "/") if p else p

# ---------------------------------------------------------------------------
# EVIDENCE: preserved snapshots + idempotent refresh + real change detection
# ---------------------------------------------------------------------------
def _canon_hash(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

_TRACKED_FIELDS = [
    ("company_name", ("navn",)),
    ("business_type", ("organisasjonsform", "beskrivelse")),
    ("industry", ("naeringskode1", "beskrivelse")),
    ("address", ("forretningsadresse",)),
    ("registration_date", ("registreringsdatoEnhetsregisteret",)),
    ("employees", ("antallAnsatte",)),
    ("foundation_date", ("stiftelsesdato",)),
    ("email", ("epostadresse",)),
    ("website", ("hjemmeside",)),
    ("share_capital", ("kapital", "belop")),
    ("under_liquidation", ("underAvvikling",)),
    ("bankruptcy", ("konkurs",)),
    ("purpose", ("vedtektsfestetFormaal",)),
]

def _diff_tracked(old, new):
    changes = []
    for label, path in _TRACKED_FIELDS:
        ov, nv = _field(old, *path), _field(new, *path)
        if ov != nv:
            changes.append({"field": label, "old": ov, "new": nv})
    return changes

def save_evidence_snapshot(org_nr, raw_item, latency_ms=0, attempts=1):
    """Write/refresh evidence for one company. Returns
    (current_path, changed, previous_history_paths, changes, content_hash).
    Idempotent: an identical snapshot creates NO new history file and NO change."""
    org = str(org_nr)
    od = os.path.join(EVIDENCE_DIR, org)
    os.makedirs(od, exist_ok=True)
    hist_dir = os.path.join(od, "history")
    cur = os.path.join(od, "current.json")
    h = _canon_hash(raw_item)
    
    changed = None
    prev = []
    changes = []
    
    if os.path.exists(cur):
        try:
            with open(cur, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = None
            
        if old is not None and _canon_hash(old) == h:
            changed = False                      # same snapshot -> no duplicate, no false change
        elif old is not None:
            changed = True
            os.makedirs(hist_dir, exist_ok=True)
            hp = os.path.join(hist_dir, f"{int(time.time() * 1000)}.json")
            os.replace(cur, hp)                  # preserve earlier evidence before overwriting
            prev = [hp]
            changes = _diff_tracked(old, raw_item)
            
    if changed is not False:                      # first sight or genuine change -> write current
        with open(cur, "w", encoding="utf-8") as f:
            json.dump(raw_item, f, ensure_ascii=False, indent=2)
            
    meta = {
        "org_number": org, "content_sha256": h, "snapshot": "current.json",
        "history": [os.path.basename(p) for p in prev],
        "latency_ms": latency_ms, "attempts": attempts, "captured_utc": _now_iso(),
    }
    with open(os.path.join(od, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        
    return cur, changed, prev, changes, h

# ---------------------------------------------------------------------------
# BASIC-MODE INTELLIGENCE (zero API calls)
# ---------------------------------------------------------------------------
def basic_summary(profile):
    def has_value(value):
        return value is not None and (
            not isinstance(value, str) or value.strip().casefold() not in {"", "n/a", "none"}
        )

    name = profile.get("company_name", "The company")
    org = profile.get("org_number", "N/A")
    btype = profile.get("business_type") or "an unspecified type"
    ind = profile.get("industry")
    addr = profile.get("address")
    reg = profile.get("registration_date")
    emp = profile.get("employees")
    web = profile.get("website")
    purpose = profile.get("purpose")
    
    # Accurate phrasing for both domestic and foreign entities registered in Norway
    bits = [f"{name} (Org. No. {org}) is an entity registered in Norway as {btype}"]
    if ind and ind != "N/A":
        bits[0] += f" operating in {ind.lower()}"
    bits[0] += "."
    
    if purpose:
        bits.append(f"Its registered purpose is: {purpose}.")
        
    if has_value(reg):
        bits.append(f"It was officially registered on {reg}.")
    if has_value(emp) and emp != 0 and has_value(addr):
        bits.append(f"It reports {emp} employees and is located at {addr}.")
    elif has_value(emp) and emp != 0:
        bits.append(f"It reports {emp} employees.")
    elif has_value(addr):
        bits.append(f"It is located at {addr}.")
    else:
        bits.append("Its address is not available.")
    
    kp = profile.get("key_people") or []
    if kp:
        who = "; ".join(f"{p['name']} ({p['role']})" for p in kp[:3])
        bits.append(f"Key registered officers: {who}.")
        
    if profile.get("revenue") or profile.get("total_assets"):
        acc_type = str(profile.get("revenue_type") or "filed").lower()
        period = profile.get("revenue_period") or "the latest period"
        fin_items = []
        for label, key in (("revenue", "revenue"), ("net profit", "net_profit"),
                           ("total assets", "total_assets"), ("equity", "equity"),
                           ("total debt", "total_debt")):
            value = profile.get(key)
            if value not in (None, ""):
                fin_items.append(f"{label} {value}")
        if fin_items:
            bits.append(f"Its {acc_type} accounts for {period} show "
                        f"{', '.join(fin_items)} (Regnskapsregisteret).")
        
    if web:
        bits.append(f"Registry-listed website: {web}.")
        
    bits.append("All facts are verified against the official Brreg.no registry.")
    return " ".join(bits)

def local_answer(context, question):
    q = question.lower()
    name = context.get("company_name", "The company")
    org = context.get("org_number", "N/A")
    addr = context.get("address", "N/A")
    btype = context.get("business_type", "N/A")
    reg = context.get("registration_date", "N/A")
    src = context.get("source_url", " ")
    
    if any(k in q for k in ["org", "registration number", "reg number", "orgnr", "org nr", "number"]):
        return f"The official organization number for {name} is {org}."
    if any(k in q for k in ["ceo", "chief executive", "daglig leder", "board", "styret", "officer"]):
        kp = context.get("key_people") or []
        if kp:
            return f"According to the Brreg roller dataset: " + "; ".join(f"{p['name']} ({p['role']})" for p in kp[:4]) + "."
        return f"The registry record for {name} lists no active officers yet."
    if any(k in q for k in ["people", "employee", "ansatte", "staff", "workers", "number of"]):
        emp = context.get("employees")
        if emp:
            return f"{name} is registered with {emp} employees (antallAnsatte, Brreg.no)."
        return (f"The cached record for {name} has no employee count yet. "
                f"Say “refresh” to pull the latest registry data.")
    if any(k in q for k in ["address", "located", "location", "where", "headquarter"]):
        return f"{name} is located at {addr}."
    if any(k in q for k in ["type", "structure", "legal form", "kind of company", "business type"]):
        return f"{name} is registered as {btype}."
    if any(k in q for k in ["registered", "founded", "when", "date", "year"]):
        return f"{name} was officially registered on {reg}."
    if any(k in q for k in ["source", "link", "evidence", "verify"]):
        return f"You can verify all facts at the official registry: {src}"
    return basic_summary(context)

def _md_plain(t, limit=0):
    """Neutralise markdown injected by scraped pages (headings, setext
    underlines, emphasis, links) so fallback text renders at one uniform size."""
    out = []
    for ln in (t or "").split("\n"):
        s = ln.strip()
        if not s or re.fullmatch(r"[=\-_*#~]{3,}", s):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", r"\1 (\2)", s)
        s = s.replace("*", "").replace("`", "").replace(">", "")
        s = re.sub(r"\s{2,}", " ", s).strip()
        if s:
            out.append(s)
    txt = "\n".join(out)
    if limit and len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt

def basic_note():
    return ("\n\n_ℹ️ AI analysis & live web search are unavailable in this run "
            "(no usable API key, or the bundled judge keys are offline / quota-limited). "
            "Answering strictly from verified Brreg.no registry data.")

# ---------------------------------------------------------------------------
# STATUS (key-aware)
# ---------------------------------------------------------------------------
def ai_status():
    client_ok = getattr(llm, "client", None) is not None
    capped = getattr(llm, "quota_blocked", lambda: False)()
    llm_ok = client_ok and not capped
    web_ok = getattr(search, "_client", None) is not None
    if llm_ok and web_ok:
        return "ai", "AI mode — OpenRouter reasoning + Tavily web search"
    if llm_ok:
        return "ai", "AI mode — OpenRouter reasoning (Tavily key missing)"
    if capped and web_ok:
        return "basic", ("Cap mode — OpenRouter daily free cap reached; showing structured "
                         "registry + web findings without AI synthesis")
    if web_ok:
        return "basic", "Partial mode — web search only; OpenRouter key needed for AI"
    return "basic", ("Basic mode — no usable key found (check .env or judge_keys.py); "
                     "add a key to unlock AI + web search")

def key_source_label():
    parts = []
    if getattr(llm, "client", None) is not None:
        if getattr(llm, "quota_blocked", lambda: False)():
            parts.append("OpenRouter (daily free cap reached)")
        else:
            src = getattr(llm, "KEY_SOURCE", "env")
            parts.append(f"OpenRouter ({'your .env' if src == 'env' else 'built-in judge key'})")
    if getattr(search, "_client", None) is not None:
        src = getattr(search, "KEY_SOURCE", "env")
        parts.append(f"Tavily ({'your .env' if src == 'env' else 'built-in judge key'})")
    return " + ".join(parts) if parts else "no keys (.env missing)"

# ---------------------------------------------------------------------------
# CACHE + permanent self-heal (replaces any one-off fix script)
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

_CONTAMINATION_MARKERS = (
    "RESOURCE_EXHAUSTED",
    "User Safety",
    "AI Generation Error",
    "Here's a thinking process",
    "thinking process:",
    "step by step",
)

def _is_contaminated(text):
    if not text or not isinstance(text, str):
        return True
    if len(text) > 700:
        return True
    low = text.lower()
    if any(m.lower() in low for m in _CONTAMINATION_MARKERS):
        return True
    try:
        if llm.looks_like_cot(text):
            return True
    except Exception:
        pass
    return False

def _heal_row(row):
    changed = False
    if "industry" not in row or row.get("industry") in (None, " "):
        row["industry"] = "N/A"
        changed = True
    if "employees" not in row:
        row["employees"] = None
        changed = True
    if _is_contaminated(row.get("ai_explanation", " ")):
        row["ai_explanation"] = basic_summary(row)
        changed = True
    return changed

def _ensure_cache_hygiene():
    if not os.path.exists(CACHE_FILE):
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[HYGIENE] Could not read cache: {e}")
        return
    if not isinstance(data, list):
        return
    fixed = 0
    for row in data:
        if isinstance(row, dict) and _heal_row(row):
            fixed += 1
    if fixed:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[HYGIENE] Healed {fixed} cached rows (schema + explanation).")
    else:
        print("[HYGIENE] Cache already clean.")

# ---------------------------------------------------------------------------
# INPUT SANITIZER
# ---------------------------------------------------------------------------
def sanitize_org_number(query):
    """Extract a valid 9-digit org number from dirty inputs: 'NO923609016MVA',
    '999 999 999', 'Org.nr 914778271'. None if not a clean 9-digit."""
    if not query:
        return None
    cleaned = re.sub(r"[^\dA-Za-z]", "", str(query).upper())
    if cleaned.startswith("NO"):
        cleaned = cleaned[2:]
    if cleaned.endswith("MVA"):
        cleaned = cleaned[:-3]
    if cleaned.isdigit() and len(cleaned) == 9:
        return cleaned
    return None

# ---------------------------------------------------------------------------
# CORE FETCH (status-aware, latency+attempts measured, 4xx never retried)
# ---------------------------------------------------------------------------
def _fetch_core(org_clean, timeout=6, max_attempts=4, deadline=None):
    """Returns (item_or_None, status, latency_ms, attempts).
    status in {'ok','not_found','blocked','failed','invalid_input'}.
    Includes adaptive backoff for HTTP 429 (Too Many Requests)."""
    if not org_clean.isdigit() or len(org_clean) != 9:
        return None, "invalid_input", 0, 0
    url = f"{BRREG_ENHETER}/{org_clean}"
    attempts = 0
    last = "failed"
    t0 = time.perf_counter()
    while attempts < max_attempts:
        if deadline is not None and time.time() > deadline:
            return None, "failed", int((time.perf_counter() - t0) * 1000), attempts
        attempts += 1
        try:
            r = _SESSION.get(url, timeout=timeout)
            sc = r.status_code
            if sc == 200:
                lat = int((time.perf_counter() - t0) * 1000)
                return r.json(), "ok", lat, attempts
            if sc == 404:
                return None, "not_found", int((time.perf_counter() - t0) * 1000), attempts
            if sc == 429:
                if attempts < max_attempts:
                    wait_time = min(2 ** attempts, 8)
                    if deadline is not None and time.time() + wait_time > deadline:
                        print(f"[RATE LIMIT] 429 for {org_clean} but no time left in budget.")
                        return None, "blocked", int((time.perf_counter() - t0) * 1000), attempts
                    print(f"[RATE LIMIT] 429 for {org_clean}. Backing off for {wait_time}s (attempt {attempts}/{max_attempts})...")
                    time.sleep(wait_time)
                    continue
                return None, "blocked", int((time.perf_counter() - t0) * 1000), attempts
            if sc == 403:
                return None, "blocked", int((time.perf_counter() - t0) * 1000), attempts
            if sc in (400, 422):
                return None, "invalid_input", int((time.perf_counter() - t0) * 1000), attempts
            last = "failed"
            time.sleep(1)
        except Exception as e:
            print(f"[NETWORK] {org_clean} attempt {attempts}: {e}")
            last = "failed"
            time.sleep(1)
    return None, last, int((time.perf_counter() - t0) * 1000), attempts

def _profile_from_item(item, org_clean):
    addr = _field(item, "forretningsadresse") or _field(item, "postadresse") or {}
    if isinstance(addr, dict):
        alist = addr.get("adresse", [])
        # Join all address lines to prevent dropping the street address when "c/o" is present
        street = ", ".join(alist) if alist else ""
        postnummer = addr.get("postnummer", "")
        poststed = addr.get("poststed", "")
        # Format postal code and city properly
        if postnummer and poststed:
            postplace = f"{postnummer} {poststed}"
        else:
            postplace = poststed or ""
        land = addr.get("land", "")
        parts = [p for p in [street, postplace] if p]
        full_address = ", ".join(parts)
        # Append country if it's not Norway
        if land and land.lower() not in ("norge", "norway", ""):
            full_address = f"{full_address}, {land}".strip(", ") if full_address else land
    else:
        full_address = ""
        
    nace = item.get("naeringskode1", {})
    industry = nace.get("beskrivelse") if isinstance(nace, dict) else None
    
    return {
        "company_name": item.get("navn"),
        "org_number": str(org_clean),
        "business_type": _field(item, "organisasjonsform", "beskrivelse"),
        "industry": industry or "N/A",
        "address": full_address if full_address else "N/A",
        "registration_date": item.get("registreringsdatoEnhetsregisteret"),
        "employees": item.get("antallAnsatte"),
        "email": _pick(item, ("epostadresse",), ("kontaktposter", "epostadresse")),
        "phone": _pick(item, ("mobil",), ("telefon",), ("kontaktposter", "mobil")),
        "website": _pick(item, ("hjemmeside",), ("kontaktposter", "hjemmeside")),
        "source_url": f"{BRREG_ENHETER}/{org_clean}",
        "retrieved_date": _today(),
    }

def _fetch_roles(org_clean, deadline=None, timeout=4):
    """-> (roles_list, attempted). One polite attempt at the roller
    sub-endpoint; never raises, never retries (budget-safe)."""
    if deadline is not None and time.time() > deadline:
        return [], False
    try:
        r = _SESSION.get(f"{BRREG_ENHETER}/{org_clean}/roller", timeout=timeout)
        if r.status_code != 200:
            return [], True
        payload = r.json()
        try:
            od = os.path.join(EVIDENCE_DIR, str(org_clean))
            os.makedirs(od, exist_ok=True)
            with open(os.path.join(od, "roles.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return _normalize_roles(payload), True
    except Exception as e:
        print(f"[ROLES] fetch failed for {org_clean}: {e}")
        return [], True

def _holder_name(entry):
    p = entry.get("person") or {}
    if isinstance(p, dict):
        if p.get("erDoed"):
            return None
        n = p.get("navn") or {}
        if isinstance(n, dict):
            parts = [n.get("fornavn"), n.get("mellomnavn"), n.get("etternavn")]
            name = " ".join(x for x in parts if isinstance(x, str) and x).strip()
            if name:
                return name
        elif isinstance(n, str) and n.strip():
            return n.strip()
    for key in ("enhet", "organisasjon", "virksomhet"):
        o = entry.get(key) or {}
        if isinstance(o, dict):
            v = o.get("navn")
            if isinstance(v, list):
                v = " ".join(x for x in v if isinstance(x, str)) or None
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None

_ROLE_PRIORITY = ("DAGL", "LEDE", "NEST", "MEDL", "VARA", "REVI", "KOMM", "BOBE")

def _normalize_roles(payload):
    out = []
    groups = (payload or {}).get("rollegrupper") or []
    if not isinstance(groups, list):
        return []
    for g in groups:
        if not isinstance(g, dict):
            continue
        for r in (g.get("roller") or []):
            if not isinstance(r, dict) or r.get("avregistrert"):
                continue
            name = _holder_name(r)
            t = r.get("type") or {}
            code = (t.get("kode") or "").upper() if isinstance(t, dict) else ""
            desc = (t.get("beskrivelse") or code) if isinstance(t, dict) else ""
            if name and (code or desc):
                out.append({"name": name, "role": desc or code, "code": code})
    out.sort(key=lambda d: _ROLE_PRIORITY.index(d["code"]) if d["code"] in _ROLE_PRIORITY else 99)
    return out[:8]

BRREG_REGNSKAP = "https://data.brreg.no/regnskapsregisteret/regnskap"

_REV_PATHS = [
    ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
    ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "salgsinntekter"),
    ("resultatregnskapResultat", "sumDriftsinntekter"),
    ("resultatregnskapResultat", "driftsinntekter"),
    ("resultatregnskapKostnad", "sumDriftsinntekter"),
    ("resultatregnskap", "sumDriftsinntekter"),
    ("sumDriftsinntekter",),
]
_PROFIT_PATHS = [
    ("resultatregnskapResultat", "aarsresultat"),
    ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
    ("resultatregnskapResultat", "driftsresultat", "driftsresultat"),
    ("resultatregnskapResultat", "aaretsResultat"),
    ("resultatregnskapResultat", "ordinartResultatFoerSkattekostnad"),
    ("resultatregnskapResultat", "driftsresultat"),
    ("aarsresultat",),
]

def _fmt_money(v, currency="NOK"):
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return None
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:.2f}B {currency}"
    if a >= 1e6:
        return f"{v/1e6:.2f}M {currency}"
    if a >= 1e3:
        return f"{v/1e3:.1f}K {currency}"
    return f"{v:,.0f} {currency}"

def _fetch_financials(org_clean, deadline=None, timeout=6):
    """-> (fin_dict_or_None, attempted). Latest filed annual accounts from
    Regnskapsregisteret; prefers KONSERN over SELSKAP.
    Skips itself when <60s of batch budget remain (budget-safe)."""
    if deadline is not None and time.time() > deadline - 60:
        return None, False
    try:
        r = _SESSION.get(f"{BRREG_REGNSKAP}/{org_clean}", timeout=timeout)
        if r.status_code != 200:
            if r.status_code != 404:
                print(f"[FINANCIALS] Registry returned HTTP {r.status_code} for {org_clean}.")
            return None, True
        payload = r.json()
    except Exception as e:
        print(f"[FINANCIALS] fetch failed for {org_clean}: {e}")
        return None, True
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        embedded = payload.get("_embedded")
        entries = embedded.get("regnskaper", []) if isinstance(embedded, dict) else []
    else:
        entries = []
    if not isinstance(entries, list):
        print(f"[FINANCIALS] Unexpected response format for {org_clean}.")
        return None, True
    entries = [e for e in entries if isinstance(e, dict) and not e.get("avviklingsregnskap")]
    if not entries:
        return None, True
    try:
        od = os.path.join(EVIDENCE_DIR, str(org_clean))
        os.makedirs(od, exist_ok=True)
        with open(os.path.join(od, "regnskap.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[FINANCIALS] Could not save account evidence for {org_clean}: {e}")
    entries.sort(key=lambda e: (e.get("regnskapsperiode") or {}).get("tilDato") or "1900-01-01",
                 reverse=True)
    pick = next((e for e in entries if (e.get("regnskapstype") or "").upper() == "KONSERN"), None) \
        or next((e for e in entries if (e.get("regnskapstype") or "").upper() == "SELSKAP"), entries[0])
    per = pick.get("regnskapsperiode") or {}
    fra, til = per.get("fraDato") or "", per.get("tilDato") or ""
    period_year = (til or fra)[:4]
    period_full = f"{fra} to {til}".strip(" to")
    cur = pick.get("valuta") or "NOK"

    def first_amount(paths):
        for path in paths:
            value = _field(pick, *path)
            if _fmt_money(value, cur) is not None:
                return value
        return None

    rev = first_amount(_REV_PATHS)
    prof = first_amount(_PROFIT_PATHS)
    eq = _field(pick, "egenkapitalGjeld", "egenkapital", "sumEgenkapital")
    debt = _field(pick, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld")
    assets = _field(pick, "eiendeler", "sumEiendeler")
    if assets is None:
        assets = _field(pick, "balanseEiendeler", "sumEiendeler")
    return {"revenue": _fmt_money(rev, cur), "profit": _fmt_money(prof, cur),
            "equity": _fmt_money(eq, cur), "debt": _fmt_money(debt, cur),
            "assets": _fmt_money(assets, cur),
            "revenue_exact": rev, "profit_exact": prof, "equity_exact": eq,
            "debt_exact": debt, "assets_exact": assets,
            "period_year": period_year, "period_full": period_full,
            "type": (pick.get("regnskapstype") or "").upper(),
            "currency": cur}, True

# ---------------------------------------------------------------------------
# PER-CATEGORY FACT EXTRACTION (drives coverage + synthesis + unknowns)
# ---------------------------------------------------------------------------
def _fact(field, value, source_url, retrieved, period=None, org=None):
    f = {"field": field, "value": value, "source_url": source_url,
         "retrieved_date": retrieved, "reporting_period": period}
    if org:
        f["evidence_ref"] = f"evidence/{org}/current.json"
    return f

def _extract_categories(item, org_clean, source_url, retrieved, roles=None, fin=None,
                        roles_checked=True, fin_checked=True):
    cats = {}
    unknowns = []
    
    # ---- company_details: harvest EVERYTHING present in the one response ----
    nace2 = _field(item, "naeringskode2", "beskrivelse")
    nace3 = _field(item, "naeringskode3", "beskrivelse")
    sektor = _field(item, "institusjonellSektorkode", "beskrivelse")
    kapital = item.get("kapital")
    kapital_val = None
    if isinstance(kapital, dict):
        belop = kapital.get("belop")
        ktype = kapital.get("type")
        if belop is not None:
            kapital_val = f"{belop} {ktype}".strip()
    elif kapital is not None:
        kapital_val = kapital
    formaal = item.get("vedtektsfestetFormaal")
    if isinstance(formaal, list):
        formaal = "; ".join(str(x) for x in formaal if x) or None
        
    cd = [
        _fact("company_name", item.get("navn"), source_url, retrieved, org=org_clean),
        _fact("business_type", _field(item, "organisasjonsform", "beskrivelse"), source_url, retrieved, org=org_clean),
        _fact("industry", _field(item, "naeringskode1", "beskrivelse"), source_url, retrieved, org=org_clean),
        _fact("secondary_industry", nace2, source_url, retrieved, org=org_clean),
        _fact("tertiary_industry", nace3, source_url, retrieved, org=org_clean),
        _fact("institutional_sector", sektor, source_url, retrieved, org=org_clean),
        _fact("language_form", item.get("maalform"), source_url, retrieved, org=org_clean),
        _fact("registration_date", item.get("registreringsdatoEnhetsregisteret"), source_url, retrieved, org=org_clean),
        _fact("foundation_date", item.get("stiftelsesdato"), source_url, retrieved, org=org_clean),
        _fact("employees", item.get("antallAnsatte"), source_url, retrieved, org=org_clean),
        _fact("share_capital", kapital_val, source_url, retrieved, org=org_clean),
        _fact("purpose", formaal, source_url, retrieved, org=org_clean),
        _fact("under_liquidation", item.get("underAvvikling"), source_url, retrieved, org=org_clean),
        _fact("bankruptcy", item.get("konkurs"), source_url, retrieved, org=org_clean),
        _fact("foretak_register_date", item.get("registreringsdatoForetaksregisteret"), source_url, retrieved, org=org_clean),
        _fact("vat_register_date", item.get("registreringsdatoMerverdiavgiftsregisteret"), source_url, retrieved, org=org_clean),
    ]
    cd = [f for f in cd if f["value"] not in (None, "")]
    cats["company_details"] = {"status": "available" if cd else "not_available", "facts": cd}
    if not cd:
        unknowns.append("company_details")
        
    # ---- contact / website (registry-listed; its own checked axis) ----
    email = _pick(item, ("epostadresse",), ("kontaktposter", "epostadresse"))
    web = _pick(item, ("hjemmeside",), ("kontaktposter", "hjemmeside"))
    phone = _pick(item, ("mobil",), ("telefon",), ("kontaktposter", "mobil"))
    ct = [
        _fact("email", email, source_url, retrieved, org=org_clean),
        _fact("website", web, source_url, retrieved, org=org_clean),
        _fact("phone", phone, source_url, retrieved, org=org_clean),
    ]
    ct = [f for f in ct if f["value"] not in (None, "")]
    cats["contact"] = {"status": "available" if ct else "not_available", "facts": ct,
                       "note": "Contact/website taken from the registry record itself (no external crawl in v1)."}
    if ct:
        unknowns.append("external_footprint_deep")   # we have the listed site, not a full crawl
    else:
        unknowns.append("contact")
        
    # ---- locations (unchanged logic, now with evidence_ref) ----
    loc = []
    fa = item.get("forretningsadresse")
    pa = item.get("postadresse")
    if fa:
        loc.append(_fact("business_address", fa, source_url, retrieved, org=org_clean))
    if pa and pa != fa:
        loc.append(_fact("postal_address", pa, source_url, retrieved, org=org_clean))
    cats["locations"] = {"status": "available" if loc else "not_available", "facts": loc,
                         "note": "Registered business/postal addresses captured; additional workplace units (arbeidssteder) not enumerated in v1."}
    if not loc:
        unknowns.append("locations")
    else:
        unknowns.append("additional_workplaces")
        
    # ---- financial results from annual filings; unavailable data stays explicit ----
    form_code = (_field(item, "organisasjonsform", "kode") or "").upper()
    fin_url = f"{BRREG_REGNSKAP}/{org_clean}"
    ff = []
    if fin and form_code not in NON_FILING_FORMS:
        for fld, ek, dk in (("revenue", "revenue_exact", "revenue"),
                            ("net_profit", "profit_exact", "profit"),
                            ("equity", "equity_exact", "equity"),
                            ("total_debt", "debt_exact", "debt"),
                            ("total_assets", "assets_exact", "assets")):
            val = fin.get(ek)
            if val is not None:
                fct = _fact(fld, val, fin_url, retrieved, period=fin.get("period_full"), org=org_clean)
                fct["display"] = fin.get(dk)
                fct["evidence_ref"] = f"evidence/{org_clean}/regnskap.json"
                ff.append(fct)
        if ff:
            ff.append(_fact("accounts_type", fin.get("type"), fin_url, retrieved,
                            period=fin.get("period_full"), org=org_clean))
    if ff:
        cats["financial_results"] = {"status": "available", "facts": ff,
                                     "note": f"Filed annual accounts ({fin.get('type')}) from "
                                             f"Regnskapsregisteret, period {fin.get('period_full')}."}
    elif form_code in NON_FILING_FORMS:
        cats["financial_results"] = {"status": "not_applicable", "facts": [],
                                     "note": "This org form files no Norwegian annual accounts."}
    elif not fin_checked:
        cats["financial_results"] = {
            "status": "not_available", "facts": [],
            "note": "Annual accounts were not fetched because the remaining batch time budget was reserved.",
        }
        unknowns.append("financial_results")
    else:
        cats["financial_results"] = {"status": "not_available", "facts": [],
                                     "note": "No filed annual accounts found in Regnskapsregisteret for this entity."}
        unknowns.append("financial_results")
        
    roles_url = f"{BRREG_ENHETER}/{org_clean}/roller"
    ppl = []
    for r in (roles or []):
        f = _fact("person", {"name": r["name"], "role": r["role"], "role_code": r["code"]},
                  roles_url, retrieved, org=org_clean)
        f["evidence_ref"] = f"evidence/{org_clean}/roles.json"
        ppl.append(f)
    if ppl:
        cats["people"] = {"status": "available", "facts": ppl,
                          "note": "Active officers/roles from the Brreg roller dataset; departed (avregistrert) roles excluded."}
    elif not roles_checked:
        cats["people"] = {
            "status": "not_available", "facts": [],
            "note": "Registered roles were not fetched because the remaining batch time budget was reserved.",
        }
        unknowns.append("people")
    else:
        cats["people"] = {"status": "not_available", "facts": [],
                          "note": "Roller endpoint returned no active roles for this entity."}
        unknowns.append("people")
        
    cats["jobs"] = {"status": "not_available", "facts": [],
                    "note": "No jobs source configured in v1 (declared scope)."}
    unknowns.append("jobs")
    cats["public_activity"] = {"status": "not_available", "facts": [],
                               "note": "Batch path does not web-search (budget); UI chat covers this."}
    unknowns.append("public_activity")
    
    return cats, unknowns

# ---------------------------------------------------------------------------
# TERMINAL ENVELOPE (one per input; six-state mapped)
# ---------------------------------------------------------------------------
def build_envelope(org_clean, label, allow_ai=False, deadline=None, allow_extras=True):
    """Live-fetch one company and return the rubric's terminal result envelope.
    Never raises; on any failure returns a 'failed'/'blocked'/'not_available'
    envelope so the run always returns one row per input. The deterministic
    summary is used here to avoid batch dependence on external LLM availability."""
    org_clean = sanitize_org_number(org_clean) or str(org_clean).strip()
    item, status, lat, att = _fetch_core(org_clean, deadline=deadline)
    base = {"input_label": label, "org_number": org_clean if org_clean.isdigit() else None,
            "request_count": att, "latency_ms": lat}
            
    if status == "ok" and item is not None:
        profile = _profile_from_item(item, org_clean)
        cur, changed, prev, changes, h = save_evidence_snapshot(org_clean, item, lat, att)
        if allow_extras:
            roles, roles_req = _fetch_roles(org_clean, deadline=deadline)
            fin, fin_req = _fetch_financials(org_clean, deadline=deadline)
        else:
            roles, roles_req = [], False
            fin, fin_req = None, False
        if roles:
            profile["key_people"] = roles
        if fin:
            profile.update({"revenue": fin.get("revenue"), "revenue_period": fin.get("period_year"),
                            "revenue_period_full": fin.get("period_full"),
                            "revenue_type": fin.get("type"), "net_profit": fin.get("profit"),
                            "total_assets": fin.get("assets"), "total_debt": fin.get("debt"),
                            "equity": fin.get("equity"),
                            "revenue_exact": fin.get("revenue_exact"), "profit_exact": fin.get("profit_exact"),
                            "equity_exact": fin.get("equity_exact"), "debt_exact": fin.get("debt_exact"),
                            "assets_exact": fin.get("assets_exact"),
                            "financial_currency": fin.get("currency"), "fin_schema": 3})
        base["request_count"] = att + (1 if roles_req else 0) + (1 if fin_req else 0)
        cats, unknowns = _extract_categories(item, org_clean, profile["source_url"],
                                             profile["retrieved_date"], roles=roles, fin=fin,
                                             roles_checked=roles_req, fin_checked=fin_req)
        ai = basic_summary(profile)
        env = dict(base)
        env.update({
            "status": "available",
            "company_name": profile["company_name"],
            "business_type": profile["business_type"],
            "industry": profile["industry"],
            "address": profile["address"],
            "registration_date": profile["registration_date"],
            "employees": profile["employees"],
            "source_url": profile["source_url"],
            "retrieved_date": profile["retrieved_date"],
            "categories": cats,
            "unknowns": unknowns,
            "changes": changes,
            "evidence_changed": changed,
            "evidence_files": [_fwd(cur)],
            "previous_evidence": [_fwd(p) for p in prev],
            "content_sha256": h,
            "ai_explanation": ai,
            "sources": [profile["source_url"]],
        })
        return env
        
    # non-ok paths
    state_map = {"not_found": "not_available", "blocked": "blocked",
                 "failed": "failed", "invalid_input": "not_available"}
    top = state_map.get(status, "failed")
    note = {
        "not_found": f"No active entity registered under {org_clean} in Brreg.no as of {_today()}.",
        "blocked": f"Brreg.no refused the request for {org_clean} (HTTP 403/429).",
        "failed": f"Registry unreachable for {org_clean} after {att} attempt(s); no facts published.",
        "invalid_input": f"“{label}” is not a valid 9-digit Norwegian organisation number.",
    }.get(status, "Lookup failed.")
    
    env = dict(base)
    env.update({
        "status": top,
        "company_name": None, "business_type": None, "industry": None,
        "address": None, "registration_date": None, "employees": None,
        "source_url": f"{BRREG_ENHETER}/{org_clean}" if (org_clean or "").isdigit() else None,
        "retrieved_date": _today(),
        "categories": {},
        "unknowns": ["company_details", "locations", "people", "financial_results",
                     "jobs", "public_activity"],
        "changes": [], "evidence_changed": None, "previous_evidence": [],
        "evidence_files": [], "content_sha256": None,
        "ai_explanation": note,
        "sources": [],
    })
    return env

def ambiguous_envelope(label, suggestions):
    env = {"input_label": label, "org_number": None, "status": "ambiguous",
           "company_name": None, "business_type": None, "industry": None,
           "address": None, "registration_date": None, "employees": None,
           "source_url": None, "retrieved_date": _today(), "categories": {},
           "unknowns": ["company_identity_unresolved"], "changes": [],
           "evidence_changed": None, "previous_evidence": [], "evidence_files": [],
           "content_sha256": None, "request_count": 0, "latency_ms": 0,
           "ai_explanation": (f"Could not confirm a single company for “{label}”. "
                              f"Closest registered names: "
                              + "; ".join(f"{n} ({o})" for n, o in (suggestions or []))
                              + ". Publishing none to avoid a wrong-company match."),
           "sources": [], "suggestions": [list(s) for s in (suggestions or [])]}
    return env

def not_available_name_envelope(label):
    env = {"input_label": label, "org_number": None, "status": "not_available",
           "company_name": None, "business_type": None, "industry": None,
           "address": None, "registration_date": None, "employees": None,
           "source_url": None, "retrieved_date": _today(), "categories": {},
           "unknowns": ["company_details"], "changes": [], "evidence_changed": None,
           "previous_evidence": [], "evidence_files": [], "content_sha256": None,
           "request_count": 0, "latency_ms": 0,
           "ai_explanation": (f"No active entity matching the name “{label}” was found in the "
                              f"Norwegian Enhetsregisteret (Brreg.no) name search as of {_today()}."),
           "sources": []}
    return env

# ---------------------------------------------------------------------------
# NAME SEARCH (four layers + ambiguous flag)
# ---------------------------------------------------------------------------
def correct_company_name(query):
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
    q = (query or "").lower().strip()
    if not q:
        return []
    pairs = []
    for org, item in load_cache().items():
        nm = (item.get("company_name") or "").strip()
        if nm:
            pairs.append((nm, org))
    close = difflib.get_close_matches(q, [nm for nm, _ in pairs], n=n, cutoff=cutoff)
    out = []
    for c in close:
        org = next((o for nm, o in pairs if nm == c), None)
        if org:
            out.append((c, org))
    return out

def search_by_name(name_query, max_rank=4):
    global LAST_SUGGESTIONS, LAST_MATCH_AMBIGUOUS
    LAST_SUGGESTIONS = []
    LAST_MATCH_AMBIGUOUS = False
    url = BRREG_ENHETER
    
    def fetch_candidates(q):
        try:
            r = _SESSION.get(url, params={"navn": q, "size": 10}, timeout=8)
            if r.status_code == 200:
                return r.json().get("_embedded", {}).get("enheter", [])
        except Exception as e:
            print(f"Error searching by name: {e}")
        return []
        
    def rank_company(company, q):
        c_name = (company.get("navn") or "").lower()
        if c_name == q:
            return 0
        if c_name in (f"{q} asa", f"{q} as"):
            return 1
        if c_name.startswith(q):
            return 2
        if q in c_name:
            return 3
        return 4
        
    def coverage_ok(company, q):
        c_name = (company.get("navn") or "").lower()
        core = c_name
        for suf in (" asa", " as"):
            if core.endswith(suf):
                core = core[:-len(suf)]
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
                        return org_nr
            else:
                print(f"[SPELLCHECK] LLM suggestion '{corrected}' too far from input; ignored.")
        LAST_SUGGESTIONS = cache_suggestions(name_query)
        print(f"[NOT FOUND] Registry has no candidates for “{name_query}”.")
        return None
        
    best_match = sorted(enheter, key=lambda c: rank_company(c, query_lower))[0]
    best_rank = rank_company(best_match, query_lower)
    
    if best_rank in (2, 3) and not coverage_ok(best_match, query_lower):
        best_rank = 4
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
                LAST_MATCH_AMBIGUOUS = bool(LAST_SUGGESTIONS)   # a real candidate existed but unsure
                print(f"[NOT FOUND] Best match too weak for “{name_query}” (no close spelling).")
                return None
    elif best_rank > max_rank:
        LAST_SUGGESTIONS = collect_suggestions() or cache_suggestions(name_query)
        LAST_MATCH_AMBIGUOUS = bool(LAST_SUGGESTIONS)
        print(f"[NOT FOUND] Best match too weak for “{name_query}”.")
        return None
        
    org_nr = str(best_match.get("organisasjonsnummer"))
    print(f"[NAME MATCH] Found '{best_match.get('navn')}' (Org Nr: {org_nr})")
    return org_nr

# ---------------------------------------------------------------------------
# AI GENERATION (write-path contamination guard)
# ---------------------------------------------------------------------------
def generate_ai_explanation(profile):
    if getattr(llm, "client", None) is None:
        return basic_summary(profile)
    prompt = (
        "You are a business analyst. Summarize this company profile from the Norwegian Business Registry in 2-3 concise "
        "sentences. Highlight its business type and location. If filed financial data exists, state whether the figures "
        "are consolidated group (KONSERN) or entity-only (SELSKAP) accounts, and include the reporting year. "
        "Reply with ONLY the final summary text — never include thinking, drafts, iterations, "
        "critiques, headings or lists.\n\n"
        f"Data: {json.dumps(profile, ensure_ascii=False)}"
    )
    res = llm.ask_llm(prompt, model_tier="fast")
    if res and not _is_contaminated(res):
        return res
    return basic_summary(profile)

def _clean_snippet(t, limit=280):
    """Strip markdown junk (headings, tables, links, emphasis) from a snippet."""
    out = []
    for ln in (t or "").split("\n"):
        s = ln.strip()
        if not s or s.startswith("|") or s.endswith("|") or set(s) <= set("|-: "):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
        s = s.replace("*", "").replace("`", "")
        s = re.sub(r"\s{2,}", " ", s).strip()
        if s:
            out.append(s)
    txt = " ".join(out)
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt

def _findings_markdown_from_items(items, cap=6):
    blocks = []
    for i, it in enumerate(items[:cap], 1):
        title = _clean_snippet(it.get("title"), 120) or "Untitled source"
        snip = _clean_snippet(it.get("snippet"), 280)
        b = f"**{i}. {title}**\n"
        if snip:
            b += f"\n> {snip}\n"
        if it.get("url"):
            b += f"\n🔗 [source]({it['url']})\n"
        blocks.append(b)
    return "\n".join(blocks)

def _structure_findings(context_str, snippet_chars=300):
    """Parse Tavily context blocks into (title, snippet, url) triples and
    neutralise markdown injected by scraped pages."""
    out = []
    for block in (context_str or "").split("\n\n"):
        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
        if not lines:
            continue
        title = re.sub(r"^\[\d+\]\s*", "", lines[0]).replace("*", "").replace("`", "").strip()
        body = lines[1:]
        url = ""
        if body and body[-1].startswith("Source:"):
            url = body[-1][len("Source:"):].strip()
            body = body[:-1]
        body = [l for l in body if not re.fullmatch(r"[=\-_*#~]{3,}", l)]
        snippet = " ".join(body).replace("*", "").replace("`", "").replace(">", "")
        snippet = re.sub(r"\s{2,}", " ", snippet).strip()
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars].rsplit(" ", 1)[0] + "…"
        if title or snippet:
            out.append((title or "Untitled source", snippet, url))
    return out

def _findings_markdown(items):
    blocks = []
    for i, (t, s, u) in enumerate(items, 1):
        b = f"**{i}. {t}**\n"
        if s:
            b += f"\n> {s}\n"
        if u:
            b += f"\n🔗 [source]({u})\n"
        blocks.append(b)
    return "\n".join(blocks)

def chat_with_gemini(company_context, user_question, use_web_search=True,
                     conversation=None, brief=False, deep=False):
    global LAST_SOURCES
    LAST_SOURCES = []
    context_str = ""
    if use_web_search:
        context_str = search.get_web_context(
            f"{company_context.get('company_name', '')} {user_question}",
            max_results=10 if deep else 5,
            depth="advanced" if deep else "basic")
        for item in getattr(search, "LAST_ITEMS", []):
            u = item.get("url")
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
                     
    kp = company_context.get("key_people") or []
    officers = ("\n".join(f"- {p['name']} — {p['role']} (code {p['code']})" for p in kp)
                if kp else "(none listed)")
                
    prompt = f"""You are an expert business research analyst.
VERIFIED REGISTRY DATA:
{json.dumps(company_context, ensure_ascii=False)}
VERIFIED OFFICERS (Brreg roller — authoritative for CEO/board questions):
{officers}
WEB SEARCH FINDINGS (numbered):
{context_str if context_str else "No web data available."}
CONVERSATION SO FAR (most recent last):
{conv_str if conv_str else "(start of conversation)"}
USER QUESTION:
{user_question}
STYLE RULES:
Answer directly; let length match the question (prefer <=350 words unless depth mode or the data demands more).
If the conversation so far already contains the facts the user asks to condense, summarize THAT instead of listing new data.
Do NOT put inline citation markers like [1] in the text; the app displays sources separately.
Use only facts present in the data above. If a metric, year or detail is missing, omit it or say in one short sentence that it is unavailable — never invent numbers and never fill tables with dashes.
For CEO/board/officer questions the VERIFIED OFFICERS list overrides web findings; if a web source names a different person, answer with the registry officer and add one sentence that the web source appears outdated.
Use a compact table only when comparing 3+ numeric values; otherwise use bullets or prose.
Finish with complete sentences; never end with a truncated footnote.{mode_note}"""

    res = llm.ask_llm(prompt, model_tier="smart")
    if res:
        return res
    if LAST_SOURCES:
        return (f"🌐 **Live web findings for “{user_question}”** "
                f"_(AI synthesis temporarily offline — cleaned source digests below)_\n\n"
                + _findings_markdown_from_items(getattr(search, "LAST_ITEMS", []))
                + f"\n_Registry facts:_ {basic_summary(company_context)}")
    return local_answer(company_context, user_question) + basic_note()

def chat_about_topic(user_question, use_web_search=True, deep=False):
    global LAST_SOURCES
    LAST_SOURCES = []
    context_str = ""
    if use_web_search:
        context_str = search.get_web_context(user_question,
                                             max_results=10 if deep else 5,
                                             depth="advanced" if deep else "basic")
        for item in getattr(search, "LAST_ITEMS", []):
            u = item.get("url")
            if u and u not in LAST_SOURCES:
                LAST_SOURCES.append(u)
                        
    prompt = f"""You are a helpful research assistant.
WEB SEARCH FINDINGS (numbered):
{context_str if context_str else "No web data available."}
USER QUESTION:
{user_question}
STYLE RULES:
Answer directly; length should match the question (no artificial cap).
Do NOT put inline citation markers like [1]; the app shows sources separately.
Use only facts from the findings; if missing, say so in one sentence — never invent.
Finish with complete sentences; never end with a truncated footnote."""

    res = llm.ask_llm(prompt, model_tier="smart")
    if res:
        return res
    items = getattr(search, "LAST_ITEMS", [])
    if items:
        return (f"🌐 **Live web findings for “{user_question}”** "
                f"_(AI synthesis temporarily offline — cleaned source digests below)_\n\n"
                + _findings_markdown_from_items(items))
    return "No reliable web information found for that topic."

# ---------------------------------------------------------------------------
# INTENT CLASSIFICATION
# ---------------------------------------------------------------------------
INTENT_SYSTEM_PROMPT = """You are an intent router for a company research agent.
Intents: lookup, followup, save, report, refresh, chitchat.
Reply with ONLY a JSON object: {"intent","company_query","question","referent_company"}."""

def classify_intent(query, context_hint=" "):
    if getattr(llm, "client", None) is None:
        return None
    prompt = f"""Context: {context_hint or 'no company in view yet'}
Examples:
"save this company details in a pdf" -> {{"intent": "save", "company_query": "", "question": "", "referent_company": ""}}
"latest news about Equinor?" -> {{"intent": "followup", "company_query": "Equinor", "question": "latest news about Equinor?", "referent_company": "Equinor"}}
"914778271" -> {{"intent": "lookup", "company_query": "914778271", "question": "", "referent_company": ""}}
"download my report" -> {{"intent": "report", "company_query": "", "question": "", "referent_company": ""}}
"refresh" -> {{"intent": "refresh", "company_query": "", "question": "", "referent_company": ""}}
"hello" -> {{"intent": "chitchat", "company_query": "", "question": "", "referent_company": ""}}
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
# CACHE LOOKUP (UI / interactive path; optional LLM synthesis)
# ---------------------------------------------------------------------------
def lookup_company(org_nr, force_refresh=False):
    global LAST_LOOKUP_STATUS
    org_clean = sanitize_org_number(org_nr) or str(org_nr).strip()
    cache = load_cache()
    
    if org_clean in cache and not force_refresh:
        print(f"[CACHE HIT] Returning stored data for org_number: {org_clean}")
        row = cache[org_clean]
        if row.get("employees") is None or "email" not in row:
            live, st, _, _ = _fetch_core(org_clean)
            if st == "ok" and live:
                row["employees"] = live.get("antallAnsatte")
                row["email"] = _pick(live, ("epostadresse",), ("kontaktposter", "epostadresse"))
                row["phone"] = _pick(live, ("mobil",), ("telefon",), ("kontaktposter", "mobil"))
                row["website"] = _pick(live, ("hjemmeside",), ("kontaktposter", "hjemmeside"))
                save_cache(cache)
        if not cache[org_clean].get("key_people"):
            rp, _ = _fetch_roles(org_clean)
            if rp:
                cache[org_clean]["key_people"] = rp
                save_cache(cache)
        if row.get("fin_schema") != 3:
            fin, _ = _fetch_financials(org_clean)
            if fin:
                row.update({"revenue": fin.get("revenue"), "revenue_period": fin.get("period_year"),
                            "revenue_period_full": fin.get("period_full"),
                            "revenue_type": fin.get("type"), "net_profit": fin.get("profit"),
                            "total_assets": fin.get("assets"), "total_debt": fin.get("debt"),
                            "equity": fin.get("equity"),
                            "revenue_exact": fin.get("revenue_exact"), "profit_exact": fin.get("profit_exact"),
                            "equity_exact": fin.get("equity_exact"), "debt_exact": fin.get("debt_exact"),
                            "assets_exact": fin.get("assets_exact"),
                            "financial_currency": fin.get("currency"), "fin_schema": 3})
                row["ai_explanation"] = basic_summary(row)
            else:
                row["fin_schema"] = 3
            save_cache(cache)
                
        current = cache[org_clean].get("ai_explanation", "")
        if ai_status()[0] == "ai" and (
            _is_contaminated(current)
            or current == basic_summary(cache[org_clean])
            or "All facts are verified against the official Brreg.no registry" in current
        ):
            print("[UPGRADING] Regenerating AI explanation for cached record...")
            cache[org_clean]["ai_explanation"] = generate_ai_explanation(cache[org_clean])
            save_cache(cache)
            
        LAST_LOOKUP_STATUS = "ok"
        return cache[org_clean]
        
    item, status, lat, att = _fetch_core(org_clean)
    if status == "ok" and item is not None:
        profile = _profile_from_item(item, org_clean)
        save_evidence_snapshot(org_clean, item, lat, att)
        rp, _ = _fetch_roles(org_clean)
        if rp:
            profile["key_people"] = rp
        fin, _ = _fetch_financials(org_clean)
        profile.update({
            "revenue": (fin or {}).get("revenue"),
            "revenue_period": (fin or {}).get("period_year"),
            "revenue_period_full": (fin or {}).get("period_full"),
            "revenue_type": (fin or {}).get("type"),
            "net_profit": (fin or {}).get("profit"),
            "total_assets": (fin or {}).get("assets"),
            "total_debt": (fin or {}).get("debt"),
            "equity": (fin or {}).get("equity"),
            "revenue_exact": (fin or {}).get("revenue_exact"),
            "profit_exact": (fin or {}).get("profit_exact"),
            "equity_exact": (fin or {}).get("equity_exact"),
            "debt_exact": (fin or {}).get("debt_exact"),
            "assets_exact": (fin or {}).get("assets_exact"),
            "financial_currency": (fin or {}).get("currency"),
            "fin_schema": 3,
        })
        profile["ai_explanation"] = generate_ai_explanation(profile)
        cache[org_clean] = profile
        save_cache(cache)
        print(f"[CACHE UPDATED] Saved {profile['company_name']} to '{CACHE_FILE}'.")
        LAST_LOOKUP_STATUS = "ok"
        return profile
        
    LAST_LOOKUP_STATUS = {"not_found": "not_found", "blocked": "fetch_error",
                          "failed": "fetch_error", "invalid_input": "not_found"}.get(status, "fetch_error")
    return None

# ---------------------------------------------------------------------------
# BATCH INIT (UI/demo only; NOT called on the official batch path)
# ---------------------------------------------------------------------------
def initialize_batch_cache(target_count=1000):
    _ensure_cache_hygiene()
    cache = load_cache()
    if len(cache) >= target_count:
        print(f"[READY] Cache already contains {len(cache)} companies.")
        return
    print(f"Pre-populating cache up to {target_count} companies...")
    page = 0
    size_per_page = 100
    while len(cache) < target_count:
        url = f"{BRREG_ENHETER}?page={page}&size={size_per_page}"
        try:
            res = _SESSION.get(url, timeout=10)
            if res.status_code != 200:
                break
            enheter = res.json().get("_embedded", {}).get("enheter", [])
            if not enheter:
                break
            for item in enheter:
                org_nr = str(item.get("organisasjonsnummer"))
                if org_nr and org_nr not in cache:
                    profile = _profile_from_item(item, org_nr)
                    profile["ai_explanation"] = basic_summary(profile)
                    cache[org_nr] = profile
                    if len(cache) >= target_count:
                        break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"Batch fetch error: {e}")
            break
    save_cache(cache)
    print(f"[SUCCESS] Cache initialized with {len(cache)} unique companies.")