# Copyright (c) 2026 <Yatharth Rastogi> — MIT License.

import streamlit as st
import time
import json
import os
import base64
from fpdf import FPDF
from fpdf.enums import XPos, YPos
import agent
import memory
import intent
from agent import (lookup_company, search_by_name, initialize_batch_cache,
                   chat_with_gemini, ai_status, key_source_label, load_cache)

_FAV = os.path.join("assets", "favicon.png")
_LOGO = os.path.join("assets", "logo.png")
_ICON = _FAV if os.path.exists(_FAV) else (_LOGO if os.path.exists(_LOGO) else "🏢")
st.set_page_config(page_title="Signalpost Intelligence", page_icon=_ICON,
                   layout="wide", initial_sidebar_state="expanded")

# --- Logo as base64 so it can render inside HTML (emoji only as fallback) ---
_LOGO_B64 = None
if os.path.exists(_LOGO):
    try:
        with open(_LOGO, "rb") as f:
            _LOGO_B64 = base64.b64encode(f.read()).decode()
    except Exception:
        _LOGO_B64 = None

def logo_img(px=26):
    if _LOGO_B64:
        return (f'<img src="data:image/png;base64,{_LOGO_B64}" '
                f'style="width:{px}px;height:{px}px;vertical-align:middle;'
                f'border-radius:6px;margin-right:8px;">')
    return "🏢 "

