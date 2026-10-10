"""
intent.py — Layer 2: hybrid intent router (LLM + offline fallbacks).
Order: orgnr -> registry strong-match (company names win) -> verb keywords
(save/report/refresh/chitchat) -> LLM classifier -> offline heuristics.
There is NO exit intent: vague or command-like words are always treated as
potential company fragments first.
"""

# Copyright (c) 2026 <Yatharth Rastogi> — MIT License.

import re
from dataclasses import dataclass
from agent import search_by_name, classify_intent, load_cache
import memory

QUESTION_HINTS = ["what", "why", "how", "who", "where", "when", "tell", "can you",
                  "could you", "search", "latest", "news", "more about", "explain",
                  "describe", "summar", "history", "projects", "revenue", "employees",
                  "ownership", "listed", "subsidiar", "competitor", "number of",
                  "people", "working", "staff", "ceo", "owner", "contact", "phone",
                  "email", "industry", "sector", "salary", "profit", "loss", "shares"]
STRIP_PREFIXES = ["look up", "lookup", "search for", "search", "find", "tell me about",
                  "show me", "get", "who is", "what is", "please"]
REFRESH_PATTERNS = ["refresh", "re-fetch", "refetch", "update data", "update the data"]
REPORT_PATTERNS = ["report", "my report", "the report", "download report",
                   "download my report", "pdf report", "show report", "show my report"]
CHITCHAT_PATTERNS = ["hi", "hello", "hey", "who are you", "what can you do", "help",
                     "thanks", "thank you"]
STOP_WORDS = {"latest", "news", "about", "the", "a", "an", "of", "in", "on", "for",
              "tell", "me", "what", "is", "how", "many", "people", "work", "works",
              "working", "company", "asa", "as", "please", "can", "you", "show",
              "current", "projects", "project", "their", "its", "it", "there", "do",
              "does", "did", "and", "or", "with", "from", "to", "at", "by", "who",
              "any", "some", "give", "get", "norwegian", "norway"}

@dataclass
class Decision:
    intent: str
    company_query: str = ""
    question: str = ""
    org_nr: str = None
    stage: str = ""

def _clean(w):
    return w.strip("?,.!\"'()")

def strip_command_prefix(q):
    s = q.strip()
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for p in STRIP_PREFIXES:
            if low.startswith(p + " "):
                s = s[len(p):].strip()
                changed = True
                break
    return s or q.strip()

def looks_like_question(q):
    ql = q.lower().strip()
    if ql.endswith("?"):
        return True
    if len(ql.split()) > 4:
        return True
    return any(ql.startswith(h) or f" {h} " in f" {ql} " for h in QUESTION_HINTS)

def _keyword_intent(q):
    """Free verb layer — runs AFTER the registry probe so company names win."""
    ql = q.lower().strip().rstrip(".!")
    if "?" in ql:
        return None
    words = len(ql.split())
    if words <= 3:
        if any(p in ql for p in REFRESH_PATTERNS):
            return "refresh"
        if any(p in ql for p in REPORT_PATTERNS):
            return "report"
        if re.search(r"\bsave\b", ql):
            return "save"
        if any(p in ql for p in CHITCHAT_PATTERNS):
            return "chitchat"
    if re.search(r"\bsave\b.*\b(pdf|report|pool)\b", ql):
        return "save"
    if re.search(r"\b(download|show|view|open)\b.*\b(report|pdf)\b", ql):
        return "report"
    if re.search(r"\brefresh\b.*\b(data|registry|profile|company)\b", ql):
        return "refresh"
    return None

def _heuristic_intent(q):
    ki = _keyword_intent(q)
    if ki:
        return ki
    return "followup" if looks_like_question(q) else "lookup"

