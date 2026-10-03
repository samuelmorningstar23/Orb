"""Orb — I-PASS Shift-Handoff Automation tests.

Seeds a patient with recent vitals, active meds, allergies and problems, then
verifies build_ipass produces all 5 populated I-PASS sections and that saving a
handoff persists a scribe_notes row with note_format='handoff'.
"""
import re
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from database import db_session
from services import ipass_service
from services.ipass_service import (
    build_ipass,
    save_ipass_handoff,
    get_recent_handoffs,
    humanise_enum,
)

PATIENT_ID = "PAT-IPASS-1"
AUTHOR_ID = "D001"  # seeded by conftest


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture(autouse=True)
def seed_ipass_patient():
    """Create an isolated patient with a full clinical picture over the last 12h."""
    now = datetime.now(timezone.utc)
    with db_session() as db:
        # Clean any prior state for this patient
        for tbl in ("vitals", "medications", "patient_allergies", "problem_list",
                    "care_bundles", "scribe_notes", "risk_scores"):
            db.execute(f"DELETE FROM {tbl} WHERE patient_id = ?", (PATIENT_ID,))
        db.execute("DELETE FROM patients WHERE id = ?", (PATIENT_ID,))

        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status, age, sex)
               VALUES (?, ?, ?, ?, ?, ?, 'admitted', ?, ?)""",
            (PATIENT_ID, "Ada I-Pass", "MRN-IPASS-1", "12B", "ICU",
             "Community-acquired pneumonia", 71, "F"),
        )

        # Vitals across the window — an escalating (concerning) trend.
        vitals_rows = [
            (now - timedelta(hours=10), 88, 128, 74, 97, 37.0, 18, "alert"),
            (now - timedelta(hours=6), 104, 112, 68, 94, 38.2, 22, "alert"),
            (now - timedelta(hours=2), 118, 98, 60, 91, 38.9, 26, "alert"),
            (now - timedelta(minutes=20), 122, 94, 58, 90, 39.1, 28, "alert"),
        ]
        for ts, hr, sbp, dbp, spo2, temp, rr, cons in vitals_rows:
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate, consciousness)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (PATIENT_ID, _iso(ts), hr, sbp, dbp, spo2, temp, rr, cons),
            )

        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status)
               VALUES (?, 'Piperacillin-Tazobactam', '4.5g', 'IV', 'Q8H', 'active')""",
            (PATIENT_ID,),
        )
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status)
               VALUES (?, 'Paracetamol', '1g', 'Oral', 'Q6H PRN', 'active')""",
            (PATIENT_ID,),
        )
        # A discontinued med must NOT appear in the action list.
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status)
               VALUES (?, 'Old Drug', '10mg', 'Oral', 'OD', 'discontinued')""",
            (PATIENT_ID,),
        )

        db.execute(
            """INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status)
               VALUES (?, 'penicillin', 'rash', 'moderate', 'active')""",
            (PATIENT_ID,),
        )
        db.execute(
            """INSERT INTO problem_list (patient_id, problem, icd10_code, status)
               VALUES (?, 'Type 2 diabetes mellitus', 'E11', 'active')""",
            (PATIENT_ID,),
        )
        db.execute(
            """INSERT INTO problem_list (patient_id, problem, status)
               VALUES (?, 'Sepsis secondary to pneumonia', 'active')""",
            (PATIENT_ID,),
        )
    yield
    with db_session() as db:
        for tbl in ("vitals", "medications", "patient_allergies", "problem_list",
                    "care_bundles", "scribe_notes", "risk_scores"):
            db.execute(f"DELETE FROM {tbl} WHERE patient_id = ?", (PATIENT_ID,))
        db.execute("DELETE FROM patients WHERE id = ?", (PATIENT_ID,))


