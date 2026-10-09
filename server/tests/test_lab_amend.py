"""Amended and withdrawn laboratory results (lis-13; ruling LIS-R10 (a)
applied as the build default, go-live held).

Pinned here:
  * Only a signatory (lab.authorise) amends or withdraws, and always with a
    reason. A result not yet reported is changed on the bench instead.
  * While two different people are required (LIS-R5, the shipped default)
    an amendment is proposed by one signatory and applied only when a
    DIFFERENT signatory confirms it; the chart keeps the reported value
    until then. With the setting off one signatory amends in one step and
    it is recorded as one person.
  * An amendment: version n+1 released, the earlier version superseded,
    the chart's old observation superseded and pointing at a new one with
    status 'corrected', the order still resulted with its fixed result
    text, an order_events row, history in lab_result_events, a fail-closed
    audit row. The chart and the ward pack show only the new value.
  * A withdrawal: the value is gone from every chart reader, its test is
    cancelled, an order whose only reported test it was stays resulted with
    an exception for the laboratory; a wrong-patient withdrawal prompts a
    NABH incident while ORB_NABH is on.
  * After either commits, a plain notice goes to the patient's case room;
    a failed notice never undoes the change.
  * An amendment past a critical limit raises a critical; a critical raised
    for the earlier value is never closed by the change.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import case_room_alerts, lab_module, orders_service, site_config_service
from services import lab_master_service as lm
from services import lab_observations as lobs
from services import lab_order_service as los
from services import lab_result_service as lrs
from services import terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now
from services.site_config_service import SiteConfig

client = TestClient(main.app)

WARD = "General Medicine"
SUFFIX = uuid.uuid4().hex[:5].upper()


def _code(key):
    return f"W7HA{SUFFIX}-{key}"


TESTS = {
    "K": {"name": "Amend potassium", "short_name": "Amd K", "decimal_places": 1,
          "unit": "mmol/L", "result_type": "numeric"},
    "NA": {"name": "Amend sodium", "short_name": "Amd Na", "decimal_places": 0,
           "unit": "mmol/L", "result_type": "numeric"},
}
PANEL = "PNL"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _user(role, dept=None):
    uid = f"AMD{uuid.uuid4().hex[:6].upper()}"
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
        lm.create_test({"code_system": "LOCAL", "code": _code(key), "kind": "analyte",
                        "section_id": "biochemistry", "specimen_type_code": "serum", **spec},
                       actor=master)
    lm.create_test({"code_system": "LOCAL", "code": _code(PANEL), "kind": "panel",
                    "name": "Amend mini panel", "short_name": "Amd panel",
                    "section_id": "biochemistry", "specimen_type_code": "serum"}, actor=master)
    lm.set_panel_components(lm.test_id_for("LOCAL", _code(PANEL)),
                            [lm.test_id_for("LOCAL", _code("K")),
                             lm.test_id_for("LOCAL", _code("NA"))], actor=master)
    created: list[str] = []
    yield created
    keys = list(TESTS) + [PANEL]
    test_ids = tuple(lm.test_id_for("LOCAL", _code(k)) for k in keys)
    tmarks = ", ".join("?" for _ in test_ids)
    with db_session() as db:
        if created:
            marks = ", ".join("?" for _ in created)
            ids = tuple(created)
            db.execute(f"DELETE FROM lab_critical_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_critical_notifications WHERE patient_id IN ({marks})",
                       ids)
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM observations WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_result_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_results WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM messages WHERE channel_id IN "
                       f"(SELECT id FROM channels WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM channels WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' "
                       f"WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM lab_critical_limits WHERE test_id IN ({tmarks})", test_ids)
        db.execute(f"DELETE FROM lab_panel_components WHERE panel_test_id IN ({tmarks})",
                   test_ids)
        db.execute(f"DELETE FROM lab_tests WHERE id IN ({tmarks})", test_ids)
        db.execute(f"DELETE FROM terminology WHERE code_system = 'LOCAL' AND code IN ({tmarks})",
                   tuple(_code(k) for k in keys))
    lab_module.refresh_drain_state()


@pytest.fixture
def lab_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", True)
    monkeypatch.setattr(site_config_service, "_loaded", SiteConfig())
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (utc_iso_now(), "test cutover")).lastrowid
    yield
    with db_session() as db:
        db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


@pytest.fixture
def one_person(monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(case_room_alerts, "push_message_new",
                        lambda msg, room, *, what="": sent.append((msg, room)) or True)
    return sent


@pytest.fixture
def potassium_limit(master):
    test_id = lm.test_id_for("LOCAL", _code("K"))
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test_id,))
    row = lm.approve_critical_limit(test_id, {"critical_low": "2.5", "critical_high": "6.5",
                                              "agreed_with": "Dr Ward, physician lead",
                                              "method_note": "Amend test limit"},
                                    actor=master)
    yield row
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test_id,))


def _patient(created, *, case_room=True):
    pid = f"PAMD{uuid.uuid4().hex[:8].upper()}"
    name = f"Amend {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-AMD-{uuid.uuid4().hex[:8].upper()}"
    room = f"amd-{pid.lower()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex) "
                   "VALUES (?, ?, ?, 'admitted', 'GM-1', ?, '1970-01-01', 'female')",
                   (pid, name, mrn, f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"AAMD{uuid.uuid4().hex[:8].upper()}", pid, WARD))
        if case_room:
            db.execute("INSERT INTO channels (id, name, type, patient_id) "
                       "VALUES (?, ?, 'case_room', ?)", (room, f"Case {name}", pid))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn, "room": room if case_room else None}


@pytest.fixture
def patient(_catalogue):
    return _patient(_catalogue)


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


@pytest.fixture(scope="module")
def signer():
    return _user("pathologist")


@pytest.fixture(scope="module")
def second_signer():
    return _user("pathologist")


def _seen(result):
    return {"value_text": result["value_text"], "comment": result.get("comment"),
            "abnormal_flag": result.get("abnormal_flag")}


def _taken_in(p, phleb_hdr, tech_hdr, order):
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, specimen_id, test_id FROM lab_order_items WHERE order_id = ? ORDER BY id",
            (order["id"],)).fetchall()]
    specimen_id = rows[0]["specimen_id"]
    identity = {"uhid": p["mrn"], "name": p["name"]}
    assert client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [specimen_id], "identity": identity},
                       headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{specimen_id}/collect",
                       json={"identity": identity}, headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{specimen_id}/receive", json={},
                       headers=tech_hdr).status_code == 200
    return specimen_id, {r["test_id"]: r["id"] for r in rows}


def _release(item_id, specimen_id, value, tech, signer):
    entered = client.post("/api/lab/results", json={"order_item_id": item_id, "value": value},
                          headers=tech[1])
    assert entered.status_code == 200, entered.text
    entered = entered.json()["result"]
    validated = client.post("/api/lab/results/tech-validate",
                            json={"results": [{"result_id": entered["id"],
                                               "expected": _seen(entered)}]}, headers=tech[1])
    assert validated.status_code == 200, validated.text
    validated = validated.json()["results"][0]
    released = client.post("/api/lab/results/authorise",
                           json={"specimen_id": specimen_id,
                                 "results": [{"result_id": validated["id"],
                                              "expected": _seen(validated)}]},
                           headers=signer[1])
    assert released.status_code == 200, released.text
    return released.json()["results"][0]


def _reported(p, tech, phleb, signer, value="4.2", key="K"):
    """One order for one test, released. Returns (order, specimen_id, result)."""
    order = orders_service.create_order(
        patient_id=p["id"], order_type="lab", description=key, code=_code(key),
        code_system="LOCAL", priority="routine", ordered_by="D001", status="active")
    specimen_id, items = _taken_in(p, phleb[1], tech[1], order)
    result = _release(items[lm.test_id_for("LOCAL", _code(key))], specimen_id, value, tech,
                      signer)
    return order, specimen_id, result


def _amend(result, hdr, value="6.0", reason="Transcription error on the bench sheet", **extra):
    return client.post(f"/api/lab/results/{result['id']}/amend",
                       json={"value": value, "reason": reason, "expected": _seen(result),
                             **extra}, headers=hdr)


def _confirm(result, proposal, hdr):
    return client.post(f"/api/lab/results/{result['id']}/amend/confirm",
                       json={"proposal_id": proposal["proposal_id"], "expected": _seen(result),
                             "proposed": {"value_text": proposal["value_text"],
                                          "comment": proposal.get("comment")}}, headers=hdr)


def _withdraw(result, hdr, reason="Result belonged to another patient", **extra):
    return client.post(f"/api/lab/results/{result['id']}/withdraw",
                       json={"reason": reason, "expected": _seen(result), **extra}, headers=hdr)


def _rows(sql, params=()):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _chart(pid):
    with db_session() as db:
        chart = lobs.chart_lab_rows(db, pid, per_analyte=10, max_rows=50)
        pack = lobs.latest_per_analyte(db, pid, limit=50)
        recent = lobs.recent_observations(db, pid, limit=50)
    return [dict(r) for r in chart], [dict(r) for r in pack], [dict(r) for r in recent]


def _note(nid):
    return _rows("SELECT * FROM lab_critical_notifications WHERE id = ?", (nid,))[0]


def _notes(pid):
    return _rows("SELECT * FROM lab_critical_notifications WHERE patient_id = ? ORDER BY rowid",
                 (pid,))


def _view(tech, nid):
    """One critical as the laboratory's Critical results screen lists it."""
    body = client.get("/api/lab/criticals", headers=tech[1]).json()
    found = [n for n in body["open"] + body["recent"] if n["id"] == nid]
    assert len(found) == 1, nid
    return found[0]


