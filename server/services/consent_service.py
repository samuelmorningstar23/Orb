"""Orb Hospital OS — DPDP Act 2023 machinery.

Five things a hospital operating under the Digital Personal Data Protection Act
2023 must be able to DO, not merely claim in a policy PDF:

  1. CONSENT      — record what was consented to, when, by what means, and by
                    whom it was taken; and let it be withdrawn, with the
                    withdrawal recorded as a NEW fact rather than an edit.
  2. RIGHTS       — produce, on request, everything Orb holds about ONE data
                    principal, in a form a person can read (s.11 right to access).
  3. RETENTION    — a stated policy per category, and a way to SEE what is past
                    it. Orb never deletes on a timer; it surfaces the list.
  4. ERASURE      — s.12(3), adjudicated against Indian medical-record law rather
                    than obeyed blindly. See THE COLLISION below.
  5. BREACH       — s.8(6): what was exposed, to whom, when, and who was told.

EVERYTHING HERE IS BLOCKING. Every public function opens a db_session and must
be called from a sync `def` route (FastAPI threadpools those) or through
``run_db`` from an async one — the 2026-07-30 loop stalls were sync SQLite on the
event loop and this module would reproduce them exactly.


════════════════════════════════════════════════════════════════════════════════
 THE COLLISION: DPDP ERASURE vs AN APPEND-ONLY AUDIT TRAIL vs INDIAN MEDICAL-
 RECORD RETENTION LAW — and the position Orb takes
════════════════════════════════════════════════════════════════════════════════

Three obligations point in different directions and cannot all be honoured
naively:

  (A) DPDP s.12(3) gives the Data Principal the right to erasure of their
      personal data, and s.8(7) obliges the Data Fiduciary to erase on
      withdrawal of consent or when the purpose is served.

  (B) Indian law COMPELS a hospital to keep medical records. IMC (Professional
      Conduct, Etiquette and Ethics) Regulations 2002 Reg. 1.3.1 requires indoor
      patient records for 3 years from the commencement of treatment. The Drugs
      and Cosmetics Rules 1945 require dispensing registers. Limitation Act 1963
      and Consumer Protection Act 2019 windows mean a record destroyed on
      request may be the very record the hospital needs to defend itself — or
      the record a coroner needs.

  (C) Orb's audit trail is a SHA-256 hash chain with BEFORE UPDATE/DELETE
      triggers. Deleting one row does not just remove that row; it breaks
      verification for EVERY row after it, so an erasure honoured inside the
      audit table would destroy the tamper-evidence of the entire hospital's
      access history to prove one person's point.

Naive readings, and why each fails:

  * "Erase everything on request."  Illegal under (B) and self-destructive
    under (C). It also erases the evidence that the erasure itself was carried
    out lawfully — the hospital would have complied and be unable to prove it.
  * "Refuse everything, we're a hospital."  Wrong, and the easy hiding place.
    A hospital's marketing list, a Bridge portal login, a stale dictation
    recording and a year of AI scratch notes are not medical records and no
    Indian law requires their retention. Refusing those is simply
    non-compliance dressed as clinical seriousness.
  * "Anonymise instead."  Attractive and mostly false in a ward system. A
    clinical narrative with the name removed is still re-identifiable from the
    ward, the dates and the diagnosis; claiming otherwise to a regulator is
    worse than not doing it.

ORB'S POSITION, and it is encoded in `retention_policies.erasable`, not argued
per request:

  1. THE LAWFUL BASIS DECIDES, NOT THE DATA TYPE. Personal data Orb holds
     because a LAW REQUIRES IT (basis `legal_obligation`) is out of reach of
     s.12(3) — DPDP's own text carves out retention "necessary for compliance
     with any law for the time being in force". Personal data Orb holds because
     the principal AGREED (basis `consent`) is in reach and is erased.

  2. THE REFUSAL IS ITSELF A DELIVERABLE. A refused category returns a stated
     reason AND the citation that compels retention, sealed into an immutable
     `erasure_decisions` row. "No" with a statute attached is a lawful answer;
     silence is not. A principal who disagrees can take that exact row to the
     Board.

  3. THE AUDIT TRAIL IS NEVER ERASED, AND ORB SAYS SO OUT LOUD. This is the
     sharpest edge and the one most likely to be argued. Orb's ground is that
     audit rows are records of ACCESS (who opened which chart, when), retained
     under s.8(5) reasonable-security safeguards, and that the chain is the
     control — an audit trail that can be edited on request is not an audit
     trail. Orb does not pretend this costs the principal nothing: an erasure
     request LEAVES A PERMANENT TRACE, because the request, the decision and the
     execution are all audited. That residue is stated to the principal in the
     decision rather than hidden.

  4. ERASURE IS ADJUDICATED, THEN EXECUTED, AND NEVER ON A TIMER. Deciding is
     one act (`adjudicate_erasure_request`) and destroying is a second, separate,
     explicitly-triggered act (`execute_erasure_request`). Retention overdue
     never auto-deletes anything (`retention_review`); it produces a list for a
     human. A background job that quietly destroyed a medical record because a
     day count elapsed would be the worst bug this system could have.

  5. WHAT ORB CANNOT YET ERASE, IT REFUSES TO CLAIM. A category may be erasable
     in law but not yet executable in code (`executable: False`), and the
     decision says so in plain words. See `_ERASURE_EXECUTORS`.

This is Orb's defensible position, not settled law: DPDP's healthcare rules were
still being written when this shipped. It is written down here so the hospital's
counsel can disagree with a specific sentence and change a specific row of
`retention_policies` — which is why that one table is mutable and everything
else in this module is append-only.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from database import db_session

logger = logging.getLogger("orb.consent")


# ════════════════════════════════════════════════════════════════
#  Vocabularies — closed sets, validated on write
# ════════════════════════════════════════════════════════════════
# Free-text purposes are how a consent register becomes unauditable: three
# spellings of "research" is three consents nobody can reconcile. The set is
# small on purpose; adding one is a deliberate act.

CONSENT_PURPOSES: dict[str, str] = {
    "treatment": "Care and treatment in this hospital, including the records it generates",
    "ai_assisted_documentation": "Sage/Scribe drafting notes and summaries from the encounter",
    "ambient_recording": "Recording the consultation audio for note drafting",
    "research_secondary_use": "Use of de-identified data for research or service evaluation",
    "family_sharing": "Sharing updates with a named relative or carer",
    "external_referral_sharing": "Sending records to another hospital or clinician",
    "patient_portal": "A Bridge portal login to view one's own record",
    "marketing_communications": "Health camps, feedback drives and other outreach",
}

CONSENT_METHODS: dict[str, str] = {
    "written_signature": "Signed paper form, scanned or filed",
    "verbal_witnessed": "Spoken, with a second member of staff witnessing",
    "digital_portal": "Tapped through in the Bridge portal",
    "kiosk_thumbprint": "Captured at a registration kiosk with a thumb impression",
    "proxy_guardian": "Given by a guardian for a child or a person with disability",
}

GIVEN_BY = ("patient", "guardian", "proxy")

BREACH_NATURES = (
    "unauthorised_access", "unauthorised_disclosure", "loss", "alteration",
    "destruction", "system_compromise", "misdirected_communication",
)


# ════════════════════════════════════════════════════════════════
#  Small helpers
# ════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _require(value: Any, name: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{name} is required")
    return value


# ════════════════════════════════════════════════════════════════
#  1. CONSENT RECORDS
# ════════════════════════════════════════════════════════════════

def record_consent(
    *,
    patient_id: str,
    purpose: str,
    method: str,
    recorded_by: str,
    recorded_by_role: str | None = None,
    scope_detail: str | None = None,
    notice_version: str | None = None,
    notice_language: str | None = None,
    granted_at: str | None = None,
    expires_at: str | None = None,
    given_by: str = "patient",
    proxy_name: str | None = None,
    proxy_relationship: str | None = None,
    evidence_ref: str | None = None,
) -> dict:
    """Write ONE immutable consent record. Returns it, hydrated.

    Validation is strict and up front because the row can never be corrected:
    `consent_records` has BEFORE UPDATE/DELETE triggers. A typo'd purpose is a
    permanent typo, so a bad purpose is refused rather than stored.
    """
    _require(patient_id, "patient_id")
    _require(recorded_by, "recorded_by")
    if purpose not in CONSENT_PURPOSES:
        raise ValueError(
            f"unknown consent purpose {purpose!r}; one of: {sorted(CONSENT_PURPOSES)}"
        )
    if method not in CONSENT_METHODS:
        raise ValueError(
            f"unknown consent method {method!r}; one of: {sorted(CONSENT_METHODS)}"
        )
    if given_by not in GIVEN_BY:
        raise ValueError(f"given_by must be one of {GIVEN_BY}")
    if given_by != "patient" and not (proxy_name and proxy_relationship):
        # DPDP s.9: consent for a child or a person with disability must come
        # from a verifiable guardian. A proxy consent with no named proxy is
        # exactly the row that cannot be verified later.
        raise ValueError(
            "a consent given by a guardian/proxy must name the proxy and their "
            "relationship to the patient (DPDP s.9 verifiable guardian consent)"
        )

    cid = _new_id("CNS")
    ts = granted_at or _now()
    with db_session() as db:
        if not db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone():
            raise ValueError(f"unknown patient {patient_id}")
        db.execute(
            """INSERT INTO consent_records
               (id, patient_id, purpose, scope_detail, notice_version, notice_language,
                method, granted_at, expires_at, given_by, proxy_name, proxy_relationship,
                evidence_ref, recorded_by, recorded_by_role)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, patient_id, purpose, scope_detail, notice_version, notice_language,
             method, ts, expires_at, given_by, proxy_name, proxy_relationship,
             evidence_ref, recorded_by, recorded_by_role),
        )
    logger.info("consent %s recorded for %s (%s)", cid, patient_id, purpose)
    return get_consent(cid)


