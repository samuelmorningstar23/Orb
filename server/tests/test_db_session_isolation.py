"""db_session transaction isolation — the read-then-write guard hazard.

Orb's clinical guards are all shaped "SELECT the row, check it, then write":
emar_service.record_administration checks `medications.status == 'active'` and
screens allergies before inserting the dose; orders/helix transitions check the
current state before writing the next one.

Python's sqlite3 in legacy mode opens a transaction only before DML, so those
SELECTs run in AUTOCOMMIT — outside the transaction the later INSERT takes.
The first test CHARACTERISES that (it is the hazard, stated as an executable
fact so nobody has to rediscover it); the second proves `immediate=True`
removes it.

Pre-fix: test_immediate_session_serialises_read_then_write FAILED with
`TypeError: db_session() got an unexpected keyword argument 'immediate'`.
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session  # noqa: E402

_TABLE = "toctou_probe"


@pytest.fixture(autouse=True)
def _probe_table():
    with db_session() as db:
        db.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} "
                   "(id INTEGER PRIMARY KEY, status TEXT, doses INTEGER DEFAULT 0)")
        db.execute(f"DELETE FROM {_TABLE}")
        db.execute(f"INSERT INTO {_TABLE} (id, status) VALUES (1, 'active')")
    yield
    with db_session() as db:
        db.execute(f"DELETE FROM {_TABLE}")


def _run_guard(immediate: bool, read_done: threading.Event,
               may_write: threading.Event, out: dict):
    """The clinical guard shape: read the state, decide, then write."""
    try:
        kw = {"immediate": True} if immediate else {}
        with db_session(**kw) as db:
            row = db.execute(f"SELECT status FROM {_TABLE} WHERE id = 1").fetchone()
            out["read_status"] = row["status"]
            read_done.set()
            may_write.wait(10)
            if out["read_status"] == "active":
                db.execute(f"UPDATE {_TABLE} SET doses = doses + 1 WHERE id = 1")
                out["wrote"] = True
    except Exception as e:  # surfaced by the caller
        out["err"] = f"{type(e).__name__}: {e}"


def _interloper(started: threading.Event, out: dict):
    """The doctor on another tablet, discontinuing the order mid-guard."""
    try:
        with db_session() as db:
            db.execute(f"UPDATE {_TABLE} SET status = 'discontinued' WHERE id = 1")
        out["interloper_committed"] = True
    except Exception as e:
        out["interloper_err"] = f"{type(e).__name__}: {str(e)[:80]}"
    finally:
        started.set()


def test_default_session_does_not_serialise_read_then_write():
    """CHARACTERISATION, not an endorsement: with the default session the
    interloper commits INSIDE the guard's block and the guard writes anyway,
    having decided on state that is no longer true.

    If this test ever starts failing because the interloper is now blocked,
    somebody made every db_session take a write lock — check that was intended
    (it serialises the whole appliance behind one writer)."""
    out = {}
    read_done, may_write, interloper_done = (threading.Event() for _ in range(3))

    guard = threading.Thread(target=_run_guard, args=(False, read_done, may_write, out))
    guard.start()
    assert read_done.wait(10), "guard never completed its read"

    threading.Thread(target=_interloper, args=(interloper_done, out)).start()
    assert interloper_done.wait(10), "interloper thread hung"

    may_write.set()
    guard.join(10)

    assert out.get("err") is None, out
    assert out["read_status"] == "active"
    assert out.get("interloper_committed") is True, \
        "interloper could not write during the guard's session"
    assert out.get("wrote") is True

    with db_session() as db:
        row = db.execute(f"SELECT status, doses FROM {_TABLE} WHERE id = 1").fetchone()
    # The hazard in one line: a dose recorded against a discontinued order.
    assert row["status"] == "discontinued" and row["doses"] == 1


def test_immediate_session_serialises_read_then_write():
    """With immediate=True the guard holds the write lock from the start, so the
    interloper cannot land between the read and the write. Whatever the guard
    read is still true when it writes."""
    out = {}
    read_done, may_write, interloper_done = (threading.Event() for _ in range(3))

    guard = threading.Thread(target=_run_guard, args=(True, read_done, may_write, out))
    guard.start()
    assert read_done.wait(10), "guard never completed its read"

    threading.Thread(target=_interloper, args=(interloper_done, out)).start()
    # The interloper must NOT get through while the guard holds the lock.
    assert not interloper_done.wait(1.5), \
        "interloper committed during an immediate session — the write lock did not hold"

    may_write.set()
    guard.join(10)
    assert interloper_done.wait(10), "interloper never completed after the guard released"

    assert out.get("err") is None, out
    assert out.get("wrote") is True
    assert out.get("interloper_err") is None, out

    with db_session() as db:
        doses = db.execute(f"SELECT doses FROM {_TABLE} WHERE id = 1").fetchone()["doses"]
    assert doses == 1


def test_immediate_is_rejected_on_a_nested_session():
    """A nested frame is a SAVEPOINT inside someone else's DEFERRED transaction
    and cannot be upgraded — asking must raise, not silently no-op."""
    with db_session() as db:
        db.execute(f"UPDATE {_TABLE} SET doses = doses WHERE id = 1")  # force a txn open
        with pytest.raises(RuntimeError, match="OUTERMOST"):
            with db_session(immediate=True):
                pass


def test_immediate_session_rolls_back_on_error():
    """The commit/rollback contract is unchanged by the flag."""
    with pytest.raises(ValueError):
        with db_session(immediate=True) as db:
            db.execute(f"UPDATE {_TABLE} SET doses = 99 WHERE id = 1")
            raise ValueError("boom")
    with db_session() as db:
        assert db.execute(f"SELECT doses FROM {_TABLE} WHERE id = 1").fetchone()["doses"] == 0