def _callback(nid, tech, value_told):
    return client.post(f"/api/lab/criticals/{nid}/callback",
                       json={"recipient_name": "Sister Mary Thomas",
                             "recipient_role": "nurse_in_charge", "method": "phone",
                             "read_back": True, "note": "Read back", "value_told": value_told},
                       headers=tech[1])


def _two_tier_ladder(monkeypatch):
    ladder = [{"tier": 1, "role": "Ward nurse in charge", "label": "Ward nurse in charge",
               "timeout_minutes": 15},
              {"tier": 2, "role": "Duty doctor", "label": "Duty doctor", "timeout_minutes": None}]
    monkeypatch.setattr(site_config_service, "_loaded",
                        SiteConfig(present=True, lab_critical={"tiers": ladder}))


def _in_16_minutes():
    from datetime import timedelta

    from services.clinical_time import format_utc, parse_to_utc
    return format_utc(parse_to_utc(utc_iso_now()) + timedelta(minutes=16))


# ── 1. Who, and why ──────────────────────────────────────────────────────────

def test_only_a_signatory_amends_or_withdraws_and_always_with_a_reason(
        lab_on, patient, tech, phleb, signer, pushes):
    _, _, result = _reported(patient, tech, phleb, signer)
    assert _amend(result, tech[1]).status_code == 403
    assert _amend(result, phleb[1]).status_code == 403
    assert _withdraw(result, tech[1]).status_code == 403
    no_reason = _amend(result, signer[1], reason="   ")
    assert no_reason.status_code == 422
    assert no_reason.json()["detail"]["error"] == "reason_required"
    assert _withdraw(result, signer[1], reason="").json()["detail"]["error"] == "reason_required"
    assert _rows("SELECT status FROM lab_results WHERE id = ?", (result["id"],))[0]["status"] == \
        "released"


def test_a_result_not_yet_reported_is_changed_on_the_bench_instead(lab_on, patient, tech, phleb,
                                                                   signer, pushes):
    order = orders_service.create_order(
        patient_id=patient["id"], order_type="lab", description="K", code=_code("K"),
        code_system="LOCAL", priority="routine", ordered_by="D001", status="active")
    _, items = _taken_in(patient, phleb[1], tech[1], order)
    entered = client.post("/api/lab/results",
                          json={"order_item_id": list(items.values())[0], "value": "4.0"},
                          headers=tech[1]).json()["result"]
    refused = _amend(entered, signer[1])
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "not_released"


def test_nothing_to_amend_and_a_stale_screen_are_refused(lab_on, patient, tech, phleb, signer,
                                                         pushes):
    _, _, result = _reported(patient, tech, phleb, signer)
    same = _amend(result, signer[1], value="4.2")
    assert same.status_code == 422 and same.json()["detail"]["error"] == "nothing_to_amend"
    stale = client.post(f"/api/lab/results/{result['id']}/amend",
                        json={"value": "5.0", "reason": "x reason",
                              "expected": {**_seen(result), "value_text": "9.9"}},
                        headers=signer[1])
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "result_changed"


