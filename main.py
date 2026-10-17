"""
main.py — Signalpost CLI (official entry point).
Canonical run command (accepts a JSONL batch of organisation numbers, one
terminal result envelope per input line, in order):
    python main.py --batch-file entry-companies.jsonl
Also accepts a plain .txt (one org number / name per line) and --companies.
Honours the official run's fixed time/resource budget: batch is template-only
by default (no per-row LLM), uses a tiny polite delay, a global wall-clock
budget and a consecutive-failure circuit breaker, so the run ALWAYS returns
exactly one result per input (a missing row or timeout = unscored).
Emits results.json (envelopes), results.csv (flat summary), results.jsonl
(canonical envelopes, one per line) and run_report.json.
"""
import argparse
import json
import csv
import time
import os
import sys
import urllib.parse
from datetime import datetime, timezone
import agent
import llm
from agent import (build_envelope, ambiguous_envelope, not_available_name_envelope,
                   sanitize_org_number, search_by_name, ai_status)

FIELDNAMES = ["input_label", "org_number", "status", "company_name", "business_type",
              "industry", "address", "registration_date", "employees", "num_facts",
              "num_unknowns", "num_changes", "source_url", "retrieved_date",
              "latency_ms", "request_count", "ai_explanation"]
STATE_KEYS = ["available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"]


def _count_facts(env):
    n = 0
    for cat in (env.get("categories") or {}).values():
        n += len(cat.get("facts") or [])
    return n


def _flat_row(env):
    return {
        "input_label": env.get("input_label"),
        "org_number": env.get("org_number"),
        "status": env.get("status"),
        "company_name": env.get("company_name"),
        "business_type": env.get("business_type"),
        "industry": env.get("industry"),
        "address": env.get("address"),
        "registration_date": env.get("registration_date"),
        "employees": env.get("employees"),
        "num_facts": _count_facts(env),
        "num_unknowns": len(env.get("unknowns") or []),
        "num_changes": len(env.get("changes") or []),
        "source_url": env.get("source_url"),
        "retrieved_date": env.get("retrieved_date"),
        "latency_ms": env.get("latency_ms"),
        "request_count": env.get("request_count"),
        "ai_explanation": env.get("ai_explanation"),
    }


def _save_results(envelopes, json_file):
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(envelopes, f, ensure_ascii=False, indent=2)
    csv_file = json_file.replace(".json", ".csv")
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for env in envelopes:
            row = _flat_row(env)
            for k, v in row.items():
                if v is None:
                    row[k] = ""                      # never 0 for missing
                elif isinstance(v, (dict, list)):
                    row[k] = json.dumps(v, ensure_ascii=False)
            w.writerow(row)
    jsonl_file = json_file.replace(".json", ".jsonl")
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for env in envelopes:
            f.write(json.dumps(env, ensure_ascii=False, separators=(",", ":")) + "\n")
    return csv_file, jsonl_file


def _read_inputs(path):
    """Return list of raw input labels (one per non-empty record), preserving
    order and duplicates. Accepts .jsonl (org number per object) or .txt."""
    labels = []
    if path.endswith(".jsonl"):
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                    val = (obj.get("organisasjonsnummer") or obj.get("org_number")
                           or obj.get("org") or obj.get("organization_number"))
                    labels.append(str(val) if val is not None else ln)
                except Exception:
                    labels.append(ln)
    else:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    labels.append(ln)
    return labels


def _resolve(label):
    """-> (org_or_None, ambiguous_bool). 9-digit/sanitized => org. Name =>
    search; None+ambiguous flag distinguishes 'unsure candidate' from 'nothing'."""
    san = sanitize_org_number(label)
    if san:
        return san, False
    if label.strip().isdigit() and len(label.strip()) != 9:
        return None, False                       # wrong-length number => not_available
    org = search_by_name(label)
    if org:
        return org, False
    return None, bool(getattr(agent, "LAST_MATCH_AMBIGUOUS", False))


