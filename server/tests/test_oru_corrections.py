"""External corrected results supersede the prior final (lis-20).

With ORB_RESULT_CORRECTIONS on (default OFF), an HL7 OBX-11 'C' replaces the
earlier final result for the SAME patient, test code, OBX-4 sub-id, sender and
order: the same OBR-3 filler number, or, when either side has none, the same
OBR-2 placer number the sender supplied. Never the random group id the adapter
invents for an empty OBR-2. The prior row gets status 'superseded' and
superseded_by on the same transaction as the corrected row, with a fail-closed
clinical_access audit row; if that row cannot be written, nothing changes.

Pinned here:
  * switch off: the correction is stored beside the original, both live, as
    before;
  * switch on: the chart, the ward pack and the Tier 1 read
    (services/lab_observations.py) show only the corrected value;
  * the generated group id never matches, a different patient is never
    superseded, two filler numbers are two orders, a different sub-id or
    sender is a different result;
  * an audit failure rolls the correction back;
  * a second correction supersedes the first; a laboratory-released result is
    never touched; a correction attached from the unmatched results queue
    supersedes exactly as it would have at receipt;
  * the strictest rule (held for Samuel): a correction supersedes only when
    exactly ONE earlier result matches and its own message carries no other
    result for the same test and order. Two isolates under one culture code,
    or two corrections in one message, supersede nothing: the correction is
    stored beside them, with a fail-closed observation_correction_not_applied
    row. A result from the correction's own message is never superseded. The
    same holds on both attach lanes of the unmatched results queue;
  * strictest option (held for Samuel): a result the queue attaches after a
    correction of it, sent later, is already on the chart is stored already
    superseded by that correction when the picture is exactly what receipt in
    order would have made, and refused otherwise (switch off, several
    corrections, a correction that replaced another result, a preliminary,
    two results of one order, an unknown message order). The stale value is
    never the newest row.
"""
from __future__ import annotations

import json
import uuid

import pytest

import config
from database import db_session
from services import ingestion_service, lab_observations, result_quarantine_service
from services.audit_service import AuditWriteError
from services.ingestion_adapters import HL7Adapter, JSONAdapter

SENDER_APP = "LABSYS"
SENDER_FAC = "CITYLAB"


def _patient(name="Ravi Sharma", dob="1970-01-01") -> dict:
    pid = f"PCOR{uuid.uuid4().hex[:8].upper()}"
    mrn = f"MRN-COR-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, date_of_birth, status) VALUES (?, ?, ?, ?, 'discharged')",
            (pid, name, mrn, dob))
    return {"id": pid, "mrn": mrn, "name": name, "dob": dob}


def _oru(identifier: str, *, value="5.1", status="F", placer="PL1", filler="FL1", sub_id="",
         code="2823-3", text="Potassium", sender_fac=SENDER_FAC, name="SHARMA^RAVI",
         dob="19700101", obs_time="20261004080000") -> str:
    return "\r".join([
        f"MSH|^~\\&|{SENDER_APP}|{sender_fac}|ORB|HOSP|20261004081000||ORU^R01|"
        f"{uuid.uuid4().hex[:12]}|P|2.5",
        f"PID|1||{identifier}^^^HOSP^MR||{name}||{dob}|M",
        f"OBR|1|{placer}|{filler}|24323-8^Basic panel^LN|||{obs_time}",
        f"OBX|1|NM|{code}^{text}^LN|{sub_id}|{value}|mmol/L|3.5-5.1|H|||{status}|||{obs_time}",
    ])


def _person() -> dict:
    """A name no other patient in the session database has, for a test that
    attaches from the unmatched results queue by name and date of birth (many
    tests register 'Ravi Sharma', born 1 January 1970, and a name and date of
    birth that two records share confirm neither)."""
    tag = f"Q{uuid.uuid4().hex[:6].upper()}"
    return {"registered": f"Ravi {tag} Sharma", "hl7": f"SHARMA^RAVI {tag}"}


async def _ingest(raw: str):
    res = await ingestion_service.ingest_payload(raw, adapter=HL7Adapter(), source_name="lis20-test")
    return res


def _rows(pid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, value_numeric, status, superseded_by, obr_placer, obr_filler, "
            "obx_sub_id, hl7_sender, obr_group_id FROM observations WHERE patient_id = ? ORDER BY id",
            (pid,)).fetchall()]


