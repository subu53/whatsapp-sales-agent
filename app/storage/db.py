"""SQLite-backed conversation + lead storage.

Kept deliberately simple (stdlib sqlite3, no ORM) — this is the kind of
component that's easy to swap for Postgres later without touching the
agent logic, as long as the function signatures below stay the same.

WAL mode is enabled on every connection: SQLite's default rollback-journal
mode serializes writers and readers against each other, which produces
real "database is locked" errors the moment two webhook requests land
concurrently (normal under any real traffic, since FastAPI runs sync
routes in a thread pool). WAL lets reads and writes proceed concurrently
and is the correct default for exactly this shape of app.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    phone_number TEXT PRIMARY KEY,
    profile_name TEXT,
    lead_stage TEXT DEFAULT 'new',
    customer_name TEXT,
    location TEXT,
    use_case TEXT,
    budget_band TEXT,
    notes TEXT,
    escalated INTEGER DEFAULT 0,
    escalation_reason TEXT,
    opted_out INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number TEXT,
    role TEXT,          -- 'user' | 'assistant'
    content TEXT,
    created_at REAL
);

-- One row per inbound webhook delivery we've actually processed, keyed on
-- Twilio's MessageSid. Twilio retries a webhook that didn't 200 promptly
-- (a slow tool call, a transient error) — without this, a retried delivery
-- re-runs the whole agent loop and the customer gets the same question
-- answered twice, or worse, two different answers.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_sid TEXT PRIMARY KEY,
    processed_at REAL
);
"""


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "conversations", "opted_out", "INTEGER DEFAULT 0")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    """Adds a column if it's missing — lets old databases created before a
    schema change (like `opted_out`) upgrade in place instead of erroring."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create_conversation(db_path: str, phone_number: str, profile_name: Optional[str] = None) -> dict:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM conversations WHERE phone_number = ?", (phone_number,)).fetchone()
        if row:
            return dict(row)
        now = time.time()
        conn.execute(
            "INSERT INTO conversations (phone_number, profile_name, lead_stage, created_at, updated_at) "
            "VALUES (?, ?, 'new', ?, ?)",
            (phone_number, profile_name, now, now),
        )
        row = conn.execute("SELECT * FROM conversations WHERE phone_number = ?", (phone_number,)).fetchone()
        return dict(row)


def update_conversation(db_path: str, phone_number: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [phone_number]
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE conversations SET {set_clause} WHERE phone_number = ?", values)


def mark_escalated(db_path: str, phone_number: str, reason: str) -> None:
    update_conversation(db_path, phone_number, escalated=1, escalation_reason=reason, lead_stage="handed_off")


def set_opted_out(db_path: str, phone_number: str, opted_out: bool) -> None:
    update_conversation(db_path, phone_number, opted_out=1 if opted_out else 0)


def is_opted_out(db_path: str, phone_number: str) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT opted_out FROM conversations WHERE phone_number = ?", (phone_number,)
        ).fetchone()
    return bool(row and row["opted_out"])


def append_message(db_path: str, phone_number: str, role: str, content: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO messages (phone_number, role, content, created_at) VALUES (?, ?, ?, ?)",
            (phone_number, role, content, time.time()),
        )


def get_recent_messages(db_path: str, phone_number: str, limit: int = 20) -> list[dict]:
    """This IS the token-budget/pruning strategy for this build: a fixed
    message-count window rather than a token-counted one. Simple, and fine
    up to normal conversation lengths — the honest limitation is that a
    handful of very long individual messages could still blow past a
    model's context window before hitting this count cap. A token-counted
    or summarizing window is the natural upgrade if that ever bites."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE phone_number = ? "
            "ORDER BY id DESC LIMIT ?",
            (phone_number, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def count_recent_messages(db_path: str, phone_number: str, window_seconds: int) -> int:
    """Powers the per-contact rate limit — counts inbound+outbound turns in
    the trailing window, cheap enough to check on every request since it's
    one indexed-ish query against a table that's already small per contact."""
    cutoff = time.time() - window_seconds
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE phone_number = ? AND created_at >= ?",
            (phone_number, cutoff),
        ).fetchone()
    return row["n"] if row else 0


def is_message_processed(db_path: str, message_sid: str) -> bool:
    if not message_sid:
        return False
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_sid = ?", (message_sid,)
        ).fetchone()
    return row is not None


def mark_message_processed(db_path: str, message_sid: str) -> None:
    if not message_sid:
        return
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_sid, processed_at) VALUES (?, ?)",
            (message_sid, time.time()),
        )


def all_leads(db_path: str) -> list[dict]:
    """Simple export for a human to review — e.g. to page through in a follow-up script."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def purge_older_than(db_path: str, cutoff_epoch: float) -> dict:
    """Minimal data-retention mechanism: permanently deletes conversations
    (and their messages) not updated since `cutoff_epoch`. Real retention
    policy (how long, what counts as "inactive", whether to anonymize
    instead of delete) is a business decision, not an engineering one —
    this is the mechanism to carry out whatever that decision is. See
    scripts/purge_old_data.py for the callable version of this."""
    with _connect(db_path) as conn:
        stale = [
            r["phone_number"]
            for r in conn.execute(
                "SELECT phone_number FROM conversations WHERE updated_at < ?", (cutoff_epoch,)
            ).fetchall()
        ]
        for phone in stale:
            conn.execute("DELETE FROM messages WHERE phone_number = ?", (phone,))
            conn.execute("DELETE FROM conversations WHERE phone_number = ?", (phone,))
    return {"deleted_conversations": len(stale), "phone_numbers": stale}
