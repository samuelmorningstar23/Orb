"""The chart route carries the recorded facts the chart and its prints need.

The chart's lab card used to read only lab_result documents and, finding none,
drew values it estimated from the diagnosis; interfaced and imported results in
`observations` never reached it. The printed record had no admission id, no
problem list and no comorbidities to print. Pinned here: the route returns
structured lab results with the time each was observed (withdrawn or erroneous
ones left out), the active problems, the comorbidities and the admission id.

The lab window keeps the NEWEST results. It first shipped as an oldest-first
LIMIT 1000, so a chronic patient with more imported results than that lost the
whole admission and the card showed a years-old value as the latest.
"""
import json
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from routes.vigil_routes import _CHART_LAB_RESULTS_MAX, _CHART_LAB_RESULTS_PER_ANALYTE

client = TestClient(app)


def _world():
    doctor = f"CLO{uuid.uuid4().hex[:8].upper()}"
    pid = f"CLOP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, 'Dr Lab', 'doctor', 'x', 'Chart Labs Medicine')",
            (doctor,),
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date)
               VALUES (?, 'Lab Patient', ?, 'L-1', 'CL-1', 61, 'F', 'Anaemia', 'admitted', '2026-10-01 06:00:00')""",
            (pid, f"MRN-{pid[-6:]}"),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'Chart Labs Medicine', 'CL-1', 'L-1', 'active', ?)",
            (f"ADM-{pid}", pid, doctor),
        )
        db.execute(
            "INSERT OR REPLACE INTO patient_profiles (patient_id, comorbidities) VALUES (?, ?)",
            (pid, json.dumps(["Chronic kidney disease"])),
        )
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, icd10_code, status) VALUES (?, 'Iron deficiency anaemia', 'D50.9', 'active')",
            (pid,),
        )
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, status) VALUES (?, 'Resolved UTI', 'resolved')",
            (pid,),
        )
        for value, status, observed in ((8.1, "final", "2026-10-01 07:00:00"),
                                        (8.9, "final", "2026-10-02 19:30:00"),
                                        (2.0, "entered-in-error", "2026-10-02 20:00:00")):
            db.execute(
                """INSERT INTO observations (patient_id, source, observation_name, value_numeric, unit,
                                             reference_range, abnormal_flag, status, observed_at)
                   VALUES (?, 'ingested_hl7', 'Haemoglobin', ?, 'g/dL', '12-16', 'L', ?, ?)""",
                (pid, value, status, observed),
            )
    return doctor, pid


def test_the_chart_carries_recorded_labs_problems_and_the_admission():
    doctor, pid = _world()
    resp = client.get(f"/api/vigil/patients/{pid}",
                      headers={"Authorization": f"Bearer {create_token(doctor, 'doctor')}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    labs = body["lab_observations"]
    assert [(o["value_numeric"], o["observed_at"]) for o in labs] == [
        (8.1, "2026-10-01 07:00:00"), (8.9, "2026-10-02 19:30:00"),
    ], "an erroneous result reached the chart, or a real one did not"
    assert labs[0]["reference_range"] == "12-16" and labs[0]["unit"] == "g/dL"

    assert [p["problem"] for p in body["problems"]] == ["Iron deficiency anaemia"]
    assert body["patient"]["comorbidities"] == ["Chronic kidney disease"]
    assert body["patient"]["admission_id"] == f"ADM-{pid}"


def _bare_patient(prefix):
    """A doctor and an admitted patient in their department, with no labs."""
    doctor = f"{prefix}{uuid.uuid4().hex[:8].upper()}"
    pid = f"{prefix}P{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, 'Dr Lab', 'doctor', 'x', 'Chart Labs Medicine')",
            (doctor,),
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date)
               VALUES (?, 'Chronic Patient', ?, 'L-2', 'CL-1', 67, 'M', 'Chronic kidney disease', 'admitted', '2026-10-01 06:00:00')""",
            (pid, f"MRN-{pid[-6:]}"),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'Chart Labs Medicine', 'CL-1', 'L-2', 'active', ?)",
            (f"ADM-{pid}", pid, doctor),
        )
    return doctor, pid


