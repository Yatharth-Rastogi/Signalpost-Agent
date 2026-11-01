# 🏢 Signalpost Intelligence

Norwegian company research agent — live Brreg.no registry (Enhetsregisteret + Roller +
Regnskapsregisteret) + OpenRouter reasoning + Tavily web search, wrapped in a
parity-guaranteed batch harness and a mobile-responsive Streamlit UI.

Built for the **Signalpost challenge (builderr.ai)**. Every published fact carries its
source URL, retrieval date, reporting period (for financials) and a local evidence
snapshot reference. Unavailable information is reported explicitly — never guessed,
never padded with zeros.

## ✨ What it does

- 🔎 **Verify any Norwegian company** by org number or name — typos, partial names,
  VAT strings (`NO923609016MVA`) and spacing variants included — against the official
  Enhetsregisteret, behind a four-layer name-safety stack (rank gate → coverage guard →
  difflib rescue → LLM spellcheck, re-verified against the registry). Uncertain matches
  return `ambiguous` with suggestions instead of publishing a guessed company.
- 💰 **Live financials** — filed annual accounts from the official **Regnskapsregisteret**:
  revenue, net profit, equity, total debt and total assets, each stamped with its
  accounting period and accounts type (KONSERN preferred over SELSKAP). Facts carry exact
  numeric values plus human-scaled display strings (`106.46B USD`); org forms that file no
  Norwegian accounts are honestly `not_applicable`.
- 👥 **Live officers & board** — active roles from the Brreg **Roller** dataset
  (daglig leder, styrets leder, styremedlemmer, revisor, regnskapsfører…); departed
  (avregistrert) roles excluded. For CEO/board questions the registry overrides web
  findings, and stale web sources are called out as outdated.
- 📇 **Nine-tile company card** in the UI (type · registration · address · employees ·
  contact · website · revenue · total assets · total debt) with hover/tap tooltips showing
  the exact unrounded figures behind every scaled number.
- 🤖 **AI research chat** — follow-up questions answered by fusing live, cited Tavily web
  search with verified registry data; brief/deep modes; company-vs-topic disambiguation
  ("tell me about python" answers the topic, not a `PYTHON AS` card); thread history,
  pinning and archives persisted in SQLite.
- 🧾 **Evidence chain** — every live lookup stores the raw registry JSON under
  `evidence/<org>/` (`current.json` + `meta.json` + `history/`, plus `roles.json` and
  `regnskap.json` sidecars). Idempotent refresh: an identical snapshot writes no duplicate
  and no false "change"; a different snapshot archives the old one and emits a real
  `changes[]` diff.
- 📄 **PDF report builder** — save companies across sessions and download unified,
  unicode-safe briefs from the sidebar.
- 🛡 **Honest six-state degradation** — `available / not_available / blocked /
  not_applicable / ambiguous / failed`, one terminal envelope per input, always.

## 🚀 Quick start

```bash
# 1) Install dependencies (pinned — see Reproducibility below)
pip install -r requirements.txt

# 2) Add keys (optional — the repo ships disposable judge keys, see "For judges" below)
copy .env.example .env        # Windows; then paste OPENROUTER_API_KEY / TAVILY_API_KEY

# 3) Run — the canonical submission command (a JSONL batch of org numbers,
#    one terminal result envelope per input line, in order):
python main.py --batch-file entry-companies.jsonl --budget 1500 --output entry-1000-envelopes.json

#    It also accepts a plain .txt (one org number / name per line):
python main.py --batch-file smoke100.txt --output smoke-results.json

#    Or a space-separated list (mixed names + numbers + typos + VAT strings):
python main.py --companies 914778271 "Equinor" NO923609016MVA eliqunor

#    Or interactive single-query (type a name/number, Ctrl+C to exit):
python main.py --interactive

# 4) Web UI (chat, history, evidence expanders, PDF reports, mobile-responsive)
streamlit run streamlit_app.py
```

## 📦 Output contract (six states, one envelope per input)

Every input line yields exactly one terminal envelope; row parity is asserted and reported
(`row_parity_ok` in `run_report.json`). Top-level `status` ∈:

| State | Meaning |
|---|---|
| `available` | Entity found; verified facts published |
| `not_available` | Real 404, no confident name match, or category genuinely empty |
| `not_applicable` | Question does not apply (e.g. NUF files no Norwegian accounts) |
| `ambiguous` | Real candidates existed but none passed confidence gates — none published |
| `blocked` | Registry refused the request (HTTP 403 / 429 after backoff) |
| `failed` | Transport death after retries, or budget / breaker short-circuit |