def test_build_ipass_has_all_five_sections_populated():
    draft = build_ipass(PATIENT_ID)

    assert draft["patient_id"] == PATIENT_ID
    assert draft["window_hours"] == 12
    assert draft["source_summary"]
    assert draft["plain_text"]

    sections = draft["sections"]
    for key in ("illness_severity", "patient_summary", "action_list",
                "situation_awareness", "synthesis"):
        assert key in sections, f"missing section {key}"
        assert sections[key], f"empty section {key}"

    # I — illness severity mapped from NEWS2 band.
    isv = sections["illness_severity"]
    assert isv["severity"] in ("Stable", "Watcher", "Unstable")
    assert isv["text"]

    # P — patient summary carries demographics, diagnosis, problems, allergies.
    ps = sections["patient_summary"]
    assert ps["name"] == "Ada I-Pass"
    assert ps["age"] == 71
    assert ps["ward"] == "ICU"
    assert "pneumonia" in (ps["diagnosis"] or "").lower()
    assert any("diabetes" in p.lower() for p in ps["problems"])
    assert any("penicillin" in a.lower() for a in ps["allergies"])

    # A — action list has the active meds (not the discontinued one).
    al = sections["action_list"]
    med_names = {m["drug_name"] for m in al["active_medications"]}
    assert "Piperacillin-Tazobactam" in med_names
    assert "Paracetamol" in med_names
    assert "Old Drug" not in med_names
    assert isinstance(al["outstanding_items"], list)

    # S — situation awareness: vitals trend + contingencies ("if X then Y").
    sa = sections["situation_awareness"]
    assert sa["readings_count"] >= 3
    assert sa["latest_vitals"]["heart_rate"] is not None
    assert len(sa["contingencies"]) >= 1
    assert all(c.lower().startswith("if ") for c in sa["contingencies"])

    # S — synthesis: read-back placeholder for the receiver.
    sy = sections["synthesis"]
    assert sy["read_back_required"] is True
    assert sy["confirmed"] is False
    assert "read-back" in sy["text"].lower()


def test_plain_text_contains_all_section_headers():
    draft = build_ipass(PATIENT_ID)
    text = draft["plain_text"]
    assert "ILLNESS SEVERITY" in text
    assert "PATIENT SUMMARY" in text
    assert "ACTION LIST" in text
    assert "SITUATION AWARENESS" in text
    assert "SYNTHESIS BY RECEIVER" in text


def test_save_ipass_handoff_persists_handoff_note():
    draft = build_ipass(PATIENT_ID)
    note_id = save_ipass_handoff(PATIENT_ID, AUTHOR_ID, draft)
    assert isinstance(note_id, int)

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM scribe_notes WHERE id = ?", (note_id,)
        ).fetchone()
    assert row is not None
    row = dict(row)
    assert row["patient_id"] == PATIENT_ID
    assert row["author_id"] == AUTHOR_ID
    assert row["note_format"] == "handoff"
    assert row["status"] == "draft"
    assert row["raw_transcript"]          # source summary
    assert "I-PASS" in row["structured_note"]  # rendered handoff


def test_get_recent_handoffs_returns_saved_note():
    draft = build_ipass(PATIENT_ID)
    note_id = save_ipass_handoff(PATIENT_ID, AUTHOR_ID, draft)

    handoffs = get_recent_handoffs(PATIENT_ID)
    assert any(h["id"] == note_id for h in handoffs)
    assert all(h["note_format"] == "handoff" for h in handoffs)


def test_build_ipass_unknown_patient_raises():
    with pytest.raises(ValueError):
        build_ipass("NO-SUCH-PATIENT-" + uuid.uuid4().hex)


# ═════════════════════════════════════════════════════════════════════════════
#  THE PRINTED SHEET SPEAKS ENGLISH  (prove-can-fail)
# ═════════════════════════════════════════════════════════════════════════════
#
# THE DEFECT. `plain_text` is the print/copy source of truth for the handover —
# the sheet that is printed at shift change, pasted into a message and kept as
# the medico-legal record of what was handed over. It rendered
#
#     Trend over the shift: insufficient_data (insufficient recent data)
#
# a raw Python identifier standing next to its own English translation. The
# browser has a formatter for this (src/lib/humanise.ts) and it cannot help: the
# sheet is assembled entirely on the server and never passes through it.
#
# The assertions below are made against the ACTUAL PRODUCED STRING, and the
# general one — no snake_case token anywhere in the sheet — is what makes this
# hold for enum values nobody has invented yet.

