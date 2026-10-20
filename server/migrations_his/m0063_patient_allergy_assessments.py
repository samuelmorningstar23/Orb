"""Migration 63: the allergy assessment record (opd-10a, ruling OPD-S7).

WHY. Orb could not tell "no known allergies" from "never asked": an empty
patient_allergies list means either, and the order-time and bedside
interlocks screen both the same way (services/allergy_interlock.py). An OPD
prescription leaves the building with nobody else checking it, so ruling
OPD-S7 (option a, applied) makes signing an outpatient note wait for an
allergy review by the consulting doctor. This table is where that review is
written down.

One new table, empty until a site switches the 'opd' feature on (ORB_OPD,
which also needs ORB_REGISTRATION and the front desk). Nothing is altered, no
column is added to an existing table, and nothing here is a foreign key, so no
table becomes an FK parent. One statement per conn.execute, never
executescript (it would commit the runner's transaction).

patient_allergy_assessments: one row per allergy review, append-only.

   Columns:
     id              integer key
     patient_id      the patient asked
     status          'none_known'          the patient was asked and knows of
                                           no allergy (nothing is charted)
                     'allergies_recorded'  the patient was asked and the
                                           allergies named are on the chart
                                           (newly charted, or confirmed)
                     'unable_to_obtain'    nobody could ask (for example the
                                           patient cannot speak and nobody with
                                           them knows); reason is required
     substances_json the allergens named at this review, as charted, for
                     'allergies_recorded' (a JSON list); NULL otherwise
     reason          why the allergies could not be obtained (required for
                     'unable_to_obtain', checked by the database too)
     note            free text the doctor added
     opd_visit_id    the OPD visit the review was made in
     admission_id    reserved for an inpatient review (no writer yet)
     department      the clinic the review was made in
     source          where it was made: 'opd' for now
     assessed_by     who asked (users.id)
     assessed_at     when, a UTC instant

   The assessment never changes how the interlock screens: the allergies
   themselves live in patient_allergies (written through
   clinical_records_service.record_allergies_on in the same transaction), and
   a 'none_known' row writes no allergy row at all.

   An UPDATE is refused by a trigger, whoever issues it: a review is what was
   said at the time, and a later review is a new row.

DELETE IS NOT REFUSED, on purpose, the same choice migrations 60, 61 and 62
made: routes/admin_routes._build_erase_plan deletes every patient-keyed table
it is not told to keep, and tests/test_erase_plan_retained.py fails the plan
if it would touch a table with a delete-refusing trigger. Nothing in Orb
deletes from this table; a retention order signed by a person
(consent_service) still can. Keeping it through an erasure instead is one
registry line in admin_routes._ERASE_RETAINED plus a DELETE trigger, for
Samuel to choose.

The table is registered for retention review in consent_service
(_RETENTION_SOURCES, wave 1, with patient_id and assessed_at) and in the
restore verifier's manifest (scripts/lib/verify_restore.py, group
'outpatient consultations').
"""

from migrations import Migration

ASSESSMENTS_DDL = """CREATE TABLE IF NOT EXISTS patient_allergy_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('none_known', 'allergies_recorded', 'unable_to_obtain')),
    substances_json TEXT,
    reason TEXT,
    note TEXT,
    opd_visit_id TEXT,
    admission_id TEXT,
    department TEXT,
    source TEXT NOT NULL DEFAULT 'opd',
    assessed_by TEXT NOT NULL CHECK (length(trim(assessed_by)) > 0),
    assessed_at TEXT NOT NULL,
    CHECK (status != 'unable_to_obtain' OR length(trim(COALESCE(reason, ''))) > 0)
)"""

TABLE_NAMES = ("patient_allergy_assessments",)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_patient_allergy_assessments_patient "
    "ON patient_allergy_assessments(patient_id, assessed_at)",
    "CREATE INDEX IF NOT EXISTS idx_patient_allergy_assessments_visit "
    "ON patient_allergy_assessments(opd_visit_id)",
)

TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS trg_patient_allergy_assessments_no_update
       BEFORE UPDATE ON patient_allergy_assessments
       BEGIN
           SELECT RAISE(ABORT, 'patient_allergy_assessments is append-only: record a new review instead');
       END""",
)


def apply(conn) -> None:
    conn.execute(ASSESSMENTS_DDL)
    for statement in INDEXES:
        conn.execute(statement)
    for statement in TRIGGERS:
        conn.execute(statement)


MIGRATION = Migration(63, "patient_allergy_assessments", apply)
