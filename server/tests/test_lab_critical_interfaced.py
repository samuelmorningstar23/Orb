"""Critical callbacks for interfaced results (lis-22, ORB_LAB_CRITICAL_INTERFACED).

Ruling LIS-R22 applied as the build default (option (a), off by default);
go-live held with LIS-R1, LIS-R3 and LIS-R20. Pinned here:
  * switch off: an external laboratory's HH result is stored exactly as
    before and raises nothing;
  * switch on (no ORB_LAB needed): a final or corrected result the sending
    laboratory flagged HH, LL or AA, for a patient Orb resolved, raises ONE
    notification in the 'interfaced' lane, posted once to the case room as
    the service identity system_lab ('Laboratory interface', is_bot 1, source
    'lab_interface') and pushed once as message:new;
  * a backfill, a simulator's result, a preliminary, an unflagged or merely
    high result, an unmatched patient and a monitor's device metric never
    raise; a flag in a later OBX-8 repetition still counts;
  * a resent message never raises twice (same patient, test, sender, order
    and value); a corrected value raises a new one naming the earlier value;
  * no case room: the post is undelivered, the critical stays open and the
    sweep climbs the ladder;
  * the ward acknowledges it on the card with a read-back;
  * a failed raise never loses the result;
  * system_lab cannot sign in, is listed as a service account and User
    Management refuses to touch it;
  * while the switch is on an open interfaced critical never makes the
    laboratory module drain; switched off with one open, the critical
    surfaces stay open.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import SERVICE_ACCOUNT_IDS, db_session
from services import case_room_alerts, ingestion_service, lab_module, site_config_service
from services import lab_critical_service as lcs
from services.audit_service import AuditWriteError
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now
from services.ingestion_adapters import HL7Adapter
from services.site_config_service import SiteConfig

client = TestClient(main.app)

WARD = "General Medicine"
OTHER_WARD = "Cardiology"
SENDER = "LABSYS^CITYLAB"
LADDER = [
    {"tier": 1, "role": "Ward nurse in charge", "label": "Ward nurse in charge",
     "timeout_minutes": 15},
    {"tier": 2, "role": "Duty doctor", "label": "Duty doctor", "timeout_minutes": None},
]


# ── fixtures ─────────────────────────────────────────────────────────────────

_CREATED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    if not _CREATED:
        return
    marks = ", ".join("?" for _ in _CREATED)
    ids = tuple(_CREATED)
    with db_session() as db:
        db.execute(f"DELETE FROM lab_critical_events WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM lab_critical_notifications WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM observations WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM messages WHERE channel_id IN "
                   f"(SELECT id FROM channels WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM channels WHERE patient_id IN ({marks})", ids)
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   ids)
    lab_module.refresh_drain_state()


@pytest.fixture
def interfaced_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", True)
    assert config.feature_enabled("lab_critical_interfaced")


@pytest.fixture
def no_ladder(monkeypatch):
    monkeypatch.setattr(site_config_service, "_loaded", SiteConfig())


@pytest.fixture
def ladder(monkeypatch):
    monkeypatch.setattr(site_config_service, "_loaded",
                        SiteConfig(present=True, lab_critical={"tiers": [dict(t) for t in LADDER]}))


@pytest.fixture
def pushes(monkeypatch):
    sent: list[tuple] = []

    def record(msg_data, channel_id, *, what=""):
        sent.append((msg_data, channel_id))
        return True

    monkeypatch.setattr(case_room_alerts, "push_message_new", record)
    return sent


def _patient(*, case_room=True, dept=WARD) -> dict:
    pid = f"PLCI{uuid.uuid4().hex[:8].upper()}"
    name = f"Interfaced {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-LCI-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', 'GM-1', ?)",
                   (pid, name, mrn, f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ALCI{uuid.uuid4().hex[:8].upper()}", pid, dept))
        if case_room:
            db.execute("INSERT INTO channels (id, name, type, patient_id) "
                       "VALUES (?, ?, 'case_room', ?)", (f"lci-{pid.lower()}", f"Case {name}", pid))
    _CREATED.append(pid)
    return {"id": pid, "name": name, "mrn": mrn,
            "room": f"lci-{pid.lower()}" if case_room else None}


def _user(role, dept=None):
    uid = f"LCI{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, dept, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


def _oru(mrn: str, *, value="7.1", flag="HH", status="F", filler="FL1", placer="PL1",
         code="2823-3", text="Potassium", unit="mmol/L", value_type="NM",
         ref="3.5-5.1") -> str:
    return "\r".join([
        f"MSH|^~\\&|LABSYS|CITYLAB|ORB|HOSP|20261004081000||ORU^R01|"
        f"{uuid.uuid4().hex[:12]}|P|2.5",
        f"PID|1||{mrn}^^^HOSP^MR||SHARMA^RAVI||19700101|M",
        f"OBR|1|{placer}|{filler}|24323-8^Basic panel^LN|||20261004080000",
        f"OBX|1|{value_type}|{code}^{text}^LN||{value}|{unit}|{ref}|{flag}|||{status}|||"
        "20261004080000",
    ])


async def _ingest(raw: str, **kwargs):
    return await ingestion_service.ingest_payload(raw, adapter=HL7Adapter(),
                                                  source_name="lis22-test", **kwargs)


def _notes(pid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_critical_notifications WHERE patient_id = ? ORDER BY rowid",
            (pid,)).fetchall()]


def _events(nid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_critical_events WHERE notification_id = ? ORDER BY id",
            (nid,)).fetchall()]


def _messages(room: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY id", (room,)).fetchall()]


def _observations(pid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, status, abnormal_flag, superseded_by FROM observations "
            "WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()]


def _later(minutes):
    return format_utc(parse_to_utc(utc_iso_now()) + timedelta(minutes=minutes))


# ── 1. The switch ────────────────────────────────────────────────────────────

async def test_with_the_switch_off_nothing_is_raised_and_the_result_lands_as_before(
        monkeypatch, no_ladder, pushes):
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
    p = _patient()
    res = await _ingest(_oru(p["mrn"]))
    assert res.parse_status == "parsed" and res.inserted_observations == 1
    assert _notes(p["id"]) == []
    assert _messages(p["room"]) == [] and pushes == []
    (obs,) = _observations(p["id"])
    assert obs["abnormal_flag"] == "HH" and obs["status"] == "F"


# ── 2. Raising and posting ───────────────────────────────────────────────────

async def test_an_hh_potassium_raises_one_notification_and_one_message_new(
        interfaced_on, no_ladder, pushes):
    p = _patient()
    res = await _ingest(_oru(p["mrn"], value="7.10"))
    assert res.parse_status == "parsed" and res.inserted_observations == 1
    (obs,) = _observations(p["id"])
    (note,) = _notes(p["id"])
    assert (note["source_lane"], note["observation_id"], note["result_id"]) == (
        "interfaced", obs["id"], None)
    assert note["status"] == "open" and note["preliminary"] == 0
    # The sending laboratory's own precision, not the stored float.
    assert note["value_display"] == "7.10 mmol/L"
    assert note["test_name"] == "Potassium" and note["test_code"] == "2823-3"
    assert note["raised_by"] == lcs.SYSTEM_LAB_ID and note["raised_in_department"] == WARD
    assert note["delivery_status"] == "posted"
    (msg,) = _messages(p["room"])
    assert msg["id"] == note["relay_message_id"]
    assert (msg["sender_id"], msg["sender_name"], msg["sender_role"]) == (
        "system_lab", "Laboratory interface", "laboratory")
    assert (msg["message_type"], msg["is_bot"], msg["source"]) == ("alert", 1, "lab_interface")
    assert msg["idempotency_key"] == f"lab-critical:{note['id']}:t1"
    for words in (p["name"], p["mrn"], "Potassium 7.10 mmol/L",
                  "flagged critical high by the sending laboratory", "3.5-5.1",
                  "Acknowledge it with a read-back"):
        assert words in msg["content"], words
    assert chr(0x2014) not in msg["content"], "no em dash in the post"
    assert "score" not in msg["content"].lower() and "NEWS2" not in msg["content"]
    assert len(pushes) == 1 and pushes[0][1] == p["room"]
    assert pushes[0][0]["id"] == msg["id"] and pushes[0][0]["is_bot"] is True
    assert pushes[0][0]["source"] == "lab_interface"
    assert [e["event_type"] for e in _events(note["id"])] == ["raised", "posted"]
    raised = json.loads(_events(note["id"])[0]["metadata_json"])
    assert raised["lab_flag"] == "HH" and raised["observation_id"] == obs["id"]
    with db_session() as db:
        audit = db.execute(
            "SELECT event_category, actor_user_id FROM audit_logs WHERE action_type = "
            "'lab_critical_raised' AND target_id = ?", (note["id"],)).fetchall()
    assert [(a[0], a[1]) for a in audit] == [("clinical_access", "system_lab")]


@pytest.mark.parametrize("flag,raises", [
    ("HH", True), ("LL", True), ("AA", True), ("HH^Critical high^HL70078", True),
    ("H", False), ("L", False), ("A", False), ("N", False), ("", False),
])
async def test_only_the_senders_hh_ll_or_aa_raises(interfaced_on, no_ladder, pushes, flag,
                                                   raises):
    p = _patient()
    await _ingest(_oru(p["mrn"], flag=flag, value="2.1" if flag.startswith("LL") else "7.1"))
    assert len(_notes(p["id"])) == (1 if raises else 0)


async def test_a_critical_flag_in_a_later_repetition_counts(interfaced_on, no_ladder, pushes):
    """The adapter stores only OBX-8's first repetition; the raw segment is
    read for the rest."""
    p = _patient()
    await _ingest(_oru(p["mrn"], flag="A~HH"))
    assert _observations(p["id"])[0]["abnormal_flag"] == "A"
    (note,) = _notes(p["id"])
    assert json.loads(_events(note["id"])[0]["metadata_json"])["lab_flag"] == "HH"


@pytest.mark.parametrize("status,raises", [("F", True), ("C", True), ("P", False),
                                           ("R", False), ("X", False)])
async def test_only_a_final_or_corrected_result_raises(interfaced_on, no_ladder, pushes, status,
                                                       raises):
    p = _patient()
    await _ingest(_oru(p["mrn"], status=status))
    assert len(_notes(p["id"])) == (1 if raises else 0)


async def test_a_coded_critical_reads_as_its_text(interfaced_on, no_ladder, pushes):
    p = _patient()
    await _ingest(_oru(p["mrn"], value_type="CE", value="GNR^Gram negative rods^L", unit="",
                       flag="AA", code="600-7", text="Blood culture", ref=""))
    (note,) = _notes(p["id"])
    assert note["value_display"] == "Gram negative rods"
    assert "flagged critical abnormal" in _messages(p["room"])[0]["content"]


# ── 3. What never raises ─────────────────────────────────────────────────────

async def test_a_backfill_never_raises(interfaced_on, no_ladder, pushes):
    p = _patient()
    res = await _ingest(_oru(p["mrn"]), backfill=True)
    assert res.inserted_observations == 1
    assert _notes(p["id"]) == [] and pushes == []


async def test_a_simulated_result_never_raises(interfaced_on, no_ladder, pushes):
    p = _patient()
    res = await _ingest(_oru(p["mrn"]), source_label="simulated")
    assert res.inserted_observations == 1
    assert _notes(p["id"]) == []


async def test_a_monitor_device_metric_never_raises(interfaced_on, no_ladder, pushes):
    p = _patient()
    res = await _ingest(_oru(p["mrn"], code="ST-II", text="ST level", unit="mm", flag="HH"),
                        source_label="monitor_hl7")
    assert res.inserted_observations == 1
    assert _notes(p["id"]) == []


async def test_an_unmatched_patient_never_raises(interfaced_on, no_ladder, pushes):
    mrn = f"MRN-NOBODY-{uuid.uuid4().hex[:8]}"
    before = _count_interfaced()
    res = await _ingest(_oru(mrn))
    assert res.inserted_observations == 1
    assert _count_interfaced() == before
    with db_session() as db:
        db.execute("DELETE FROM observations WHERE patient_id IS NULL AND source_event_id = ?",
                   (res.event_id,))


def _count_interfaced() -> int:
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM lab_critical_notifications "
                          "WHERE source_lane = 'interfaced'").fetchone()[0]


def test_raise_interfaced_rechecks_the_stored_row(interfaced_on, no_ladder):
    """The service decides from the stored row, not from the caller: a row
    already raised, or one Orb's own laboratory released, raises nothing."""
    p = _patient()
    with db_session() as db:
        oid = db.execute(
            "INSERT INTO observations (patient_id, source, observation_code, observation_name, "
            "value_numeric, unit, abnormal_flag, status, lab_result_id) "
            "VALUES (?, 'lis_manual', '2823-3', 'Potassium', 7.2, 'mmol/L', 'HH', 'final', 12345)",
            (p["id"],)).lastrowid
    assert lcs.raise_interfaced(oid) is None
    assert lcs.raise_interfaced(oid, backfill=True) is None
    assert lcs.raise_interfaced(987654321) is None