#: A lower-case machine identifier: `insufficient_data`, `done_on_time`,
#: `urine_output`.
_SNAKE_CASE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")


def _machine_tokens(text: str) -> list[str]:
    return sorted(set(_SNAKE_CASE.findall(text)))


def _risk(level: str) -> dict:
    return {"score": 0.5, "level": level, "raw_news2": 6,
            "drivers": ["Tachycardia (HR 122)"], "scoring_model": "NEWS2"}


#: Every value `vigil_service.get_vitals_trend` can return, plus one that does
#: not exist yet — a vocabulary grows, and a translation TABLE alone would let
#: the next addition leak straight onto the sheet.
@pytest.mark.parametrize("trend_value,expected_phrase", [
    ("improving", "Trend over the shift: improving"),
    ("deteriorating", "Trend over the shift: deteriorating"),
    ("stable", "Trend over the shift: stable"),
    ("insufficient_data", "Trend over the shift: not enough data to call a trend"),
    # Not a value today. The point of the generic humaniser: a vocabulary that
    # grows must not be able to leak a new identifier onto the sheet.
    ("awaiting_second_window", "Trend over the shift: awaiting second window"),
])
def test_no_machine_enum_reaches_the_printed_handover_trend(
        trend_value, expected_phrase, monkeypatch):
    monkeypatch.setattr(ipass_service, "get_vitals_trend",
                        lambda pid, hours=6: {"trend": trend_value,
                                              "reason": "no earlier vitals to compare against"})
    draft = build_ipass(PATIENT_ID)
    text = draft["plain_text"]

    assert expected_phrase in text, (
        f"expected {expected_phrase!r} on the sheet; got:\n"
        + next((ln for ln in text.splitlines() if "Trend over the shift" in ln), "(no trend line)")
    )
    assert not _machine_tokens(text), (
        f"machine identifiers on a medico-legal handover: {_machine_tokens(text)}"
    )
    # The structured payload KEEPS the raw enum — the wire format is not renamed,
    # only the prose is written in English.
    assert draft["sections"]["situation_awareness"]["trend"] == trend_value
    assert "Trend over the shift:" in text


@pytest.mark.parametrize("level,expected_phrase", [
    ("critical", "NEWS2 6 (critical band)"),
    ("elevated", "NEWS2 6 (elevated band)"),
    ("stable", "NEWS2 6 (stable band)"),
    ("not_scored", "NEWS2 6 (not scored)"),
])
def test_no_machine_enum_reaches_the_printed_handover_severity(
        level, expected_phrase, monkeypatch):
    monkeypatch.setattr(ipass_service, "risk_for_row", lambda row, **kw: _risk(level))
    draft = build_ipass(PATIENT_ID)
    text = draft["plain_text"]

    assert expected_phrase in text, (
        f"expected {expected_phrase!r}; got:\n"
        + next((ln for ln in text.splitlines() if "NEWS2" in ln), "(no severity line)")
    )
    assert not _machine_tokens(text), (
        f"machine identifiers on a medico-legal handover: {_machine_tokens(text)}"
    )
    assert draft["sections"]["illness_severity"]["news2_level"] == level
    # The source summary is stored as the note's `raw_transcript` and is read by
    # a human too, so it is held to the same standard.
    assert not _machine_tokens(draft["source_summary"])


