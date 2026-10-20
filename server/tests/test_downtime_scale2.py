"""The downtime paper must say which SpO2 table its NEWS2 was scored on.

WHY THIS FILE EXISTS (found 2026-10-02, NEWS2 render-site sweep). During an
outage a nurse re-scores NEWS2 by hand from the downtime print pack. Scale 2 is
the SpO2 table for COPD / chronic type-2 respiratory failure (target 88-92%),
chosen per patient by `patients.on_scale_2`. capture_snapshot already scored the
snapshot on the right table and stored `news2.spo2_scale`, but nothing printed
it, so a Scale 2 patient on air at SpO2 90 printed

    SpO₂ 90%    NEWS2 0 (stable)

with no marker. On the default Scale 1 paper chart SpO2 90 scores 3, so the
re-scored sheet reads NEWS2 3 (elevated) and contradicts the pack it came from.

Pinned here: the per-patient vitals row, the census index row and the text
summary carry the Scale 2 marker only when spo2_scale == 2, and their Scale 1
output is byte-identical to what it was. The last test drives the real
capture_snapshot and the real ward-pack build end to end. Nothing in Orb can SET
on_scale_2 yet (gap register X003), so the test sets the column directly.
"""
from __future__ import annotations

import uuid

import pytest

import services.downtime_service as ds
from database import db_session
from services.downtime_service import (
    _build_summary,
    _render_vitals_news2,
    _render_ward_pack,
    build_ward_print_packs,
    capture_snapshot,
    get_latest_snapshot,
)

SCALE_2_BAND = " · SpO₂ Scale 2"


