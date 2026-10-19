"""History browsing keeps its own identity window and per-thread projection."""
from contextlib import contextmanager
import sqlite3

from routes import sage_routes


def _database(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE sage_conversations (id INTEGER PRIMARY KEY, user_id TEXT, role TEXT, content TEXT, sources TEXT, conversation_id TEXT, timestamp TEXT)")

    @contextmanager
    def session():
        yield db

    monkeypatch.setattr(sage_routes, "db_session", session)
    monkeypatch.setattr(sage_routes, "_display_answer", lambda value: value)
    return db


def _insert(db, owner, role, content, thread):
    db.execute("INSERT INTO sage_conversations (user_id,role,content,conversation_id,timestamp) VALUES (?,?,?,?,?)",
               (owner, role, content, thread, "2026-10-11 10:00:00"))


def test_interleaved_threads_never_project_from_another_threads_question(monkeypatch):
    db = _database(monkeypatch)
    try:
        _insert(db, "demo", "user", "Question A", "A")
        _insert(db, "demo", "user", "Question B", "B")
        _insert(db, "demo", "assistant", "Answer A", "A")
        _insert(db, "demo", "assistant", "Answer B", "B")
        seen = []
        monkeypatch.setattr(sage_routes, "_history_safe_answer", lambda sources, previous: seen.append(previous) or None)
        result = sage_routes._history_rows("demo")
        assert [row["content"] for row in result] == ["Question A", "Question B", "Answer A", "Answer B"]
        assert [previous["content"] for previous in seen] == ["Question A", "Question B"]
    finally:
        db.close()


def test_browsing_window_is_bounded_deterministic_and_owned(monkeypatch):
    db = _database(monkeypatch)
    try:
        for index in range(220):
            _insert(db, "demo", "user", f"Question {index}", f"thread-{index // 2}")
        _insert(db, "someone-else", "user", "Private other account", "other")
        result = sage_routes._history_rows("demo")
        assert len(result) == 200
        assert result[0]["content"] == "Question 20"
        assert result[-1]["content"] == "Question 219"
        assert all(row["content"] != "Private other account" for row in result)
    finally:
        db.close()