def guess_company_org(q):
    if q.isdigit() and len(q) == 9:
        return q
    words = [_clean(w) for w in q.split()]
    pairs = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)
             if words[i] and words[i+1]
             and words[i].lower() not in STOP_WORDS and words[i+1].lower() not in STOP_WORDS
             and words[i][0].isupper() and words[i+1][0].isupper()]
    for cand in sorted(pairs, key=len, reverse=True):
        org = search_by_name(cand, max_rank=2)
        if org:
            return org
    singles = [w for w in words if w and w.lower() not in STOP_WORDS
               and (w[0].isupper() or (w.isdigit() and len(w) != 9))]
    for cand in sorted(set(singles), key=len, reverse=True):
        org = search_by_name(cand, max_rank=2)
        if org:
            return org
    return None

def context_hint(thread_id):
    if not thread_id:
        return ""
    comps = memory.thread_companies(thread_id)
    if not comps:
        return ""
    listing = ", ".join(f"{c['name']} ({c['org_nr']})" for c in comps[-6:])
    last = comps[-1]
    return (f"companies discussed in this thread: {listing}. "
            f"company currently in view: {last['name']} ({last['org_nr']})")

def _resolve_referent(ref, thread_id):
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.isdigit() and len(ref) == 9:
        return ref
    if thread_id:
        rl = ref.lower()
        for c in memory.thread_companies(thread_id):
            if rl and (rl in c["name"].lower() or c["name"].lower() in rl or rl == c["org_nr"]):
                return c["org_nr"]
    return search_by_name(ref)

def route(query, thread_id=None):
    q = (query or "").strip()
    if not q:
        return Decision(intent="chitchat", stage="empty")

    # Stage 1 — exact org number (free)
    if q.isdigit() and len(q) == 9:
        return Decision(intent="lookup", company_query=q, org_nr=q, stage="orgnr")

    # Stage 2 — registry strong-match FIRST: company names beat command words
    probe = strip_command_prefix(q)
    org = search_by_name(probe, max_rank=2)
    if org:
        return Decision(intent="lookup", company_query=probe, org_nr=org, stage="registry")

    # Stage 2.5 — free verb keywords (only when no company matched)
    ki = _keyword_intent(q)
    if ki:
        org2 = memory.last_company_org(thread_id) if thread_id and ki in ("save", "refresh") else None
        return Decision(intent=ki, org_nr=org2, stage="keyword")

    # Stage 3 — LLM classifier
    parsed = classify_intent(q, context_hint(thread_id))
    if parsed:
        i = parsed.get("intent", "followup")
        cq = (parsed.get("company_query") or "").strip()
        question = (parsed.get("question") or "").strip() or q
        referent = (parsed.get("referent_company") or "").strip()
        org_nr = None
        if i == "lookup":
            cq = strip_command_prefix(cq or q)
            org_nr = cq if (cq.isdigit() and len(cq) == 9) else search_by_name(cq)
        elif i in ("save", "refresh", "followup"):
            org_nr = _resolve_referent(referent or cq, thread_id)
        return Decision(intent=i, company_query=cq, question=question,
                        org_nr=org_nr, stage="llm")

    # Stage 4 — offline heuristics
    intent = _heuristic_intent(q)
    org_nr = None
    if intent == "lookup":
        org_nr = search_by_name(probe)
    elif intent == "followup":
        org_nr = guess_company_org(q) or (memory.last_company_org(thread_id) if thread_id else None)
    elif intent in ("save", "refresh") and thread_id:
        org_nr = memory.last_company_org(thread_id)
    return Decision(intent=intent,
                    company_query=probe if intent == "lookup" else "",
                    question=q if intent in ("followup", "chitchat") else "",
                    org_nr=org_nr, stage="heuristic")

# ---------------------------------------------------------------- smoke test
if __name__ == "__main__":
    for t in ["914778271", "quit", "Quite Simple", "report",
              "save this company details in a pdf", "Latest news about Equinor?",
              "search for a company whose name is like quit please"]:
        d = route(t, thread_id=None)
        print(f"{t!r:52} -> {d.intent:8} stage={d.stage:9} org={d.org_nr}")