@pytest.fixture(autouse=True)
def _hospital_zone_ist(monkeypatch):
    """The pack prints times on the ward's clock (F025); pin the zone so the
    byte-identical rows below do not depend on the machine running the suite."""
    from zoneinfo import ZoneInfo
    from services.vitals_source import hl7v2
    monkeypatch.setattr(hl7v2, "_default_tz", ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(hl7v2, "_default_tz_resolved", True)

SCALE_2_SPO2 = "<br><small>Scale 2, target 88-92%</small>"

# Normal on every parameter except SpO2, so the SpO2 table alone decides the
# score: SpO2 90 on air is 0 points on Scale 2 and 3 points on Scale 1.
_VITALS_ON_AIR = {
    "heart_rate": 80, "systolic_bp": 125, "diastolic_bp": 75, "spo2": 90,
    "respiratory_rate": 16, "temperature": 37.0, "consciousness": "alert",
    "on_oxygen": 0, "timestamp": "2026-10-02T08:00:00",
}

_DEMO = {"name": "Asha Rao", "mrn": "MRN-1", "age": 71, "sex": "F",
         "ward": "Ward 7", "bed": "B2"}


def _snap(news2: dict | None, **vitals) -> dict:
    return {
        "demographics": dict(_DEMO),
        "allergies": [], "problems": [], "medications": [], "recent_notes": [],
        "latest_vitals": {**_VITALS_ON_AIR, **vitals},
        "news2": news2,
    }


# ── The per-patient vitals row ───────────────────────────────────────────────

# The Scale 1 row exactly as it printed before this change. Any byte that moves
# here moves on every patient's printed page.
_SCALE_1_ROW = (
    "<table><thead><tr><th>HR</th><th>BP</th><th>SpO₂</th><th>RR</th>"
    "<th>Temp</th><th>ACVPU</th><th>Pain</th><th>NEWS2</th><th>Recorded</th></tr></thead>"
    "<tbody><tr><td>80</td><td>125/75</td><td>90%</td><td>16</td><td>37.0</td>"
    "<td>alert</td><td>—</td><td><b>3 (elevated)</b></td><td>2 Oct 2026, 13:30 IST</td>"
    "</tr></tbody></table>"
)


def test_scale_1_vitals_row_is_byte_identical():
    html = _render_vitals_news2(
        _snap({"raw_news2": 3, "level": "elevated", "spo2_scale": 1}))
    assert html == _SCALE_1_ROW


def test_a_snapshot_stored_before_spo2_scale_existed_prints_as_scale_1():
    """Snapshots captured before the key was stored have no spo2_scale at all.
    They were scored on Scale 1 and must keep printing unmarked."""
    html = _render_vitals_news2(_snap({"raw_news2": 3, "level": "elevated"}))
    assert html == _SCALE_1_ROW


def test_scale_2_vitals_row_marks_the_spo2_cell_and_the_band():
    html = _render_vitals_news2(
        _snap({"raw_news2": 0, "level": "stable", "spo2_scale": 2}))
    assert f"<td>90%{SCALE_2_SPO2}</td>" in html, html
    assert f"<td><b>0 (stable){SCALE_2_BAND}</b></td>" in html, html
    # The value itself is still the charted value, and only the marked cells
    # changed: strip the two markers and the row is the plain Scale 1 shape.
    plain = html.replace(SCALE_2_SPO2, "").replace(SCALE_2_BAND, "")
    assert plain == _SCALE_1_ROW.replace("3 (elevated)", "0 (stable)")


def test_scale_2_on_oxygen_keeps_the_oxygen_note_before_the_marker():
    html = _render_vitals_news2(
        _snap({"raw_news2": 3, "level": "stable", "spo2_scale": 2},
              spo2=94, on_oxygen=1))
    assert f"<td>94% on O₂{SCALE_2_SPO2}</td>" in html, html
    assert f"<td><b>3 (stable){SCALE_2_BAND}</b></td>" in html, html


def test_scale_2_marker_is_absent_from_every_scale_1_variant():
    for news2, vitals in (
        ({"raw_news2": 3, "level": "elevated", "spo2_scale": 1}, {}),
        ({"raw_news2": 2, "level": "stable", "spo2_scale": 1},
         {"spo2": 97, "on_oxygen": 1}),
        ({"raw_news2": 3, "level": "elevated"}, {}),
    ):
        html = _render_vitals_news2(_snap(news2, **vitals))
        assert "Scale 2" not in html and "88-92" not in html, html


# ── The census index row ─────────────────────────────────────────────────────

def _item(news2: dict | None, *, bed="B2", name="Asha Rao", mrn="MRN-1") -> dict:
    return {
        "bed": bed, "name": name, "mrn": mrn, "ward": "Ward 7",
        "code_status": "Full code", "escalation": None,
        "envelope": {"created_at": "2026-10-02T07:55:00+00:00",
                     "created_by": "auto", "snapshot": _snap(news2)},
    }


def _index_row(doc: str, mrn: str) -> str:
    """The census index row for one MRN (the index table comes first)."""
    index = doc.split("</table>", 1)[0]
    for row in index.split("<tr>"):
        if f"<td>{mrn}</td>" in row:
            return "<tr>" + row.split("</tr>", 1)[0] + "</tr>"
    raise AssertionError(f"no index row for {mrn}")


def test_census_index_row_scale_1_is_byte_identical():
    doc = _render_ward_pack(
        "Ward 7",
        [_item({"raw_news2": 3, "level": "elevated", "spo2_scale": 1})],
        "2026-10-02T08:00:00+00:00",
    )
    assert _index_row(doc, "MRN-1") == (
        "<tr><td>B2</td><td>Asha Rao</td><td>MRN-1</td><td>3 (elevated)</td>"
        "<td>2 Oct 2026, 13:25 IST</td></tr>"
    )
    assert "Scale 2" not in doc


def test_census_index_row_marks_a_scale_2_patient_only():
    doc = _render_ward_pack(
        "Ward 7",
        [_item({"raw_news2": 0, "level": "stable", "spo2_scale": 2}),
         _item({"raw_news2": 3, "level": "elevated", "spo2_scale": 1},
               bed="B3", name="Ravi Iyer", mrn="MRN-2")],
        "2026-10-02T08:00:00+00:00",
    )
    assert _index_row(doc, "MRN-1") == (
        f"<tr><td>B2</td><td>Asha Rao</td><td>MRN-1</td><td>0 (stable){SCALE_2_BAND}</td>"
        "<td>2 Oct 2026, 13:25 IST</td></tr>"
    )
    assert _index_row(doc, "MRN-2") == (
        "<tr><td>B3</td><td>Ravi Iyer</td><td>MRN-2</td><td>3 (elevated)</td>"
        "<td>2 Oct 2026, 13:25 IST</td></tr>"
    )


def test_census_index_row_without_a_snapshot_is_unchanged():
    item = _item(None)
    item["envelope"] = None
    doc = _render_ward_pack("Ward 7", [item], "2026-10-02T08:00:00+00:00")
    assert "NO SNAPSHOT" in _index_row(doc, "MRN-1")
    assert "Scale 2" not in doc


# ── The text summary ─────────────────────────────────────────────────────────

def test_summary_scale_1_is_byte_identical():
    assert _build_summary(
        dict(_DEMO), [], [], [], {"raw_news2": 3, "level": "elevated", "spo2_scale": 1},
    ) == ("Asha Rao (MRN MRN-1, 71F) (Ward 7 / B2). No allergies recorded. "
          "0 active problems. 0 active meds. NEWS2 3 (elevated).")
    assert _build_summary(
        dict(_DEMO), [], [], [], {"raw_news2": 3, "level": "elevated"},
    ).endswith("NEWS2 3 (elevated).")


def test_summary_names_scale_2():
    assert _build_summary(
        dict(_DEMO), [], [], [], {"raw_news2": 0, "level": "stable", "spo2_scale": 2},
    # news2_text's label since 2026-10-03, the board badge's sentence.
    ).endswith("NEWS2 0 (stable) · SpO2 Scale 2.")


# ── End to end: the real capture and the real ward-pack build ────────────────

_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_created():
    """Every admitted patient in the shared session DB is walked by the ward
    pack build and the FHIR self-export (see conftest _sweep_admitted_leftovers),
    so each one created here is discharged on teardown, never deleted."""
    yield
    if _CREATED:
        with db_session() as db:
            db.executemany(
                "UPDATE patients SET status = 'discharged' WHERE id = ?",
                [(p,) for p in _CREATED],
            )
        _CREATED.clear()


def _mk_patient(ward: str, on_scale_2: int) -> tuple[str, str]:
    pid = f"PATS2{uuid.uuid4().hex[:8].upper()}"
    mrn = f"MRN-S2-{uuid.uuid4().hex[:6]}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status,
                                     age, sex, on_scale_2)
               VALUES (?, 'Scale Two Test', ?, 'B1', ?, 'COPD exacerbation',
                       'admitted', 71, 'F', ?)""",
            (pid, mrn, ward, on_scale_2),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen)
               VALUES (?, '2026-10-02T08:00:00+00:00', 80, 125, 75, 90, 37.0, 16,
                       'alert', 0)""",
            (pid,),
        )
    _CREATED.append(pid)
    return pid, mrn