def withdraw_consent(
    *,
    consent_id: str,
    recorded_by: str,
    method: str,
    reason: str | None = None,
    recorded_by_role: str | None = None,
    withdrawn_at: str | None = None,
) -> dict:
    """Record a withdrawal as a NEW row. The original consent is never touched.

    Returns the withdrawal plus the erasure review it triggers. DPDP s.8(7) says
    withdrawal creates an erasure obligation — but Orb does NOT act on it here.
    It names the categories a human must now review and stops. Auto-deleting on
    a consent withdrawal is how a patient who withdrew consent for family
    updates loses their AI-drafted notes.
    """
    _require(consent_id, "consent_id")
    _require(recorded_by, "recorded_by")
    if method not in CONSENT_METHODS:
        raise ValueError(
            f"unknown withdrawal method {method!r}; one of: {sorted(CONSENT_METHODS)}"
        )

    wid = _new_id("CNW")
    ts = withdrawn_at or _now()
    with db_session() as db:
        row = db.execute("SELECT * FROM consent_records WHERE id = ?",
                         (consent_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown consent {consent_id}")
        existing = db.execute(
            "SELECT id FROM consent_withdrawals WHERE consent_id = ?", (consent_id,)
        ).fetchone()
        if existing:
            # The UNIQUE index would reject this anyway; raising a ValueError
            # first gives the route a 409 instead of a 500.
            raise ValueError(f"consent {consent_id} was already withdrawn")
        db.execute(
            """INSERT INTO consent_withdrawals
               (id, consent_id, patient_id, withdrawn_at, method, reason,
                recorded_by, recorded_by_role)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wid, consent_id, row["patient_id"], ts, method, reason,
             recorded_by, recorded_by_role),
        )
        purpose = row["purpose"]
        patient_id = row["patient_id"]

    categories = sorted(_PURPOSE_TO_CATEGORIES.get(purpose, set()))
    logger.info("consent %s withdrawn (%s)", consent_id, purpose)
    return {
        "id": wid,
        "consent_id": consent_id,
        "patient_id": patient_id,
        "purpose": purpose,
        "withdrawn_at": ts,
        "method": method,
        "reason": reason,
        "recorded_by": recorded_by,
        "recorded_by_role": recorded_by_role,
        # DPDP s.8(7) obligation, surfaced not executed.
        "erasure_review_required": True,
        "categories_for_review": categories,
        "auto_deleted": False,
        "next_step": (
            "Raise an erasure request for this patient and adjudicate it. Orb "
            "deletes nothing on a withdrawal by itself."
        ),
    }


#: Which retention categories a withdrawn consent puts in play. Deliberately
#: narrow — withdrawing consent for family updates does not reach the record.
_PURPOSE_TO_CATEGORIES: dict[str, set[str]] = {
    "treatment": {"clinical_record"},
    "ai_assisted_documentation": {"ai_interaction_transcript"},
    "ambient_recording": {"ambient_audio", "ai_interaction_transcript"},
    "research_secondary_use": {"ai_interaction_transcript"},
    "family_sharing": {"non_clinical_contact"},
    "external_referral_sharing": {"non_clinical_contact"},
    "patient_portal": {"patient_portal_account"},
    "marketing_communications": {"marketing_communications", "non_clinical_contact"},
}


def _effective_status(consent: dict, withdrawal: dict | None,
                      now: str | None = None) -> str:
    """Derived, never stored. See the migration-14 docstring for why there is no
    `status` column to flip."""
    if withdrawal:
        return "withdrawn"
    expires = consent.get("expires_at")
    if expires and str(expires) <= (now or _now()):
        return "expired"
    return "active"


def _hydrate_consent(db, row: dict) -> dict:
    wd = db.execute("SELECT * FROM consent_withdrawals WHERE consent_id = ?",
                    (row["id"],)).fetchone()
    wd = dict(wd) if wd else None
    out = dict(row)
    out["purpose_label"] = CONSENT_PURPOSES.get(row["purpose"], row["purpose"])
    out["method_label"] = CONSENT_METHODS.get(row["method"], row["method"])
    out["withdrawal"] = wd
    out["effective_status"] = _effective_status(out, wd)
    return out


def get_consent(consent_id: str) -> dict | None:
    with db_session() as db:
        row = db.execute("SELECT * FROM consent_records WHERE id = ?",
                         (consent_id,)).fetchone()
        if not row:
            return None
        return _hydrate_consent(db, dict(row))


def list_consents(patient_id: str) -> list[dict]:
    """Every consent this patient ever gave, newest first, each with its
    withdrawal (if any) and derived effective status. Withdrawn consents are
    ALWAYS included — a register that hides them cannot show the history the
    Board asks for."""
    with db_session() as db:
        rows = _rows(db.execute(
            "SELECT * FROM consent_records WHERE patient_id = ? "
            "ORDER BY granted_at DESC, rowid DESC", (patient_id,)))
        return [_hydrate_consent(db, r) for r in rows]


def has_active_consent(patient_id: str, purpose: str) -> bool:
    """Is there an unwithdrawn, unexpired consent for this purpose right now?"""
    return any(c["effective_status"] == "active"
               for c in list_consents(patient_id) if c["purpose"] == purpose)


# ════════════════════════════════════════════════════════════════
#  2. DATA-PRINCIPAL RIGHTS — the export
# ════════════════════════════════════════════════════════════════
#
# DPDP s.11: the principal may ask for a summary of the personal data being
# processed and the processing activities undertaken. In a hospital that means
# the whole chart, and this endpoint is therefore itself a PHI disclosure — the
# single largest one Orb can perform. It is admin-gated, department-scoped and
# audited with the row counts it produced.
#
# HOW TABLES ARE CHOSEN, and why it is discovered rather than hand-listed:
# a hand-maintained list of tables goes stale the first time somebody adds a
# feature, and a stale list makes the export QUIETLY INCOMPLETE — the failure
# mode nobody notices until a regulator does. So the export walks the live
# schema and takes every table with a `patient_id` column, plus `patients`
# itself. A table added tomorrow is in the export tomorrow.
#
# The structural exclusion rule that keeps that safe: a table carrying MORE THAN
# ONE patient-referencing column (e.g. `room_patient_id` + `suspected_patient_id`
# on message_room_fit_flags) is refused automatically, because filtering on one
# of them would emit ANOTHER data principal's identifier inside this principal's
# export. That is a rule about shape, not a list of names, so it also catches the
# table nobody has written yet.

#: Tables never included, each with the reason the principal is told. Static and
#: never mutated at runtime — a module-level dict that grew during a request
#: would make one export's exclusion list depend on which exports ran before it.
_EXPORT_EXCLUDED: dict[str, str] = {
    "sage_decision_cache": (
        "A short-lived performance cache rebuilt from the record it duplicates; "
        "including it would repeat the same clinical content twice."
    ),
    "event_deliveries": (
        "Internal message-delivery bookkeeping with no personal content."
    ),
    "world_model_state": (
        "A derived, versioned and encrypted snapshot of the same clinical picture "
        "already set out above. It holds nothing that is not in the record itself."
    ),
}

#: Tables that ARE exported, but through a curated section further down rather
#: than as raw rows — so the schema walk must skip them or the principal gets the
#: same consents twice, once readable and once not.
_EXPORT_RENDERED_ELSEWHERE = frozenset({"consent_records", "consent_withdrawals"})

#: Columns dropped from `audit_logs` rows in the access-history section. The
#: hash-chain columns are chain internals, and the IP/user-agent of the STAFF
#: member who opened the chart is the staff member's personal data, not the
#: patient's — an access history is "who looked and when", not a dossier on the
#: nurse.
_ACCESS_HISTORY_COLUMNS = (
    "timestamp", "event_category", "action_type", "outcome", "actor_user_id",
    "actor_role", "request_path", "http_method", "summary_message",
)
_ACCESS_HISTORY_LIMIT = 2000


def _patient_tables(db) -> tuple[list[str], dict[str, str], int]:
    """Every table safe to filter by a single `patient_id`, discovered live.

    Returns (included, refused_by_shape, tables_scanned). `refused_by_shape` is
    built per call and returned rather than stashed on a module global, so an
    export's exclusion list depends only on the schema, never on which exports
    happened to run before it in this process.
    """
    rows = db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    included: list[str] = []
    refused: dict[str, str] = {}
    for row in rows:
        name, ddl = row[0], (row[1] or "")
        if (name in _EXPORT_EXCLUDED or name in _EXPORT_RENDERED_ELSEWHERE
                or name in ("patients", "audit_logs")):
            continue

        # VIRTUAL TABLES ARE SKIPPED BEFORE THEY ARE TOUCHED, and this is a
        # correctness fix, not tidiness. `clinical_knowledge_vec` is a `vec0`
        # table from the sqlite-vec extension, and that extension is PER
        # CONNECTION state that an ordinary db_session does not load — so even
        # `PRAGMA table_info(clinical_knowledge_vec)` raises
        # "no such module: vec0". An export that walks the schema therefore
        # crashed with a 500 on any appliance with the knowledge corpus loaded,
        # which is every real one. Found by running this module together with
        # the corpus tests; running it alone can never see it, because alone
        # there is no vec0 table in the database.
        #
        # Nothing is lost: fts5/vec0 indexes hold embeddings of the CLINICAL
        # REFERENCE CORPUS, not patient data.
        if ddl.upper().lstrip().startswith("CREATE VIRTUAL TABLE"):
            refused[name] = (
                "A search index built by a database extension, holding the "
                "hospital's clinical reference material rather than anything "
                "about you."
            )
            continue

        try:
            cols = [r[1] for r in db.execute(f"PRAGMA table_info({name})").fetchall()]
        except Exception as exc:
            # Belt and braces for the same failure class in a shape the DDL
            # sniff above does not catch (a shadow table of an extension, a
            # table whose module was removed). Refuse and SAY so; never let one
            # unreadable table take down the whole export.
            logger.warning("export: cannot introspect %s: %s", name, exc)
            refused[name] = (
                f"Orb could not read this table's structure at export time "
                f"({exc}), so it was left out rather than exported blind."
            )
            continue
        patient_cols = [c for c in cols if c == "patient_id" or c.endswith("_patient_id")]
        if not patient_cols:
            continue  # nothing patient-keyed at all — not this principal's data
        # THE STRUCTURAL GUARD, and it is deliberately conservative: a table is
        # exported only when EXACTLY ONE column could name a patient and it is
        # the plain `patient_id`. Anything else — two such columns
        # (message_room_fit_flags: room_patient_id + suspected_patient_id, where
        # the second names the patient a message may have been MISDIRECTED to),
        # or a single oddly-named one — is refused.
        #
        # The refusal is DECLARED rather than silent. A table quietly skipped is
        # how an export becomes incomplete without anybody noticing; a table
        # listed in `excluded` with a reason can be argued about.
        #
        # The reason says "cannot prove", not "contains someone else", because
        # sometimes it genuinely is the same person under a different name
        # (history_import_patient_xref.external_patient_id is this patient's
        # number at their old hospital). Orb refuses because it cannot tell the
        # two cases apart from the schema, and says exactly that.
        if patient_cols != ["patient_id"]:
            refused[name] = (
                f"This table has more than one column that could name a patient "
                f"({', '.join(patient_cols)}), so Orb cannot prove from the "
                f"schema alone that every row it would export is about you and "
                f"nobody else. It is left out rather than risk disclosing another "
                f"person inside your record. Ask the records officer if you need it."
            )
            continue
        included.append(name)
    return included, refused, len(rows)


def data_principal_export(patient_id: str, *,
                          include_access_history: bool = True) -> dict:
    """Everything Orb holds about ONE patient, section by section.

    Read-only. Returns plain JSON-able dicts so the caller can render it, hand it
    over as a file, or print it — DPDP does not prescribe a format, and a
    hospital in India will most often print this.
    """
    _require(patient_id, "patient_id")
    sections: dict[str, dict] = {}
    excluded_reasons: dict[str, str] = dict(_EXPORT_EXCLUDED)
    total = 0

    with db_session() as db:
        patient = db.execute("SELECT * FROM patients WHERE id = ?",
                             (patient_id,)).fetchone()
        if not patient:
            raise ValueError(f"unknown patient {patient_id}")

        sections["patients"] = {
            "title": "Your registration record",
            "rows": [dict(patient)],
            "row_count": 1,
        }
        total += 1

        tables, refused_by_shape, scanned = _patient_tables(db)
        excluded_reasons.update(refused_by_shape)

        for table in tables:
            try:
                rows = _rows(db.execute(
                    f"SELECT * FROM {table} WHERE patient_id = ?", (patient_id,)))
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("export: table %s unreadable: %s", table, exc)
                excluded_reasons[table] = (
                    f"This table could not be read at export time ({exc}).")
                continue
            if not rows:
                continue
            sections[table] = {
                "title": table.replace("_", " ").capitalize(),
                "rows": rows,
                "row_count": len(rows),
            }
            total += len(rows)

        if include_access_history:
            cols = ", ".join(_ACCESS_HISTORY_COLUMNS)
            rows = _rows(db.execute(
                f"SELECT {cols} FROM audit_logs WHERE patient_id = ? "
                f"ORDER BY id DESC LIMIT {_ACCESS_HISTORY_LIMIT}", (patient_id,)))
            sections["access_history"] = {
                "title": "Who has opened your record, and when",
                "rows": rows,
                "row_count": len(rows),
                "note": (
                    f"Most recent {_ACCESS_HISTORY_LIMIT} entries. Staff IP "
                    f"addresses and device details are omitted. They are the "
                    f"staff member's personal data, not yours."
                ),
            }
            total += len(rows)

        consents = [_hydrate_consent(db, r) for r in _rows(db.execute(
            "SELECT * FROM consent_records WHERE patient_id = ? "
            "ORDER BY granted_at DESC", (patient_id,)))]

    if consents:
        sections["consents"] = {
            "title": "Consents you gave, and any you withdrew",
            "rows": consents,
            "row_count": len(consents),
        }
        total += len(consents)

    excluded = [{"name": k, "reason": v} for k, v in sorted(excluded_reasons.items())]
    excluded.append({
        "name": "hash-chain columns of the audit trail",
        "reason": (
            "prev_hash/row_hash are tamper-evidence internals, not information "
            "about you."
        ),
    })

    return {
        "patient_id": patient_id,
        "generated_at": _now(),
        "basis": "DPDP Act 2023 s.11, right to access information about personal data",
        "sections": sections,
        "section_count": len(sections),
        "record_count": total,
        "tables_scanned": scanned,
        "excluded": excluded,
        "notes": [
            "This export covers the data held inside Orb only. Paper records, "
            "images stored on imaging equipment, and anything sent to another "
            "hospital are not included.",
            "Some rows name the staff member who wrote them. That is part of "
            "your record, a clinical entry without an author is not a record.",
        ],
    }


# ════════════════════════════════════════════════════════════════
#  3. RETENTION — see it, never sweep it
# ════════════════════════════════════════════════════════════════
#
# Each category maps to the tables that actually hold it, with the timestamp
# column the clock runs from.
#
# THE COMPARISON IS `col < 'YYYY-MM-DD'`, and the shape of it is deliberate.
# This schema stores timestamps in TWO formats — ISO-8601 with a 'T' from
# datetime.isoformat(), and 'YYYY-MM-DD HH:MM:SS' from SQLite's CURRENT_TIMESTAMP
# — so anything that assumes one format mis-buckets every row in the other.
# Comparing the whole column against a bare 10-character DATE is correct for
# both, because a string always sorts after its own prefix: a row stamped on the
# cutoff date reads as ' 2026-08-07T09:00' > '2026-08-07' and is not counted,
# while any earlier date differs within the first ten characters and is. That is
# the same answer `substr(col,1,10) < ?` gives, but it is SARGABLE — SQLite can
# use an index on the timestamp column, whereas wrapping it in substr() forces a
# full scan of a table like audit_logs, which on a live appliance is the biggest
# one there is.
#
# PRE-REGISTERED TABLES (3 October 2026). The hospital-system build registered
# every PHI table it plans for registration and outpatients, the laboratory and
# the pharmacy store here in wave 1, before most of them exist, so no later
# increment can create a PHI table the retention review never sees. A table
# that does not exist yet is skipped without a word (retention_review checks
# sqlite_master first), so its line in the review appears only once the table
# is created. One of them exists already: opd_visits, the front desk's visit
# register, which the review had never counted and now does.
# tests/test_opt_in_platform.py checks that every registered table that DOES
# exist has the patient and timestamp columns named here: a later migration
# that names them differently fails there, not silently here.
_RETENTION_SOURCES: dict[str, list[tuple[str, str, str]]] = {
    # category: [(table, patient_column, timestamp_column)]
    "clinical_record": [
        ("clinical_documents", "patient_id", "created_at"),
        ("scribe_notes", "patient_id", "created_at"),
        ("discharge_summaries", "patient_id", "discharged_at"),
        ("observations", "patient_id", "recorded_at"),
        ("problem_list", "patient_id", "recorded_at"),
        ("admissions", "patient_id", "created_at"),
        # Registration and outpatients (opd-1 j, opd-10, opd-10a).
        ("opd_visits", "patient_id", "registered_at"),
        ("patient_demographics", "patient_id", "registered_at"),
        ("patient_identity_changes", "patient_id", "changed_at"),
        ("patient_match_keys", "patient_id", "computed_at"),
        ("opd_consultations", "patient_id", "created_at"),
        ("opd_consultation_addenda", "patient_id", "created_at"),
        ("patient_allergy_assessments", "patient_id", "assessed_at"),
        # Laboratory (lis design, section 10). result_quarantine holds results
        # no patient has been matched to yet, so it has no patient column.
        ("lab_specimens", "patient_id", "created_at"),
        ("lab_specimen_events", "patient_id", "occurred_at"),
        ("lab_order_items", "patient_id", "created_at"),
        ("lab_results", "patient_id", "entered_at"),
        ("lab_result_events", "patient_id", "occurred_at"),
        ("lab_critical_notifications", "patient_id", "raised_at"),
        ("lab_critical_events", "patient_id", "occurred_at"),
        ("result_quarantine", None, "created_at"),
    ],
    "prescription_and_dispensing": [
        ("medications", "patient_id", "started_at"),
        ("medication_administrations", "patient_id", "administered_at"),
        # Pharmacy store (grn-15). The NDPS register is NOT here: its live rows
        # are kept by law, see ERASURE_RETAINED_TABLES.
        ("pharmacy_ward_returns", "patient_id", "created_at"),
    ],
    "imaging": [
        ("lens_analyses", "patient_id", "created_at"),
    ],
    "audit_trail": [
        ("audit_logs", "patient_id", "timestamp"),
    ],
    "ai_interaction_transcript": [
        ("sage_observations", "patient_id", "created_at"),
        ("sage_room_memory", "patient_id", "updated_at"),
        ("sage_insights", "patient_id", "created_at"),
        ("insight_feedback", "patient_id", "created_at"),
    ],
    "patient_portal_account": [
        ("discharge_access_tokens", "patient_id", "created_at"),
    ],
    "breach_record": [
        ("breach_records", None, "discovered_at"),
    ],
    "consent_record": [
        ("consent_records", "patient_id", "granted_at"),
    ],
}


# ════════════════════════════════════════════════════════════════
#  The minority protection
# ════════════════════════════════════════════════════════════════
# Limitation Act 1963 s.6: time does not run against a person under a legal
# disability until the disability ceases. For a child that is majority, so the
# claim window for care given to a minor closes at 18 + 3 = 21, not three years
# after the last visit. A retention review that put a paediatric record on the
# destroyable list at three years would be handing a hospital a list of exactly
# the records it is most likely to be asked for.
#
# It is applied as a FLOOR over retention_policies rather than a row inside it,
# because "was this patient a child" is a property of the PATIENT and that table
# is keyed by CATEGORY. A second category mapping to the same tables would
# double-count every paediatric record in the review.
#
# Only the categories held to answer a CLAIM are protected. The consent-basis
# categories (AI transcripts, portal accounts, marketing) are not evidence in a
# limitation suit and a child has the same erasure rights over them as anyone.
_MINORITY_PROTECTED_CATEGORIES = frozenset({
    "clinical_record",
    "prescription_and_dispensing",
    "imaging",
})

# Releasable ONLY if the patient is demonstrably past the protected age.
#
# date_of_birth is authoritative when present. When it is NULL we fall back to
# patients.age, and the direction of that fallback's error is the whole reason
# it is safe: age is a snapshot that is only ever refreshed UPWARDS, so a stale
# age understates the patient's real age, and understating age can only
# over-protect. It can never release a record early. (Measured 2026-09-07: every
# patient row in the development database has a NULL date_of_birth and a
# populated age, so this is the live path, not the fallback.)
#
# A patient with neither is NOT releasable. That is deliberate: the alternative
# is authorising the destruction of a medical record on the strength of not
# knowing whose it is. Those rows are counted separately and reported, so the
# hospital sees a data-quality problem rather than a silently shorter list.
_RELEASABLE_PATIENT_SQL = """
    SELECT id FROM patients
     WHERE (date_of_birth IS NOT NULL AND date_of_birth <= ?)
        OR (date_of_birth IS NULL AND age IS NOT NULL AND age >= ?)
"""
_MINOR_PATIENT_SQL = """
    SELECT id FROM patients
     WHERE (date_of_birth IS NOT NULL AND date_of_birth > ?)
        OR (date_of_birth IS NULL AND age IS NOT NULL AND age < ?)
"""


def _majority_cutoff(ref: datetime) -> str:
    """The latest date of birth that is already past the protected age.

    Uses a plain year subtraction rather than a 365-day multiple so it lands on
    the birthday, and steps 29 February back to the 28th rather than raising.
    """
    from config import MINOR_RETENTION_AGE
    try:
        return ref.replace(year=ref.year - MINOR_RETENTION_AGE).date().isoformat()
    except ValueError:  # 29 Feb in a non-leap target year
        return ref.replace(year=ref.year - MINOR_RETENTION_AGE, day=28).date().isoformat()


def list_retention_policies() -> list[dict]:
    with db_session() as db:
        return _rows(db.execute(
            "SELECT * FROM retention_policies ORDER BY erasable, category"))


def retention_review(*, now: datetime | None = None,
                     patient_id: str | None = None) -> dict:
    """What is past its retention period, per category. DELETES NOTHING.

    `deleted_anything` is returned as a literal False, and it is not decoration:
    the one thing this function must never grow is a sweep. A hospital reading
    this list decides, per category, whether the record can go — and a signed
    destruction order, not a day count, is what authorises it.
    """
    from config import MINOR_RETENTION_AGE as minor_age

    policies = list_retention_policies()
    ref = now or datetime.now(timezone.utc)
    maj = _majority_cutoff(ref)
    out: list[dict] = []

    with db_session() as db:
        # Pre-registered tables that a later increment has not created yet are
        # skipped silently (see _RETENTION_SOURCES), so the review reads the same
        # as before they were registered.
        existing_tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        for p in policies:
            days = p["retention_days"]
            entry = {
                "category": p["category"],
                "description": p["description"],
                "retention_days": days,
                "anchor": p["anchor"],
                "legal_basis": p["legal_basis"],
                "authority": p["authority"],
                "erasable": bool(p["erasable"]),
                "disposition": p["disposition"],
                "cutoff_date": None,
                "overdue_count": 0,
                "by_table": {},
                "measurable": bool(days) and p["category"] in _RETENTION_SOURCES,
            }
            if not days:
                entry["note"] = (
                    "No fixed period: retained until a named person lifts the hold."
                )
                out.append(entry)
                continue
            if p["category"] not in _RETENTION_SOURCES:
                entry["note"] = (
                    "Orb holds no table for this category yet; the policy is "
                    "declared so the hospital's answer is on record."
                )
                out.append(entry)
                continue

            cutoff = (ref - timedelta(days=int(days))).date().isoformat()
            entry["cutoff_date"] = cutoff
            protected = p["category"] in _MINORITY_PROTECTED_CATEGORIES
            if protected:
                # Reported even when it holds nothing back, because "we checked
                # and no paediatric record is in this list" is the useful
                # statement; a key that only appears when it bites reads as an
                # error the first time a hospital sees it.
                entry["minority_protection"] = {
                    "applies": True,
                    "protected_to_age": minor_age,
                    "majority_cutoff": maj,
                    "authority": "Limitation Act 1963 s.6",
                    "held_minor": 0,
                    "held_age_unknown": 0,
                }
            for table, pcol, tcol in _RETENTION_SOURCES[p["category"]]:
                if table not in existing_tables:
                    continue
                if patient_id and not pcol:
                    # This table is not keyed by patient (the breach register is
                    # about incidents, not people). Counting it unfiltered would
                    # report every breach in the hospital as this one patient's
                    # overdue data. Skip it and SAY so, rather than return a
                    # number that reads as if it were theirs.
                    entry.setdefault("not_patient_scoped", []).append(table)
                    continue
                where = f"{tcol} < ?"
                params: list[Any] = [cutoff]
                if patient_id and pcol:
                    where += f" AND {pcol} = ?"
                    params.append(patient_id)

                def _count(clause: str, args: list[Any], _t=table) -> int:
                    return int(db.execute(
                        f"SELECT COUNT(*) AS c FROM {_t} WHERE {clause}",
                        tuple(args)).fetchone()["c"] or 0)

                try:
                    if protected and pcol:
                        age_args = [maj, minor_age]
                        # Overdue means overdue AND releasable. A record whose
                        # patient is still inside the s.6 window is not overdue,
                        # it is protected, and it must not appear on a list a
                        # hospital destroys from.
                        n = _count(
                            f"{where} AND {pcol} IN ({_RELEASABLE_PATIENT_SQL})",
                            params + age_args)
                        held_minor = _count(
                            f"{where} AND {pcol} IN ({_MINOR_PATIENT_SQL})",
                            params + age_args)
                        total = _count(where, params)
                        mp = entry["minority_protection"]
                        mp["held_minor"] += held_minor
                        # The remainder is rows Orb cannot age at all: no DOB,
                        # no age, or no patient row. Held back and SAID, because
                        # the alternative is destroying a record on the strength
                        # of not knowing whose it is.
                        mp["held_age_unknown"] += max(total - n - held_minor, 0)
                    else:
                        n = _count(where, params)
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning("retention probe failed for %s: %s", table, exc)
                    continue
                entry["by_table"][table] = n
                entry["overdue_count"] += n
            if protected:
                mp = entry["minority_protection"]
                mp["held_total"] = mp["held_minor"] + mp["held_age_unknown"]
                if mp["held_total"]:
                    entry["note"] = (
                        f"{mp['held_total']} record(s) past the {days}-day period are "
                        f"HELD BACK and excluded from the count above: "
                        f"{mp['held_minor']} belong to patients under "
                        f"{minor_age} (Limitation Act 1963 s.6), "
                        f"{mp['held_age_unknown']} to patients whose age Orb "
                        f"cannot establish. Neither is destroyable."
                    )
            out.append(entry)

    return {
        "generated_at": _now(),
        "as_of": ref.date().isoformat(),
        "patient_id": patient_id,
        "categories": out,
        "total_overdue": sum(c["overdue_count"] for c in out),
        # Load-bearing, not decoration. See the docstring.
        "deleted_anything": False,
        "action": (
            "Review each overdue category with the records officer. Orb does not "
            "destroy anything on a timer; destruction needs a signed order."
        ),
    }


# ════════════════════════════════════════════════════════════════
#  4. ERASURE — request, adjudicate, then (separately) execute
# ════════════════════════════════════════════════════════════════
#
# The executors below are the ONLY places this module deletes patient data. Each
# takes (db, patient_id) and returns (rows_erased, detail). A category with no
# executor is honoured in law and reported as `executable: False` with a plain
# sentence saying so — Orb does not claim an erasure it did not perform.

def _erase_ai_transcripts(db, patient_id: str) -> tuple[int, dict]:
    """Sage's working memory. Derived material: every one of these tables is
    regenerated from the clinical record, so erasing them removes the AI's notes
    about the patient without touching a single word a clinician wrote."""
    detail: dict[str, int] = {}
    total = 0
    for table in ("sage_observations", "sage_room_memory", "sage_decision_cache",
                  "sage_insights", "insight_feedback"):
        try:
            cur = db.execute(f"DELETE FROM {table} WHERE patient_id = ?", (patient_id,))
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("erasure: %s not erasable here: %s", table, exc)
            continue
        detail[table] = n
        total += n
    return total, detail


def _erase_portal_account(db, patient_id: str) -> tuple[int, dict]:
    """Revoke the login, keep the record it opened.

    The linked `users` row is DEACTIVATED and stripped of its credentials rather
    than deleted: audit rows and clinical rows name that user id as an actor, and
    deleting it would make those rows unreadable — an erasure that damages
    somebody else's record is not an erasure, it is a second incident.

    `password_hash` is NOT NULL in the schema, so the credential is overwritten
    with a random tombstone rather than nulled. The tombstone does not start with
    '$2', so `auth.verify_password` routes it to the legacy SHA-256 branch and
    compares a 64-char digest against a longer, random string — `compare_digest`
    returns False for every possible password, and nobody (including whoever
    reads the row) knows a preimage. Bumping `token_version` kills any JWT
    already outstanding, so the erasure takes effect on the next request rather
    than at the next expiry.
    """
    import secrets

    detail: dict[str, int] = {}
    total = 0
    cur = db.execute(
        "UPDATE discharge_access_tokens SET revoked_at = ? "
        "WHERE patient_id = ? AND revoked_at IS NULL", (_now(), patient_id))
    detail["discharge_access_tokens_revoked"] = max(cur.rowcount or 0, 0)
    total += detail["discharge_access_tokens_revoked"]

    row = db.execute("SELECT user_id FROM patients WHERE id = ?",
                     (patient_id,)).fetchone()
    uid = row["user_id"] if row else None
    if uid:
        tombstone = f"erased-{secrets.token_hex(32)}"
        cur = db.execute(
            "UPDATE users SET password_hash = ?, pin_hash = NULL, "
            "totp_secret = NULL, totp_enabled = 0, device_pin_hash = NULL, "
            "is_active = 0, token_version = COALESCE(token_version, 0) + 1 "
            "WHERE id = ? AND role = 'patient'", (tombstone, uid))
        detail["portal_logins_disabled"] = max(cur.rowcount or 0, 0)
        total += detail["portal_logins_disabled"]
    else:
        detail["portal_logins_disabled"] = 0
    return total, detail


def _erase_ambient_audio(db, patient_id: str) -> tuple[int, dict]:
    """Detach this patient's dictation audio from their notes. The FILES go later.

    A file deleted inside the transaction cannot be rolled back with it: if a
    later executor or the execution row failed, the record would say nothing was
    erased while the audio was already gone. So this only nulls the pointers and
    names the files; execute_erasure_request unlinks them after the commit
    (_unlink_erased_audio). The signed note is the medical record and is not
    touched; the audio is working material (retention category ambient_audio).

    scribe_notes.audio_path arrives from the client, so it is not trusted as a
    path: a file is only scheduled when it resolves inside UPLOAD_DIR, and never
    when another patient's note points at the same file. Anything refused keeps
    its pointer and is counted, so the administrator can see it.
    """
    import os
    from config import UPLOAD_DIR

    def _resolve(p) -> str | None:
        # A malformed pointer (embedded NUL, not a string) must not abort the
        # whole erasure; it is simply not a file Orb can vouch for.
        try:
            return os.path.realpath(p)
        except (TypeError, ValueError):
            return None

    root = os.path.realpath(str(UPLOAD_DIR))
    rows = db.execute(
        "SELECT DISTINCT audio_path FROM scribe_notes WHERE patient_id = ? "
        "AND audio_path IS NOT NULL AND TRIM(audio_path) <> ''", (patient_id,)
    ).fetchall()

    files: list[str] = []
    detach: list[str] = []
    outside = shared = 0
    for r in rows:
        raw = r["audio_path"]
        resolved = _resolve(raw)
        if (resolved is None or os.path.commonpath([root, resolved]) != root
                or resolved == root):
            outside += 1
            continue
        base = os.path.basename(resolved)
        like = "%" + base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        others = db.execute(
            "SELECT audio_path FROM scribe_notes WHERE audio_path LIKE ? ESCAPE '\\' "
            "AND (patient_id IS NULL OR patient_id <> ?)", (like, patient_id)
        ).fetchall()
        if any(_resolve(o["audio_path"]) == resolved for o in others):
            shared += 1
            continue
        files.append(os.path.relpath(resolved, root))
        detach.append(raw)

    notes = 0
    for raw in detach:
        cur = db.execute(
            "UPDATE scribe_notes SET audio_path = NULL WHERE patient_id = ? "
            "AND audio_path = ?", (patient_id, raw))
        notes += max(cur.rowcount or 0, 0)

    return len(files), {
        "audio_files": sorted(files),
        "notes_detached": notes,
        "kept_outside_upload_folder": outside,
        "kept_shared_with_another_record": shared,
    }


def _unlink_erased_audio(names: Iterable[str]) -> list[str]:
    """Delete the files _erase_ambient_audio named. Runs AFTER the commit.

    Returns the names that could not be removed. A file that is already gone
    counts as removed. The containment check is repeated here because this is
    the line that deletes.
    """
    import os
    from config import UPLOAD_DIR

    root = os.path.realpath(str(UPLOAD_DIR))
    failed: list[str] = []
    for name in names:
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) != root or path == root:
            failed.append(name)
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("erasure: could not remove audio file %s: %s", name, exc)
            failed.append(name)
    return failed


_ERASURE_EXECUTORS = {
    "ai_interaction_transcript": _erase_ai_transcripts,
    "patient_portal_account": _erase_portal_account,
    "ambient_audio": _erase_ambient_audio,
}

#: Erasable in law, not yet executable in Orb. Stated to the principal verbatim.
_NOT_YET_EXECUTABLE: dict[str, str] = {
    "non_clinical_contact": (
        "Orb holds no contact store separate from the medical record. The name, "
        "address and next-of-kin inside the record are part of the record and are "
        "handled under the clinical_record policy."
    ),
    "marketing_communications": (
        "Orb runs no outreach or campaign list. There is nothing held to erase."
    ),
}

#: Stores that still name the patient after ANY erasure, each with the reason.
#: Every one except audit_logs is append-only at the storage layer (a BEFORE
#: DELETE trigger that RAISEs ABORT); erasure_requests is the parent the
#: decision and execution rows point at. Nothing can delete from them, so the
#: point of the list is that an erasure SAYS they remain instead of implying the
#: patient is gone: shadow_events.phi_json can carry a recorder's own words.
#: routes/admin_routes.py builds its destruction plan's keep set from this dict.
ERASURE_RETAINED_TABLES: dict[str, str] = {
    # Hash-chained: deleting rows would destroy the record of the erasure and
    # break tamper-evidence. Its rows hold the patient id and no PHI.
    "audit_logs": "The tamper-evident access trail; it records that this erasure happened.",
    "shadow_events": (
        "The pilot's evidence log, immutable for every non-simulator row. Its "
        "phi_json column can still hold free text about this patient."
    ),
    "consent_records": "Proof that consent was given and when (DPDP s.5 and s.6).",
    "consent_withdrawals": "Proof that consent was withdrawn and when.",
    "erasure_requests": "The request this erasure answers.",
    "erasure_decisions": "The sealed decision for each category, with its reason and authority.",
    "erasure_executions": "The record of what was erased, when and by whom.",
    "breach_notifications": "Who was told about a breach and when (DPDP s.8(6)).",
    # Pre-registered 3 October 2026 for the pharmacy store (grn-13): the table
    # arrives with migration 54 and is skipped until then. Only its LIVE rows
    # are a legal record (a delete-abort trigger guards them); rows written on a
    # non-production profile are cleared by a development reseed, and the count
    # below leaves them out (ERASURE_RETAINED_ROW_FILTERS).
    "ndps_register": (
        "The narcotic and psychotropic drug register. Its live entries are a "
        "statutory record under the NDPS Act and cannot be erased."
    ),
}

#: Which rows of a retained table the erasure result reports as kept. A table
#: absent here is reported whole.
ERASURE_RETAINED_ROW_FILTERS: dict[str, str] = {
    "ndps_register": "lane = 'live'",
}


def _retained_after_erasure(db, patient_id: str) -> list[dict]:
    """The retained stores that hold rows for this patient, with counts and why.

    Only tables with a patient_id column can be counted (breach_notifications
    names patients through its breach, not a column) and a table missing on an
    older database is skipped rather than failing an erasure that has run.
    """
    out: list[dict] = []
    for table, reason in ERASURE_RETAINED_TABLES.items():
        try:
            cols = {c[1] for c in db.execute(f'PRAGMA table_info("{table}")').fetchall()}
            if "patient_id" not in cols:
                continue
            row_filter = ERASURE_RETAINED_ROW_FILTERS.get(table)
            n = db.execute(f'SELECT COUNT(*) AS n FROM "{table}" WHERE patient_id = ?'
                           + (f" AND {row_filter}" if row_filter else ""),
                           (patient_id,)).fetchone()["n"]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("erasure: could not count retained %s: %s", table, exc)
            continue
        if n:
            out.append({"table": table, "rows": n, "reason": reason})
    return out


def record_erasure_request(
    *,
    patient_id: str,
    requested_by: str,
    categories: Optional[Iterable[str]] = None,
    requester_identity: str = "patient",
    channel: str = "in_person",
    note: str | None = None,
) -> dict:
    """Log the request. Decides nothing, deletes nothing.

    `categories=None` means "everything" and is the normal case — a patient says
    "delete my data", not "delete retention category 7".
    """
    _require(patient_id, "patient_id")
    _require(requested_by, "requested_by")
    cats = sorted(set(categories)) if categories else None
    rid = _new_id("ERQ")
    with db_session() as db:
        if not db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone():
            raise ValueError(f"unknown patient {patient_id}")
        known = {r["category"] for r in db.execute(
            "SELECT category FROM retention_policies").fetchall()}
        if cats:
            unknown = [c for c in cats if c not in known]
            if unknown:
                raise ValueError(f"unknown retention categories: {unknown}")
        db.execute(
            """INSERT INTO erasure_requests
               (id, patient_id, requested_at, requested_by, requester_identity,
                channel, categories_json, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (rid, patient_id, _now(), requested_by, requester_identity, channel,
             json.dumps(cats) if cats else None, note),
        )
    return get_erasure_request(rid)["request"]


def adjudicate_erasure_request(request_id: str, *, decided_by: str) -> dict:
    """THE HARD PART, executed. One sealed decision per category.

    Reads `retention_policies.erasable` — the encoded position, not a judgement
    made here — and produces, for every category in scope, a disposition with a
    reason and (for refusals) the citation that compels retention.
    """
    _require(decided_by, "decided_by")
    with db_session() as db:
        req = db.execute("SELECT * FROM erasure_requests WHERE id = ?",
                         (request_id,)).fetchone()
        if not req:
            raise ValueError(f"unknown erasure request {request_id}")
        if req["status"] != "recorded":
            raise ValueError(
                f"request {request_id} is already {req['status']}; a decision is "
                "sealed and cannot be re-taken (raise a new request instead)"
            )
        patient_id = req["patient_id"]
        wanted = json.loads(req["categories_json"]) if req["categories_json"] else None
        policies = _rows(db.execute("SELECT * FROM retention_policies ORDER BY category"))
        if wanted:
            policies = [p for p in policies if p["category"] in wanted]

        ts = _now()
        decisions: list[dict] = []
        for p in policies:
            cat = p["category"]
            if p["erasable"]:
                executor = _ERASURE_EXECUTORS.get(cat)
                if executor:
                    disposition, executable = "honoured", True
                    reason = (
                        f"Held on a {p['legal_basis']} basis, which DPDP s.12(3) "
                        f"reaches. Orb will erase it."
                    )
                else:
                    disposition, executable = "honoured", False
                    reason = (
                        f"Held on a {p['legal_basis']} basis, so the request is "
                        f"granted. {_NOT_YET_EXECUTABLE.get(cat, 'Orb cannot yet carry this out automatically.')}"
                    )
                authority = None
            else:
                disposition, executable = "refused", False
                reason = (
                    f"Refused: Orb holds this on a {p['legal_basis']} basis, and "
                    f"DPDP s.12(3) does not reach personal data whose retention is "
                    f"necessary for compliance with a law in force. "
                    f"{p['disposition'].capitalize()}."
                )
                authority = p["authority"]

            did = _new_id("ERD")
            db.execute(
                """INSERT INTO erasure_decisions
                   (id, request_id, patient_id, category, disposition, basis,
                    reason, authority, executable, decided_at, decided_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (did, request_id, patient_id, cat, disposition, p["legal_basis"],
                 reason, authority, 1 if executable else 0, ts, decided_by),
            )
            decisions.append({
                "id": did, "category": cat, "disposition": disposition,
                "basis": p["legal_basis"], "reason": reason, "authority": authority,
                "executable": executable,
            })

        db.execute(
            "UPDATE erasure_requests SET status = 'decided', decided_at = ?, "
            "decided_by = ? WHERE id = ?", (ts, decided_by, request_id))

    honoured = [d for d in decisions if d["disposition"] == "honoured"]
    return {
        "request_id": request_id,
        "patient_id": patient_id,
        "decided_at": ts,
        "decided_by": decided_by,
        "decisions": decisions,
        "honoured_count": len(honoured),
        "refused_count": len(decisions) - len(honoured),
        "executable_count": sum(1 for d in honoured if d["executable"]),
        # Said to the principal rather than discovered by them later.
        "residue_notice": (
            "This request, this decision and anything Orb erases because of it "
            "are themselves recorded in the tamper-evident audit trail, which is "
            "not erased. The hospital keeps a permanent record that you asked."
        ),
    }


def execute_erasure_request(request_id: str, *, executed_by: str) -> dict:
    """Carry out ONLY what was adjudicated honourable AND executable.

    Deliberately a separate call from adjudication: deciding and destroying are
    two acts, and the second one wants a human to have read the first.
    """
    _require(executed_by, "executed_by")
    with db_session() as db:
        req = db.execute("SELECT * FROM erasure_requests WHERE id = ?",
                         (request_id,)).fetchone()
        if not req:
            raise ValueError(f"unknown erasure request {request_id}")
        if req["status"] == "recorded":
            raise ValueError(
                f"request {request_id} has not been adjudicated; erasure without a "
                "recorded decision would destroy data with no stated basis"
            )
        if req["status"] == "executed":
            raise ValueError(f"request {request_id} was already executed")

        patient_id = req["patient_id"]
        decisions = _rows(db.execute(
            "SELECT * FROM erasure_decisions WHERE request_id = ? "
            "AND disposition = 'honoured' AND executable = 1", (request_id,)))

        ts = _now()
        total = 0
        performed: list[dict] = []
        for d in decisions:
            executor = _ERASURE_EXECUTORS.get(d["category"])
            if not executor:  # pragma: no cover — executable=1 implies one exists
                continue
            n, detail = executor(db, patient_id)
            total += n
            eid = _new_id("ERX")
            db.execute(
                """INSERT INTO erasure_executions
                   (id, request_id, decision_id, patient_id, category, rows_erased,
                    detail_json, executed_at, executed_by)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (eid, request_id, d["id"], patient_id, d["category"], n,
                 json.dumps(detail), ts, executed_by),
            )
            performed.append({"category": d["category"], "rows_erased": n,
                              "detail": detail})

        db.execute(
            "UPDATE erasure_requests SET status = 'executed', executed_at = ?, "
            "executed_by = ? WHERE id = ?", (ts, executed_by, request_id))
        retained = _retained_after_erasure(db, patient_id)

    # Files only after the commit above: a rolled-back execution must not have
    # destroyed anything. A file that will not go is reported, not hidden.
    files_not_removed: list[str] = []
    for p in performed:
        if p["category"] == "ambient_audio":
            p["files_not_removed"] = _unlink_erased_audio(p["detail"]["audio_files"])
            files_not_removed.extend(p["files_not_removed"])

    logger.info("erasure request %s executed: %d rows", request_id, total)
    return {
        "request_id": request_id,
        "patient_id": patient_id,
        "status": "executed",
        "executed_at": ts,
        "executed_by": executed_by,
        "rows_erased": total,
        "categories": performed,
        "files_not_removed": files_not_removed,
        # What still names this patient and why, so "executed" is never read as
        # "the patient is gone". See ERASURE_RETAINED_TABLES.
        "retained": retained,
    }


def get_erasure_request(request_id: str) -> dict:
    with db_session() as db:
        req = db.execute("SELECT * FROM erasure_requests WHERE id = ?",
                         (request_id,)).fetchone()
        if not req:
            raise ValueError(f"unknown erasure request {request_id}")
        req = dict(req)
        req["categories"] = (json.loads(req["categories_json"])
                             if req["categories_json"] else None)
        decisions = _rows(db.execute(
            "SELECT * FROM erasure_decisions WHERE request_id = ? ORDER BY category",
            (request_id,)))
        for d in decisions:
            d["executable"] = bool(d["executable"])
        executions = _rows(db.execute(
            "SELECT * FROM erasure_executions WHERE request_id = ? ORDER BY category",
            (request_id,)))
    return {"request": req, "decisions": decisions, "executions": executions}


def list_erasure_requests(*, patient_id: str | None = None,
                          status: str | None = None, limit: int = 200,
                          department: str | None = None) -> list[dict]:
    """The register of erasure requests.

    `department` is the CALLER'S ward scope, and it is not optional in spirit:
    every row here names a patient, so an unfiltered listing hands a
    Cardiology-scoped administrator the id of every patient in the hospital who
    ever asked to be forgotten. The filter goes through
    permissions.scope_patient_ids_sql — the one derivation — rather than a
    second, local idea of what a ward is. `None` means a genuinely unscoped
    governance account, per the department_scope() convention.
    """
    from permissions import scope_patient_ids_sql

    sql = "SELECT * FROM erasure_requests"
    where, params = [], []
    if patient_id:
        where.append("patient_id = ?")
        params.append(patient_id)
    if status:
        where.append("status = ?")
        params.append(status)
    if department is not None:
        scope_sql, scope_params = scope_patient_ids_sql(department)
        where.append(f"patient_id IN ({scope_sql})")
        params.extend(scope_params)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY requested_at DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = _rows(db.execute(sql, tuple(params)))
    for r in rows:
        r["categories"] = json.loads(r["categories_json"]) if r["categories_json"] else None
    return rows


# ════════════════════════════════════════════════════════════════
#  5. BREACH REGISTER — DPDP s.8(6)
# ════════════════════════════════════════════════════════════════

def record_breach(
    *,
    nature: str,
    description: str,
    reported_by: str,
    discovered_at: str | None = None,
    occurred_at: str | None = None,
    exposed_to: str | None = None,
    data_categories: Optional[Iterable[str]] = None,
    records_affected: int | None = None,
    principals_affected: int | None = None,
    affected_patient_ids: Optional[Iterable[str]] = None,
    containment_at: str | None = None,
    containment_actions: str | None = None,
) -> dict:
    """Open a breach record. WHAT was exposed, TO WHOM, WHEN.

    `discovered_at` defaults to now and `occurred_at` is separate and optional,
    because the two are almost never the same moment and the gap between them is
    the number a regulator asks about first.
    """
    if nature not in BREACH_NATURES:
        raise ValueError(f"unknown breach nature {nature!r}; one of: {list(BREACH_NATURES)}")
    _require(description, "description")
    _require(reported_by, "reported_by")

    bid = _new_id("BRC")
    with db_session() as db:
        db.execute(
            """INSERT INTO breach_records
               (id, nature, description, occurred_at, discovered_at, exposed_to,
                data_categories_json, records_affected, principals_affected,
                affected_patient_ids_json, containment_at, containment_actions,
                status, reported_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
            (bid, nature, description, occurred_at, discovered_at or _now(),
             exposed_to,
             json.dumps(sorted(set(data_categories))) if data_categories else None,
             records_affected, principals_affected,
             json.dumps(sorted(set(affected_patient_ids))) if affected_patient_ids else None,
             containment_at, containment_actions, reported_by),
        )
    return get_breach(bid)


def record_breach_notification(
    *,
    breach_id: str,
    party_type: str,
    notified_party: str,
    method: str | None = None,
    reference: str | None = None,
    content_summary: str | None = None,
    recorded_by: str,
    notified_at: str | None = None,
) -> dict:
    """One immutable row per notification sent.

    Notifying the Board and notifying the affected people are two separate
    obligations discharged at two separate times; the header's
    `board_notified_at` / `principals_notified_at` are convenience mirrors kept
    in step here so a register listing does not need a join.
    """
    _require(breach_id, "breach_id")
    _require(notified_party, "notified_party")
    _require(recorded_by, "recorded_by")
    if party_type not in ("board", "data_principal", "regulator", "internal", "other"):
        raise ValueError(f"unknown party_type {party_type!r}")

    nid = _new_id("BRN")
    ts = notified_at or _now()
    with db_session() as db:
        if not db.execute("SELECT 1 FROM breach_records WHERE id = ?",
                          (breach_id,)).fetchone():
            raise ValueError(f"unknown breach {breach_id}")
        db.execute(
            """INSERT INTO breach_notifications
               (id, breach_id, party_type, notified_party, notified_at, method,
                reference, content_summary, recorded_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (nid, breach_id, party_type, notified_party, ts, method, reference,
             content_summary, recorded_by),
        )
        if party_type == "board":
            db.execute(
                "UPDATE breach_records SET board_notified_at = COALESCE(board_notified_at, ?), "
                "status = 'notified', updated_at = ? WHERE id = ?", (ts, _now(), breach_id))
        elif party_type == "data_principal":
            db.execute(
                "UPDATE breach_records SET principals_notified_at = COALESCE(principals_notified_at, ?), "
                "updated_at = ? WHERE id = ?", (ts, _now(), breach_id))
        row = db.execute("SELECT * FROM breach_notifications WHERE id = ?",
                         (nid,)).fetchone()
        return dict(row)


def get_breach(breach_id: str, *, department: str | None = None) -> dict:
    """One breach with its notification history.

    `department` is the CALLER'S ward scope. A breach is a hospital-wide
    governance record and stays fully visible — but its list of AFFECTED
    PATIENTS is PHI, and a Cardiology-scoped administrator has no business
    reading the ids of affected Oncology patients out of an incident report. The
    list is filtered to their ward and the number withheld is stated, so the
    reader knows the incident is wider than what they can see and can escalate
    rather than under-report it.
    """
    from permissions import scope_patient_ids_sql

    with db_session() as db:
        row = db.execute("SELECT * FROM breach_records WHERE id = ?",
                         (breach_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown breach {breach_id}")
        out = dict(row)
        out["data_categories"] = (json.loads(out["data_categories_json"])
                                  if out["data_categories_json"] else [])
        affected = (json.loads(out["affected_patient_ids_json"])
                    if out["affected_patient_ids_json"] else [])
        out["affected_patient_ids_withheld"] = 0
        if department is not None and affected:
            scope_sql, scope_params = scope_patient_ids_sql(department)
            in_scope = {r[0] for r in db.execute(scope_sql, scope_params).fetchall()}
            visible = [pid for pid in affected if pid in in_scope]
            out["affected_patient_ids_withheld"] = len(affected) - len(visible)
            affected = visible
        out["affected_patient_ids"] = affected
        out["notifications"] = _rows(db.execute(
            "SELECT * FROM breach_notifications WHERE breach_id = ? "
            "ORDER BY notified_at", (breach_id,)))
    out["outstanding"] = [
        label for label, done in (
            ("Data Protection Board not yet notified", out["board_notified_at"]),
            ("Affected patients not yet notified", out["principals_notified_at"]),
        ) if not done
    ]
    return out


def list_breaches(*, limit: int = 200) -> list[dict]:
    with db_session() as db:
        rows = _rows(db.execute(
            "SELECT b.*, "
            "(SELECT COUNT(*) FROM breach_notifications n WHERE n.breach_id = b.id) "
            "AS notification_count "
            "FROM breach_records b ORDER BY discovered_at DESC LIMIT ?", (int(limit),)))
    for r in rows:
        r["data_categories"] = (json.loads(r["data_categories_json"])
                                if r["data_categories_json"] else [])
        # The affected-patient id list is deliberately NOT expanded in a listing:
        # a register page is read by whoever is on shift, and it does not need
        # the names to show that a breach is open and unnotified.
        r.pop("affected_patient_ids_json", None)
    return rows


# ════════════════════════════════════════════════════════════════
#  Posture summary — consumed by services/compliance_service.py
# ════════════════════════════════════════════════════════════════

def dpdp_posture() -> dict:
    """Counts the Trust Center renders as DPDP controls. Never raises on a fresh
    DB — an empty register is a legitimate state, not an error."""
    with db_session() as db:
        def count(sql: str, params: tuple = ()) -> int:
            try:
                return int(db.execute(sql, params).fetchone()[0] or 0)
            except Exception:  # pragma: no cover — table absent pre-migration
                return 0

        consents = count("SELECT COUNT(*) FROM consent_records")
        withdrawals = count("SELECT COUNT(*) FROM consent_withdrawals")
        patients_with_consent = count(
            "SELECT COUNT(DISTINCT patient_id) FROM consent_records")
        policies = count("SELECT COUNT(*) FROM retention_policies")
        erasure_open = count(
            "SELECT COUNT(*) FROM erasure_requests WHERE status = 'recorded'")
        erasure_total = count("SELECT COUNT(*) FROM erasure_requests")
        breaches_total = count("SELECT COUNT(*) FROM breach_records")
        breaches_unnotified = count(
            "SELECT COUNT(*) FROM breach_records WHERE board_notified_at IS NULL "
            "AND status != 'closed'")
    return {
        "consents_recorded": consents,
        "consents_withdrawn": withdrawals,
        "patients_with_consent": patients_with_consent,
        "retention_policies": policies,
        "erasure_requests_total": erasure_total,
        "erasure_requests_awaiting_decision": erasure_open,
        "breaches_recorded": breaches_total,
        "breaches_awaiting_board_notification": breaches_unnotified,
    }