# ── 4. Never twice ───────────────────────────────────────────────────────────

async def test_a_resent_message_does_not_duplicate(interfaced_on, no_ladder, pushes):
    p = _patient()
    first = _oru(p["mrn"], value="7.1", filler="FL-R1")
    await _ingest(first)
    # The very same bytes are a duplicate event: not even stored again.
    dup = await _ingest(first)
    assert dup.parse_status == "duplicate"
    # The same result resent under a new message control id is stored again,
    # but raises nothing.
    again = await _ingest(_oru(p["mrn"], value="7.1", filler="FL-R1"))
    assert again.inserted_observations == 1
    assert len(_observations(p["id"])) == 2
    assert len(_notes(p["id"])) == 1 and len(_messages(p["room"])) == 1 and len(pushes) == 1
    # A new specimen (another filler number) with the same value is a new result.
    await _ingest(_oru(p["mrn"], value="7.1", filler="FL-R2"))
    assert len(_notes(p["id"])) == 2


async def test_a_result_with_no_order_numbers_dedupes_on_its_collection_time(
        interfaced_on, no_ladder, pushes):
    p = _patient()
    await _ingest(_oru(p["mrn"], filler="", placer=""))
    await _ingest(_oru(p["mrn"], filler="", placer=""))
    assert len(_notes(p["id"])) == 1