def _patient_page(doc: str, mrn: str) -> str:
    for section in doc.split('<section class="wp-page">')[2:]:
        if f"<b>MRN</b>{mrn}<" in section:
            return section
    raise AssertionError(f"no patient page for {mrn}")


def test_a_scale_2_patient_is_marked_through_capture_and_the_ward_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    ward = f"WardS2-{uuid.uuid4().hex[:6]}"
    copd, copd_mrn = _mk_patient(ward, on_scale_2=1)
    twin, twin_mrn = _mk_patient(ward, on_scale_2=0)

    # The real capture scores on the patient's own table and stores which one.
    env = capture_snapshot(copd, created_by="auto")
    news2 = env["snapshot"]["news2"]
    assert news2["spo2_scale"] == 2
    assert news2["raw_news2"] == 0 and news2["level"] == "stable"
    assert env["summary"].endswith("NEWS2 0 (stable) · SpO2 Scale 2.")
    stored = get_latest_snapshot(copd)
    assert stored["snapshot"]["news2"]["spo2_scale"] == 2
    assert stored["summary"] == env["summary"]

    # The same vitals on Scale 1 score 3: the difference the marker explains.
    twin_env = capture_snapshot(twin, created_by="auto")
    assert twin_env["snapshot"]["news2"]["spo2_scale"] == 1
    assert twin_env["snapshot"]["news2"]["raw_news2"] == 3
    # SpO2 90 on Scale 1 is a single parameter scoring 3, and the label says so.
    assert twin_env["summary"].endswith("NEWS2 3 (elevated) · a single parameter scores 3.")

    # The real ward-pack build, read back from the file a nurse would print.
    build_ward_print_packs(created_by="auto")
    with open(ds._ward_pack_path(tmp_path, ward, "latest"), encoding="utf-8") as f:
        doc = f.read()

    assert f"<td>0 (stable){SCALE_2_BAND}</td>" in _index_row(doc, copd_mrn)
    page = _patient_page(doc, copd_mrn)
    assert f"<td>90%{SCALE_2_SPO2}</td>" in page, page
    assert f"<td><b>0 (stable){SCALE_2_BAND}</b></td>" in page, page

    # The NEWS2 cell carries news2_text's clauses after the band (2026-10-03).
    assert "<td>3 (elevated) · a single parameter scores 3</td>" in _index_row(doc, twin_mrn)
    twin_page = _patient_page(doc, twin_mrn)
    assert ("<td>90%</td>" in twin_page
            and "<td><b>3 (elevated) · a single parameter scores 3</b></td>" in twin_page)
    assert "Scale 2" not in twin_page