# ── 2. Two signatories (LIS-R5 default) ──────────────────────────────────────

def test_an_amendment_waits_for_a_second_signatory(lab_on, patient, tech, phleb, signer,
                                                   second_signer, pushes):
    order, specimen_id, result = _reported(patient, tech, phleb, signer)
    proposed = _amend(result, signer[1])
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["proposed"] is True and body["amended"] is False
    proposal = body["proposal"]
    assert proposal["value_text"] == "6.0" and proposal["you_proposed"] is True
    assert proposal["may_confirm"] is False
    # The chart keeps the reported value until it is confirmed.
    chart, _, _ = _chart(patient["id"])
    assert [r["value_numeric"] for r in chart] == [4.2]
    assert _amend(result, second_signer[1]).json()["detail"]["error"] == "amendment_pending"
    mine = _confirm(result, proposal, signer[1])
    assert mine.status_code == 403
    assert mine.json()["detail"]["error"] == "second_person_required"
    confirmed = _confirm(result, proposal, second_signer[1])
    assert confirmed.status_code == 200, confirmed.text
    out = confirmed.json()
    assert out["amended"] is True
    new = out["result"]
    assert (new["version"], new["status"], new["value_text"], new["amend_reason"]) == (
        2, "released", "6.0", "Transcription error on the bench sheet")
    assert (new["entered_by"], new["released_by"]) == (signer[0], second_signer[0])
    event = _rows("SELECT after_json FROM lab_result_events WHERE result_id = ? AND "
                  "event_type = 'amended'", (new["id"],))[0]
    after = json.loads(event["after_json"])
    assert after["one_person"] is False and after["proposal_id"] == proposal["proposal_id"]


def test_a_proposal_can_be_cancelled_and_made_again(lab_on, patient, tech, phleb, signer,
                                                    second_signer, pushes):
    _, _, result = _reported(patient, tech, phleb, signer)
    proposal = _amend(result, signer[1]).json()["proposal"]
    cancelled = client.post(f"/api/lab/results/{result['id']}/amend/cancel",
                            json={"proposal_id": proposal["proposal_id"],
                                  "reason": "Wrong sample checked"}, headers=second_signer[1])
    assert cancelled.status_code == 200, cancelled.text
    late = _confirm(result, proposal, second_signer[1])
    assert late.status_code == 409 and late.json()["detail"]["error"] == "no_pending_amendment"
    again = _amend(result, signer[1], value="5.5")
    assert again.status_code == 200 and again.json()["proposal"]["value_text"] == "5.5"
    changed = client.post(f"/api/lab/results/{result['id']}/amend/confirm",
                          json={"proposal_id": again.json()["proposal"]["proposal_id"],
                                "expected": _seen(result),
                                "proposed": {"value_text": "6.0", "comment": None}},
                          headers=second_signer[1])
    assert changed.status_code == 409 and changed.json()["detail"]["error"] == "amendment_changed"


# ── 3. What an amendment writes ──────────────────────────────────────────────