async def test_a_corrected_critical_value_raises_a_new_one_naming_the_earlier(
        interfaced_on, no_ladder, pushes):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="7.1", filler="FL-C1"))
    await _ingest(_oru(p["mrn"], value="7.6", filler="FL-C1", status="C"))
    first, second = _notes(p["id"])
    assert (first["value_display"], second["value_display"]) == ("7.1 mmol/L", "7.6 mmol/L")
    meta = json.loads(_events(second["id"])[0]["metadata_json"])
    assert meta["previous_notification_id"] == first["id"]
    assert meta["previous_value_display"] == "7.1 mmol/L"
    assert "changed from 7.1 mmol/L" in _events(second["id"])[0]["detail"]


async def test_a_correction_to_a_non_critical_value_shows_on_the_card(
        interfaced_on, no_ladder, pushes, monkeypatch):
    monkeypatch.setattr(config, "RESULT_CORRECTIONS_ENABLED", True)
    p = _patient()
    _, nurse = _user("nurse", WARD)
    await _ingest(_oru(p["mrn"], value="7.1", filler="FL-N1"))
    await _ingest(_oru(p["mrn"], value="5.4", flag="H", filler="FL-N1", status="C"))
    (note,) = _notes(p["id"])
    assert note["status"] == "open", "a correction never closes a critical by itself"
    card = client.get("/api/lab/criticals/ward", headers=nurse).json()
    (item,) = [n for n in card["open"] if n["patient"]["id"] == p["id"]]
    assert item["value_told"] == "7.1 mmol/L"
    assert "Corrected by the sending laboratory" in item["change_text"]
    assert "5.4 mmol/L" in item["change_text"]


