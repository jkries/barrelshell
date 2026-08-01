"""Bundled skill: a permanent, structured, queryable backup of every
message ever exchanged with this Barrel.

Durability is NOT what this adds — agent_log.jsonl already captures
every user message and, as of this version, a clean marker for the
model's final user-visible reply, disk-backed and restart-proof,
regardless of whether this skill has run recently. This skill turns
that append-only log into an indexed SQLite table: fast date-range
queries, real search, and an export you can actually walk away with.
If ingestion lags an hour, nothing is lost — the source of truth is
still sitting in the log, waiting to be read on the next cycle.

Ingestion is pure mechanical copying — no summarization, no judgment
— so unlike archive.py it doesn't need the model to write anything.
It's still triggered through the ordinary pulse mechanism (a trivial
tool call, always replying PULSE_OK) rather than a second core hook
that bypasses the model, to keep the core touch this feature needed
to exactly one line.

Concurrency: this is the first skill three different threads (the
Telegram loop, the pulse thread, and the dashboard's per-request
threads) can plausibly touch at once, so writes go through WAL mode,
a busy_timeout, and a dedicated lock — never the wider agent_lock,
which shouldn't be held for a database round-trip.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, date

import barrel_v1 as core

DB_FILE = "messages.db"
EXPORT_DIR = "exports"
MAX_LIST_CHARS = 3000

_lock = threading.Lock()
_fts_available = None   # detected once, on first connect


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _fts_available
    conn.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        role TEXT NOT NULL,
        text TEXT NOT NULL
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts "
                "ON messages(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat "
                "ON messages(chat_id, ts)")
    conn.execute('''CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT)''')
    if _fts_available is None:
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                        "USING fts5(text, content='messages', "
                        "content_rowid='id')")
            conn.execute('''CREATE TRIGGER IF NOT EXISTS messages_ai
                AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, text)
                    VALUES (new.id, new.text);
                END''')
            _fts_available = True
        except sqlite3.OperationalError:
            _fts_available = False   # this Python's sqlite3 lacks FTS5
    conn.commit()


def _get_watermark(conn: sqlite3.Connection):
    row = conn.execute("SELECT value FROM meta WHERE key='last_ts'").fetchone()
    return row[0] if row else None


def _set_watermark(conn: sqlite3.Connection, ts: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES ('last_ts', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ts,))


def ingest_log(log_path: str, conn: sqlite3.Connection,
               since: str = None) -> tuple:
    """Shared by the live skill and the standalone backfill script.
    Reads user_msg + final_reply events after `since` (exclusive) and
    inserts them. Returns (rows_inserted, newest_ts_seen_or_None).
    Advances only as far as what was actually read this pass — never
    to wall-clock "now" — so nothing between the last line read and
    a message that arrives mid-scan can be silently skipped."""
    since = since or ""
    rows, newest = [], since
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = e.get("ts", "")
                if ts <= since:
                    continue
                event = e.get("event")
                if event == "user_msg" and e.get("kind") != "pulse":
                    role = "user"
                elif event == "final_reply" and e.get("kind") != "pulse":
                    role = "assistant"
                else:
                    continue
                text = str(e.get("text", "")).strip()
                if not text:
                    continue
                rows.append((ts, e.get("chat_id", 0),
                           e.get("kind", "chat"), role, text))
                if ts > newest:
                    newest = ts
    except OSError as e:
        return 0, None, f"couldn't read {log_path}: {e.__class__.__name__}"
    if rows:
        conn.executemany(
            "INSERT INTO messages(ts, chat_id, kind, role, text) "
            "VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
    return len(rows), (newest if rows else None), None


def _ingest(arg: str) -> str:
    with _lock:
        conn = _connect()
        try:
            since = _get_watermark(conn)
            if since is None:
                # No watermark at all means this skill has never run —
                # start fresh from now rather than pulling in the
                # entire pre-existing log. Run backfill_transcript.py
                # once, before this first fires, to capture history
                # instead of skipping it.
                since = datetime.now().isoformat()
                _set_watermark(conn, since)
                conn.commit()
                return ("(no prior watermark — starting fresh from now; "
                        "run backfill_transcript.py once if you want "
                        "existing history included)")
            n, newest, err = ingest_log(core.LOG_FILE, conn, since)
            if err:
                return f"(ingest failed: {err})"
            if n:
                _set_watermark(conn, newest)
                conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            return (f"(ingested {n} new message(s), {total} total)" if n
                   else "(no new messages since last ingest)")
        finally:
            conn.close()


def _recent(arg: str) -> str:
    try:
        n = max(1, min(int(arg.strip()), 100)) if arg.strip() else 20
    except ValueError:
        n = 20
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT ts, role, text FROM messages ORDER BY id DESC "
                "LIMIT ?", (n,)).fetchall()
        finally:
            conn.close()
    if not rows:
        return "(no messages backed up yet — has ingest run?)"
    lines = [f"[{ts}] {role}: {text}" for ts, role, text in reversed(rows)]
    return "\n".join(lines)[-MAX_LIST_CHARS:]


def _search(query: str) -> str:
    query = query.strip()
    if not query:
        return "(use: search | words to look for)"
    with _lock:
        conn = _connect()
        try:
            if _fts_available:
                fts_q = '"' + query.replace('"', '""') + '"'
                try:
                    rows = conn.execute(
                        "SELECT m.ts, m.role, m.text FROM messages_fts f "
                        "JOIN messages m ON m.id = f.rowid "
                        "WHERE messages_fts MATCH ? "
                        "ORDER BY rank LIMIT 20", (fts_q,)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            else:
                rows = conn.execute(
                    "SELECT ts, role, text FROM messages WHERE text "
                    "LIKE ? ORDER BY ts DESC LIMIT 20",
                    (f"%{query}%",)).fetchall()
        finally:
            conn.close()
    if not rows:
        return f"(no backed-up messages mention '{query}')"
    lines = [f"[{ts}] {role}: {text}" for ts, role, text in rows]
    return "\n".join(lines)[-MAX_LIST_CHARS:]


def _export(arg: str) -> str:
    parts = [p.strip() for p in arg.split("|")]
    since = parts[0] if len(parts) > 0 and parts[0] else "0000-00-00"
    until = parts[1] if len(parts) > 1 and parts[1] else "9999-99-99"
    fmt = (parts[2].lower() if len(parts) > 2 and parts[2] else "jsonl")
    if fmt not in ("jsonl", "text"):
        return "(format must be jsonl or text)"

    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT ts, chat_id, kind, role, text FROM messages "
                "WHERE ts >= ? AND ts < ? ORDER BY ts",
                (since, until)).fetchall()
        finally:
            conn.close()
    if not rows:
        return f"(no messages in that range to export)"

    label_since = since if since != "0000-00-00" else "all"
    label_until = until if until != "9999-99-99" else "now"
    ext = "jsonl" if fmt == "jsonl" else "txt"
    name = f"transcript_{label_since}_to_{label_until}.{ext}"
    path = core._workspace_path(f"{EXPORT_DIR}/{name}")
    if not path:
        return "(refused: export path escapes the workspace)"
    os.makedirs(os.path.dirname(path), exist_ok=True)

    try:
        with open(path, "w", encoding="utf-8") as f:
            if fmt == "jsonl":
                for ts, chat_id, kind, role, text in rows:
                    f.write(json.dumps({"ts": ts, "chat_id": chat_id,
                                       "kind": kind, "role": role,
                                       "text": text}) + "\n")
            else:
                for ts, chat_id, kind, role, text in rows:
                    who = "You" if role == "user" else "Bot"
                    f.write(f"[{ts}] {who}: {text}\n")
    except OSError as e:
        return f"(export failed: {e})"

    core.log_event("transcript_exported", rows=len(rows), path=name)
    return (f"(exported {len(rows)} message(s) to {EXPORT_DIR}/{name} — "
           f"use <file>send {EXPORT_DIR}/{name}</file> to have it "
           f"delivered to this chat)")


def transcript(arg: str, chat_id: int) -> str:
    verb, _, rest = arg.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb == "ingest":
        return _ingest(rest)
    if verb == "recent":
        return _recent(rest)
    if verb == "search":
        return _search(rest.lstrip("|").strip())
    if verb == "export":
        return _export(rest)
    return ("(unknown transcript command — use: ingest | recent [N] | "
           "search | words | export since | until | jsonl-or-text "
           "(any field blank-able))")


SKILL = {
    "name": "transcript",
    "desc": "The permanent backup of every message ever exchanged with "
            "this Barrel (separate from archive, which holds condensed "
            "topics, not raw messages). <transcript>ingest</transcript> "
            "copies new messages in — used automatically on a "
            "schedule, no need to call it otherwise. "
            "<transcript>recent [N]</transcript> or "
            "<transcript>search | words</transcript> to look something "
            "up. <transcript>export since | until | jsonl-or-text"
            "</transcript> — the FIRST field has no pipe before "
            "it, only between fields; any field may be blank, "
            "e.g. 'export | | jsonl' for everything. Saves a "
            "portable copy into the workspace.",
    "handler": transcript,
}