def test_an_amendment_replaces_the_value_everywhere_and_keeps_the_order(
        lab_on, one_person, patient, tech, phleb, signer, pushes):
    order, specimen_id, result = _reported(patient, tech, phleb, signer)
    before = _rows("SELECT * FROM orders WHERE id = ?", (order["id"],))[0]
    assert before["status"] == "resulted" and before["result_text"] == los.LAB_RESULT_TEXT
    resp = _amend(result, signer[1], value="5.0")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    new = out["result"]
    assert out["amended"] is True and out["proposed"] is False
    assert (new["version"], new["status"], new["value_text"]) == (2, "released", "5.0")
    old = _rows("SELECT * FROM lab_results WHERE id = ?", (result["id"],))[0]
    assert (old["status"], old["superseded_by_result_id"]) == ("superseded", new["id"])
    # The chart's copy: the old row superseded and pointing at the corrected one.
    old_obs = _rows("SELECT * FROM observations WHERE id = ?", (old["observation_id"],))[0]
    new_obs = _rows("SELECT * FROM observations WHERE id = ?", (new["observation_id"],))[0]
    assert (old_obs["status"], old_obs["superseded_by"]) == ("superseded", new_obs["id"])
    assert (new_obs["status"], new_obs["lab_result_id"], new_obs["value_text"]) == (
        "corrected", new["id"], "5.0")
    assert new_obs["observed_at"] == old_obs["observed_at"], "the collection instant stands"
    chart, pack, recent = _chart(patient["id"])
    assert [r["value_numeric"] for r in chart] == [5.0]
    assert [lobs.display_value(r) for r in pack] == ["5.0"]
    assert [r["id"] for r in recent] == [new_obs["id"]]
    # The order: same status, same fixed text, and a note that it was amended.
    after = _rows("SELECT * FROM orders WHERE id = ?", (order["id"],))[0]
    assert (after["status"], after["result_text"]) == ("resulted", los.LAB_RESULT_TEXT)
    notes = _rows("SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (order["id"],))
    assert notes[-1]["prior_status"] == notes[-1]["new_status"] == "resulted"
    assert notes[-1]["reason"].startswith("Laboratory result amended")
    # History, both versions.
    kinds = {(e["result_id"], e["event_type"]) for e in _rows(
        "SELECT result_id, event_type FROM lab_result_events WHERE result_id IN (?, ?)",
        (result["id"], new["id"]))}
    assert {(result["id"], "released"), (result["id"], "superseded"),
            (new["id"], "amended")} <= kinds
    amended = json.loads(_rows("SELECT after_json FROM lab_result_events WHERE result_id = ? "
                               "AND event_type = 'amended'", (new["id"],))[0]["after_json"])
    assert amended["one_person"] is True and amended["reason"]
    audit = _rows("SELECT event_category, metadata_json FROM audit_logs WHERE action_type = "
                  "'lab_result_amended' AND target_id = ?", (str(new["id"]),))
    assert len(audit) == 1 and audit[0]["event_category"] == "clinical_access"
    meta = json.loads(audit[0]["metadata_json"])
    assert meta["previous_result_id"] == result["id"] and meta["one_person"] is True
    # A version already replaced cannot be amended again.
    stale = _amend(result, signer[1], value="5.1")
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "already_amended"


def test_an_amendment_whose_audit_fails_changes_nothing(lab_on, one_person, patient, tech,
                                                        phleb, signer, pushes, monkeypatch):
    _, _, result = _reported(patient, tech, phleb, signer)
    real = lrs.log_audit_event

    def failing(**kwargs):
        if kwargs.get("action_type") == "lab_result_amended":
            raise AuditWriteError("audit store down")
        return real(**kwargs)

    monkeypatch.setattr(lrs, "log_audit_event", failing)
    assert _amend(result, signer[1]).status_code == 503
    assert _rows("SELECT status FROM lab_results WHERE id = ?", (result["id"],))[0]["status"] == \
        "released"
    chart, _, _ = _chart(patient["id"])
    assert [r["value_numeric"] for r in chart] == [4.2]


# ── 4. The ward is told ──────────────────────────────────────────────────────

def test_the_ward_is_told_after_the_amendment_commits(lab_on, one_person, patient, tech, phleb,
                                                      signer, pushes):
    _, _, result = _reported(patient, tech, phleb, signer)
    out = _amend(result, signer[1], value="5.0").json()
    notice = out["ward_notice"]
    assert notice["posted"] is True
    msg = _rows("SELECT * FROM messages WHERE id = ?", (notice["message_id"],))[0]
    assert (msg["sender_id"], msg["sender_name"], msg["sender_role"]) == (
        signer[0], "Laboratory", "laboratory")
    assert (msg["message_type"], msg["is_bot"], msg["source"]) == ("text", 0, "lab")
    for words in (patient["name"], "amended a reported result", "was 4.2 mmol/L",
                  "now 5.0 mmol/L", "Transcription error on the bench sheet"):
        assert words in msg["content"], words
    assert "\u2014" not in msg["content"]
    assert any(room == patient["room"] for _msg, room in pushes)
    events = [e["event_type"] for e in _rows(
        "SELECT event_type FROM lab_result_events WHERE result_id = ? ORDER BY id",
        (out["result"]["id"],))]
    assert events[-1] == "ward_notified"


def test_a_failed_notice_does_not_undo_the_amendment(lab_on, one_person, patient, tech, phleb,
                                                     signer, pushes, monkeypatch):
    _, _, result = _reported(patient, tech, phleb, signer)

    def broken(*args, **kwargs):
        raise RuntimeError("relay is down")

    monkeypatch.setattr(case_room_alerts, "persist_case_room_alert", broken)
    resp = _amend(result, signer[1], value="5.0")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ward_notice"] == {"posted": False, "reason": "failed"}
    assert out["result"]["status"] == "released" and out["result"]["version"] == 2
    chart, _, _ = _chart(patient["id"])
    assert [r["value_numeric"] for r in chart] == [5.0]
    last = _rows("SELECT event_type FROM lab_result_events WHERE result_id = ? "
                 "ORDER BY id DESC LIMIT 1", (out["result"]["id"],))[0]
    assert last["event_type"] == "ward_notice_failed"


# ── 5. Critical values ───────────────────────────────────────────────────────

def test_an_amendment_past_a_critical_limit_raises_a_critical(lab_on, one_person, patient, tech,
                                                              phleb, signer, pushes,
                                                              potassium_limit):
    _, _, result = _reported(patient, tech, phleb, signer)
    out = _amend(result, signer[1], value="7.2").json()
    assert out["result"]["is_critical"] == 1 and out["result"]["abnormal_flag"] == "HH"
    (critical,) = out["criticals"]
    assert critical["posted"] is True
    note = _rows("SELECT * FROM lab_critical_notifications WHERE id = ?",
                 (critical["notification_id"],))[0]
    assert (note["result_id"], note["preliminary"], note["status"]) == (
        out["result"]["id"], 0, "open")
    post = _rows("SELECT content FROM messages WHERE id = ?", (note["relay_message_id"],))[0]
    assert "Amended result" in post["content"]


def test_a_critical_for_the_earlier_value_stays_open_and_records_the_amendment(
        lab_on, one_person, patient, tech, phleb, signer, pushes, potassium_limit):
    """Round 3 review: the amendment goes through the critical raised for the
    version it replaces, as a result sent back to the bench does. The open
    critical stays open, now stands for the amended value (so a callback
    reads that back), and the ward is told the correction at once."""
    _, _, result = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _rows("SELECT * FROM lab_critical_notifications WHERE result_id = ?",
                     (result["id"],))
    out = _amend(result, signer[1], value="5.0").json()
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        first["id"], True, "corrected")
    note = _note(first["id"])
    assert (note["status"], note["result_id"], note["current_tier"]) == (
        "open", result["id"], 1)
    assert _rows("SELECT COUNT(*) AS n FROM lab_critical_notifications WHERE patient_id = ?",
                 (patient["id"],))[0]["n"] == 1, "no second critical beside it"
    post = _rows("SELECT content, idempotency_key FROM messages WHERE id = ?",
                 (note["relay_message_id"],))[0]
    assert post["idempotency_key"] == f"lab-critical:{first['id']}:t1:r1"
    for words in ("Correction to a critical laboratory result", "7.0 mmol/L as critical",
                  "it is now 5.0 mmol/L, which is not critical", "The chart shows the amended"):
        assert words in post["content"], words
    assert "\u2014" not in post["content"]
    msg = _rows("SELECT content FROM messages WHERE id = ?",
                (out["ward_notice"]["message_id"],))[0]
    assert ("The earlier value was reported as critical; the laboratory will phone to confirm "
            "the change.") in msg["content"]
    view = _view(tech, first["id"])
    assert view["value_told"] == "5.0 mmol/L" and view["value_now"] == "5.0 mmol/L"
    assert view["result_now"]["status"] == "amended" and "now 5.0" in view["result_now"]["text"]
    assert "Phone the ward" in view["result_now"]["text"]
    # The call about the correction is logged against the value told now,
    # never the value it replaced.
    stale = _callback(first["id"], tech, "7.0 mmol/L")
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "value_changed"
    assert _callback(first["id"], tech, "5.0 mmol/L").status_code == 200
    audit = _rows("SELECT summary_message FROM audit_logs WHERE action_type = "
                  "'lab_critical_callback_logged' AND target_id = ?", (first["id"],))
    assert len(audit) == 1 and "5.0 mmol/L" in audit[0]["summary_message"]
    assert "7.0" not in audit[0]["summary_message"]


