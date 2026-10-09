"""Delta checks (lis-12, ruling LIS-R9 (a), the strictest option).

Pinned here:
  * Absolute and percent thresholds; a change at the threshold is flagged,
    one below it is not; a change from zero in percent terms is flagged.
  * The earlier result is the newest live one before this sample was
    collected, from any lane, read through lab_observations.previous_result:
    mixed observed_at spellings pick the right row; superseded, voided and
    simulated rows are passed over; a released laboratory result counts.
  * A bound (either side), a text earlier result, another unit, an earlier
    result outside the window or none at all: no flag, and the result says
    why. A test with no delta check configured carries none.
  * A flag needs a technologist's reason before technical validation; the
    reason is audited and shown to the signatory; a reason given against one
    earlier result never covers a different one; changing the value clears
    it.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import lab_module
from services import lab_master_service as lm
from services import lab_result_service as lrs
from services import orders_service, terminology_service
from services.clinical_time import format_utc, utc_iso_now

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
SUFFIX = uuid.uuid4().hex[:5].upper()


def _code(name):
    return f"W6FD{SUFFIX}-{name}"


TESTS = {
    # Potassium: absolute, 1.0 mmol/L in 48 hours.
    "K": {"name": "Delta potassium", "short_name": "Delta K", "decimal_places": 1,
          "unit": "mmol/L", "result_type": "numeric",
          "delta": {"delta_kind": "absolute", "delta_threshold_milli": 1000,
                    "delta_window_hours": 48}},
    # Creatinine: percent, 50% with no window.
    "CR": {"name": "Delta creatinine", "short_name": "Delta Cr", "decimal_places": 2,
           "unit": "mg/dL", "result_type": "numeric",
           "delta": {"delta_kind": "percent", "delta_threshold_milli": 50000}},
    # Sodium: no delta check configured.
    "NA": {"name": "Delta sodium", "short_name": "Delta Na", "decimal_places": 0,
           "unit": "mmol/L", "result_type": "numeric", "delta": None},
}


# ── fixtures ─────────────────────────────────────────────────────────────────

def _user(role, dept=None):
    uid = f"DLT{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, dept, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def master():
    uid, _ = _user("pathologist")
    return {"id": uid, "role": "pathologist"}


@pytest.fixture(scope="module", autouse=True)
def _catalogue(master):
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    for key, spec in TESTS.items():
        spec = dict(spec)
        delta = spec.pop("delta")
        lm.create_test({"code_system": "LOCAL", "code": _code(key), "kind": "analyte",
                        "section_id": "biochemistry", "specimen_type_code": "serum", **spec},
                       actor=master)
        if delta:
            lm.update_test(lm.test_id_for("LOCAL", _code(key)), delta, actor=master)
    created: list[str] = []
    yield created
    test_ids = [lm.test_id_for("LOCAL", _code(k)) for k in TESTS]
    tmarks = ", ".join("?" for _ in test_ids)
    with db_session() as db:
        if created:
            marks = ", ".join("?" for _ in created)
            ids = tuple(created)
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM observations WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_result_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_results WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' "
                       f"WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM lab_tests WHERE id IN ({tmarks})", tuple(test_ids))
        db.execute(f"DELETE FROM terminology WHERE code_system = 'LOCAL' AND code IN ({tmarks})",
                   tuple(_code(k) for k in TESTS))
    lab_module.refresh_drain_state()


@pytest.fixture
def lab_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", True)
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (utc_iso_now(), "test cutover")).lastrowid
    yield
    with db_session() as db:
        db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


@pytest.fixture
def patient(_catalogue):
    pid = f"PDLT{uuid.uuid4().hex[:8].upper()}"
    name = f"Delta {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-DLT-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex) "
                   "VALUES (?, ?, ?, 'admitted', 'GEN-1', ?, '1975-02-03', 'male')",
                   (pid, name, mrn, f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ADLT{uuid.uuid4().hex[:8].upper()}", pid, WARD))
    _catalogue.append(pid)
    return {"id": pid, "name": name, "mrn": mrn}


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


@pytest.fixture(scope="module")
def signer():
    return _user("pathologist")


def _received(p, phleb_hdr, tech_hdr, key="K"):
    order = orders_service.create_order(
        patient_id=p["id"], order_type="lab", description=key, code=_code(key),
        code_system="LOCAL", priority="routine", ordered_by=DOCTOR_ID, status="active")
    with db_session() as db:
        item = dict(db.execute("SELECT id, specimen_id FROM lab_order_items WHERE order_id = ?",
                               (order["id"],)).fetchone())
    identity = {"uhid": p["mrn"], "name": p["name"]}
    assert client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [item["specimen_id"]], "identity": identity},
                       headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{item['specimen_id']}/collect",
                       json={"identity": identity}, headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{item['specimen_id']}/receive", json={},
                       headers=tech_hdr).status_code == 200
    return order, item["specimen_id"], item["id"]


def _enter(item_id, value, hdr):
    resp = client.post("/api/lab/results", json={"order_item_id": item_id, "value": value},
                       headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _seen(result):
    return {"value_text": result["value_text"], "comment": result.get("comment"),
            "abnormal_flag": result.get("abnormal_flag")}


def _validate(result, hdr):
    return client.post("/api/lab/results/tech-validate",
                       json={"results": [{"result_id": result["id"], "expected": _seen(result)}]},
                       headers=hdr)


def _ack(result, reason, hdr):
    return client.post(f"/api/lab/results/{result['id']}/delta-ack",
                       json={"reason": reason, "expected": _seen(result)}, headers=hdr)


def _earlier(p, key, value, observed_at, *, unit=None, status="final", source="ingested_hl7",
             value_text=None):
    """An interfaced result already on the chart."""
    spec = TESTS[key]
    with db_session() as db:
        return int(db.execute(
            """INSERT INTO observations
               (patient_id, source, observation_code, observation_code_system,
                observation_name, value_text, value_numeric, unit, status, observed_at)
               VALUES (?, ?, ?, 'LOCAL', ?, ?, ?, ?, ?, ?)""",
            (p["id"], source, _code(key), spec["name"], value_text, value,
             spec["unit"] if unit is None else unit, status, observed_at)).lastrowid)


def _ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# ── 1. The rule ─────────────────────────────────────────────────────────────

def _check(value_milli, previous_value, *, kind="absolute", threshold=1000, window=None,
           hours=1.0, unit="mmol/L", previous_unit="mmol/L", comparator=None, dp=1):
    previous = None if previous_value is False else {
        "id": 7, "value_numeric": previous_value, "value_text": None, "unit": previous_unit,
        "observed_at": "2026-10-09T01:00:00Z", "source": "ingested_hl7", "lab_result_id": None}
    return lrs.delta_check(value_milli=value_milli, comparator=comparator,
                           value_text="x", unit=unit, decimal_places=dp, kind=kind,
                           threshold_milli=threshold, window_hours=window, previous=previous,
                           hours_before=hours)


@pytest.mark.parametrize("value, before, status", [
    (5000, 4.0, "flagged"),     # +1.0, exactly the threshold
    (4900, 4.0, "within"),      # +0.9
    (3000, 4.0, "flagged"),     # -1.0
    (5500, 4.0, "flagged"),
])
def test_an_absolute_threshold(value, before, status):
    assert _check(value, before)["status"] == status


@pytest.mark.parametrize("value, before, status", [
    (1500, 1.00, "flagged"),    # +50%
    (1490, 1.00, "within"),     # +49%
    (500, 1.00, "flagged"),     # -50%
    (10, 0.0, "flagged"),       # any change from zero
    (0, 0.0, "within"),
])
def test_a_percent_threshold(value, before, status):
    out = _check(value, before, kind="percent", threshold=50000, dp=2, unit="mg/dL",
                 previous_unit="mg/dL")
    assert out["status"] == status


def test_the_change_is_judged_at_the_reporting_precision():
    # 4.04 prints as 4.0 on a one-place test, so 5.0 is a change of exactly 1.0.
    assert _check(5000, 4.04)["status"] == "flagged"
    assert _check(5000, 4.06)["status"] == "within"


@pytest.mark.parametrize("over, status", [
    ({"comparator": "<", "value_milli": None}, "bound"),
    ({"before": False}, "no_previous"),
    ({"window": 24, "hours": 30.0}, "outside_window"),
    ({"previous_unit": "mEq/L"}, "unit_mismatch"),
    ({"previous_unit": ""}, "unit_mismatch"),
    ({"before": None}, "previous_not_numeric"),
])
def test_what_is_not_compared_says_why(over, status):
    args = {"value_milli": 9000, "before": 4.0}
    args.update(over)
    value = args.pop("value_milli")
    before = args.pop("before")
    out = _check(value, before, **args)
    assert out["status"] == status
    assert out["note"]


def test_inside_the_window_is_compared():
    assert _check(9000, 4.0, window=24, hours=24.0)["status"] == "flagged"


def test_a_unit_differing_only_in_capitals_or_spaces_is_the_same_unit():
    assert _check(9000, 4.0, unit="mmol/L", previous_unit="MMOL / l")["status"] == "flagged"


# ── 2. The earlier result, from any lane ────────────────────────────────────

def test_mixed_spellings_pick_the_newest_earlier_result(lab_on, patient, tech, phleb):
    # As instants the newest is the naive 1-hour-ago row. As strings, the
    # +05:30 row (3 hours ago) sorts first and would be wrongly chosen.
    offset = _ago(3).astimezone(timezone(timedelta(hours=5, minutes=30)))
    wrong = _earlier(patient, "K", 5.4, offset.isoformat())
    _earlier(patient, "K", 5.3, format_utc(_ago(2)))
    right = _earlier(patient, "K", 4.0, _ago(1).strftime("%Y-%m-%d %H:%M:%S"))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    assert result["delta"]["previous"]["observation_id"] == right != wrong
    assert result["delta"]["status"] == "flagged"
    assert result["delta_flag"] == 1 and result["needs_delta_reason"] is True
    assert result["delta"]["change_text"] == "+1.5 mmol/L"
    assert result["delta"]["threshold_text"] == "1 mmol/L"


def test_superseded_voided_and_simulated_rows_are_passed_over(lab_on, patient, tech, phleb):
    right = _earlier(patient, "K", 4.0, format_utc(_ago(5)))
    _earlier(patient, "K", 5.4, format_utc(_ago(4)), status="superseded")
    _earlier(patient, "K", 5.4, format_utc(_ago(3)), status="W")
    _earlier(patient, "K", 5.4, format_utc(_ago(2)), source="simulated")
    later = _earlier(patient, "K", 5.4, format_utc(_ago(1)))
    with db_session() as db:
        db.execute("UPDATE observations SET superseded_by = ? WHERE id = ?", (right, later))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    assert result["delta"]["previous"]["observation_id"] == right
    assert result["delta"]["status"] == "flagged"


def test_a_result_after_this_collection_is_not_the_earlier_one(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(2)))
    _, specimen_id, item_id = _received(patient, phleb[1], tech[1])
    # A later interfaced value, observed after this sample was collected.
    _earlier(patient, "K", 5.5, format_utc(datetime.now(timezone.utc) + timedelta(minutes=5)))
    result = _enter(item_id, "4.2", tech[1])
    assert result["delta"]["status"] == "within"


def test_an_earlier_bound_is_not_compared(lab_on, patient, tech, phleb):
    _earlier(patient, "K", None, format_utc(_ago(1)), value_text="<1.0")
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "6.0", tech[1])
    assert result["delta"]["status"] == "previous_not_numeric"
    assert result["delta_flag"] == 0 and result["needs_delta_reason"] is False
    assert "<1.0" in result["delta"]["note"]


def test_a_bound_now_is_not_compared(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, ">9.0", tech[1])
    assert result["delta"]["status"] == "bound" and result["delta_flag"] == 0


def test_an_earlier_result_in_another_unit_is_not_compared(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)), unit="mEq/L")
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "6.0", tech[1])
    assert result["delta"]["status"] == "unit_mismatch" and result["delta_flag"] == 0


def test_an_earlier_result_outside_the_window_raises_no_flag(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(60)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "6.0", tech[1])
    assert result["delta"]["status"] == "outside_window" and result["delta_flag"] == 0


def test_no_window_compares_any_earlier_result(lab_on, patient, tech, phleb):
    _earlier(patient, "CR", 1.0, format_utc(_ago(24 * 400)))
    _, _, item_id = _received(patient, phleb[1], tech[1], "CR")
    result = _enter(item_id, "2.10", tech[1])
    assert result["delta"]["status"] == "flagged"
    assert result["delta"]["percent_text"] == "+110%"


def test_a_test_with_no_delta_check_carries_none(lab_on, patient, tech, phleb):
    _earlier(patient, "NA", 120.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1], "NA")
    result = _enter(item_id, "150", tech[1])
    assert result["delta"] is None and result["delta_flag"] == 0


def test_a_released_laboratory_result_is_an_earlier_result(lab_on, patient, tech, phleb, signer):
    _, first_specimen, first_item = _received(patient, phleb[1], tech[1])
    first = _enter(first_item, "4.0", tech[1])
    validated = _validate(first, tech[1]).json()["results"][0]
    assert client.post("/api/lab/results/authorise",
                       json={"specimen_id": first_specimen,
                             "results": [{"result_id": validated["id"],
                                          "expected": _seen(validated)}]},
                       headers=signer[1]).status_code == 200
    _, _, second_item = _received(patient, phleb[1], tech[1])
    second = _enter(second_item, "5.2", tech[1])
    assert second["delta"]["status"] == "flagged"
    assert second["delta"]["previous"]["from_laboratory"] is True
    assert second["delta"]["previous"]["value_text"] == "4.0"


# ── 3. The reason before validation ─────────────────────────────────────────

def test_an_unexplained_delta_blocks_validation_and_a_reason_unblocks_it(
        lab_on, patient, tech, phleb, signer):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    refused = _validate(result, tech[1])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "delta_reason_required"
    with db_session() as db:
        assert db.execute("SELECT status FROM lab_results WHERE id = ?",
                          (result["id"],)).fetchone()[0] == "entered"
    assert _ack(result, "  ", tech[1]).status_code == 422
    acked = _ack(result, "Patient on potassium replacement since yesterday", tech[1])
    assert acked.status_code == 200, acked.text
    body = acked.json()
    assert body["acknowledged"] is True
    assert body["result"]["delta_ack"]["reason"] == (
        "Patient on potassium replacement since yesterday")
    assert body["result"]["needs_delta_reason"] is False
    with db_session() as db:
        audit = dict(db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'lab_delta_acknowledged' "
            "AND target_id = ?", (str(result["id"]),)).fetchone())
        event = db.execute("SELECT after_json FROM lab_result_events WHERE result_id = ? "
                           "AND event_type = 'delta_acknowledged'", (result["id"],)).fetchone()
    assert audit["event_category"] == "clinical_access" and audit["actor_user_id"] == tech[0]
    assert json.loads(audit["metadata_json"])["reason"].startswith("Patient on potassium")
    assert json.loads(event[0])["basis"]["status"] == "flagged"
    again = _ack(body["result"], "Second reason", tech[1])
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_acknowledged"
    validated = _validate(body["result"], tech[1])
    assert validated.status_code == 200, validated.text
    # The signatory sees the flag and the reason.
    queues = client.get("/api/lab/validation", headers=signer[1]).json()
    entry = next(r for g in queues["authorisation"] for r in g["results"]
                 if r["result_id"] == result["id"])
    assert entry["delta"]["status"] == "flagged"
    assert entry["delta_ack"]["reason"] == "Patient on potassium replacement since yesterday"
    assert entry["delta_ack"]["by_name"]


def test_a_reason_against_one_earlier_result_does_not_cover_another(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(3)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    acked = _ack(result, "Known trend", tech[1]).json()["result"]
    # A newer earlier result arrives late on the interface, still before this
    # sample was collected, and it is also a big change.
    newer = _earlier(patient, "K", 3.9, format_utc(_ago(1)))
    refused = _validate(acked, tech[1])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "delta_reason_required"
    second = _ack(acked, "Rechecked against the newer value", tech[1])
    assert second.status_code == 200 and second.json()["acknowledged"] is True
    assert second.json()["result"]["delta"]["previous"]["observation_id"] == newer
    assert _validate(second.json()["result"], tech[1]).status_code == 200


def test_a_reason_no_longer_needed_is_said_and_not_recorded(lab_on, patient, tech, phleb):
    earlier = _earlier(patient, "K", 4.0, format_utc(_ago(3)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    with db_session() as db:
        # The earlier result is withdrawn by its sender before the reason.
        db.execute("UPDATE observations SET status = 'W' WHERE id = ?", (earlier,))
    resp = _ack(result, "Reason", tech[1])
    assert resp.status_code == 200 and resp.json()["acknowledged"] is False
    assert resp.json()["result"]["delta_flag"] == 0
    assert resp.json()["result"]["delta_ack"] is None
    assert _validate(resp.json()["result"], tech[1]).status_code == 200


def test_changing_the_value_clears_the_reason(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    acked = _ack(result, "Known trend", tech[1]).json()["result"]
    comment_only = client.patch(f"/api/lab/results/{acked['id']}",
                                json={"value": "5.5", "comment": "Repeat confirmed",
                                      "expected": _seen(acked)}, headers=tech[1])
    assert comment_only.status_code == 200, comment_only.text
    kept = comment_only.json()["result"]
    assert kept["delta_ack"] is not None and kept["needs_delta_reason"] is False
    changed = client.patch(f"/api/lab/results/{kept['id']}",
                           json={"value": "5.7", "comment": "Repeat confirmed",
                                 "expected": _seen(kept)}, headers=tech[1])
    assert changed.status_code == 200, changed.text
    cleared = changed.json()["result"]
    assert cleared["delta_ack"] is None and cleared["needs_delta_reason"] is True
    assert _validate(cleared, tech[1]).status_code == 409


def test_a_reason_from_a_stale_screen_or_after_validation_is_refused(lab_on, patient, tech,
                                                                     phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    stale = _ack({**result, "value_text": "5.4"}, "Known trend", tech[1])
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "result_changed"
    acked = _ack(result, "Known trend", tech[1]).json()["result"]
    validated = _validate(acked, tech[1]).json()["results"][0]
    late = _ack(validated, "Late reason", tech[1])
    assert late.status_code == 409 and late.json()["detail"]["error"] == "already_validated"


def test_only_result_staff_give_a_delta_reason(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    assert _ack(result, "Known trend", phleb[1]).status_code == 403


def test_the_person_who_explained_the_delta_cannot_authorise_it(lab_on, patient, tech, phleb,
                                                                signer):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, specimen_id, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    acked = _ack(result, "Known trend", signer[1])
    assert acked.status_code == 200, acked.text
    validated = _validate(acked.json()["result"], tech[1]).json()["results"][0]
    refused = client.post("/api/lab/results/authorise",
                          json={"specimen_id": specimen_id,
                                "results": [{"result_id": validated["id"],
                                             "expected": _seen(validated)}]},
                          headers=signer[1])
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "second_person_required"


def test_the_worklist_shows_the_delta(lab_on, patient, tech, phleb):
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    _enter(item_id, "5.5", tech[1])
    bench = client.get("/api/lab/worklists/biochemistry", headers=tech[1]).json()
    row = next(i for i in bench["items"] if i["item_id"] == item_id)
    assert row["result"]["delta"]["status"] == "flagged"
    assert row["result"]["needs_delta_reason"] is True


def test_the_validate_queue_shows_the_check_validation_will_apply(lab_on, patient, tech, phleb):
    """A value stored by a lane that did not check it (an analyser intake,
    say) carries no delta on its row. The queue decides the check as
    technical validation will, so the screen asks for the reason the server
    is going to require."""
    _earlier(patient, "K", 4.0, format_utc(_ago(1)))
    _, _, item_id = _received(patient, phleb[1], tech[1])
    result = _enter(item_id, "5.5", tech[1])
    with db_session() as db:
        db.execute("UPDATE lab_results SET delta_flag = 0, delta_json = NULL WHERE id = ?",
                   (result["id"],))
    queues = client.get("/api/lab/validation", headers=tech[1]).json()
    entry = next(r for g in queues["technical"] for r in g["results"]
                 if r["result_id"] == result["id"])
    assert entry["delta"]["status"] == "flagged" and entry["needs_delta_reason"] is True
    assert _validate(result, tech[1]).status_code == 409
    acked = _ack(result, "Known trend", tech[1])
    assert acked.status_code == 200 and acked.json()["acknowledged"] is True
    queues = client.get("/api/lab/validation", headers=tech[1]).json()
    entry = next(r for g in queues["technical"] for r in g["results"]
                 if r["result_id"] == result["id"])
    assert entry["needs_delta_reason"] is False and entry["delta_ack"]["reason"] == "Known trend"
    assert _validate(acked.json()["result"], tech[1]).status_code == 200
