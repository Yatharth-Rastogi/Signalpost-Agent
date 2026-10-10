"""
memory.py — Signalpost persistent memory layer (SQLite, local-first).
Threads (pin/archive/auto-title), messages (text / company_card / research /
notice), saved companies for PDF reports, kv settings.
"""

import sqlite3
import time
import uuid
import os
import json
import datetime as dt
from contextlib import contextmanager

DB_FILE = os.getenv("SIGNALPOST_DB", "signalpost.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    auto_title INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    content TEXT NOT NULL,
    org_nr TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_companies (
    org_nr TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    saved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at DESC);
"""

@contextmanager
def _db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def _migrate(c):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(threads)").fetchall()}
    if "pinned" not in cols:
        c.execute("ALTER TABLE threads ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    if "archived" not in cols:
        c.execute("ALTER TABLE threads ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "auto_title" not in cols:
        c.execute("ALTER TABLE threads ADD COLUMN auto_title INTEGER NOT NULL DEFAULT 1")

def init_db():
    with _db() as c:
        c.executescript(_SCHEMA)
        _migrate(c)

init_db()

# ---------------------------------------------------------------- threads
def new_thread(title="New research"):
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    with _db() as c:
        c.execute("INSERT INTO threads (id,title,created_at,updated_at,pinned,archived,auto_title)"
                  " VALUES (?,?,?,?,0,0,1)", (tid, title, now, now))
    return tid

def touch_thread(tid):
    with _db() as c:
        c.execute("UPDATE threads SET updated_at=? WHERE id=?", (time.time(), tid))

def maybe_auto_title(tid, title):
    with _db() as c:
        row = c.execute("SELECT auto_title FROM threads WHERE id=?", (tid,)).fetchone()
        if row and row["auto_title"]:
            c.execute("UPDATE threads SET title=?, auto_title=0 WHERE id=?", (title, tid))

def set_title(tid, title):
    with _db() as c:
        c.execute("UPDATE threads SET title=?, auto_title=0 WHERE id=?", (title, tid))

def set_pinned(tid, pinned: bool):
    with _db() as c:
        c.execute("UPDATE threads SET pinned=? WHERE id=?", (1 if pinned else 0, tid))

def set_archived(tid, archived: bool):
    with _db() as c:
        c.execute("UPDATE threads SET archived=? WHERE id=?", (1 if archived else 0, tid))

def delete_thread(tid):
    with _db() as c:
        c.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
        c.execute("DELETE FROM threads WHERE id=?", (tid,))

def get_thread(tid):
    with _db() as c:
        row = c.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else None

# --------------------------------------------------------------- messages
def add_message(tid, role, content, kind="text", org_nr=None):
    now = time.time()
    payload = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    with _db() as c:
        c.execute("INSERT INTO messages (thread_id,role,kind,content,org_nr,created_at)"
                  " VALUES (?,?,?,?,?,?)", (tid, role, kind, payload, org_nr, now))
        c.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, tid))
    return now

def load_messages(tid):
    with _db() as c:
        rows = c.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (tid,)).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        if m["kind"] in ("company_card", "research"):
            try:
                m["content"] = json.loads(m["content"])
            except Exception:
                pass
        out.append(m)
    return out

def update_message(mid, content):
    payload = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    with _db() as c:
        c.execute("UPDATE messages SET content=? WHERE id=?", (payload, mid))

def last_company_org(tid):
    with _db() as c:
        row = c.execute("SELECT org_nr FROM messages WHERE thread_id=? AND kind='company_card'"
                        " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return row["org_nr"] if row else None

def thread_companies(tid):
    with _db() as c:
        rows = c.execute("SELECT org_nr, content FROM messages WHERE thread_id=? AND kind='company_card'"
                         " ORDER BY id", (tid,)).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["org_nr"] in seen:
            continue
        seen.add(r["org_nr"])
        try:
            name = json.loads(r["content"]).get("company_name", r["org_nr"])
        except Exception:
            name = r["org_nr"]
        out.append({"org_nr": r["org_nr"], "name": name})
    return out

# ------------------------------------------------- history buckets
def _bucket(ts):
    d = dt.datetime.fromtimestamp(ts).date()
    delta = (dt.date.today() - d).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta <= 7:
        return "Previous 7 days"
    if delta <= 30:
        return "Previous 30 days"
    return "Older"

BUCKET_ORDER = ["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"]

def grouped_threads():
    with _db() as c:
        rows = c.execute("SELECT * FROM threads WHERE archived=0 ORDER BY updated_at DESC").fetchall()
    pinned = [dict(r) for r in rows if r["pinned"]]
    groups = {b: [] for b in BUCKET_ORDER}
    for r in rows:
        if not r["pinned"]:
            groups[_bucket(r["updated_at"])].append(dict(r))
    return pinned, {b: groups[b] for b in BUCKET_ORDER if groups[b]}

def archived_threads():
    with _db() as c:
        rows = c.execute("SELECT * FROM threads WHERE archived=1 ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]

# --------------------------------------------------------- saved companies
def save_company(profile):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO saved_companies (org_nr,payload,saved_at)"
                  " VALUES (?,?,?)",
                  (str(profile["org_number"]), json.dumps(profile, ensure_ascii=False), time.time()))

def list_saved():
    with _db() as c:
        rows = c.execute("SELECT * FROM saved_companies ORDER BY saved_at").fetchall()
    return [json.loads(r["payload"]) for r in rows]

def is_saved(org_nr):
    with _db() as c:
        row = c.execute("SELECT 1 FROM saved_companies WHERE org_nr=?", (str(org_nr),)).fetchone()
    return row is not None

def remove_saved(org_nr):
    with _db() as c:
        c.execute("DELETE FROM saved_companies WHERE org_nr=?", (str(org_nr),))

def clear_saved():
    with _db() as c:
        c.execute("DELETE FROM saved_companies")

# ------------------------------------------------------------------- kv
def kv_set(key, value):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO kv (key,value) VALUES (?,?)", (key, json.dumps(value)))

def kv_get(key, default=None):
    with _db() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default

# ------------------------------------------------------------- smoke test
if __name__ == "__main__":
    t = new_thread("Smoke test thread")
    add_message(t, "user", "telenor")
    add_message(t, "assistant", {"company_name": "TELENOR ASA"}, kind="company_card", org_nr="982463718")
    add_message(t, "assistant", {"text": "answer", "sources": ["https://x.no"]}, kind="research")
    print("Messages:", [(m["role"], m["kind"]) for m in load_messages(t)])
    print("Companies:", thread_companies(t))
    delete_thread(t)
    print("Memory layer OK ✔")