CUSTOM_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
#MainMenu, footer {visibility: hidden;}
[data-testid="stCaptionContainer"], .stCaption { color: #9AA3B2 !important; }
/* Hide "Press Enter to apply" hints on every text input (multi-fallback) */
[data-testid="stTextInput"] [data-testid="stInputActionHint"],
[data-testid="stTextInput"] [data-testid*="Hint" i],
[data-testid="stTextInput"] [data-testid*="Action" i],
[data-testid="stTextInput"] div:has(> input) + div { display: none !important; }
.block-container { padding-top: 2.4rem; padding-bottom: 40px; max-width: 1000px; }
.hero { background: linear-gradient(120deg,#1b2a6b 0%,#4338ca 45%,#7c3aed 100%);
        border-radius:16px; padding:16px 24px; color:#fff; margin-top: .25rem;
        box-shadow:0 10px 28px rgba(76,29,149,.35); margin-bottom:10px;}
.hero h1 {margin:0 0 2px 0; font-size:22px; font-weight:800; letter-spacing:-.4px;}
.hero p {margin:0 0 8px 0; opacity:.92; font-size:12.5px;}
.pill {display:inline-block; background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.34);
       border-radius:999px; padding:2px 10px; font-size:11.5px; margin-right:6px;}
.card {background:#161B26; border:1px solid #232A36; border-radius:14px; padding:16px 18px; height:100%;}
.card h3 {margin:0 0 6px 0; font-size:15px; font-weight:700; color:#E7EAF0;}
.card p {margin:0; font-size:13px; color:#9AA3B2; line-height:1.5;}
.grid {display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px; margin:10px 0;}
.tile {background:#161B26; border:1px solid #232A36; border-radius:12px; padding:10px 12px;}
.tile .label {font-size:10.5px; text-transform:uppercase; letter-spacing:.6px; color:#8B93A5; margin-bottom:3px;}
.tile .value {font-size:13.5px; font-weight:600; color:#E7EAF0;}
.orgpill {display:inline-block; font-family:monospace; font-size:12px; background:rgba(124,108,240,.15);
          color:#A5B4FC; border:1px solid rgba(124,108,240,.4); padding:2px 10px; border-radius:999px;}
.ai-box {background:linear-gradient(180deg, rgba(124,108,240,.12), rgba(59,130,246,.08));
         border:1px solid rgba(124,108,240,.35); border-radius:12px; padding:12px 14px; margin:10px 0 4px 0;}
.ai-box h4 {margin:0 0 4px 0; font-size:12px; font-weight:700; color:#C4B5FD;
            text-transform:uppercase; letter-spacing:.6px;}
.ai-box p {margin:0; font-size:13.5px; color:#DDE1EA; line-height:1.5;}
.sidefoot {color:#9AA3B2; font-size:11px; text-align:center; margin-top:22px; line-height:1.5;}
/* Qwen-style: pinned chat input lifted; disclaimer hangs beneath it via ::after */
div[data-testid="stChatInput"] { bottom: 30px !important; }
div[data-testid="stChatInput"]::after {
    content: "Registry facts are verified live against Brreg.no · AI summaries & web findings may contain errors.";
    position: absolute; left: 0; right: 0; bottom: -20px; text-align: center;
    color: #9AA3B2; font-size: 11px; letter-spacing: .2px; pointer-events: none; }
/* Kebab dots: invisible until the row is hovered (Gemini-style) */
[data-testid="stSidebar"] button { min-height: 2.4rem; }
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] [data-testid="stPopover"] {
    opacity: 0; transition: opacity .15s ease; }
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"]:hover [data-testid="stPopover"],
[data-testid="stSidebar"] [data-testid="stPopover"]:focus-within,
[data-testid="stSidebar"] [data-testid="stPopover"] button[aria-expanded="true"] { opacity: 1; }
@media (pointer: coarse) {
    [data-testid="stSidebar"] [data-testid="stHorizontalBlock"] [data-testid="stPopover"] { opacity: .6; }
}
[data-testid="stSidebar"] [data-testid="stPopover"] button {
    background: transparent !important; border: none !important; box-shadow: none !important;
    width: 1.9rem; min-height: 0 !important; height: 2.0rem; padding: 0.15rem 0.35rem; }
[data-testid="stSidebar"] [data-testid="stPopover"] button:hover { background: transparent !important; }
[data-testid="stSidebar"] [data-testid="stPopover"] button svg,
[data-testid="stSidebar"] [data-testid="stPopover"] button [data-testid^="stIcon"],
[data-testid="stSidebar"] [data-testid="stPopover"] button p { display: none !important; }
[data-testid="stSidebar"] [data-testid="stPopover"] button::before {
    content: "•\A•\A•"; white-space: pre; line-height: 0.72em; display: block;
    text-align: center; font-size: 15px; font-weight: 700; color: #C4C9D4; }
[data-testid="stSidebar"] [data-testid="stPopover"] button:hover::before,
[data-testid="stSidebar"] [data-testid="stPopover"] button[aria-expanded="true"]::before { color: #FFFFFF; }
/* Compact menu items so panels never scroll */
[data-testid="stSidebar"] [data-testid="stPopover"] [data-testid="stVerticalBlock"] button {
    min-height: 1.9rem; padding: 0.25rem 0.5rem; font-size: 0.8rem; }
@media (max-width: 700px) {
    .hero { padding: 12px 16px; }
    .hero h1 { font-size: 18px; }
    .grid { grid-template-columns: 1fr 1fr; }
    .block-container { padding-top: 1.6rem; }
}
"""
st.markdown(f"<style>{CUSTOM_CSS}</style>", unsafe_allow_html=True)

AGENT_INTRO = ("👋 I'm the **Signalpost research agent**. I look up any Norwegian company by name or "
               "org number, answer follow-up questions with **live web search + citations**, save companies "
               "into PDF reports, and refresh data straight from the official registry.\n\n"
               "Try: `Telenor` · `914778271` · `latest projects of Equinor?` · `save this company`")

# --- INIT ---
if "cache_initialized" not in st.session_state:
    with st.spinner("⏳ Initializing agent cache (1,000 companies from Brreg.no)..."):
        initialize_batch_cache(1000)
    st.session_state.cache_initialized = True
for _k, _v in [("active_thread", None), ("dlg", None), ("menu_epoch", 0)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v
N_CACHE = len(load_cache())

# --- PDF (unicode-safe text cleaning) ---
_UNICODE_FIX = {
    "\u00a0": " ", "\u2000": " ", "\u2001": " ", "\u2002": " ", "\u2003": " ",
    "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ", "\u3000": " ",
    "\u2013": "-", "\u2014": "-", "\u2011": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00d7": "x",
}

def clean_for_pdf(text):
    if not text:
        return "N/A"
    text = str(text).replace("**", "").replace("*", "")
    for bad, good in _UNICODE_FIX.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "ignore").decode("latin-1").strip() or "N/A"

def generate_pdf_report(companies, title="Signalpost Intelligence Report"):
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, clean_for_pdf(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 10, f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(10)
    for comp in companies:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, clean_for_pdf(comp.get("company_name")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 11)
        for line in [f"Org Number: {clean_for_pdf(comp.get('org_number'))}",
                     f"Type: {clean_for_pdf(comp.get('business_type'))}",
                     f"Address: {clean_for_pdf(comp.get('address'))}",
                     f"Registered: {clean_for_pdf(comp.get('registration_date'))}",
                     f"Employees: {clean_for_pdf(comp.get('employees'))}",
                     f"Source: {clean_for_pdf(comp.get('source_url'))}"]:
            pdf.multi_cell(0, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)
        pdf.set_font("Helvetica", "I", 11)
        pdf.multi_cell(0, 6, f"AI Analysis: {clean_for_pdf(comp.get('ai_explanation'))}",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(8)
    return bytes(pdf.output())

@st.cache_data(show_spinner=False)
def _pdf_bytes(payload_json: str, title: str):
    return generate_pdf_report(json.loads(payload_json), title)

# --- RENDER HELPERS ---
def card_html(p):
    return f"""
    <div class="grid">
      <div class="tile"><div class="label">Business Type</div><div class="value">{p.get('business_type','N/A')}</div></div>
      <div class="tile"><div class="label">Registration Date</div><div class="value">{p.get('registration_date','N/A')}</div></div>
      <div class="tile"><div class="label">Address</div><div class="value">{p.get('address','N/A')}</div></div>
      <div class="tile"><div class="label">Employees</div><div class="value">{p.get('employees','N/A')}</div></div>
    </div>
    <div class="ai-box"><h4>🤖 AI Analysis</h4><p>{p.get('ai_explanation','Not available.')}</p></div>
    """

def render_card(p):
    st.markdown(f"### {p.get('company_name')}")
    st.markdown(f"<span class='orgpill'>Org. Nr {p.get('org_number')}</span>", unsafe_allow_html=True)
    st.markdown(card_html(p), unsafe_allow_html=True)
    with st.expander("🧾 Evidence & sources"):
        st.markdown(f"Registry: [{p.get('source_url')}]({p.get('source_url')})")
        org = p.get("org_number")
        if os.path.exists(os.path.join("evidence", f"{org}.json")):
            st.caption(f"Raw registry JSON: `evidence/{org}.json`")
        else:
            st.caption("Raw registry JSON not captured yet — use **Refresh live** to store it.")

def render_sources(srcs):
    if srcs:
        with st.expander(f"🌐 Web sources ({len(srcs)})"):
            for s in srcs:
                st.markdown(f"- [{s}]({s})")

def replay_thread(tid):
    for m in memory.load_messages(tid):
        if m["role"] == "user":
            with st.chat_message("user"):
                st.markdown(m["content"])
        else:
            with st.chat_message("assistant"):
                if m["kind"] == "company_card" and isinstance(m["content"], dict):
                    render_card(m["content"])
                    _org = m["content"].get("org_number")
                    ba1, ba2, _sp = st.columns([2, 2, 8])
                    if memory.is_saved(_org):
                        ba1.button("✅ In report", key=f"csave_{m['id']}", width="stretch",
                                   disabled=True, help="Already in the report pool")
                    elif ba1.button("📌 Save to report", key=f"csave_{m['id']}", width="stretch",
                                    help="Add this company to the PDF report pool"):
                        memory.save_company(m["content"])
                        st.toast(f"📌 {m['content'].get('company_name')} added to report pool")
                        st.rerun()
                    if ba2.button("🔄 Refresh live", key=f"cref_{m['id']}", width="stretch",
                                  help="Re-fetch live data from Brreg.no and update this card"):
                        fresh = lookup_company(_org, force_refresh=True)
                        if fresh:
                            memory.update_message(m["id"], fresh)
                            st.toast(f"🔄 {fresh.get('company_name')} refreshed from Brreg.no")
                        st.rerun()
                elif m["kind"] == "research" and isinstance(m["content"], dict):
                    st.markdown(m["content"].get("text", ""))
                    render_sources(m["content"].get("sources") or [])
                else:
                    st.markdown(m["content"])

def notice(tid, text):
    memory.add_message(tid, "assistant", text, kind="notice")

def transcript_md(tid):
    t = memory.get_thread(tid) or {}
    lines = [f"# {t.get('title','Research thread')}",
             f"_Exported {time.strftime('%Y-%m-%d %H:%M')} — Signalpost Intelligence_", ""]
    for m in memory.load_messages(tid):
        who = "**You**" if m["role"] == "user" else "**Agent**"
        if m["kind"] == "company_card" and isinstance(m["content"], dict):
            p = m["content"]
            lines.append(f"{who}: 📇 {p.get('company_name')} (Org. Nr {p.get('org_number')})")
            lines.append(f"  - source: {p.get('source_url')}")
        elif m["kind"] == "research" and isinstance(m["content"], dict):
            lines.append(f"{who}: {m['content'].get('text','')}")
            for s in (m["content"].get("sources") or []):
                lines.append(f"  - source: {s}")
        else:
            lines.append(f"{who}: {m['content']}")
        lines.append("")
    return "\n".join(lines)

# --- DIALOGS (Qwen-style modal confirmations; self-clearing) ---
@st.dialog("Delete this chat?")
def _delete_dialog(tid):
    st.session_state.dlg = None
    t = memory.get_thread(tid) or {}
    st.warning(f"**{t.get('title', 'This chat')}** will be permanently deleted. This cannot be undone.")
    c1, c2 = st.columns(2)
    if c1.button("Cancel", width="stretch"):
        st.rerun()
    if c2.button("Delete", type="primary", width="stretch"):
        memory.delete_thread(tid)
        if st.session_state.active_thread == tid:
            st.session_state.active_thread = None
        st.rerun()

@st.dialog("Rename chat")
def _rename_dialog(tid):
    st.session_state.dlg = None
    t = memory.get_thread(tid) or {}
    with st.form("rename_form"):
        new = st.text_input("New title", value=t.get("title", ""))
        if st.form_submit_button("Save", type="primary", width="stretch"):
            if new.strip():
                memory.set_title(tid, new.strip())
            st.rerun()

@st.dialog("Remove from report pool?")
def _remove_dialog(org):
    st.session_state.dlg = None
    name = next((c["company_name"] for c in memory.list_saved() if c["org_number"] == org), org)
    st.warning(f"**{name}** will be removed from the saved report pool. "
               f"PDFs you already downloaded are unaffected.")
    c1, c2 = st.columns(2)
    if c1.button("Cancel", width="stretch"):
        st.rerun()
    if c2.button("Remove", type="primary", width="stretch"):
        memory.remove_saved(org)
        st.session_state.pop(f"sel_{org}", None)
        st.toast(f"🗑️ {name} removed from report pool")
        st.rerun()

@st.dialog("Clear the whole report pool?")
def _clear_dialog():
    st.session_state.dlg = None
    n = len(memory.list_saved())
    st.warning(f"All **{n}** saved companies will be removed from the pool. This cannot be undone.")
    c1, c2 = st.columns(2)
    if c1.button("Cancel", width="stretch"):
        st.rerun()
    if c2.button("Clear all", type="primary", width="stretch"):
        memory.clear_saved()
        st.toast("🗑️ Report pool cleared")
        st.rerun()

def _bump():
    st.session_state.menu_epoch = st.session_state.get("menu_epoch", 0) + 1

# --- INTENT DISPATCH (guaranteed acknowledgement on every message) ---
def handle_input(text):
    tid = st.session_state.active_thread
    if not tid:
        tid = memory.new_thread(title=text[:40] + ("…" if len(text) > 40 else ""))
        st.session_state.active_thread = tid
    memory.add_message(tid, "user", text)
    with st.chat_message("user"):
        st.markdown(text)
    use_ws = st.session_state.get("web_search_toggle", True)

    def answer_question(profile, question):
        status.write("Searching web & generating answer...")
        resp = chat_with_gemini(profile, question, use_web_search=use_ws)
        sources = list(agent.LAST_SOURCES)
        memory.add_message(tid, "assistant", {"text": resp, "sources": sources}, kind="research")

    def add_card(profile):
        memory.add_message(tid, "assistant", profile, kind="company_card",
                           org_nr=profile["org_number"])
        memory.maybe_auto_title(tid, f"{profile['company_name']} — research")

    def in_thread(org):
        return org in [c["org_nr"] for c in memory.thread_companies(tid)]

    def ensure_profile(org):
        if not org:
            return None
        p = load_cache().get(str(org))
        if p:
            status.write(f"Company resolved: {p['company_name']} (`{org}`) · served from cache")
            return p
        p = lookup_company(org)
        if p:
            status.write(f"Company resolved: {p['company_name']} (`{org}`) · fetched live")
        return p

    def fail_notice(short_text):
        msg = f"❌ No match for “{short_text}”."
        if agent.LAST_SUGGESTIONS:
            msg += (" Closest registered names: "
                    + "; ".join(f"**{n}** (`{o}`)" for n, o in agent.LAST_SUGGESTIONS)
                    + " — reply with one to open it.")
        notice(tid, msg)

    with st.status("🧠 Thinking…", expanded=False) as status:
        try:
            d = intent.route(text, thread_id=tid)
            status.write(f"Intent: **{d.intent}** · routed via {d.stage}")

            if d.intent == "lookup":
                org = d.org_nr or (search_by_name(d.company_query) if d.company_query else None)
                p = ensure_profile(org)
                if p:
                    add_card(p)
                    question = d.question or (text if intent.looks_like_question(text) else "")
                    if question:
                        answer_question(p, question)
                elif intent.looks_like_question(text) and memory.last_company_org(tid):
                    fb = ensure_profile(memory.last_company_org(tid))
                    if fb:
                        answer_question(fb, text)
                    else:
                        notice(tid, "🔎 Search for a company first — then ask me anything about it.")
                else:
                    fail_notice(text[:60] + ("…" if len(text) > 60 else ""))

            elif d.intent == "followup":
                org = d.org_nr or memory.last_company_org(tid)
                p = ensure_profile(org)
                if not p and d.company_query:
                    org = search_by_name(d.company_query)
                    p = ensure_profile(org)
                if not p:
                    notice(tid, "🔎 Search for a company first — then ask me anything about it.")
                else:
                    if not in_thread(str(org)):
                        add_card(p)
                    answer_question(p, d.question or text)

            elif d.intent == "save":
                text_low = text.lower()
                comps = memory.thread_companies(tid)
                targets = []
                # 1) every discussed company whose name (or core name) appears in the request
                for c in comps:
                    name_low = c["name"].lower()
                    core = name_low
                    for suf in (" asa", " as"):
                        if core.endswith(suf):
                            core = core[: -len(suf)]
                            break
                    if len(core) >= 4 and (name_low in text_low or core in text_low):
                        targets.append(c)
                # 2) "all / both / every" without names -> everything in this thread
                if not targets and comps and any(w in text_low for w in ("all", "both", "every")):
                    targets = list(comps)
                # 3) fallback: classifier referent or company in view
                if not targets:
                    org = d.org_nr or memory.last_company_org(tid)
                    if org:
                        p0 = load_cache().get(str(org))
                        if p0:
                            targets = [{"org_nr": str(org),
                                        "name": p0.get("company_name", str(org))}]
                saved_names = []
                for c in targets:
                    p = load_cache().get(str(c["org_nr"])) or lookup_company(c["org_nr"])
                    if p:
                        memory.save_company(p)
                        if p.get("company_name") not in saved_names:
                            saved_names.append(p.get("company_name"))
                if saved_names:
                    plural = "y" if len(saved_names) == 1 else "ies"
                    memory.add_message(
                        tid, "assistant",
                        f"📌 Saved {len(saved_names)} company{plural} to your report pool: "
                        + ", ".join(f"**{n}**" for n in saved_names)
                        + ". They are pre-selected together in the sidebar → **Report Builder**, "
                          "so one PDF download includes all of them.")
                else:
                    notice(tid, "📌 Nothing to save yet — look up a company first, "
                                "or name it: “save Telenor”.")

            elif d.intent == "report":
                saved = memory.list_saved()
                if not saved:
                    notice(tid, "📊 Your report pool is empty. Say “save this company” after a lookup.")
                else:
                    names = "\n".join(f"- {c['company_name']} (`{c['org_number']}`)" for c in saved)
                    memory.add_message(tid, "assistant",
                                       f"📊 **Report pool ({len(saved)}):**\n{names}\n\n"
                                       f"Select companies and download from the sidebar → **Report Builder**.")

            elif d.intent == "refresh":
                org = d.org_nr or memory.last_company_org(tid)
                if not org:
                    notice(tid, "🔄 Nothing in view to refresh — look up a company first.")
                else:
                    p = lookup_company(org, force_refresh=True)
                    if p:
                        status.write(f"Live registry refresh for {p['company_name']}")
                        add_card(p)
                        memory.add_message(tid, "assistant",
                                           f"🔄 Live registry data refreshed for **{p['company_name']}**.")

            else:  # chitchat
                memory.add_message(tid, "assistant", AGENT_INTRO)

            status.update(label="View reasoning", state="complete")
        except Exception as e:
            status.update(label="⚠️ Processing error", state="error")
            notice(tid, f"⚠️ Something went wrong while processing that message ({e}). "
                        f"Your message is saved — try again or rephrase.")
    st.rerun()

# --- THREAD ROW (hover kebab + epoch-remount menu close) ---
def thread_row(t):
    epoch = st.session_state.menu_epoch
    c1, c2 = st.columns([12, 1], gap="small")
    if c1.button(t["title"], width="stretch", key=f"th_{t['id']}",
                 type="primary" if t["id"] == st.session_state.active_thread else "secondary"):
        st.session_state.active_thread = t["id"]
        st.rerun()
    with c2.popover(" ", help="Thread options", key=f"menu_{t['id']}_{epoch}"):
        if st.button("Rename", width="stretch", key=f"ren_{t['id']}"):
            _bump(); st.session_state.dlg = ("rename", t["id"]); st.rerun()
        if st.button("Unpin" if t["pinned"] else "Pin", width="stretch", key=f"pin_{t['id']}"):
            _bump(); memory.set_pinned(t["id"], not t["pinned"]); st.rerun()
        if t["archived"]:
            if st.button("Restore", width="stretch", key=f"res_{t['id']}"):
                _bump(); memory.set_archived(t["id"], False); st.rerun()
        else:
            if st.button("Archive", width="stretch", key=f"arc_{t['id']}"):
                _bump(); memory.set_archived(t["id"], True)
                if st.session_state.active_thread == t["id"]:
                    st.session_state.active_thread = None
                st.rerun()
        st.download_button("Export transcript (.md)", data=transcript_md(t["id"]),
                           file_name=f"signalpost_{t['id']}.md", mime="text/markdown",
                           width="stretch", key=f"exp_{t['id']}")
        if st.button("Delete", width="stretch", key=f"del_{t['id']}"):
            _bump(); st.session_state.dlg = ("delete", t["id"]); st.rerun()

# --- SIDEBAR ---
with st.sidebar:
    st.markdown(f"## {logo_img(24)}Signalpost", unsafe_allow_html=True)
    if st.button("➕ New research", width="stretch", type="primary", key="new_thread"):
        st.session_state.active_thread = None
        st.rerun()

    pinned, groups = memory.grouped_threads()
    arch = memory.archived_threads()

    # Live (per-keystroke, client-side) thread jump — no blank option, reset before creation
    if st.session_state.pop("jump_reset", False):
        st.session_state["jump"] = None
    all_threads = pinned + [t for g in groups.values() for t in g] + arch
    if all_threads:
        labels, seen = [], {}
        for t in all_threads:
            base = t["title"]
            n = seen.get(base, 0)
            seen[base] = n + 1
            labels.append(base if n == 0 else f"{base} ({n + 1})")
        label2id = {lab: t["id"] for lab, t in zip(labels, all_threads)}
        choice = st.selectbox("Jump to thread", labels, key="jump", index=None,
                              label_visibility="collapsed",
                              placeholder="🔎 Search / jump to thread…")
        if choice:
            st.session_state.active_thread = label2id[choice]
            st.session_state["jump_reset"] = True
            st.rerun()

    if pinned:
        st.caption("Pinned")
        for t in pinned:
            thread_row(t)
    if groups:
        st.caption("History")
        for bucket, threads in groups.items():
            st.caption(bucket)
            for t in threads:
                thread_row(t)
    if arch:
        with st.expander(f"Archived ({len(arch)})"):
            for t in arch:
                thread_row(t)

    with st.expander("⚙️ Engine Status"):
        _mode, _desc = ai_status()
        st.caption(_desc)
        st.caption(f"Keys: {key_source_label()}")
        st.checkbox("🌐 Live web search in chat (uses Tavily credits)", value=True,
                    key="web_search_toggle")
        if _mode != "ai":
            st.caption("Copy `.env.example` to `.env` and add your keys to unlock AI + web search.")
        st.caption("Raw registry evidence for every lookup: `evidence/<org>.json`")

    st.markdown("### 📊 Report Builder")
    saved = memory.list_saved()
    if saved:
        pool = st.expander(f"Saved companies ({len(saved)})", expanded=True) \
            if len(saved) > 6 else st.container()
        with pool:
            epoch = st.session_state.menu_epoch
            for c in saved:
                rc1, rc2 = st.columns([11, 1], gap="small")
                rc1.checkbox(c["company_name"], value=True, key=f"sel_{c['org_number']}")
                with rc2.popover(" ", help="Company options",
                                 key=f"poolmenu_{c['org_number']}_{epoch}"):
                    if st.button("Remove from pool", width="stretch", key=f"rm_{c['org_number']}"):
                        _bump(); st.session_state.dlg = ("rm", c["org_number"]); st.rerun()
        report_name = st.text_input("Report name", key="report_name",
                                    label_visibility="collapsed",
                                    placeholder="Report name (optional)")
        selected = [c for c in saved if st.session_state.get(f"sel_{c['org_number']}", True)]
        if selected:
            title = report_name.strip() or "Signalpost Intelligence Report"
            st.download_button(f"📥 Download PDF ({len(selected)})",
                               data=_pdf_bytes(json.dumps(selected, ensure_ascii=False), title),
                               file_name=(report_name.strip() or "Signalpost_Report") + ".pdf",
                               mime="application/pdf", width="stretch")
        else:
            st.caption("Select at least one company above.")
        if st.button("🗑 Clear all saved", width="stretch", key="clear_report"):
            st.session_state.dlg = ("clear", None)
            st.rerun()
    else:
        st.info("No companies saved yet. Chat: “save this company”.")

    st.markdown("<div class='sidefoot'>Built for the Signalpost challenge (builderr.ai)<br>"
                "Data © Brønnøysund Register Centre (Brreg.no)</div>", unsafe_allow_html=True)

# --- DIALOG TRIGGERS ---
if st.session_state.dlg:
    kind, payload = st.session_state.dlg
    if kind == "delete":
        _delete_dialog(payload)
    elif kind == "rename":
        _rename_dialog(payload)
    elif kind == "rm":
        _remove_dialog(payload)
    elif kind == "clear":
        _clear_dialog()

# --- MAIN ---
mode = ai_status()[0]
st.markdown(f"""
<div class="hero">
  <h1>{logo_img(30)}Signalpost Intelligence</h1>
  <p>Norwegian company research agent — live Brreg.no registry + OpenRouter reasoning + Tavily web search.</p>
  <span class="pill">{'🟢 AI mode' if mode == 'ai' else '🟠 Basic mode'}</span>
  <span class="pill">📦 {N_CACHE:,} cached</span>
  <span class="pill">🔑 {key_source_label()}</span>
</div>
""", unsafe_allow_html=True)

if st.session_state.active_thread:
    replay_thread(st.session_state.active_thread)
else:
    st.markdown("<br>", unsafe_allow_html=True)
    e1, e2, e3 = st.columns(3)
    e1.markdown("<div class='card'><h3>🔎 Verify</h3><p>Any Norwegian company by name or org "
                "number — typos and partial names included — against the official registry.</p></div>",
                unsafe_allow_html=True)
    e2.markdown("<div class='card'><h3>🤖 Research</h3><p>Follow-up questions answered with live, "
                "cited web search; sources in a clean dropdown.</p></div>", unsafe_allow_html=True)
    e3.markdown("<div class='card'><h3>📥 Export</h3><p>“Save this company”, then build custom "
                "PDF briefs from the sidebar Report Builder.</p></div>", unsafe_allow_html=True)
    st.caption("Try one:")
    p1, p2, p3 = st.columns(3)
    for col, pill in [(p1, "Look up Telenor ASA"), (p2, "914778271"), (p3, "Latest news about Equinor?")]:
        if col.button(pill, width="stretch", key=f"pill_{pill}"):
            handle_input(pill)

_last_org = memory.last_company_org(st.session_state.active_thread) if st.session_state.active_thread else None
_last_name = (load_cache().get(str(_last_org)) or {}).get("company_name") if _last_org else None
if prompt := st.chat_input(f"Ask about {_last_name}… " if _last_name else "Search a company or ask anything…"):
    handle_input(prompt)