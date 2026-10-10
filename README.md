Built by <Yatharth Rastogi> for the Signalpost challenge (builderr.ai).

# 🏢 Signalpost Intelligence

**Norwegian company research agent** — official Brreg.no registry + OpenRouter AI reasoning + Tavily live web search, served through a judge-friendly CLI *and* a polished Streamlit chat UI.

Built for the [builderr.ai Signalpost challenge](https://unstop.com/hackathons/signalpost-build-an-agent-that-finds-company-information-builderrai-1754883).

---

## ✨ What it does

Give it a Norwegian **organisation number or company name** — exact, partial, typo'd ("eliqunor asa"), or garbled — and it returns:

- ✅ **Verified registry facts** (name, org form, address, registration date, employee count) straight from the official Brreg.no Enhetsregisteret
- 🔗 **Source links + retrieval dates** for every fact
- 🤖 **AI explanations** (2–3 sentence business summaries) via free OpenRouter models
- 🌐 **Live web research** with citations for follow-up questions ("latest news about Equinor?")
- 🧾 **Evidence chain**: every live lookup stores the full raw registry JSON in `evidence/<org>.json`
- 📄 **PDF report builder**: save companies across sessions, download unified briefs
- 🛡️ **Honest degradation**: missing company → explicit `not_found`; unreachable registry → retried then `fetch_error`; quota dead → registry-template summaries. **Never a fabricated value.**

---

## 🚀 Quick start

```bash
# 1) Install dependencies
pip install -r requirements.txt

# 2) Add keys (optional but recommended — see "Keys" below)
cp .env.example .env        # then edit .env with your keys

# 3) Run — three modes:

#    a) Interactive single-query (type a name/number, Ctrl+C to exit)
python main.py

#    b) Batch from file (mixed names + org numbers, one per line)
python main.py --batch-file test_batch.txt

#    c) Batch from command line
python main.py --companies 914778271 Telenor "DNB BANK ASA"

# 4) Web UI (chat, history, reports, evidence)
streamlit run streamlit_app.py

## 🔑 For judges: clone-and-run

This repo ships with **disposable free-tier API keys** (`judge_keys.py`) so you can test the full agent immediately:

```bash
git clone https://github.com/Yatharth-Rastogi/Signalpost-Agent.git
cd Signalpost-Agent
pip install -r requirements.txt
python main.py --batch-file test_batch.txt