def _insert_results(pid, rows):
    """rows: (name, value, unit, observed_at, source)."""
    with db_session() as db:
        db.executemany(
            """INSERT INTO observations (patient_id, source, observation_name, value_numeric, unit,
                                         status, observed_at)
               VALUES (?, ?, ?, ?, ?, 'final', ?)""",
            [(pid, source, name, value, unit, observed) for name, value, unit, observed, source in rows],
        )


def _chart_labs(doctor, pid):
    resp = client.get(f"/api/vigil/patients/{pid}",
                      headers={"Authorization": f"Bearer {create_token(doctor, 'doctor')}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["lab_observations"]


def _hourly(start, n):
    t0 = datetime.fromisoformat(start)
    return [(t0 + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(n)]


def test_years_of_imported_history_do_not_push_the_admission_off_the_chart():
    doctor, pid = _bare_patient("CLH")
    history = _hourly("2023-01-01 00:00:00", _CHART_LAB_RESULTS_MAX + 100)
    _insert_results(pid, [("Creatinine", 2.0 + (i % 10) / 10, "mg/dL", ts, "history_import")
                          for i, ts in enumerate(history)])
    # One old result for a second analyte, then the current admission.
    _insert_results(pid, [("Haemoglobin", 10.4, "g/dL", "2024-03-01 09:00:00", "history_import"),
                          ("Creatinine", 4.6, "mg/dL", "2026-10-01 07:00:00", "ingested_hl7"),
                          ("Creatinine", 5.3, "mg/dL", "2026-10-02 07:00:00", "ingested_hl7")])

    labs = _chart_labs(doctor, pid)
    creat = [o for o in labs if o["observation_name"] == "Creatinine"]
    assert [(o["value_numeric"], o["observed_at"]) for o in creat[-2:]] == [
        (4.6, "2026-10-01 07:00:00"), (5.3, "2026-10-02 07:00:00"),
    ], "the admission's results were cut and an imported one is shown as the latest"
    # The newest per analyte, nothing older than the window.
    assert len(creat) == _CHART_LAB_RESULTS_PER_ANALYTE
    assert creat[0]["observed_at"] == history[-(_CHART_LAB_RESULTS_PER_ANALYTE - 2)]
    # A busy analyte does not crowd out a quiet one.
    assert [o["value_numeric"] for o in labs if o["observation_name"] == "Haemoglobin"] == [10.4]
    # Oldest first: the card takes the reference range from the newest row.
    stamps = [o["observed_at"] for o in labs]
    assert stamps == sorted(stamps)


def test_the_overall_cap_keeps_every_analytes_latest_result_first():
    doctor, pid = _bare_patient("CLC")
    analytes = (_CHART_LAB_RESULTS_MAX // _CHART_LAB_RESULTS_PER_ANALYTE) + 5
    rows = []
    for a in range(analytes):
        for i, ts in enumerate(_hourly(f"2026-0{1 + a % 9}-01 00:00:00", _CHART_LAB_RESULTS_PER_ANALYTE + 10)):
            rows.append((f"Analyte {a:02d}", float(i), "u", ts, "history_import"))
    _insert_results(pid, rows)

    labs = _chart_labs(doctor, pid)
    assert len(labs) == _CHART_LAB_RESULTS_MAX
    newest = _CHART_LAB_RESULTS_PER_ANALYTE + 9
    for a in range(analytes):
        mine = [o["value_numeric"] for o in labs if o["observation_name"] == f"Analyte {a:02d}"]
        assert mine and mine[-1] == newest, f"Analyte {a:02d} lost its latest result to the cap"


def test_results_are_ranked_by_time_not_by_how_the_timestamp_was_written(monkeypatch):
    # Interfaced rows keep observed_at as received. As strings, 'T09' sorts
    # after ' 10' on the same day, so a string sort would call the 09:00
    # result the newest.
    monkeypatch.setattr("routes.vigil_routes._CHART_LAB_RESULTS_PER_ANALYTE", 1)
    doctor, pid = _bare_patient("CLT")
    _insert_results(pid, [("Potassium", 5.1, "mmol/L", "2026-10-02T09:00:00", "ingested_hl7"),
                          ("Potassium", 6.2, "mmol/L", "2026-10-02 10:00:00", "ingested_json")])
    assert [o["value_numeric"] for o in _chart_labs(doctor, pid)] == [6.2]