def _live_values(pid: str) -> dict:
    with db_session() as db:
        chart = [r["value_numeric"] for r in lab_observations.chart_lab_rows(
            db, pid, per_analyte=10, max_rows=50)]
        pack = [r["value_numeric"] for r in lab_observations.latest_per_analyte(db, pid, limit=20)]
        tier1 = [r["value_numeric"] for r in lab_observations.recent_observations(db, pid, limit=20)]
    return {"chart": chart, "pack": pack, "tier1": tier1}


def _superseded_audits(observation_id: int) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT event_category, action_type, outcome, patient_id, metadata_json FROM audit_logs "
            "WHERE action_type = 'observation_superseded' AND target_id = ?",
            (str(observation_id),)).fetchall()]


@pytest.fixture
def corrections_on(monkeypatch):
    monkeypatch.setattr(config, "RESULT_CORRECTIONS_ENABLED", True)
    assert config.feature_enabled("result_corrections")


# ── 1. The adapter carries the order identity, never the invented id ──────────

def test_the_adapter_keeps_a_sent_placer_and_never_stores_an_invented_one():
    sent = HL7Adapter().parse(_oru("X1", placer="PL7", filler="FL7", sub_id="2"))[0]
    assert (sent.obr_placer, sent.obr_filler, sent.obx_sub_id) == ("PL7", "FL7", "2")
    assert sent.obr_group_id == "PL7" and sent.group_id_generated is False
    assert sent.hl7_sender == f"{SENDER_APP}^{SENDER_FAC}"
    missing = HL7Adapter().parse(_oru("X1", placer="", filler=""))[0]
    assert missing.obr_placer is None and missing.obr_filler is None
    assert missing.group_id_generated is True
    assert missing.obr_group_id and len(missing.obr_group_id) == 8, "obr_group_id keeps its old fallback"


async def test_the_invented_group_id_is_never_written_as_a_placer():
    p = _patient()
    res = await _ingest(_oru(p["mrn"], placer="", filler=""))
    assert res.inserted_observations == 1
    row = _rows(p["id"])[0]
    assert row["obr_placer"] is None and row["obr_filler"] is None
    assert row["obr_group_id"], "obr_group_id is still written exactly as before"


# ── 2. Switch off: stored beside the original, as before ─────────────────────

async def test_switch_off_the_correction_is_stored_beside_the_original():
    assert not config.feature_enabled("result_corrections")
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F"))
    await _ingest(_oru(p["mrn"], value="5.6", status="C"))
    rows = _rows(p["id"])
    assert [r["status"] for r in rows] == ["F", "C"]
    assert all(r["superseded_by"] is None for r in rows)
    assert sorted(_live_values(p["id"])["tier1"]) == [5.1, 5.6]


# ── 3. Switch on: only the corrected value anywhere ──────────────────────────