@pytest.mark.parametrize("bundle_type,expected", [
    ("sepsis6", "Sepsis Six"),
    ("deterioration", "Deterioration"),
    ("stroke_thrombolysis", "Stroke thrombolysis"),
])
def test_care_bundle_names_are_printed_as_names(bundle_type, expected, monkeypatch):
    """An open bundle appears twice in the sheet — the bundle line and each
    outstanding item — and both used to print the machine key."""
    import services.sepsis_bundle_service as bundles

    monkeypatch.setattr(bundles, "get_bundles_for_patient", lambda pid, active_only=True: [{
        "bundle_type": bundle_type,
        "compliance_pct": 50.0,
        "completion_pct": 50.0,
        "elements": [
            {"key": "antibiotics", "label": "Give IV antibiotics", "state": "overdue"},
            {"key": "lactate", "label": "Measure serum lactate", "state": "pending"},
        ],
    }])

    draft = build_ipass(PATIENT_ID)
    text = draft["plain_text"]

    assert f"[{expected}]" in text, f"bundle line does not name the bundle:\n{text}"
    assert f"OVERDUE ({expected}): Give IV antibiotics" in text
    assert f"Pending ({expected}): Measure serum lactate" in text
    assert not _machine_tokens(text), _machine_tokens(text)
    # Raw key still on the wire for anything that matches on it.
    assert draft["sections"]["action_list"]["open_care_bundles"][0]["bundle_type"] == bundle_type


def test_insufficient_data_prints_the_reason_not_a_repeated_phrase(monkeypatch):
    """The honest version of the original line.

    "insufficient_data (insufficient recent data)" said one thing twice and one
    of the two in Python. `get_vitals_trend` states WHY it cannot compare, and
    that reason is the half a receiving nurse can act on: a patient who arrived
    an hour ago has no baseline, which is different from a broken feed.
    """
    monkeypatch.setattr(ipass_service, "get_vitals_trend",
                        lambda pid, hours=6: {"trend": "insufficient_data",
                                              "reason": "no earlier vitals to compare against"})
    draft = build_ipass(PATIENT_ID)
    line = next(ln for ln in draft["plain_text"].splitlines()
                if "Trend over the shift" in ln)
    assert "not enough data to call a trend" in line
    assert "no earlier vitals to compare against" in line
    assert "insufficient_data" not in line


def test_humanise_enum_rules():
    """The server's own copy of src/lib/humanise.ts, stated as a table."""
    assert humanise_enum("insufficient_data") == "insufficient data"
    assert humanise_enum("insufficient_data", sentence=True) == "Insufficient data"
    assert humanise_enum("spo2_delta") == "SpO2 delta"
    assert humanise_enum("news2_level", sentence=True) == "NEWS2 level"
    assert humanise_enum("stable") == "stable"
    assert humanise_enum(None) == "not recorded"
    assert humanise_enum("", "nothing") == "nothing"


# ═════════════════════════════════════════════════════════════════════════════
#  THE HANDOVER PROSE IS WRITTEN FOR A NURSE, NOT A LOG
# ═════════════════════════════════════════════════════════════════════════════

def test_situation_awareness_prose_drops_the_readings_count():
    """"Readings analysed: 4." was an input to the trend printed as if it were a
    fact about the patient. The count stays on the wire for anything that
    wants it; the sentence a nurse reads does not carry it."""
    draft = build_ipass(PATIENT_ID)
    sa = draft["sections"]["situation_awareness"]

    assert "Readings analysed" not in sa["text"]
    assert "Readings analysed" not in draft["plain_text"]
    assert sa["readings_count"] >= 3
    # The deterioration flag still closes the sentence on this escalating chart.
    assert sa["active_deterioration"] is True
    assert sa["text"].endswith("ACTIVE DETERIORATION: watch closely.")


def test_read_back_is_labelled_prompts_not_underscore_blanks():
    """The synthesis prose carried "Confirmed by: ____________  at ____________",
    which on screen reads as a template nobody finished. It is now a sentence,
    with the sign-off as structured prompts that the printed sheet gives one
    labelled line each."""
    draft = build_ipass(PATIENT_ID)
    sy = draft["sections"]["synthesis"]

    assert "_" not in sy["text"]
    assert "read-back" in sy["text"].lower()
    assert sy["prompts"] == ["Outstanding questions", "Confirmed by", "Time"]

    text = draft["plain_text"]
    assert "____" not in text
    tail = text[text.index("S. SYNTHESIS BY RECEIVER"):].splitlines()
    assert tail[-3:] == ["   Outstanding questions:", "   Confirmed by:", "   Time:"]