def _run_report(envelopes, items_in, start, command, budget, breaker, ai_on, delay):
    counts = {s: 0 for s in STATE_KEYS}
    for e in envelopes:
        counts[e.get("status", "failed")] = counts.get(e.get("status", "failed"), 0) + 1
    total_req = sum(e.get("request_count", 0) or 0 for e in envelopes)
    lats = [e.get("latency_ms", 0) or 0 for e in envelopes]
    rep = {
        "run_id": time.strftime("signalpost-%Y%m%d-%H%M%S"),
        "command": command,
        "items_in": items_in,
        "items_recorded": len(envelopes),
        "row_parity_ok": items_in == len(envelopes),
        "states": counts,
        "runtime_seconds": round(time.time() - start, 1),
        "registry_requests": total_req,
        "web_search_calls": 0,
        "latency_ms_avg": round(sum(lats) / len(lats), 1) if lats else 0,
        "models": {"smart": (llm.MODEL_CHAIN[0] if getattr(llm, "MODEL_CHAIN", None) else "none"),
                   "fast": (llm.FAST_CHAIN[0] if getattr(llm, "FAST_CHAIN", None) else "none")},
        "ai_synthesis_enabled": ai_on,
        "per_row_delay_s": delay,
        "budget_seconds": budget,
        "circuit_breaker_tripped": breaker,
        "third_party_cost_usd": 0.0,
        "cost_basis": "free-tier OpenRouter + keyless Brreg; declared ceiling <= $10/day",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open("run_report.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    return rep


def batch_mode(labels, output="results.json", limit=0, ai_on=False,
               budget=1500, delay=0.05, command="python main.py --batch-file <file>"):
    if limit > 0:
        labels = labels[:limit]
    if not labels:
        print("❌ Error: no input items.")
        sys.exit(1)
    items_in = len(labels)
    print(f"\n[2/3] Processing {items_in} inputs (one envelope per input, in order)...")
    _, desc = ai_status()
    print(f"      AI status: {desc} | synthesis in batch: {'ON (--ai)' if ai_on else 'OFF (template, budget-safe)'}")
    print(f"      Budget: {budget}s | per-row delay: {delay}s | circuit breaker: 8 consecutive failures\n")

    envelopes = []
    memo = {}
    start = time.time()
    deadline = start + budget
    consecutive_failed = 0
    breaker = False
    for i, label in enumerate(labels, 1):
        if time.time() > deadline:
            envelopes.append(_quick_failed(label, "global budget exceeded"))
            continue
        org, amb = _resolve(label)
        if amb and not org:
            envelopes.append(ambiguous_envelope(label, getattr(agent, "LAST_SUGGESTIONS", [])))
            continue
        if not org:
            envelopes.append(not_available_name_envelope(label))
            continue
        if org in memo:
            env = dict(memo[org])
            env["input_label"] = label
            envelopes.append(env)
            continue
        env = build_envelope(org, label, allow_ai=ai_on, deadline=deadline)
        memo[org] = {k: v for k, v in env.items() if k != "input_label"}
        envelopes.append(env)
        if env.get("status") in ("failed", "blocked"):
            consecutive_failed += 1
            if consecutive_failed >= 8:
                breaker = True
                print("  [BREAKER] 8 consecutive failures — marking remaining inputs failed without fetching.")
        else:
            consecutive_failed = 0
        if not breaker:
            time.sleep(delay)
        tag = env.get("status")
        nm = env.get("company_name") or label
        print(f"  [{i}/{items_in}] {label} -> {tag} ({nm})")

    # if breaker tripped mid-loop, ensure every remaining input still got a row
    while len(envelopes) < items_in:
        envelopes.append(_quick_failed(labels[len(envelopes)], "circuit breaker / budget"))

    print(f"\n[3/3] Saving results...")
    csv_file, jsonl_file = _save_results(envelopes, output)
    rep = _run_report(envelopes, items_in, start, command, budget, breaker, ai_on, delay)
    counts = " · ".join(f"{k}:{rep['states'][k]}" for k in STATE_KEYS)
    print("\n" + "=" * 60)
    print(f"  ✅ COMPLETE in {rep['runtime_seconds']}s — {rep['items_recorded']}/{rep['items_in']} rows (parity {rep['row_parity_ok']})")
    print(f"     {counts}")
    print("=" * 60 + "\n")
    print(f"📦 Envelopes (.jsonl): {os.path.abspath(jsonl_file)}")
    print(f"📄 JSON:  {os.path.abspath(output)}")
    print(f"📊 CSV:   {os.path.abspath(csv_file)}")
    print(f"📑 Report:{os.path.abspath('run_report.json')}\n")
    print("💡 Web UI:  streamlit run streamlit_app.py\n")
    if not rep["row_parity_ok"]:
        print("⚠️ ROW PARITY BROKEN — inputs != recorded. Fix before submitting.\n")


def _quick_failed(label, reason):
    return {"input_label": label, "org_number": None, "status": "failed",
            "company_name": None, "business_type": None, "industry": None,
            "address": None, "registration_date": None, "employees": None,
            "source_url": None, "retrieved_date": agent._today(), "categories": {},
            "unknowns": ["not_attempted_budget_or_breaker"], "changes": [],
            "evidence_changed": None, "previous_evidence": [], "evidence_files": [],
            "content_sha256": None, "request_count": 0, "latency_ms": 0,
            "ai_explanation": f"Not fetched this run ({reason}); row emitted to preserve one-result-per-input.",
            "sources": []}


def interactive_mode():
    print("\n" + "=" * 60)
    print("  SIGNALPOST AGENT — INTERACTIVE MODE")
    print("=" * 60)
    print("[INIT] Warming local cache + self-heal (demo/UI convenience only)...")
    agent.initialize_batch_cache(1000)
    _, desc = ai_status()
    print(f"  AI status: {desc}")
    print("  Type a company name or 9-digit org number. Ctrl+C to exit.\n")
    results = []
    start = time.time()
    try:
        while True:
            try:
                raw = input("🔍 > ").strip()
            except EOFError:
                print("\n[EXIT] End of input.")
                break
            if not raw:
                continue
            org, amb = _resolve(raw)
            if amb and not org:
                env = ambiguous_envelope(raw, getattr(agent, "LAST_SUGGESTIONS", []))
            elif not org:
                env = not_available_name_envelope(raw)
            else:
                env = build_envelope(org, raw, allow_ai=True)   # interactive: LLM synthesis ok
            results.append(env)
            print(f"\n  status={env['status']}  {env.get('company_name') or raw}")
            for c, cat in (env.get("categories") or {}).items():
                fs = cat.get("facts") or []
                print(f"     {c}: {cat.get('status')} ({len(fs)} facts)")
            if env.get("unknowns"):
                print(f"     unknowns: {', '.join(env['unknowns'])}")
            if env.get("changes"):
                print(f"     changes vs previous evidence: {len(env['changes'])}")
            print(f"     AI: {env.get('ai_explanation', '')}\n")
    except KeyboardInterrupt:
        print("\n[EXIT] Interrupted by user.")
    if results:
        csv_file, jsonl_file = _save_results(results, "results.json")
        rep = _run_report(results, len(results), start, "python main.py (interactive)",
                          0, False, True, 0)
        print(f"📦 {os.path.abspath('results.jsonl')}  📄 results.json  📊 {os.path.abspath(csv_file)}  📑 run_report.json")
        print(f"   states: {rep['states']}\n")


def main():
    p = argparse.ArgumentParser(description="Signalpost: Norwegian Company Intelligence Agent",
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--batch-file", type=str,
                   help="Path to a .jsonl (org number per line) or .txt batch")
    p.add_argument("--companies", nargs="+", help="Space-separated org numbers / names")
    p.add_argument("--output", type=str, default="results.json")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--ai", action="store_true",
                   help="Enable per-row LLM synthesis in batch (OFF by default to protect the run budget)")
    p.add_argument("--budget", type=int, default=1500,
                   help="Wall-clock seconds for the batch run (default 1500)")
    p.add_argument("--delay", type=float, default=0.05,
                   help="Polite per-row delay seconds (default 0.05)")
    a = p.parse_args()
    if a.batch_file:
        if not os.path.exists(a.batch_file):
            print(f"❌ Batch file '{a.batch_file}' not found.")
            sys.exit(1)
        labels = _read_inputs(a.batch_file)
        print("\n" + "=" * 60)
        print("  SIGNALPOST AGENT — BATCH MODE (canonical official run)")
        print("=" * 60)
        batch_mode(labels, a.output, a.limit, a.ai, a.budget, a.delay,
                   command=f"python main.py --batch-file {a.batch_file}")
    elif a.companies:
        print("\n" + "=" * 60)
        print("  SIGNALPOST AGENT — BATCH MODE (--companies)")
        print("=" * 60)
        batch_mode([c for c in a.companies if c.strip()], a.output, a.limit, a.ai,
                   a.budget, a.delay, command="python main.py --companies ...")
    else:
        interactive_mode()


if __name__ == "__main__":
    main()