"""Tests for the storage-layer hardening added in response to review
feedback: WAL mode, opt-out tracking, webhook idempotency, rate-limit
counting, and the retention purge."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.storage import db  # noqa: E402


def _fresh_db(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


def test_wal_mode_enabled(tmp_path):
    path = _fresh_db(tmp_path)
    with db._connect(path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_opt_out_round_trip(tmp_path):
    path = _fresh_db(tmp_path)
    phone = "+254700000001"
    db.get_or_create_conversation(path, phone)
    assert db.is_opted_out(path, phone) is False
    db.set_opted_out(path, phone, True)
    assert db.is_opted_out(path, phone) is True
    db.set_opted_out(path, phone, False)
    assert db.is_opted_out(path, phone) is False


def test_is_opted_out_defaults_false_for_unknown_number(tmp_path):
    path = _fresh_db(tmp_path)
    assert db.is_opted_out(path, "+254700000099") is False


def test_message_idempotency(tmp_path):
    path = _fresh_db(tmp_path)
    assert db.is_message_processed(path, "SM123") is False
    db.mark_message_processed(path, "SM123")
    assert db.is_message_processed(path, "SM123") is True
    db.mark_message_processed(path, "SM123")  # marking twice must not raise


def test_count_recent_messages_window(tmp_path):
    path = _fresh_db(tmp_path)
    phone = "+254700000002"
    for i in range(5):
        db.append_message(path, phone, "user", f"msg {i}")
    assert db.count_recent_messages(path, phone, window_seconds=3600) == 5
    assert db.count_recent_messages(path, "+254700009999", window_seconds=3600) == 0


def test_purge_older_than(tmp_path):
    path = _fresh_db(tmp_path)
    old_phone, fresh_phone = "+254700000003", "+254700000004"
    db.get_or_create_conversation(path, old_phone)
    db.get_or_create_conversation(path, fresh_phone)
    db.append_message(path, old_phone, "user", "old message")
    db.append_message(path, fresh_phone, "user", "fresh message")

    with db._connect(path) as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE phone_number = ?",
            (time.time() - 200 * 86400, old_phone),
        )

    result = db.purge_older_than(path, cutoff_epoch=time.time() - 100 * 86400)
    assert result["deleted_conversations"] == 1
    assert old_phone in result["phone_numbers"]

    with db._connect(path) as conn:
        remaining = {r["phone_number"] for r in conn.execute("SELECT phone_number FROM conversations").fetchall()}
        remaining_msgs = {r["phone_number"] for r in conn.execute("SELECT phone_number FROM messages").fetchall()}
    assert old_phone not in remaining
    assert fresh_phone in remaining
    assert old_phone not in remaining_msgs