Each envelope carries identity fields; a `categories{}` map over the rubric info-types
(`company_details`, `contact`, `locations`, `financial_results`, `people`, `jobs`,
`public_activity`) whose facts include `field / value / source_url / retrieved_date /
reporting_period / evidence_ref` (plus `display` for scaled financials); `unknowns[]`
naming exactly what could not be established; `changes[]` and evidence SHA-256 hashes;
`request_count`; `latency_ms`; and an `ai_explanation` (LLM synthesis when keys allow,
deterministic registry template otherwise — batch runs default to the template to honour
fixed time/cost budgets). Missing values are `null`, never `0`.

## 🧪 Committed run evidence

- **Inputs** — `smoke100.txt` / `smoke100.jsonl` (100-company parity proof),
  `entry-companies.jsonl` (1,000-company stress set), `test_batch.txt` (live feature demo:
  typos, MVA normalisation, ambiguous names, odd-character registry names).
- **100-company smoke (Rev-2 code, 2026-10-31)** — `smoke-results.jsonl/.json/.csv` +
  `smoke-run-report.json`: 100/100 rows, parity True, 100 available, 300 registry requests
  (3 per row: enheter + roller + regnskap), 97.4 s.
- **1,000-company stress (Rev-2 code, 2026-10-31)** — `entry-1000-envelopes.jsonl/.csv` +
  `entry-1000-run-report.json`: 1000/1000 rows, parity True, 1000 available, 1058.9 s
  inside the default 1500 s budget with the pace governor armed but never latched.
- **Circuit-breaker proof** — a 10-row run of unreachable org numbers: 8 consecutive
  transport failures trip the breaker; rows 9–10 are emitted instantly with
  `request_count: 0` and `unknowns: ["not_attempted_budget_or_breaker"]`
  (`registry_requests: 8`, parity True).
- v1-era proofs remain inspectable in git history at commits `ec8a910` (scored Rev 1)
  and `291cf24`.

## 🗂 Evidence & idempotent refresh

Every live lookup writes `evidence/<org>/current.json` (raw registry payload) and
`meta.json` (content SHA-256, latency, attempts, captured-at). On genuine change the
previous snapshot is archived to `history/<ts>.json` and a material `changes[]` diff is
emitted. Roller and Regnskapsregisteret payloads are preserved alongside as `roles.json`
and `regnskap.json` — the targets of per-fact `evidence_ref` for people and financial
facts. `evidence/` is runtime state: gitignored and regenerated by any run.

## 🌐 Models / APIs / licences / source-rights

- **Registry (primary truth):** Brreg.no open data — Enhetsregisteret
  (`/enhetsregisteret/api/enheter`), Roller (`.../enheter/{org}/roller`),
  Regnskapsregisteret (`/regnskapsregisteret/regnskap/{org}`). Keyless; NLOD 2.0;
  © Brønnøysund Register Centre.
- **Reasoning:** OpenRouter, free-tier-first chain (Nemotron-3 Ultra → Inkling-small →
  Nemotron-3.5 Super-VL → `openrouter/free` net → two paid tails for credit-holding keys),
  with multi-key failover, per-model cooldowns, retired-model silencing, chain-of-thought
  stripping and junk/safety-classifier output rejection.
- **Web search:** Tavily, structured results; when synthesis is offline the UI renders
  cleaned, numbered source digests (title + snippet + link) instead of raw markdown.
- **Deliberately not used:** NAV's vacancy feed (bearer-token national firehose, no
  per-org synchronous query) and Brreg Kunngjøringer (no open REST API) — see scope below.

### Expected run cost
Three polite registry GETs per company (~0.3 s each) plus a 0.05 s inter-row delay:
100 companies ≈ 100–120 s; 1,000 companies ≈ 1,050–1,200 s measured, inside the default
1,500 s budget. Batch synthesis is template-only by default, so a full 1,000-row run makes
**zero LLM calls** and costs nothing on free tiers.

## 🧭 Declared scope / breadth boundary (honest)

**Wired and verified in the batch path (Rev 2):** `company_details`, `contact`,
`locations` (Enhetsregisteret); `financial_results` (Regnskapsregisteret — period-stamped,
exact + display values, `not_applicable` for non-filing org forms); `people` (Roller —
active roles only).

**Declared out of scope for the batch path, reported `not_available` with reasons:**
`jobs` — Norway's official vacancy feed cannot be queried per-organisation synchronously
within a fixed budget; `public_activity` — Brreg announcements ship no open REST endpoint
(web portal / subscription XML only). Both remain researchable in the UI chat via cited
web search. Per the challenge rule: *better to miss information than publish it under the
wrong company* — and better to say "not available" than to invent a source.