def test_an_open_critical_amended_to_another_critical_value_records_it_and_raises_no_second(
        lab_on, one_person, patient, tech, phleb, signer, pushes, potassium_limit):
    _, _, result = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _notes(patient["id"])
    out = _amend(result, signer[1], value="7.5").json()
    assert out["result"]["is_critical"] == 1
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        first["id"], True, "changed")
    assert [n["id"] for n in _notes(patient["id"])] == [first["id"]]
    post = _rows("SELECT content, idempotency_key FROM messages WHERE id = ?",
                 (_note(first["id"])["relay_message_id"],))[0]
    assert post["idempotency_key"] == f"lab-critical:{first['id']}:t1:r1"
    for words in ("Amended result", "Changed result: the laboratory reported 7.0 mmol/L for this "
                  "test earlier; the value is now 7.5 mmol/L", "Critical laboratory result for",
                  "Amend potassium 7.5 mmol/L", "agreed critical limit"):
        assert words in post["content"], words
    view = _view(tech, first["id"])
    assert view["value_told"] == "7.5 mmol/L" and view["value_now"] == "7.5 mmol/L"
    assert "was 7.0 mmol/L, now 7.5 mmol/L, still critical" in view["result_now"]["text"]


def test_a_confirmed_critical_amended_to_normal_is_reopened_and_the_ward_told(
        lab_on, one_person, patient, tech, phleb, signer, pushes, potassium_limit):
    """Round 3 review: the ward confirmed 7.0 with a read-back and may have
    treated it; the amendment to 4.5 reopens that critical at tier 1 (the
    bench lane's strictest option) so the laboratory phones the correction,
    and the ward notice's promise of that call is now tracked."""
    _, _, result = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _notes(patient["id"])
    assert _callback(first["id"], tech, "7.0 mmol/L").status_code == 200
    assert _note(first["id"])["status"] == "acknowledged"
    out = _amend(result, signer[1], value="4.5").json()
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        first["id"], True, "corrected")
    note = _note(first["id"])
    assert (note["status"], note["current_tier"], note["acknowledged_at"], note["read_back"]) == (
        "open", 1, None, None)
    changed = [json.loads(e["metadata_json"]) for e in _rows(
        "SELECT metadata_json FROM lab_critical_events WHERE notification_id = ? "
        "AND event_type = 'escalated' ORDER BY id", (first["id"],))]
    assert [(c["reason"], c["trigger"], c["result_id"], c["from_value"], c["to_value"],
             c["still_critical"], c["reopened"]) for c in changed] == [
        ("value_changed", "amended", out["result"]["id"], "7.0 mmol/L", "4.5 mmol/L", False, True)]
    audit = _rows("SELECT event_category FROM audit_logs WHERE action_type = "
                  "'lab_critical_value_changed' AND target_id = ?", (first["id"],))
    assert [a["event_category"] for a in audit] == ["clinical_access"]
    post = _rows("SELECT content FROM messages WHERE id = ?", (note["relay_message_id"],))[0]
    assert "Correction to a critical laboratory result" in post["content"]
    assert "it is now 4.5 mmol/L, which is not critical" in post["content"]
    notice = _rows("SELECT content FROM messages WHERE id = ?",
                   (out["ward_notice"]["message_id"],))[0]
    assert "the laboratory will phone to confirm the change" in notice["content"]
    view = _view(tech, first["id"])
    assert view["status"] == "open" and view["acknowledged"] is None
    assert view["callbacks"][0]["reached"] is True, "the earlier callback stays in the log"
    assert view["value_told"] == "4.5 mmol/L" and "Phone the ward" in view["result_now"]["text"]
    assert _callback(first["id"], tech, "4.5 mmol/L").status_code == 200
    assert _note(first["id"])["status"] == "acknowledged"


def test_a_confirmed_critical_amended_to_another_critical_value_raises_one_naming_the_earlier(
        lab_on, one_person, patient, tech, phleb, signer, pushes, potassium_limit):
    _, _, result = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _notes(patient["id"])
    assert _callback(first["id"], tech, "7.0 mmol/L").status_code == 200
    out = _amend(result, signer[1], value="7.5").json()
    notes = _notes(patient["id"])
    assert [(n["status"], n["value_display"], n["result_id"]) for n in notes] == [
        ("acknowledged", "7.0 mmol/L", result["id"]),
        ("open", "7.5 mmol/L", out["result"]["id"])]
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["kind"]) == (notes[1]["id"], "changed")
    raised = json.loads(_rows("SELECT metadata_json FROM lab_critical_events WHERE "
                              "notification_id = ? AND event_type = 'raised'",
                              (notes[1]["id"],))[0]["metadata_json"])
    assert raised["previous_notification_id"] == first["id"]
    assert raised["previous_value_display"] == "7.0 mmol/L"
    post = _rows("SELECT content FROM messages WHERE id = ?",
                 (notes[1]["relay_message_id"],))[0]
    assert "reported 7.0 mmol/L" in post["content"] and "now 7.5 mmol/L" in post["content"]


def test_a_result_amended_twice_says_the_value_standing_now(
        lab_on, one_person, patient, tech, phleb, signer, pushes, potassium_limit, monkeypatch):
    """Round 3 review: the current value is read at the head of the version
    chain, never one hop away. 7.0 (critical) amended to 5.0, then to 7.5:
    the critical raised for 7.0 says 7.5, still critical, on its card and in
    every post, and nothing says the result is now 5.0."""
    from services import lab_critical_service as lcs

    _two_tier_ladder(monkeypatch)
    _, _, v1 = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _notes(patient["id"])
    v2 = _amend(v1, signer[1], value="5.0").json()["result"]
    out = _amend(v2, signer[1], value="7.5").json()
    v3 = out["result"]
    assert v3["is_critical"] == 1 and v3["version"] == 3
    with db_session() as db:
        assert lcs.version_chain(db, v1["id"]) == [v1["id"], v2["id"], v3["id"]]
        assert lcs.version_chain(db, v3["id"]) == [v1["id"], v2["id"], v3["id"]]
        assert lcs.head_result_id(db, v1["id"]) == v3["id"]
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        first["id"], True, "changed")
    assert [n["id"] for n in _notes(patient["id"])] == [first["id"]], "one critical, not two"
    view = _view(tech, first["id"])
    assert view["value_told"] == "7.5 mmol/L" and view["value_now"] == "7.5 mmol/L"
    assert view["result_now"]["status"] == "amended"
    assert "was 5.0 mmol/L, now 7.5 mmol/L, still critical" in view["result_now"]["text"]
    assert "now 5.0 mmol/L." not in view["result_now"]["text"]
    lcs.sweep(now=_in_16_minutes())
    note = _note(first["id"])
    assert (note["status"], note["current_tier"]) == ("open", 2)
    post = _rows("SELECT content, idempotency_key FROM messages WHERE id = ?",
                 (note["relay_message_id"],))[0]
    assert post["idempotency_key"] == f"lab-critical:{first['id']}:t2:r2"
    assert "Amend potassium 7.5 mmol/L" in post["content"]
    assert "Critical laboratory result for" in post["content"]
    assert "it is now 5.0" not in post["content"] and "not critical" not in post["content"]


