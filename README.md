# 🏢 Signalpost Intelligence

**Norwegian company research agent** — official Brreg.no registry + OpenRouter AI
reasoning + Tavily live web search, served through a judge-friendly CLI *and* a
polished Streamlit chat UI.

Built by **Yatharth Rastogi** for the
[builderr.ai Signalpost challenge](https://unstop.com/hackathons/signalpost-build-an-agent-that-finds-company-information-builderrai-1754883).

> **In plain English:** type a Norwegian company name or organisation number — exact,
> partial, typo'd (`eliqunor asa`), or a VAT-style string (`NO923609016MVA`) — and the
> agent tells you what the company *is*: legal form, industry, registered address,
> registration date and headcount, pulled live and verifiably from the official
> Brreg.no Enhetsregisteret, with the raw source JSON archived per company. Ask a
> follow-up in the chat UI and it adds **cited web research** (news, hiring, activity).
> It will **never invent a fact**: anything it can't confirm comes back as
> `not_available` and is listed under `unknowns`, and an unsure name match comes back
> as `ambiguous` with the closest real candidates instead of a guessed company.

---

## ✨ What it does

- ✅ **Verified registry facts** — name, org form, NACE industry, structured business +
  postal addresses, registration date, foundation date, employee count — straight from
  the official Brreg.no Enhetsregisteret.
- 🔗 **Source links + retrieval dates** on *every* fact, plus a content hash per company.
- 🤖 **AI explanations** (2–3 sentence business summaries) via free OpenRouter models,
  with chain-of-thought leakage and error dumps rejected on both write and read.
- 🌐 **Live web research with citations** for follow-up questions
  ("latest news about Equinor?"), including brief / deep modes and company-vs-topic
  disambiguation ("tell me about python" answers the topic, not a `PYTHON AS` card).
- 🧾 **Evidence chain** — every live lookup stores the full raw registry JSON under
  `evidence/<org>/` (`current.json` + `meta.json` + `history/`), so a refresh that
  returns the *same* snapshot writes no duplicate and no false "change", and a refresh
  that returns a *different* snapshot archives the old one and emits a real `changes[]`
  diff (previous-snapshot → material-change).
- 📄 **PDF report builder** — save companies across sessions, download unified briefs
  (unicode-safe: "Aksjeselskap", "22 October 2022", Norwegian æøå render correctly).
- 🛡️ **Honest degradation, six states** — see the output contract below. A missing
  company is `not_available`, a refused source is `blocked`, an unsure name is
  `ambiguous`, a dead transport is `failed`, a category that doesn't apply to the org
  form is `not_applicable`. **Never a fabricated value, never a silently dropped row.**

---

## 🚀 Quick start

```bash
# 1) Install dependencies (pinned — see Reproducibility below)
pip install -r requirements.txt

# 2) Add keys (optional — the repo ships disposable judge keys, see "For judges" below)
cp .env.example .env        # then edit .env with your own keys; they take priority

# 3) Run — the canonical submission command (a JSONL batch of org numbers,
#    one terminal result envelope per input line, in order):
python main.py --batch-file entry-companies.jsonl

#    It also accepts a plain .txt (one org number / name per line):
python main.py --batch-file test_batch.txt

#    Or a space-separated list (mixed names + numbers + typos + VAT strings):
python main.py --companies 914778271 Telenor "NO923609016MVA" "eliqunor asa"

#    Or interactive single-query (type a name/number, Ctrl+C to exit):
python main.py

# 4) Web UI (chat, history, evidence expanders, PDF reports, mobile-responsive)
streamlit run streamlit_app.py
```

Per run the CLI writes four files: `results.jsonl` (**canonical** envelopes, one per
line), `results.json` (same, pretty-printed for humans), `results.csv` (flat skim:
`status`, `num_facts`, `num_unknowns`, `num_changes`…) and `run_report.json`
(machine-readable: runtime, request count, six-state rollup, `row_parity_ok`, models,
third-party cost).

---

## 📦 Output contract (six states, one envelope per input)

Every input line yields **exactly one** terminal result envelope, in order, never
dropped — duplicate org numbers memoize the *fetch* but still emit their own row, so a
batch of *N* inputs always returns *N* results (a missing row would make a run
unscored). Each envelope carries one of the six states the challenge rules define:

| state | meaning |
|---|---|
| `available` | found it — facts + per-fact `source_url` / `retrieved_date` / `reporting_period` |
| `not_available` | looked, nothing there (real 404, or no name match) |
| `blocked` | source refused the request (HTTP 403 / 429) |
| `not_applicable` | category doesn't apply to this org form (e.g. a foreign branch → no Norwegian accounts) |
| `ambiguous` | couldn't confirm a single company; closest registered names returned, **none published** (a wrong-company match blocks qualification, so we'd rather miss than mis-attribute) |
| `failed` | transport died after retries / wall-clock budget / circuit-breaker — row still emitted so the run is never silently short |