# ── 5. Delivery that fails, and the ladder ───────────────────────────────────

async def test_no_case_room_is_undelivered_and_the_sweep_escalates(interfaced_on, ladder,
                                                                   pushes):
    p = _patient(case_room=False)
    await _ingest(_oru(p["mrn"]))
    (note,) = _notes(p["id"])
    assert (note["status"], note["delivery_status"], note["current_tier"]) == (
        "open", "no_case_room", 1)
    assert parse_to_utc(note["tier_due_at"]) <= parse_to_utc(utc_iso_now())
    out = lcs.sweep()
    assert out["ran"] is True and out["escalated"] >= 1
    (note,) = _notes(p["id"])
    assert note["current_tier"] == 2 and note["status"] == "open"
    assert ("escalated", 2) in [(e["event_type"], e["tier"]) for e in _events(note["id"])]


async def test_the_sweep_posts_the_next_tier_as_system_lab(interfaced_on, ladder, pushes):
    p = _patient()
    await _ingest(_oru(p["mrn"]))
    lcs.sweep(now=_later(20))
    (note,) = _notes(p["id"])
    assert note["current_tier"] == 2
    msgs = _messages(p["room"])
    assert len(msgs) == 2 and all(m["sender_id"] == "system_lab" and m["is_bot"] == 1
                                  for m in msgs)
    assert "Not yet acknowledged with a read-back" in msgs[1]["content"]
    assert "Duty doctor" in msgs[1]["content"]