def test_an_amended_result_then_withdrawn_says_withdrawn_everywhere(
        lab_on, one_person, patient, tech, phleb, signer, pushes, potassium_limit, monkeypatch):
    """Round 3 review: the newest version was withdrawn after an amendment,
    so nothing is on the chart; the critical raised for the first version
    says withdrawn, never 'it is now 5.0 ... the chart shows the amended
    value'."""
    from services import lab_critical_service as lcs

    _two_tier_ladder(monkeypatch)
    _, _, v1 = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _notes(patient["id"])
    v2 = _amend(v1, signer[1], value="5.0").json()["result"]
    out = _withdraw(v2, signer[1]).json()
    assert out["open_criticals"] == 1
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        first["id"], True, "corrected")
    view = _view(tech, first["id"])
    assert view["value_told"] == lcs.WITHDRAWN_TOLD and view["value_now"] is None
    assert view["result_now"]["status"] == "withdrawn"
    assert "entered in error" in view["result_now"]["text"]
    lcs.sweep(now=_in_16_minutes())
    note = _note(first["id"])
    assert (note["status"], note["current_tier"]) == ("open", 2)
    post = _rows("SELECT content FROM messages WHERE id = ?", (note["relay_message_id"],))[0]
    assert "withdrawn because it was entered in error" in post["content"]
    assert "7.0 mmol/L as critical" in post["content"]
    for stale in ("it is now 5.0", "The chart shows the amended value"):
        assert stale not in post["content"], stale


# ── 6. Withdrawal ────────────────────────────────────────────────────────────

def test_a_withdrawn_value_is_gone_from_every_reader(lab_on, patient, tech, phleb, signer,
                                                     pushes):
    from services import patient_timeline_service

    order, specimen_id, result = _reported(patient, tech, phleb, signer)
    resp = _withdraw(result, signer[1])
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["result"]["status"] == "withdrawn"
    obs = _rows("SELECT status FROM observations WHERE lab_result_id = ?", (result["id"],))
    assert [o["status"] for o in obs] == ["entered-in-error"]
    chart, pack, recent = _chart(patient["id"])
    assert chart == [] and pack == [] and recent == []
    with db_session() as db:
        assert lobs.previous_result(db, patient["id"], _code("K"), utc_iso_now()) is None
    timeline = patient_timeline_service.build_chart_timeline(patient["id"], limit=200)
    assert not any(e["kind"] == "observation" for e in timeline["events"])
    item = _rows("SELECT status, cancelled_reason, cancelled_by FROM lab_order_items "
                 "WHERE order_id = ?", (order["id"],))[0]
    assert item["status"] == "cancelled" and item["cancelled_reason"].startswith("Result withdrawn")
    assert item["cancelled_by"] == signer[0]
    audit = _rows("SELECT event_category FROM audit_logs WHERE action_type = "
                  "'lab_result_withdrawn' AND target_id = ?", (str(result["id"]),))
    assert [a["event_category"] for a in audit] == ["clinical_access"]
    again = _withdraw(result, signer[1])
    assert again.status_code == 409 and again.json()["detail"]["error"] == "withdrawn"