Each envelope also carries, per information category (`company_details`, `locations`,
`financial_results`, `people`, `jobs`, `public_activity`): a `status`, a `facts[]` list
(where every fact has `field` / `value` / `source_url` / `retrieved_date` /
`reporting_period`), and a `note` when a category is empty. Missing values are `null`
and listed in `unknowns[]` — **never replaced with `0`**.

---

## 🧪 100-company smoke test (committed evidence)

The challenge asks for a 100-company smoke-test result or run report. It is committed
in this repo:

- inputs — `smoke100.txt` (plain) and `smoke100.jsonl` (canonical JSONL)
- outputs — `smoke-results.jsonl`, `smoke-results.json`, `smoke-results.csv`,
  `smoke-run-report.json`

Result: **100 / 100 rows, `row_parity_ok: true`, ~41.5 s**, template-only batch (no
per-row LLM, to honour the run's fixed time/resource budget — a timed-out run scores
zero). The mix is 100% `available` *because the inputs are 100 real, currently-registered
entities* (seeded from the committed cache's first 100, which legitimately include
punctuation-prefixed Norwegian names like `& OLSEN HOLDING AS`, `>GLADBIA LARS LARSEN`,
`(U)SERIØST`, `¡ HOLY TOLEDO !` — an incidental but real stress-test of the whole
pipeline against odd registry characters). The other five states are demonstrated
elsewhere: the 3-row live check (`999999999 → not_available` on a real 404) and the
name-resolution unit probes (`eliqunor → ambiguous`). `test_batch.txt` is kept as a
**live feature demo** (typos, MVA normalisation, ambiguous names) — run it to watch
those paths, but the committed parity proof is the 100-row set above.

Beyond identity, the smoke shows the agent capturing **extra checked facts on the same
fetch**: NACE industry in Norwegian (`Produksjon av aluminium`, `Utvinning av råolje`),
`foundation_date` (e.g. Hydro 1905-12-02, Equinor 1972-09-18), and full structured
addresses (kommune, kommunenummer, postnummer, landkode) — more facts per company
without extra requests.

---

## 🗂 Evidence & idempotent refresh

Each live lookup archives the raw registry response under `evidence/<org>/`:
`current.json` + `meta.json` (with `content_sha256`, latency, attempts, capture time) +
`history/*.json`. A refresh that returns the **same** snapshot writes **no** new history
file and reports `evidence_changed: false` (no duplicate records, no false changes). A
refresh that returns a **different** snapshot archives the old one to `history/` and
emits a real `changes[]` list (field / old / new). So two versions of one company diff
cleanly — the "keep profiles current, show what changed, preserve the earlier evidence"
requirement — and reruns stay idempotent.

---

## 🌐 Models / APIs / licences / source-rights

- **Registry (primary, authoritative):** Brreg.no *Enhetsregisteret* REST — public
  Norwegian government data, keyless. Facts carry `source_url` + `retrieved_date`; raw
  JSON preserved as evidence. © Brønnøysund Register Centre.
- **Reasoning:** OpenRouter **free tier** — merged chain with winner-memory promotion,
  multi-key failover, per-hop timeouts, 429/timeout cooldowns, and safe empty-`choices`
  parsing (no crash on throttled/moderated responses):
  - smart: `nvidia/nemotron-3-super-120b-a12b:free`, `meta-llama/llama-3.3-70b-instruct:free`,
    `nvidia/nemotron-3-ultra-550b-a55b:free`, `qwen/qwen-2.5-72b-instruct:free`,
    `google/gemma-2-9b-it:free`
  - fast: `nvidia/nemotron-3.5-lightning:free`, `google/gemma-2-9b-it:free`,
    `meta-llama/llama-3.3-70b-instruct:free`
- **Web search (chat only):** Tavily (`basic` = 5 results, `advanced` = 10 for deep mode).
- **Licences / source-rights:** repository MIT (see `LICENSE`). Registry data is public
  government source. Models used per provider ToS on the free tier. **We do not scrape
  restricted platforms** (LinkedIn / Meta / Indeed) — consistent with the challenge's
  source rule; `people` / `jobs` therefore come only from registry-listed or openly
  published sources, else `not_available`.

### Expected run cost

**$0.00 per 100-company run** on free tiers (free-tier OpenRouter + keyless Brreg; the
batch path makes **0** Tavily calls). Declared ceiling **≤ $10 / day**. Roughly 100–300
registry requests and ~4–25 min per 100 companies (provider limits: 2,000 requests /
45 min). The official batch is template-only by default, so LLM spend is bounded by the
optional `--ai` flag, which is **off** unless explicitly enabled.

---

## 🧭 Declared scope / breadth boundary (honest)