# ── 6. The ward acknowledges it ──────────────────────────────────────────────

async def test_the_ward_card_shows_it_and_a_read_back_closes_it(interfaced_on, ladder, pushes):
    p = _patient()
    _, nurse = _user("nurse", WARD)
    _, other = _user("nurse", OTHER_WARD)
    await _ingest(_oru(p["mrn"]))
    body = client.get("/api/lab/criticals/ward", headers=nurse)
    assert body.status_code == 200, body.text
    (item,) = [n for n in body.json()["open"] if n["patient"]["id"] == p["id"]]
    assert item["sender"] == "Sending laboratory" and item["source_lane"] == "interfaced"
    assert item["lab_flag"] == {"code": "HH", "text": "Critical high"}
    assert item["reference_range"] == "3.5-5.1" and item["critical_limit_text"] is None
    assert item["preliminary"] is False
    assert item["state_text"] == "Final result from the sending laboratory"
    assert p["id"] not in {n["patient"]["id"]
                           for n in client.get("/api/lab/criticals/ward", headers=other)
                           .json()["open"]}
    out = client.post(f"/api/lab/criticals/{item['id']}/acknowledge", headers=other,
                      json={"recipient_name": "Sister Rao", "read_back": True,
                            "value_told": item["value_told"]})
    assert out.status_code == 404
    out = client.post(f"/api/lab/criticals/{item['id']}/acknowledge", headers=nurse,
                      json={"recipient_name": "Sister Rao", "read_back": True, "method": "phone",
                            "caller_name": "City Lab, Mr Iyer",
                            "value_told": item["value_told"]})
    assert out.status_code == 200, out.text
    (note,) = _notes(p["id"])
    assert (note["status"], note["method"], note["read_back"]) == ("acknowledged", "phone", 1)
    ack = [e for e in _events(note["id"]) if e["event_type"] == "acknowledged"]
    meta = json.loads(ack[0]["metadata_json"])
    assert meta["closure"] == "ward_read_back" and meta["caller_name"] == "City Lab, Mr Iyer"
    assert meta["source_lane"] == "interfaced"
    lcs.sweep(now=_later(60))
    assert _notes(p["id"])[0]["current_tier"] == 1, "an acknowledged critical never climbs"


async def test_the_laboratory_desk_lists_it_and_its_callback_closes_it(interfaced_on, no_ladder,
                                                                       pushes):
    p = _patient()
    _, lab = _user("pathologist")
    await _ingest(_oru(p["mrn"], value="2.0", flag="LL"))
    body = client.get("/api/lab/criticals", headers=lab)
    assert body.status_code == 200, body.text
    (view,) = [n for n in body.json()["open"] if n["patient"]["id"] == p["id"]]
    assert view["source_lane"] == "interfaced" and view["result_id"] is None
    assert view["lab_flag"] == {"code": "LL", "text": "Critical low"}
    assert view["reference_range"] == "3.5-5.1"
    assert view["result_now"]["text"] == "Final result from the sending laboratory"
    assert view["value_now"] == "2.0 mmol/L" and view["value_told"] == "2.0 mmol/L"
    closed = client.post(f"/api/lab/criticals/{view['id']}/callback", headers=lab,
                         json={"recipient_name": "Sister Rao", "recipient_role": "nurse",
                               "method": "phone", "read_back": True,
                               "value_told": view["value_told"]})
    assert closed.status_code == 200, closed.text
    assert closed.json()["notification"]["acknowledged"]["closure"] == "laboratory_callback"
    assert _notes(p["id"])[0]["status"] == "acknowledged"


# ── 7. A failed raise never loses the result ─────────────────────────────────

