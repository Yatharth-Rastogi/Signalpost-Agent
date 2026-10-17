"""
main.py — Signalpost CLI Agent (official hackathon entry point)
Modes:
  --batch-file PATH   (txt one-per-line OR .jsonl universe batch)
  --companies A B C   (space-separated on the command line)
  (no arguments)      (interactive: one query at a time, Ctrl+C to exit)
Emits results.json + results.csv + results.jsonl (one envelope per input)
+ run_report.json (machine-readable runtime/request/cost report).
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
from agent import lookup_company, initialize_batch_cache, ai_status, search_by_name

REGISTRY_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{}"
FIELDNAMES = ["company_name", "org_number", "business_type", "industry", "address",
              "registration_date", "employees", "retrieved_date",
              "ai_explanation", "source_url", "status"]

def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _missing_entry(label, org, status):
    if status == "fetch_error":
        expl = (f"The Brreg.no registry could not be reached for organization number {org} "
                f"during this run (transient network error); no facts are published to avoid "
                f"fabrication. Re-run the batch to retry this organization.")
    elif org:
        expl = (f"No active entity is registered under organization number {org} in the "
                f"Norwegian Enhetsregisteret (Brreg.no) as of {_today()}; the registry "
                f"returned no record for it.")
    elif label.isdigit() and len(label) != 9:
        expl = (f"“{label}” looks like an organization number but has {len(label)} digits; "
                f"Norwegian organization numbers are exactly 9 digits, and no registry entity "
                f"matches it as a name either (checked {_today()}).")
    else:
        expl = (f"No active entity matching the name “{label}” was found in the Norwegian "
                f"Enhetsregisteret (Brreg.no) name search as of {_today()}.")
    sugg = getattr(agent, "LAST_SUGGESTIONS", []) or []
    if sugg and status == "not_found":
        expl += " Closest registered names: " + "; ".join(f"{n} ({o})" for n, o in sugg) + "."
    if org:
        source_url = REGISTRY_URL.format(org)
    else:
        encoded_query = urllib.parse.quote(str(label or "").strip())
        source_url = f"https://data.brreg.no/enhetsregisteret/api/enheter?navn={encoded_query}"
    return {
        "company_name": None if org else label,
        "org_number": org,
        "business_type": None,
        "industry": None,
        "address": None,
        "registration_date": None,
        "employees": None,
        "source_url": source_url,
        "retrieved_date": _today(),
        "ai_explanation": expl,
        "status": status,
    }

def _save_results(results, json_file):
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    csv_file = json_file.replace('.json', '.csv')
    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
        writer.writeheader()
        for row in results:
            flat = {}
            for k in FIELDNAMES:
                v = row.get(k)
                if v is None:
                    flat[k] = ""
                elif isinstance(v, (dict, list)):
                    flat[k] = str(v)
                else:
                    flat[k] = v
            writer.writerow(flat)
    # JSONL: exactly one terminal envelope per input record
    jsonl_file = json_file.replace('.json', '.jsonl')
    with open(jsonl_file, 'w', encoding='utf-8') as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return csv_file, jsonl_file

def _write_run_report(results, start, items_in, command):
    ok_n = sum(1 for r in results if r.get("status") == "ok")
    nf_n = sum(1 for r in results if r.get("status") == "not_found")
    fe_n = sum(1 for r in results if r.get("status") == "fetch_error")
    report = {
        "run_id": time.strftime("signalpost-%Y%m%d-%H%M%S"),
        "command": command,
        "items_in": items_in,
        "items_recorded": len(results),
        "ok": ok_n, "not_found": nf_n, "fetch_error": fe_n,
        "runtime_seconds": round(time.time() - start, 1),
        "registry_requests": ok_n + fe_n,
        "web_search_calls": 0,
        "models": {"smart": (llm.MODEL_CHAIN[0] if getattr(llm, "MODEL_CHAIN", None) else "none"),
                   "fast": (llm.FAST_CHAIN[0] if getattr(llm, "FAST_CHAIN", None) else "none")},
        "third_party_cost_usd": 0.0,
        "cost_basis": "free-tier OpenRouter + keyless Brreg; declared ceiling <= $10/day",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open("run_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report

def _print_summary(results, elapsed):
    ok_n = sum(1 for r in results if r.get("status") == "ok")
    nf_n = sum(1 for r in results if r.get("status") == "not_found")
    fe_n = sum(1 for r in results if r.get("status") == "fetch_error")
    print("\n" + "=" * 60)
    print(f"  ✅ COMPLETE in {elapsed:.1f}s — {len(results)} items recorded")
    print(f"     ok: {ok_n} · not_found: {nf_n} · fetch_error: {fe_n}")
    print("=" * 60 + "\n")

def _resolve_item(item):
    """Resolve one input line into {label, org}. org is None if no match."""
    sanitized = agent.sanitize_org_number(item)
    if sanitized:
        return {"label": item, "org": sanitized}
    print(f"[NAME SEARCH] Resolving “{item}”...")
    org = search_by_name(item)
    if org:
        print(f"[NAME MATCH] “{item}” -> org {org}")
    else:
        print(f"[NAME MISS] No registry match for “{item}”")
    return {"label": item, "org": org}

def _batch_header():
    print("\n" + "=" * 60)
    print("  SIGNALPOST AGENT — BATCH PROCESSING MODE")
    print("=" * 60 + "\n")
    print("[1/3] Initializing local cache (1,000 companies) + self-heal...")
    initialize_batch_cache(1000)

def batch_mode(raw_lines, output="results.json", limit=0, command="python main.py --batch-file <file>"):
    if limit > 0:
        raw_lines = raw_lines[:limit]
    if not raw_lines:
        print("❌ Error: No input items found to process.")
        sys.exit(1)

    tasks, seen = [], set()
    for item in raw_lines:
        t = _resolve_item(item)
        if t["org"]:
            if t["org"] in seen:
                print(f"[DUPLICATE] Skipping repeated org {t['org']}")
                continue
            seen.add(t["org"])
        tasks.append(t)

    print(f"\n[2/3] Processing {len(tasks)} items...")
    _, desc = ai_status()
    print(f"      AI Status: {desc}\n")

    results = []
    start = time.time()
    for i, t in enumerate(tasks, 1):
        label, org = t["label"], t["org"]
        if not org:
            results.append(_missing_entry(label, None, "not_found"))
            print(f"  [{i}/{len(tasks)}] {label}... ⛔ no registry match for name")
            continue
        print(f"  [{i}/{len(tasks)}] Fetching {label} ({org})...", end=" ", flush=True)
        profile = lookup_company(org, force_refresh=True)
        status = agent.LAST_LOOKUP_STATUS
        if profile:
            entry = dict(profile); entry["status"] = "ok"
            results.append(entry)
            print(f"✅ {profile.get('company_name', 'Unknown')}")
        else:
            results.append(_missing_entry(label, org, status))
            print("⛔ not found in registry" if status == "not_found"
                  else "⚠️ registry unreachable (recorded as fetch_error)")
        time.sleep(0.6)

    print(f"\n[3/3] Saving results...")
    csv_file, jsonl_file = _save_results(results, output)
    _print_summary(results, time.time() - start)
    _write_run_report(results, start, len(raw_lines), command)
    print(f"📄 JSON Output:   {os.path.abspath(output)}")
    print(f"📊 CSV Output:    {os.path.abspath(csv_file)}")
    print(f"📦 JSONL Envelopes: {os.path.abspath(jsonl_file)}")
    print(f"📑 Run Report:    {os.path.abspath('run_report.json')}\n")
    print("💡 WANT THE INTERACTIVE WEB UI?")
    print("   👉 streamlit run streamlit_app.py\n")

def interactive_mode():
    print("\n" + "=" * 60)
    print("  SIGNALPOST AGENT — INTERACTIVE MODE")
    print("=" * 60)
    print("[INIT] Initializing local cache (1,000 companies) + self-heal...")
    initialize_batch_cache(1000)
    _, desc = ai_status()
    print(f"  AI Status: {desc}")
    print("  Type a company name or 9-digit org number to look up.")
    print("  Press Ctrl+C to exit.\n")

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
            t = _resolve_item(raw)
            label, org = t["label"], t["org"]
            if not org:
                entry = _missing_entry(label, None, "not_found")
                results.append(entry)
                print(f"  ⛔ no registry match for name")
                print(f"  AI: {entry['ai_explanation']}\n")
                continue
            print(f"[LIVE FETCH] Querying {org}...")
            profile = lookup_company(org, force_refresh=True)
            status = agent.LAST_LOOKUP_STATUS
            if profile:
                entry = dict(profile); entry["status"] = "ok"
                results.append(entry)
                print(f"\n  ✅ {profile.get('company_name')}")
                print(f"     Type:        {profile.get('business_type')}")
                print(f"     Industry:    {profile.get('industry', 'N/A')}")
                print(f"     Address:     {profile.get('address')}")
                print(f"     Registered:  {profile.get('registration_date')}")
                print(f"     Employees:   {profile.get('employees') or 'N/A'}")
                print(f"     AI:          {profile.get('ai_explanation', '')}")
                print(f"     Source:      {profile.get('source_url')}\n")
            else:
                entry = _missing_entry(label, org, status)
                results.append(entry)
                print(f"  ⛔ {'not found' if status == 'not_found' else 'registry unreachable'}")
                print(f"  AI: {entry['ai_explanation']}\n")
    except KeyboardInterrupt:
        print("\n[EXIT] Interrupted by user.")

    if results:
        out = "results.json"
        csv_file, jsonl_file = _save_results(results, out)
        _print_summary(results, time.time() - start)
        _write_run_report(results, start, len(results), "python main.py (interactive)")
        print(f"📄 JSON Output:   {os.path.abspath(out)}")
        print(f"📊 CSV Output:    {os.path.abspath(csv_file)}")
        print(f"📦 JSONL Envelopes: {os.path.abspath(jsonl_file)}")
        print(f"📑 Run Report:    {os.path.abspath('run_report.json')}\n")

def _read_batch_file(path):
    """Support both plain txt (one per line) and official .jsonl universe batches."""
    if path.endswith(".jsonl"):
        lines = []
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                    val = obj.get("organisasjonsnummer") or obj.get("org_number") or obj.get("org")
                    lines.append(str(val) if val else ln)
                except Exception:
                    lines.append(ln)
        return [x for x in lines if x]
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

def main():
    parser = argparse.ArgumentParser(
        description="Signalpost: Norwegian Company Intelligence Agent",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--batch-file', type=str,
                        help='Path to a .txt (one per line) or .jsonl batch of org numbers/names')
    parser.add_argument('--companies', nargs='+',
                        help='Space-separated list of org numbers and/or company names')
    parser.add_argument('--output', type=str, default='results.json',
                        help='Output filename for JSON (default: results.json)')
    parser.add_argument('--limit', type=int, default=0,
                        help='Max number of items to process (0 for all)')
    args = parser.parse_args()

    if args.batch_file:
        if not os.path.exists(args.batch_file):
            print(f"❌ Error: Batch file '{args.batch_file}' not found.")
            sys.exit(1)
        raw_lines = _read_batch_file(args.batch_file)
        _batch_header()
        batch_mode(raw_lines, args.output, args.limit,
                   command=f"python main.py --batch-file {args.batch_file}")
    elif args.companies:
        _batch_header()
        batch_mode([c for c in args.companies if c.strip()], args.output, args.limit,
                   command="python main.py --companies ...")
    else:
        interactive_mode()

if __name__ == "__main__":
    main()