async def test_switch_on_the_prior_final_is_superseded_everywhere(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F"))
    res = await _ingest(_oru(p["mrn"], value="5.6", status="C"))
    assert not res.errors and res.inserted_observations == 1
    prior, corrected = _rows(p["id"])
    assert prior["status"] == "superseded" and prior["superseded_by"] == corrected["id"]
    assert corrected["status"] == "C" and corrected["superseded_by"] is None
    live = _live_values(p["id"])
    assert live == {"chart": [5.6], "pack": [5.6], "tier1": [5.6]}
    audits = _superseded_audits(prior["id"])
    assert len(audits) == 1
    assert audits[0]["event_category"] == "clinical_access"
    assert audits[0]["patient_id"] == p["id"]
    meta = json.loads(audits[0]["metadata_json"])
    assert meta["superseded_by"] == corrected["id"] and meta["matched_on"] == "filler"
    assert meta["previous_status"] == "F"


async def test_a_sender_supplied_placer_matches_when_there_is_no_filler(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F", placer="PL9", filler=""))
    await _ingest(_oru(p["mrn"], value="5.6", status="C", placer="PL9", filler=""))
    prior, corrected = _rows(p["id"])
    assert prior["superseded_by"] == corrected["id"]
    assert json.loads(_superseded_audits(prior["id"])[0]["metadata_json"])["matched_on"] == "placer"


async def test_a_second_correction_supersedes_the_first(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F"))
    await _ingest(_oru(p["mrn"], value="5.6", status="C"))
    await _ingest(_oru(p["mrn"], value="5.4", status="C"))
    first, second, third = _rows(p["id"])
    assert first["superseded_by"] == second["id"]
    assert second["superseded_by"] == third["id"]
    assert _live_values(p["id"])["tier1"] == [5.4]


# ── 4. What must never match ─────────────────────────────────────────────────

async def test_the_generated_group_id_never_matches(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F", placer="", filler=""))
    invented = _rows(p["id"])[0]["obr_group_id"]
    # Neither another message with no order numbers, nor one whose OBR-2
    # happens to equal the invented id, may supersede it.
    await _ingest(_oru(p["mrn"], value="5.6", status="C", placer="", filler=""))
    await _ingest(_oru(p["mrn"], value="5.7", status="C", placer=invented, filler=""))
    rows = _rows(p["id"])
    assert rows[0]["superseded_by"] is None and rows[0]["status"] == "F"
    assert 5.1 in _live_values(p["id"])["tier1"]


async def test_a_different_patient_is_never_superseded(corrections_on):
    a, b = _patient(), _patient(name="Asha Rao", dob="1980-02-02")
    await _ingest(_oru(a["mrn"], value="5.1", status="F"))
    await _ingest(_oru(b["mrn"], value="5.6", status="C", name="RAO^ASHA", dob="19800202"))
    assert _rows(a["id"])[0]["superseded_by"] is None
    assert _live_values(a["id"])["tier1"] == [5.1]


async def test_two_filler_numbers_are_two_orders_whatever_the_placer(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F", placer="PL1", filler="FL1"))
    await _ingest(_oru(p["mrn"], value="5.6", status="C", placer="PL1", filler="FL2"))
    assert _rows(p["id"])[0]["superseded_by"] is None


async def test_a_different_sub_id_or_sender_is_a_different_result(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F", sub_id="1"))
    await _ingest(_oru(p["mrn"], value="5.6", status="C", sub_id="2"))
    await _ingest(_oru(p["mrn"], value="5.7", status="C", sub_id="1", sender_fac="OTHERLAB"))
    assert _rows(p["id"])[0]["superseded_by"] is None
    await _ingest(_oru(p["mrn"], value="5.8", status="C", sub_id="1"))
    rows = _rows(p["id"])
    assert rows[0]["superseded_by"] == rows[-1]["id"]


async def test_a_preliminary_result_is_not_replaced(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="P"))
    await _ingest(_oru(p["mrn"], value="5.6", status="C"))
    assert _rows(p["id"])[0]["superseded_by"] is None


async def test_a_laboratory_released_result_is_never_touched(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F"))
    prior = _rows(p["id"])[0]
    with db_session() as db:
        db.execute("UPDATE observations SET lab_result_id = 424242 WHERE id = ?", (prior["id"],))
    await _ingest(_oru(p["mrn"], value="5.6", status="C"))
    assert _rows(p["id"])[0]["superseded_by"] is None


async def test_a_json_correction_has_no_order_numbers_and_is_stored_as_before(corrections_on):
    p = _patient()
    for value, status in ((5.1, "final"), (5.6, "C")):
        await ingestion_service.ingest_payload(json.dumps({
            "patient_id": p["mrn"], "message_id": uuid.uuid4().hex,
            "observations": [{"code": "2823-3", "name": "Potassium", "value": value,
                              "unit": "mmol/L", "status": status}]}),
            adapter=JSONAdapter(), source_name="lis20-json")
    assert all(r["superseded_by"] is None for r in _rows(p["id"]))


# ── 5. The audit row is part of the correction ───────────────────────────────

async def test_an_audit_failure_rolls_the_correction_back(corrections_on, monkeypatch):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="5.1", status="F"))
    real = ingestion_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "observation_superseded":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(ingestion_service, "log_audit_event", failing)
    res = await _ingest(_oru(p["mrn"], value="5.6", status="C"))
    assert res.errors, "the failed correction is reported"
    rows = _rows(p["id"])
    assert len(rows) == 1, "the corrected row was rolled back with the audit"
    assert rows[0]["superseded_by"] is None and rows[0]["status"] == "F"


# ── 6. The same rule on the unmatched results queue's attach ─────────────────

async def test_a_correction_attached_from_the_queue_supersedes_the_prior_final(
        corrections_on, monkeypatch):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    who = _person()
    p = _patient(name=who["registered"])
    await _ingest(_oru(p["mrn"], value="5.1", status="F"))
    # The correction arrives under a number that matches nobody.
    res = await _ingest(_oru("EXT-UNKNOWN-77", value="5.6", status="C", name=who["hl7"]))
    assert res.quarantined == 1
    with db_session() as db:
        qid = db.execute("SELECT id FROM result_quarantine WHERE ingestion_event_id = ?",
                         (res.event_id,)).fetchone()["id"]
    out = result_quarantine_service.attach(
        qid, hospital_number=p["mrn"], name=who["registered"], date_of_birth="1970-01-01",
        reason="External lab number; confirmed on the request form",
        user={"id": "LABTECH-COR", "role": "lab_technician"})
    assert out["attached"] is True
    prior, corrected = _rows(p["id"])
    assert prior["superseded_by"] == corrected["id"] == out["observation_id"]
    assert out["superseded"] == [prior["id"]]
    assert _live_values(p["id"])["tier1"] == [5.6]


# ── 7. Strictest rule: one correction hides at most one result, and only when
#       the test and order name exactly one ───────────────────────────────────

CULTURE = "600-7"


def _oru_many(identifier: str, results: list[tuple[str, str]], *, placer="PL1", filler="FL1",
              code=CULTURE, text="Bacteria identified", name="SHARMA^RAVI", dob="19700101",
              obs_time="20261004080000") -> str:
    """One message, one order, several text results under the same test code
    and a blank OBX-4: results is [(value, OBX-11 status), ...]."""
    segments = [
        f"MSH|^~\\&|{SENDER_APP}|{SENDER_FAC}|ORB|HOSP|20261004081000||ORU^R01|"
        f"{uuid.uuid4().hex[:12]}|P|2.5",
        f"PID|1||{identifier}^^^HOSP^MR||{name}||{dob}|M",
        f"OBR|1|{placer}|{filler}|630-4^Urine culture^LN|||{obs_time}",
    ]
    for i, (value, status) in enumerate(results, start=1):
        segments.append(
            f"OBX|{i}|ST|{code}^{text}^LN||{value}||||||{status}|||{obs_time}")
    return "\r".join(segments)


def _text_rows(pid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, value_text, status, superseded_by, source_event_id FROM observations "
            "WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()]


def _live_texts(pid: str) -> dict:
    """The live text results (the chart's lab card is numeric only, so the
    shared live reader that Tier 1 and the timeline use stands for all)."""
    with db_session() as db:
        tier1 = sorted(r["value_text"] for r in lab_observations.recent_observations(
            db, pid, limit=20))
    return {"tier1": tier1}


def _not_applied(observation_id: int) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT event_category, outcome, patient_id, metadata_json FROM audit_logs "
            "WHERE action_type = 'observation_correction_not_applied' AND target_id = ?",
            (str(observation_id),)).fetchall()
    return [{**dict(r), "metadata": json.loads(r["metadata_json"])} for r in rows]


def _any_superseded(pid: str) -> bool:
    return any(r["superseded_by"] is not None or r["status"] == "superseded"
               for r in _text_rows(pid))


@pytest.mark.parametrize("one_message", [True, False], ids=["one-message", "two-messages"])
async def test_two_isolates_on_one_order_are_not_superseded_by_one_correction(
        corrections_on, one_message):
    p = _patient()
    if one_message:
        await _ingest(_oru_many(p["mrn"], [("E. coli", "F"), ("Klebsiella pneumoniae", "F")]))
    else:
        await _ingest(_oru_many(p["mrn"], [("E. coli", "F")]))
        await _ingest(_oru_many(p["mrn"], [("Klebsiella pneumoniae", "F")]))
    res = await _ingest(_oru_many(p["mrn"], [("E. coli (ESBL)", "C")]))
    assert not res.errors and res.inserted_observations == 1
    assert not _any_superseded(p["id"]), "neither isolate may be hidden"
    every = ["E. coli", "E. coli (ESBL)", "Klebsiella pneumoniae"]
    assert _live_texts(p["id"]) == {"tier1": every}
    rows = _text_rows(p["id"])
    corrected = rows[-1]
    audits = _not_applied(corrected["id"])
    assert len(audits) == 1
    assert audits[0]["event_category"] == "clinical_access" and audits[0]["patient_id"] == p["id"]
    assert audits[0]["metadata"]["reason"] == "several_earlier_results"
    assert sorted(audits[0]["metadata"]["candidates"]) == [rows[0]["id"], rows[1]["id"]]
    for prior in rows[:2]:
        assert _superseded_audits(prior["id"]) == []


async def test_two_corrections_in_one_message_supersede_nothing(corrections_on):
    p = _patient()
    await _ingest(_oru_many(p["mrn"], [("E. coli", "F")]))
    res = await _ingest(_oru_many(p["mrn"], [("E. coli (ESBL)", "C"), ("Proteus mirabilis", "C")]))
    assert not res.errors and res.inserted_observations == 2
    assert not _any_superseded(p["id"]), "the message does not say which correction replaces it"
    every = ["E. coli", "E. coli (ESBL)", "Proteus mirabilis"]
    assert _live_texts(p["id"])["tier1"] == every
    original, first, second = _text_rows(p["id"])
    # The first was marked before anything was written (the whole message is
    # known at receipt); the second also finds the first in the database.
    first_audit = _not_applied(first["id"])[0]["metadata"]
    assert first_audit["reason"] == "several_results_in_message"
    assert first_audit["sibling_in_message"] is True and first_audit["candidates"] == [original["id"]]
    second_audit = _not_applied(second["id"])[0]["metadata"]
    assert second_audit["same_message"] == [first["id"]]


async def test_a_correction_never_supersedes_a_result_from_its_own_message(corrections_on):
    p = _patient()
    # No earlier result: the two corrections of one message stand side by side.
    await _ingest(_oru_many(p["mrn"], [("A2", "C"), ("B2", "C")]))
    assert not _any_superseded(p["id"])
    assert _live_texts(p["id"])["tier1"] == ["A2", "B2"]
    # A final and a correction in one message, after an earlier final: the
    # correction hides neither, the test and order name two results.
    q = _patient()
    await _ingest(_oru_many(q["mrn"], [("E. coli", "F")]))
    await _ingest(_oru_many(q["mrn"], [("Klebsiella pneumoniae", "F"), ("E. coli (ESBL)", "C")]))
    assert not _any_superseded(q["id"])
    assert _live_texts(q["id"])["tier1"] == ["E. coli", "E. coli (ESBL)", "Klebsiella pneumoniae"]


async def test_one_isolate_and_one_correction_still_supersedes(corrections_on):
    p = _patient()
    await _ingest(_oru_many(p["mrn"], [("E. coli", "F")]))
    await _ingest(_oru_many(p["mrn"], [("E. coli (ESBL)", "C")]))
    prior, corrected = _text_rows(p["id"])
    assert prior["superseded_by"] == corrected["id"]
    assert _live_texts(p["id"])["tier1"] == ["E. coli (ESBL)"]
    assert _not_applied(corrected["id"]) == []


async def test_switch_off_two_isolates_and_a_correction_are_all_stored_as_before():
    assert not config.feature_enabled("result_corrections")
    p = _patient()
    await _ingest(_oru_many(p["mrn"], [("E. coli", "F"), ("Klebsiella pneumoniae", "F")]))
    await _ingest(_oru_many(p["mrn"], [("E. coli (ESBL)", "C")]))
    assert not _any_superseded(p["id"])
    assert _not_applied(_text_rows(p["id"])[-1]["id"]) == []


async def test_an_audit_failure_on_a_correction_not_applied_rolls_it_back(
        corrections_on, monkeypatch):
    p = _patient()
    await _ingest(_oru_many(p["mrn"], [("E. coli", "F"), ("Klebsiella pneumoniae", "F")]))
    real = ingestion_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "observation_correction_not_applied":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(ingestion_service, "log_audit_event", failing)
    res = await _ingest(_oru_many(p["mrn"], [("E. coli (ESBL)", "C")]))
    assert res.errors
    assert [r["value_text"] for r in _text_rows(p["id"])] == ["E. coli", "Klebsiella pneumoniae"]


async def test_queue_attach_of_one_of_two_corrections_in_a_message_supersedes_nothing(
        corrections_on, monkeypatch):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    who = _person()
    p = _patient(name=who["registered"])
    await _ingest(_oru_many(p["mrn"], [("E. coli", "F")]))
    res = await _ingest(_oru_many(f"EXT-{uuid.uuid4().hex[:6].upper()}",
                                  [("E. coli (ESBL)", "C"), ("Proteus mirabilis", "C")],
                                  name=who["hl7"]))
    assert res.quarantined == 2
    with db_session() as db:
        qids = [r["id"] for r in db.execute(
            "SELECT id FROM result_quarantine WHERE ingestion_event_id = ? ORDER BY id",
            (res.event_id,)).fetchall()]
    out = result_quarantine_service.attach(
        qids[0], hospital_number=p["mrn"], name=who["registered"], date_of_birth="1970-01-01",
        reason="Outside lab number; form checked",
        user={"id": "LABTECH-COR", "role": "lab_technician"})
    assert out["attached"] is True and out["superseded"] == []
    assert not _any_superseded(p["id"])
    meta = _not_applied(out["observation_id"])[0]["metadata"]
    assert meta["sibling_in_message"] is True
    assert _live_texts(p["id"])["tier1"] == ["E. coli", "E. coli (ESBL)"]


async def test_swept_attach_of_one_of_two_corrections_in_a_message_supersedes_nothing(
        corrections_on, monkeypatch):
    who = _person()
    p = _patient(name=who["registered"])
    await _ingest(_oru_many(p["mrn"], [("E. coli", "F")]))
    # The queue is off: both corrections are stored with no patient.
    res = await _ingest(_oru_many(f"EXT-{uuid.uuid4().hex[:6].upper()}",
                                  [("E. coli (ESBL)", "C"), ("Proteus mirabilis", "C")],
                                  name=who["hl7"]))
    assert res.inserted_observations == 2 and not res.quarantined
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    result_quarantine_service.sweep_unlinked()
    with db_session() as db:
        swept = [dict(r) for r in db.execute(
            "SELECT id, source_observation_id FROM result_quarantine "
            "WHERE ingestion_event_id = ? ORDER BY id", (res.event_id,)).fetchall()]
    assert len(swept) == 2 and all(r["source_observation_id"] for r in swept)
    out = result_quarantine_service.attach(
        swept[0]["id"], hospital_number=p["mrn"], name=who["registered"], date_of_birth="1970-01-01",
        reason="Outside lab number; form checked",
        user={"id": "LABTECH-COR", "role": "lab_technician"})
    assert out["attached"] is True and out["superseded"] == []
    assert not _any_superseded(p["id"])
    meta = _not_applied(out["observation_id"])[0]["metadata"]
    # Found in the database: the other correction is a row of the same message.
    assert meta["same_message"] == [swept[1]["source_observation_id"]]
    assert meta["reason"] == "several_results_in_message"


# ── 8. A result attached late, after a correction of it (strictest option) ───
#
# The queue can attach a final long after the laboratory sent a correction of
# it. Written as it stands, the stale final would be the newest row (same
# observed time, higher id): the ward pack would show only it and Tier 1 would
# read it first. Pinned: when the picture is what receipt in order would have
# made, the final is stored already superseded by the correction; otherwise the
# attach is refused and the final stays in the queue.

def _queue_ids(event_id: int) -> list[int]:
    with db_session() as db:
        return [r["id"] for r in db.execute(
            "SELECT id FROM result_quarantine WHERE ingestion_event_id = ? ORDER BY id",
            (event_id,)).fetchall()]


def _attach_late(qid: int, who: dict, p: dict) -> dict:
    return result_quarantine_service.attach(
        qid, hospital_number=p["mrn"], name=who["registered"], date_of_birth="1970-01-01",
        reason="Outside lab number; form checked",
        user={"id": "LABTECH-COR", "role": "lab_technician"})


def _pack(pid: str) -> list[tuple]:
    with db_session() as db:
        return [(r["value_numeric"], r["status"])
                for r in lab_observations.latest_per_analyte(db, pid, limit=20)]


def _queue_status(qid: int) -> str:
    with db_session() as db:
        return db.execute("SELECT status FROM result_quarantine WHERE id = ?",
                          (qid,)).fetchone()["status"]


@pytest.fixture
def both_on(corrections_on, monkeypatch):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)


async def test_a_final_attached_after_its_queued_correction_is_stored_superseded(both_on):
    """The reviewer's case: a final of 6.9 and then a correction of 4.2 for
    the same patient, order, test and sender, both unmatched. The list shows
    the correction first; attaching top-down must not leave 6.9 on top."""
    who = _person()
    p = _patient(name=who["registered"])
    ext = f"EXT-{uuid.uuid4().hex[:6].upper()}"
    final = await _ingest(_oru(ext, value="6.9", status="F", name=who["hl7"]))
    corrected = await _ingest(_oru(ext, value="4.2", status="C", name=who["hl7"]))
    first = _attach_late(_queue_ids(corrected.event_id)[0], who, p)
    assert first["attached"] and first["superseded"] == [] and first["superseded_by"] is None
    late = _attach_late(_queue_ids(final.event_id)[0], who, p)
    assert late["attached"] is True and late["superseded"] == []
    assert late["superseded_by"] == first["observation_id"]
    rows = {r["id"]: r for r in _rows(p["id"])}
    assert rows[late["observation_id"]]["status"] == "superseded"
    assert rows[late["observation_id"]]["superseded_by"] == first["observation_id"]
    assert _live_values(p["id"]) == {"chart": [4.2], "pack": [4.2], "tier1": [4.2]}
    assert _pack(p["id"]) == [(4.2, "C")]
    audits = _superseded_audits(late["observation_id"])
    assert len(audits) == 1 and audits[0]["event_category"] == "clinical_access"
    meta = json.loads(audits[0]["metadata_json"])
    assert meta["superseded_by"] == first["observation_id"]
    assert meta["attached_after_correction"] is True and meta["previous_status"] == "F"


async def test_a_final_attached_after_a_correction_stored_at_receipt(both_on):
    who = _person()
    p = _patient(name=who["registered"])
    final = await _ingest(_oru(f"EXT-{uuid.uuid4().hex[:6].upper()}", value="6.9", status="F",
                               name=who["hl7"]))
    corrected = await _ingest(_oru(p["mrn"], value="4.2", status="C"))
    assert corrected.inserted_observations == 1
    out = _attach_late(_queue_ids(final.event_id)[0], who, p)
    assert out["attached"] is True and out["superseded_by"] is not None
    assert _live_values(p["id"]) == {"chart": [4.2], "pack": [4.2], "tier1": [4.2]}


async def test_a_swept_final_attached_after_a_later_correction_is_stored_superseded(
        corrections_on, monkeypatch):
    who = _person()
    p = _patient(name=who["registered"])
    # The queue is off: the final is stored with no patient.
    final = await _ingest(_oru(f"EXT-{uuid.uuid4().hex[:6].upper()}", value="6.9", status="F",
                               name=who["hl7"]))
    assert final.inserted_observations == 1 and not final.quarantined
    await _ingest(_oru(p["mrn"], value="4.2", status="C"))
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    result_quarantine_service.sweep_unlinked()
    qid = _queue_ids(final.event_id)[0]
    out = _attach_late(qid, who, p)
    assert out["attached"] is True and out["superseded_by"] is not None
    assert _live_values(p["id"]) == {"chart": [4.2], "pack": [4.2], "tier1": [4.2]}
    assert _superseded_audits(out["observation_id"])


async def test_with_corrections_off_a_final_after_a_later_correction_is_refused(monkeypatch):
    """Switch off, nothing supersedes anything, so the stale final cannot be
    placed behind the correction: the attach is refused and the final waits."""
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    assert not config.feature_enabled("result_corrections")
    who = _person()
    p = _patient(name=who["registered"])
    final = await _ingest(_oru(f"EXT-{uuid.uuid4().hex[:6].upper()}", value="6.9", status="F",
                               name=who["hl7"]))
    await _ingest(_oru(p["mrn"], value="4.2", status="C"))
    qid = _queue_ids(final.event_id)[0]
    out = _attach_late(qid, who, p)
    assert out["attached"] is False and out["refusal"] == "later_correction_on_chart"
    assert _queue_status(qid) == "open"
    assert _live_values(p["id"])["tier1"] == [4.2]
    with db_session() as db:
        refusal = db.execute(
            "SELECT event_category, outcome, metadata_json FROM audit_logs WHERE action_type = "
            "'lab_quarantine_attach_refused' AND target_id = ? ORDER BY id DESC", (str(qid),)
        ).fetchone()
    assert refusal["event_category"] == "clinical_access" and refusal["outcome"] == "failure"
    assert json.loads(refusal["metadata_json"])["why"] == "corrections_switched_off"


async def test_a_correction_that_already_replaced_a_result_refuses_the_late_final(both_on):
    who = _person()
    p = _patient(name=who["registered"])
    await _ingest(_oru(p["mrn"], value="5.0", status="F"))
    late_final = await _ingest(_oru(f"EXT-{uuid.uuid4().hex[:6].upper()}", value="6.9",
                                    status="F", name=who["hl7"]))
    await _ingest(_oru(p["mrn"], value="4.2", status="C"))  # replaces the 5.0 at receipt
    qid = _queue_ids(late_final.event_id)[0]
    out = _attach_late(qid, who, p)
    assert out["attached"] is False and out["refusal"] == "later_correction_on_chart"
    assert _queue_status(qid) == "open"
    assert _live_values(p["id"])["tier1"] == [4.2]


async def test_a_late_preliminary_after_a_correction_is_refused(both_on):
    who = _person()
    p = _patient(name=who["registered"])
    prelim = await _ingest(_oru(f"EXT-{uuid.uuid4().hex[:6].upper()}", value="6.9", status="P",
                                name=who["hl7"]))
    await _ingest(_oru(p["mrn"], value="4.2", status="C"))
    out = _attach_late(_queue_ids(prelim.event_id)[0], who, p)
    assert out["attached"] is False and out["refusal"] == "later_correction_on_chart"


async def test_one_of_two_isolates_attached_after_a_correction_is_refused(both_on):
    """One correction hides at most one result: with two isolates of one
    order waiting, the correction cannot be pinned to either."""
    who = _person()
    p = _patient(name=who["registered"])
    isolates = await _ingest(_oru_many(f"EXT-{uuid.uuid4().hex[:6].upper()}",
                                       [("E. coli", "F"), ("Klebsiella pneumoniae", "F")],
                                       name=who["hl7"]))
    await _ingest(_oru_many(p["mrn"], [("E. coli (ESBL)", "C")]))
    for qid in _queue_ids(isolates.event_id):
        out = _attach_late(qid, who, p)
        assert out["attached"] is False and out["refusal"] == "later_correction_on_chart"
    assert not _any_superseded(p["id"])


async def test_a_correction_from_an_earlier_message_leaves_a_later_final_live(both_on):
    """As at receipt: a correction only ever replaces what came before it."""
    who = _person()
    p = _patient(name=who["registered"])
    await _ingest(_oru(p["mrn"], value="4.2", status="C"))
    final = await _ingest(_oru(f"EXT-{uuid.uuid4().hex[:6].upper()}", value="6.9", status="F",
                               name=who["hl7"]))
    out = _attach_late(_queue_ids(final.event_id)[0], who, p)
    assert out["attached"] is True and out["superseded_by"] is None
    assert sorted(_live_values(p["id"])["tier1"]) == [4.2, 6.9]


async def test_an_audit_failure_on_the_late_supersede_rolls_the_attach_back(both_on, monkeypatch):
    who = _person()
    p = _patient(name=who["registered"])
    final = await _ingest(_oru(f"EXT-{uuid.uuid4().hex[:6].upper()}", value="6.9", status="F",
                               name=who["hl7"]))
    await _ingest(_oru(p["mrn"], value="4.2", status="C"))
    real = ingestion_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "observation_superseded":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(ingestion_service, "log_audit_event", failing)
    qid = _queue_ids(final.event_id)[0]
    with pytest.raises(AuditWriteError):
        _attach_late(qid, who, p)
    assert _queue_status(qid) == "open"
    assert [r["value_numeric"] for r in _rows(p["id"])] == [4.2]


async def test_an_unknown_message_order_refuses(corrections_on):
    p = _patient()
    await _ingest(_oru(p["mrn"], value="4.2", status="C"))
    with db_session() as db:
        decision = ingestion_service.later_correction_for_attach(
            db, patient_id=p["id"], code="2823-3", sub_id="", placer="PL1", filler="FL1",
            sender=f"{SENDER_APP}^{SENDER_FAC}", source="ingested_hl7", status="F",
            event_id=None)
    assert decision["outcome"] == "refuse" and decision["reason"] == "message_order_unknown"


def test_correction_order_rules():
    order = ingestion_service.correction_order
    assert order("PL1", "FL1", "PL9", "FL1") == "filler"
    assert order("PL1", "FL1", "PL1", "FL2") is None, "two filler numbers are two orders"
    assert order("PL1", None, "PL1", "FL2") == "placer"
    assert order(None, "FL1", "PL1", None) is None
    assert order(None, None, None, None) is None