The challenge asks five things of a company: *what it does, who runs it, where it
operates, how the latest filing looks, whether it is hiring.* This v1 owns the first and
third **authoritatively** from the registry (company details + structured addresses) and
answers the others through **cited web research in the chat UI**. In the batch/CLI path,
`people`, `financial_results`, `jobs`, `public_activity` and `additional_workplaces` are
returned as `not_available` and listed in each envelope's `unknowns[]`, because their
verified connectors (Brreg `roller`/officers, `regnskapsregisteret` annual accounts,
`eierstruktur` group links, `arbeidssteder` workplaces, website/jobs crawl) were **not**
wired under the time box — a half-integrated connector that 404s, hangs, or mis-joins on
the official run is a *gate* failure (unscored), far worse than a low coverage score. The
official starter kit's `scripts/` (`run_annual_report_workforce_connector`,
`discover_linkedin_company_profiles`, `extract_company_site_news`, `run_refresh_replay`)
are the named **post-contest** path for that breadth. Per the rules: *better to miss some
information than publish it under the wrong company* — so we miss, loudly, with a source
note, rather than guess.

---

## 🛡️ Resilience (what survives a judge's clone + a flaky network)

Multi-key OpenRouter failover with account-cap hop + 6 h quiet period · per-hop request
timeouts + 429/timeout cooldowns + winner memory · safe content extraction (no
`IndexError` on empty `choices[]`) · four-layer name safety (rank gate → coverage guard →
difflib typo-rescue → LLM spellcheck re-verified; uncertain → `ambiguous`, never a guess) ·
org-number sanitiser (VAT `NO…MVA` / spaced / prefixed inputs → clean 9-digit) · 4xx
never retried (no retry penalty) · `requests.Session` connection pooling · permanent
three-point cache self-heal (startup + write-guard + read-upgrade; **no manual migration
script needed**) · batch wall-clock budget + consecutive-failure circuit breaker so the
run **always** returns one row per input · graceful Basic-mode degradation to registry
templates when keys die · unicode-safe PDF export · mobile-responsive Streamlit UI with
hover kebab menus, modal confirmations and a centred empty state.

---

## 🔁 Reproducibility (clean-machine check)

Tested on **Python 3.12–3.13**. `requirements.txt` is auto-pinned to exact versions
(`pin_deps.py` regenerates it from a proven environment, so the clean-machine install
resolves to the same versions that pass the smoke). To verify the way the harness does:

```bash
git clone https://github.com/Yatharth-Rastogi/Signalpost-Agent.git
cd Signalpost-Agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py --batch-file smoke100.jsonl --output smoke-results.json
# expect: row_parity_ok true, 100 rows, six-state rollup in run_report.json
```

---

## 🔑 For judges: clone-and-run

This repo ships with **disposable free-tier API keys** (`judge_keys.py`) so the full
agent runs with zero setup. They are *not* tied to a personal paid account; if you prefer
your own, drop them in `.env` (see `.env.example`) and they take priority. **Post-contest
the owner revokes these keys** — see below.

```bash
git clone https://github.com/Yatharth-Rastogi/Signalpost-Agent.git
cd Signalpost-Agent
pip install -r requirements.txt
python main.py --batch-file test_batch.txt        # live feature demo (typos, MVA, ambiguity)
# or the canonical 100-company parity proof:
python main.py --batch-file smoke100.jsonl --output smoke-results.json
```

### Keys & cost / post-contest

`judge_keys.py` holds disposable free-tier keys for zero-setup judging; personal `.env`
keys always take priority. **After results are announced, the owner revokes the
OpenRouter and Tavily judge keys and closes the GitHub secret alert.** No paid spend is
incurred by any run.

---

## 📁 Repository layout

```
main.py              CLI entry point (batch / --companies / interactive)
agent.py             core engine: registry fetch, name resolution, envelopes, evidence, self-heal
llm.py               OpenRouter multi-key chain, safe parsing, CoT rejection
search.py            Tavily web search with depth control + judge-key fallback
intent.py            fast-model intent routing (lookup / followup / save / report / refresh / chitchat)
memory.py            thread + report-pool persistence (SQLite)
streamlit_app.py     chat UI (history, evidence, PDF builder, mobile-responsive)
judge_keys.py        disposable free-tier keys for clone-and-run
.env.example         template for your own keys (never commit a real .env)
company_profiles.json  1,000+ cached profiles (demo/clone-and-run convenience; NOT used for ranking)
evidence/<org>/      raw registry JSON per company (current + meta + history)
smoke100.* / smoke-results.* / smoke-run-report.json   committed 100-company smoke evidence
test_batch.txt       live feature demo (mixed names, typos, VAT strings, wrong-length numbers)
pin_deps.py          one-shot generator that pins requirements.txt to a proven env
```

---

## 📬 Contact

Yatharth Rastogi — **[YathartRastogi99@gmail.com]**  <!-- REPLACE this bracketed line with your real email before submitting -->

## 📜 License

MIT — see `LICENSE`. Registry data © Brønnøysund Register Centre (Brreg.no).