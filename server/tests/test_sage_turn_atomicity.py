"""A saved answer and its question are one durable transaction."""

import json
import uuid

import db_backend
import pytest
from database import db_session
from db_backend import DBIntegrityError
from routes.sage_routes import _save_turn


@pytest.mark.parametrize("pooled", [True, False])
def test_pair_keeps_thread_and_metadata_and_failed_insert_rolls_back(monkeypatch, pooled):
    monkeypatch.setattr(db_backend, "POOL_ENABLED", pooled)
    thread = "atomic-" + uuid.uuid4().hex
    metadata = json.dumps({"proposed_action": False, "sources": ["synthetic"]})
    try:
        _save_turn("DOC001", "synthetic question", "synthetic answer", metadata, thread)
        with db_session() as db:
            rows = db.execute(
                "SELECT role, content, sources FROM sage_conversations "
                "WHERE conversation_id = ? ORDER BY id", (thread,),
            ).fetchall()
            assert [(row["role"], row["content"]) for row in rows] == [
                ("user", "synthetic question"), ("assistant", "synthetic answer"),
            ]
            assert rows[1]["sources"] == metadata
            # Exercise a real storage error after the user INSERT, rather
            # than mocking away the transaction this regression protects.
            db.execute("""CREATE TRIGGER capacity_test_reject_answer
                BEFORE INSERT ON sage_conversations
                WHEN NEW.content = 'synthetic rejected answer'
                BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END""")
        with pytest.raises(DBIntegrityError):
            _save_turn("DOC001", "question must roll back", "synthetic rejected answer", metadata, thread)
        with db_session() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM sage_conversations WHERE conversation_id = ?", (thread,),
            ).fetchone()[0] == 2
        _save_turn("DOC001", "cancelled synthetic question", "", None, thread)
        with db_session() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM sage_conversations WHERE conversation_id = ?", (thread,),
            ).fetchone()[0] == 3
    finally:
        with db_session() as db:
            db.execute("DROP TRIGGER IF EXISTS capacity_test_reject_answer")
            db.execute("DELETE FROM sage_conversations WHERE conversation_id = ?", (thread,))