## 🛡️ Resilience (what survives a judge's clone + a flaky network)

- **Row parity, always:** one envelope per input line; budget overruns and breaker trips
  emit explicit `failed` rows instead of dropping inputs.
- **Adaptive 429 backoff:** exponential (2ⁿ s, capped 8 s) retries on rate limits,
  budget-aware (never sleeps past the deadline); other 4xx are never retried.
- **Circuit breaker:** 8 *consecutive* transport failures (`failed`/`blocked`) short-circuit
  the remaining rows with zero network calls. Honest answers (`available`,
  `not_available`, `ambiguous`) reset the streak — so an input list that *starts* with
  unregistered companies never trips it.
- **Adaptive pace governor:** after row 5, if projected runtime ≥ 85 % of the budget,
  optional endpoints (roller + regnskap) switch off for remaining uncached rows; those rows
  state "not fetched this run" instead of claiming the data is absent.
- **Key resilience:** env keys → bundled disposable judge keys → graceful Basic mode
  (registry templates, zero API calls). Daily-cap detection flips the UI into an honest
  "Cap mode" state instead of pretending AI is live.
- **Cache self-heal:** startup hygiene + write-guard + read-upgrade migrate old cached rows
  (contacts, officers, financials schema) lazily — no manual migration script needed.
- **Contamination guards:** CoT / junk / safety-classifier outputs are rejected at write
  time and healed in cache on next read.

## 🔁 Reproducibility (clean-machine check)

Tested on **Python 3.12–3.14** (primary testing on 3.14). `requirements.txt` is
auto-pinned to exact versions (`pin_deps.py` regenerates it from a proven environment, so
a clean-machine install resolves to the same versions that pass the smoke). To verify the
way the harness does:

```bash
git clone https://github.com/Yatharth-Rastogi/Signalpost-Agent.git
cd Signalpost-Agent
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py --batch-file smoke100.jsonl --output smoke-results.json
# expect: row_parity_ok true, 100 rows, six-state rollup in run_report.json
```

## 🔑 For judges: clone-and-run

This repo ships with **disposable free-tier API keys** (`judge_keys.py`) so the full agent
runs with zero setup. They are *not* tied to a personal paid account; if you prefer your
own, drop them in `.env` (see `.env.example`) and they take priority. **Post-contest the
owner revokes these keys** — see below.

```bash
git clone https://github.com/Yatharth-Rastogi/Signalpost-Agent.git
cd Signalpost-Agent
pip install -r requirements.txt
python main.py --batch-file test_batch.txt        # live feature demo (typos, MVA, ambiguity)
# or the canonical 100-company parity proof:
python main.py --batch-file smoke100.jsonl --output smoke-results.json
streamlit run streamlit_app.py                    # UI: chat, 9-tile cards, tooltips, PDFs
```

### Keys & cost / post-contest
Free-tier OpenRouter + Tavily only; batch synthesis is template-only by default (`--ai`
opts in), so official runs cost zero LLM calls. After the contest: revoke the judge keys,
rotate any personal keys, and clear the GitHub secret-scanning alert raised by the v1
commit.

## 📁 Repository layout

```
agent.py               core engine: fetchers (enheter/roller/regnskap), evidence, envelopes
main.py                CLI: batch harness (budget, governor, breaker, parity), interactive
streamlit_app.py       web UI: chat, threads, 9-tile cards, exact-value tooltips, PDF builder
intent.py / llm.py / search.py / memory.py    router, OpenRouter engine, Tavily, SQLite memory
judge_keys.py          disposable free-tier keys for zero-setup judging
smoke100.* / entry-companies.jsonl / test_batch.txt        inputs
smoke-results.* / smoke-run-report.json                    committed 100-company proof (Rev 2)
entry-1000-envelopes.* / entry-1000-run-report.json        committed 1,000-company proof (Rev 2)
company_profiles.json  enriched demo cache (contacts + officers + financials)
evidence/              runtime evidence snapshots (gitignored, regenerated per run)
pin_deps.py            one-shot generator that pins requirements.txt to a proven env
```

## 📬 Contact
Yatharth Rastogi — **YathartRastogi99@gmail.com**

## 📜 License
MIT — see `LICENSE`. Registry data © Brønnøysund Register Centre (Brreg.no); annual
accounts via the Regnskapsregisteret open API; officers via the Roller open dataset.