def test_withdrawing_the_only_reported_test_keeps_the_order_resulted_and_tells_the_lab(
        lab_on, patient, tech, phleb, signer, pushes):
    order, _, result = _reported(patient, tech, phleb, signer)
    out = _withdraw(result, signer[1]).json()
    assert out["order"] == {"id": order["id"], "status": "resulted", "conflict": True}
    stored = _rows("SELECT status, result_text FROM orders WHERE id = ?", (order["id"],))[0]
    assert (stored["status"], stored["result_text"]) == ("resulted", los.LAB_RESULT_TEXT)
    (exc,) = _rows("SELECT * FROM lab_exceptions WHERE order_id = ? AND resolved_at IS NULL",
                   (order["id"],))
    assert exc["kind"] == "order_state_conflict" and "was withdrawn" in exc["detail"]
    note = _rows("SELECT reason FROM order_events WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                 (order["id"],))[0]
    assert note["reason"].startswith("Laboratory result withdrawn")


def test_withdrawing_one_test_of_a_reported_panel_leaves_the_order_resulted_quietly(
        lab_on, patient, tech, phleb, signer, pushes):
    order = orders_service.create_order(
        patient_id=patient["id"], order_type="lab", description="Amend mini panel",
        code=_code(PANEL), code_system="LOCAL", priority="routine", ordered_by="D001",
        status="active")
    specimen_id, items = _taken_in(patient, phleb[1], tech[1], order)
    k = _release(items[lm.test_id_for("LOCAL", _code("K"))], specimen_id, "4.4", tech, signer)
    _release(items[lm.test_id_for("LOCAL", _code("NA"))], specimen_id, "139", tech, signer)
    out = _withdraw(k, signer[1], reason="Potassium entered on the wrong line").json()
    assert out["order"]["status"] == "resulted" and out["order"]["conflict"] is False
    assert _rows("SELECT * FROM lab_exceptions WHERE order_id = ? AND resolved_at IS NULL",
                 (order["id"],)) == []


def test_the_next_tier_for_an_amended_or_withdrawn_critical_posts_the_correction(
        lab_on, one_person, _catalogue, tech, phleb, signer, pushes, potassium_limit,
        monkeypatch):
    """A critical still open after its result was amended or withdrawn climbs
    its ladder with the value as it now stands, never the stale value again
    as critical."""
    from datetime import timedelta

    from services import lab_critical_service as lcs
    from services.clinical_time import format_utc, parse_to_utc

    ladder = [{"tier": 1, "role": "Ward nurse in charge", "label": "Ward nurse in charge",
               "timeout_minutes": 15},
              {"tier": 2, "role": "Duty doctor", "label": "Duty doctor", "timeout_minutes": None}]
    monkeypatch.setattr(site_config_service, "_loaded",
                        SiteConfig(present=True, lab_critical={"tiers": ladder}))
    later = format_utc(parse_to_utc(utc_iso_now()) + timedelta(minutes=16))
    amended_patient, withdrawn_patient = _patient(_catalogue), _patient(_catalogue)
    _, _, first = _reported(amended_patient, tech, phleb, signer, value="7.0")
    assert _amend(first, signer[1], value="5.0").status_code == 200
    _, _, second = _reported(withdrawn_patient, tech, phleb, signer, value="7.0")
    assert _withdraw(second, signer[1]).status_code == 200
    lcs.sweep(now=later)
    for p, words in ((amended_patient, "it is now 5.0 mmol/L, which is not critical"),
                     (withdrawn_patient, "withdrawn because it was entered in error")):
        (note,) = _rows("SELECT * FROM lab_critical_notifications WHERE patient_id = ?",
                        (p["id"],))
        assert (note["status"], note["current_tier"]) == ("open", 2)
        post = _rows("SELECT content, idempotency_key FROM messages WHERE id = ?",
                     (note["relay_message_id"],))[0]
        # The step posted the change at tier 1 under its own key ('r1'); the
        # next tier posts it again under that change.
        assert post["idempotency_key"] == f"lab-critical:{note['id']}:t2:r1"
        assert "Correction to a critical laboratory result" in post["content"]
        assert "7.0 mmol/L as critical" in post["content"] and words in post["content"]
        assert "Critical laboratory result for" not in post["content"]
        assert "\u2014" not in post["content"]


def test_a_wrong_patient_withdrawal_prompts_an_incident_only_with_nabh_on(
        lab_on, _catalogue, tech, phleb, signer, pushes, monkeypatch):
    _, _, first = _reported(_patient(_catalogue), tech, phleb, signer)
    monkeypatch.setattr(config, "NABH_ENABLED", True, raising=False)
    out = _withdraw(first, signer[1], wrong_patient=True).json()
    assert out["incident_prompt"]["incident_type"] == "patient_identification"
    msg = _rows("SELECT content FROM messages WHERE id = ?",
                (out["ward_notice"]["message_id"],))[0]
    assert "It belonged to another patient" in msg["content"]
    _, _, second = _reported(_patient(_catalogue), tech, phleb, signer)
    monkeypatch.setattr(config, "NABH_ENABLED", False, raising=False)
    assert _withdraw(second, signer[1], wrong_patient=True).json()["incident_prompt"] is None


def test_a_withdrawal_cancels_a_waiting_amendment_and_keeps_a_critical_open(
        lab_on, patient, tech, phleb, signer, pushes, potassium_limit):
    _, _, result = _reported(patient, tech, phleb, signer, value="7.0")
    proposal = _amend(result, signer[1], value="7.1").json()["proposal"]
    out = _withdraw(result, signer[1]).json()
    assert out["open_criticals"] == 1
    kinds = [e["event_type"] for e in _rows(
        "SELECT event_type FROM lab_result_events WHERE result_id = ? ORDER BY id",
        (result["id"],))]
    assert "amendment_cancelled" in kinds
    assert proposal["proposal_id"]
    assert _rows("SELECT status FROM lab_critical_notifications WHERE result_id = ?",
                 (result["id"],))[0]["status"] == "open"


def test_an_open_critical_withdrawn_records_the_withdrawal_and_stays_open(
        lab_on, patient, tech, phleb, signer, pushes, potassium_limit):
    from services import lab_critical_service as lcs

    _, _, result = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _notes(patient["id"])
    out = _withdraw(result, signer[1]).json()
    assert out["open_criticals"] == 1
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        first["id"], True, "corrected")
    note = _note(first["id"])
    assert (note["status"], note["current_tier"]) == ("open", 1)
    post = _rows("SELECT content, idempotency_key FROM messages WHERE id = ?",
                 (note["relay_message_id"],))[0]
    assert post["idempotency_key"] == f"lab-critical:{first['id']}:t1:r1"
    assert "withdrawn because it was entered in error" in post["content"]
    assert "Do not act on it" in post["content"]
    changed = json.loads(_rows("SELECT metadata_json FROM lab_critical_events WHERE "
                               "notification_id = ? AND event_type = 'escalated'",
                               (first["id"],))[0]["metadata_json"])
    assert (changed["trigger"], changed["to_value"], changed["withdrawn"],
            changed["still_critical"], changed["reopened"]) == (
        "withdrawn", lcs.WITHDRAWN_TOLD, True, False, False)
    assert _view(tech, first["id"])["value_told"] == lcs.WITHDRAWN_TOLD


