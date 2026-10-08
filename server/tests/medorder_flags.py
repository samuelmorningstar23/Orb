"""Re-run a lane suite with the medication-order store switched on and every
chart row it touches LINKED to an order (medorders-3, W5-E).

A pytest plugin, not a test module (the name does not start with test_).
Load it for one run:

    python -m pytest -p tests.medorder_flags tests/test_emar.py

and every test in that run sees:
  * ORB_MED_ORDER_STORE on (config.MED_ORDER_STORE_ENABLED), so the Vigil
    chart add writes through services/medorders/chart_lane.py;
  * ORB_MED_ORDER_LIFECYCLE on as well when the environment sets
    ORB_TEST_MED_ORDER_LIFECYCLE=1;
  * the patient's unlinked chart rows adopted into the store just before every
    bedside administration (emar_service.record_administration) and every
    Verify Queue decision (pharmacy_verification_service.verify_medication and
    reject_medication). The suites insert their medications rows directly, so
    without this the eMAR order check and the legacy mirror would only ever
    meet unlinked rows and prove nothing.

The run ends with one line counting the calls that met a chart row and how
many of those rows were linked to an order, so a green run also shows that it
exercised the store.

The suite's own expectations are NOT changed: the point of the run is that
every verdict, every refusal and every response is the one the suite already
expects with the store off (the design's flag promise and invariant I1).

Nothing is imported at load time: the conftest must set ORB_DB_PATH before
config or database is first imported, and -p plugins load before it.
"""
from __future__ import annotations

import functools
import os

import pytest


#: What the run met, printed at the end so a green store-on run also shows
#: that it reached LINKED rows (a run that only met unlinked rows proves
#: nothing about the store).
_STATS = {"calls": 0, "linked": 0, "chart_adds": 0}


def _adopt_patient_of(med_id) -> None:
    """Adopt the unlinked chart rows of the patient who owns `med_id`. Opens
    its own immediate sessions, so it must run outside any session, as the
    wrapped entry points are called."""
    from database import db_session
    from services.medorders import adoption
    if med_id is None:
        return
    with db_session() as db:
        row = db.execute("SELECT patient_id FROM medications WHERE id = ?", (med_id,)).fetchone()
    if row is None:
        return
    adoption.adopt_legacy(trigger="script", patient_ids=[row["patient_id"]])
    with db_session() as db:
        linked = db.execute("SELECT med_order_id FROM medications WHERE id = ?",
                            (med_id,)).fetchone()
    _STATS["calls"] += 1
    if linked is not None and linked["med_order_id"]:
        _STATS["linked"] += 1


def _counting_chart_adds(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        _STATS["chart_adds"] += 1
        return out
    return wrapper


def _adopting(fn, med_arg: str = "med_id"):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _adopt_patient_of(kwargs.get(med_arg, args[0] if args else None))
        return fn(*args, **kwargs)
    wrapper.__wrapped_by_medorder_flags__ = True  # type: ignore[attr-defined]
    return wrapper


@pytest.fixture(autouse=True)
def _medorder_store_on(monkeypatch):
    import config
    from services import emar_service, pharmacy_verification_service as pv
    from services.medorders import chart_lane

    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    if os.environ.get("ORB_TEST_MED_ORDER_LIFECYCLE", "").strip().lower() in ("1", "true", "yes", "on"):
        monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr(emar_service, "record_administration",
                        _adopting(emar_service.record_administration))
    monkeypatch.setattr(pv, "verify_medication", _adopting(pv.verify_medication))
    monkeypatch.setattr(pv, "reject_medication", _adopting(pv.reject_medication))
    monkeypatch.setattr(chart_lane, "create_chart_order",
                        _counting_chart_adds(chart_lane.create_chart_order))
    yield


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(
        f"medorder_flags: {_STATS['calls']} bedside or Verify Queue calls on an existing "
        f"chart row, {_STATS['linked']} of them on a row linked to a medication order; "
        f"{_STATS['chart_adds']} chart adds written through the store")