async def test_a_failed_raise_keeps_the_result_and_writes_no_critical(interfaced_on, no_ladder,
                                                                      pushes, monkeypatch):
    real = lcs.log_audit_event

    def failing(**kwargs):
        if kwargs.get("action_type") == "lab_critical_raised":
            raise AuditWriteError("audit store down")
        return real(**kwargs)

    monkeypatch.setattr(lcs, "log_audit_event", failing)
    p = _patient()
    res = await _ingest(_oru(p["mrn"]))
    assert res.parse_status == "parsed" and res.inserted_observations == 1
    assert len(_observations(p["id"])) == 1
    assert _notes(p["id"]) == [] and _messages(p["room"]) == []


# ── 8. The service identity ──────────────────────────────────────────────────

async def test_system_lab_cannot_sign_in_and_is_a_service_account(interfaced_on, no_ladder,
                                                                  pushes):
    assert "system_lab" in SERVICE_ACCOUNT_IDS
    p = _patient()
    await _ingest(_oru(p["mrn"]))
    with db_session() as db:
        row = dict(db.execute("SELECT role, department, is_active, password_hash FROM users "
                              "WHERE id = 'system_lab'").fetchone())
    assert row["is_active"] == 0 and row["department"] is None
    assert not row["password_hash"].startswith("$2")
    for password in ("system", "password", ""):
        login = client.post("/api/auth/login",
                            json={"employee_id": "system_lab", "password": password})
        assert login.status_code in (400, 401, 403), login.text
        assert "access_token" not in login.text
    _, admin = _user("admin")
    staff = client.get("/api/admin/users", headers=admin).json()["users"]
    assert "system_lab" not in {u["id"] for u in staff}
    reset = client.post("/api/admin/users/system_lab/reset-password", headers=admin,
                        json={"new_password": "Another-long-password-1"})
    assert reset.status_code == 409, reset.text


def test_an_existing_active_system_lab_row_is_made_inactive():
    with db_session() as db:
        lcs.ensure_system_lab_user(db)
        db.execute("UPDATE users SET is_active = 1 WHERE id = 'system_lab'")
        lcs.ensure_system_lab_user(db)
        assert db.execute("SELECT is_active FROM users WHERE id = 'system_lab'").fetchone()[0] == 0


# ── 9. The module state ──────────────────────────────────────────────────────

def test_an_open_interfaced_critical_never_drains_the_laboratory_while_the_switch_is_on(
        monkeypatch):
    p = _patient()
    nid = f"LC-{uuid.uuid4().hex[:20]}"
    with db_session() as db:
        oid = db.execute(
            "INSERT INTO observations (patient_id, source, observation_code, observation_name, "
            "value_numeric, unit, abnormal_flag, status) "
            "VALUES (?, 'ingested_hl7', '2823-3', 'Potassium', 7.2, 'mmol/L', 'HH', 'F')",
            (p["id"],)).lastrowid
        db.execute(
            "INSERT INTO lab_critical_notifications (id, source_lane, observation_id, patient_id, "
            "test_name, value_display, raised_at, raised_by, tier_due_at, status) "
            "VALUES (?, 'interfaced', ?, ?, 'Potassium', '7.2 mmol/L', ?, 'system_lab', ?, 'open')",
            (nid, oid, p["id"], utc_iso_now(), utc_iso_now()))
    saved = dict(lab_module._drain)
    try:
        monkeypatch.setattr(config, "LAB_ENABLED", False)
        monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", True)
        with db_session() as db:
            on_counts = lab_module.open_work_counts(db)
        monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
        with db_session() as db:
            off_counts = lab_module.open_work_counts(db)
            open_interfaced = db.execute(
                "SELECT COUNT(*) FROM lab_critical_notifications WHERE status = 'open' "
                "AND source_lane = 'interfaced'").fetchone()[0]
        assert open_interfaced >= 1
        assert off_counts["critical_results"] == on_counts["critical_results"] + open_interfaced
        # Switched off with it open: the module drains, so the critical
        # surfaces (the ward card included) stay open.
        config.register_module_drain_check("lab", lab_module.drain_check)
        assert lab_module.refresh_drain_state() is True
        assert config.module_state("lab") == "draining"
        assert lcs.surface_open()
        _, nurse = _user("nurse", WARD)
        card = client.get("/api/lab/criticals/ward", headers=nurse)
        assert card.status_code == 200
        assert nid in {n["id"] for n in card.json()["open"]}
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_critical_notifications WHERE id = ?", (nid,))
        config.register_module_drain_check("lab", None)
        lab_module._drain.update(saved)
        lab_module.refresh_drain_state()