def test_a_confirmed_critical_withdrawn_for_the_wrong_patient_is_reopened(
        lab_on, patient, tech, phleb, signer, pushes, potassium_limit):
    """Round 3 review: the ward confirmed 7.0 with a read-back and may have
    treated it; the result belonged to another patient. The critical is
    reopened at tier 1, open_criticals says so (so the reported-results
    screen tells the laboratory to phone), the ward notice's promise of a
    call is tracked on Critical results, and only a callback about the
    withdrawal closes it again."""
    from services import lab_critical_service as lcs

    _, _, result = _reported(patient, tech, phleb, signer, value="7.0")
    (first,) = _notes(patient["id"])
    assert _callback(first["id"], tech, "7.0 mmol/L").status_code == 200
    out = _withdraw(result, signer[1], wrong_patient=True).json()
    assert out["open_criticals"] == 1
    (answer,) = out["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        first["id"], True, "corrected")
    note = _note(first["id"])
    assert (note["status"], note["current_tier"], note["acknowledged_at"], note["read_back"]) == (
        "open", 1, None, None)
    post = _rows("SELECT content FROM messages WHERE id = ?", (note["relay_message_id"],))[0]
    assert "7.0 mmol/L as critical" in post["content"]
    assert "withdrawn because it was entered in error" in post["content"]
    notice = _rows("SELECT content FROM messages WHERE id = ?",
                   (out["ward_notice"]["message_id"],))[0]
    assert "It belonged to another patient" in notice["content"]
    assert "the laboratory will phone to confirm the withdrawal" in notice["content"]
    withdrawn_audit = json.loads(_rows(
        "SELECT metadata_json FROM audit_logs WHERE action_type = 'lab_result_withdrawn' "
        "AND target_id = ?", (str(result["id"]),))[0]["metadata_json"])
    assert withdrawn_audit["open_criticals"] == 1
    assert withdrawn_audit["critical_notifications"] == [first["id"]]
    view = _view(tech, first["id"])
    assert view["status"] == "open" and view["value_told"] == lcs.WITHDRAWN_TOLD
    assert view["result_now"]["status"] == "withdrawn"
    assert "Tell the ward it was entered in error" in view["result_now"]["text"]
    assert view["changes"][-1]["reopened"] is True
    # A screen that still showed 7.0 logs nothing; the call about the
    # withdrawal closes it and is audited as the withdrawal.
    stale = _callback(first["id"], tech, "7.0 mmol/L")
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "value_changed" and "was withdrawn" in detail["message"]
    assert "\u2014" not in detail["message"]
    assert _callback(first["id"], tech, lcs.WITHDRAWN_TOLD).status_code == 200
    audit = _rows("SELECT summary_message FROM audit_logs WHERE action_type = "
                  "'lab_critical_callback_logged' AND target_id = ? ORDER BY id",
                  (first["id"],))
    assert len(audit) == 2 and "Amend potassium was withdrawn" in audit[-1]["summary_message"]
    view = _view(tech, first["id"])
    assert view["status"] == "acknowledged"
    assert view["result_now"]["text"] == ("Withdrawn after this was raised. The ward was told it "
                                          "was entered in error.")
    # Withdrawn twice is refused, and records nothing more.
    assert _withdraw(result, signer[1]).status_code == 409
    assert _note(first["id"])["status"] == "acknowledged"


def test_the_ward_notice_promises_a_call_only_when_a_critical_tracks_it():
    base = {"patient_name": "Asha", "mrn": "MRN-1", "test": "Potassium", "reason": "Wrong line",
            "by_name": "Dr Path", "at": None}
    amended = {**base, "kind": "amended", "before": "7.0 mmol/L", "after": "4.5 mmol/L",
               "flag_before": "HH", "flag_after": "", "was_critical": True, "now_critical": False}
    assert "the laboratory will phone" in lrs.result_notice_text(amended)
    assert "the laboratory will phone" in lrs.result_notice_text(
        {**amended, "critical_followup": True})
    untracked = lrs.result_notice_text({**amended, "critical_followup": False})
    assert "The earlier value was reported as critical." in untracked
    assert "will phone" not in untracked
    withdrawn = {**base, "kind": "withdrawn", "before": "7.0 mmol/L", "was_critical": True}
    assert "will phone to confirm the withdrawal" in lrs.result_notice_text(
        {**withdrawn, "critical_followup": True})
    assert "will phone" not in lrs.result_notice_text({**withdrawn, "critical_followup": False})


def test_a_discharged_patient_is_not_told_in_a_closed_room(lab_on, one_person, patient, tech,
                                                           phleb, signer, pushes):
    _, _, result = _reported(patient, tech, phleb, signer)
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient["id"],))
    out = _amend(result, signer[1], value="5.0").json()
    assert out["ward_notice"] == {"posted": False, "reason": "case_room_closed"}
    assert out["result"]["version"] == 2


# ── 7. The reported results of a sample ──────────────────────────────────────

def test_the_reported_results_list_versions_and_the_waiting_amendment(
        lab_on, patient, tech, phleb, signer, second_signer, pushes):
    _, specimen_id, result = _reported(patient, tech, phleb, signer)
    _amend(result, signer[1], value="5.0")
    resp = client.get(f"/api/lab/results/reported/{specimen_id}", headers=second_signer[1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (test,) = body["tests"]
    assert test["current"]["result_id"] == result["id"]
    assert test["pending_amendment"]["value_text"] == "5.0"
    assert test["pending_amendment"]["may_confirm"] is True
    assert body["can_amend"] is True and body["require_distinct_authoriser"] is True
    mine = client.get(f"/api/lab/results/reported/{specimen_id}", headers=signer[1]).json()
    assert mine["tests"][0]["pending_amendment"]["may_confirm"] is False
    _confirm(result, test["pending_amendment"], second_signer[1])
    after = client.get(f"/api/lab/results/reported/{specimen_id}", headers=tech[1]).json()
    (test,) = after["tests"]
    assert test["current"]["version"] == 2 and [h["version"] for h in test["history"]] == [1]
    assert after["can_amend"] is False
    audit = _rows("SELECT event_category, patient_id FROM audit_logs WHERE action_type = "
                  "'lab_reported_results_viewed' AND actor_user_id = ? ORDER BY id DESC LIMIT 1",
                  (tech[0],))
    assert audit[0]["event_category"] == "phi_access" and audit[0]["patient_id"] == patient["id"]
    bad = client.get("/api/lab/results/reported/261009000120", headers=tech[1])
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "bad_accession"


# ── 8. Module state ──────────────────────────────────────────────────────────

def test_amend_and_withdraw_are_served_while_draining_and_404_when_off(
        lab_on, one_person, patient, tech, phleb, signer, pushes, monkeypatch):
    _, _, result = _reported(patient, tech, phleb, signer)
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    resp = _amend(result, signer[1], value="5.0")
    assert resp.status_code == 200, resp.text
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    for path in (f"/api/lab/results/{resp.json()['result']['id']}/withdraw",
                 f"/api/lab/results/{resp.json()['result']['id']}/amend",
                 "/api/lab/results/1/amend/confirm", "/api/lab/results/1/amend/cancel"):
        off = client.post(path, json={}, headers=signer[1])
        assert off.status_code == 404 and off.json()["detail"]["error"] == "module_disabled"
    off = client.get("/api/lab/results/reported/261009000127", headers=signer[1])
    assert off.status_code == 404
