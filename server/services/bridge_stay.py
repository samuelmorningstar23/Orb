"""R029: current-stay portal readers; historical records remain in the chart."""
from services.stay_times import latest_admission, stay_start_stored


def current_summary(db, patient_id):
    admission = latest_admission(db, patient_id)
    if admission is None:
        return db.execute("SELECT * FROM discharge_summaries WHERE patient_id=? ORDER BY julianday(discharged_at) DESC,id DESC LIMIT 1",
                          (patient_id,)).fetchone()
    return db.execute("""SELECT * FROM discharge_summaries WHERE patient_id=? AND
        (admission_id=? OR (admission_id IS NULL AND julianday(discharged_at)>=julianday(?)))
        ORDER BY julianday(discharged_at) DESC,id DESC LIMIT 1""",
        (patient_id, admission["id"], stay_start_stored(admission))).fetchone()


def current_medications(db, patient_id):
    admission = latest_admission(db, patient_id)
    where, args = "m.patient_id=?", [patient_id]
    if admission is not None:
        # Explicit encounter provenance wins over a legacy projection's time.
        # Unlinked legacy rows must have a readable time inside this stay.
        where += " AND (o.admission_id=? OR (o.admission_id IS NULL AND julianday(m.started_at)>=julianday(?)))"
        args.extend((admission["id"], stay_start_stored(admission)))
    return db.execute(f"""SELECT m.drug_name,m.dosage,m.route,m.frequency,m.status,m.started_at,m.held_at,m.hold_reason
        FROM medications m LEFT JOIN med_orders o ON o.legacy_medication_id=m.id AND o.patient_id=m.patient_id
        WHERE {where} ORDER BY CASE m.status WHEN 'active' THEN 0 WHEN 'prn' THEN 1 ELSE 2 END,
        julianday(m.started_at) DESC,m.id DESC""", args).fetchall()


def vitals_boundary(db, patient_id):
    admission = latest_admission(db, patient_id)
    if admission is None:
        return None
    from services.vigil_service import current_stay_start_on
    boundary = current_stay_start_on(db, patient_id)
    return boundary.isoformat() if boundary is not None else stay_start_stored(admission) or "unreadable"


def current_vitals(db, patient_id, *, limit=48):
    boundary = vitals_boundary(db, patient_id)
    where, args = "patient_id=? AND voided_at IS NULL", [patient_id]
    if boundary is not None:
        where += " AND julianday(timestamp)>=julianday(?)"
        args.append(boundary)
    return db.execute(f"SELECT * FROM vitals WHERE {where} ORDER BY julianday(timestamp) DESC,id DESC LIMIT ?",
                      (*args, limit)).fetchall()
