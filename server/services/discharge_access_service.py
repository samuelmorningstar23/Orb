"""Orb Discharge Access Service — Patient-Facing Token Verification"""
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import time
import threading
from typing import Optional, Dict, Any
import jwt

from database import db_session
from services.audit_service import log_audit_event
from config import (
    DISCHARGE_ACCESS_MAX_ATTEMPTS, DISCHARGE_ACCESS_LOCK_MINUTES, DISCHARGE_JWT_SECRET, JWT_ALGORITHM,
)
from services.discharge_service import (
    PENDING_REVIEW_MESSAGE,
    build_medication_reconciliation,
    is_reviewed,
    parse_comorbidities,
)

# ────────────────────────────────────────────────────────────────
#  Per-IP global throttle for /verify  (finding: discharge DOB
#  brute-force — per-token lockout is bypassable by holding many
#  tokens, since each token has an independent counter and one
#  source IP is never globally throttled).
#
#  This is an in-process sliding window keyed on source IP across
#  ALL tokens. It is intentionally simple and thread-safe; the
#  current server runs single-process (main.py uvicorn workers=1).
#  DEFERRED (cross-process / multi-worker / restart-durable): move
#  this state to a shared store (a login_attempts-style SQLite table
#  or Redis) so it survives restarts and is shared across workers.
#  Tracked alongside the auth_routes login-limiter deferral.
# ────────────────────────────────────────────────────────────────
_VERIFY_IP_ATTEMPTS: Dict[str, list] = {}
_VERIFY_IP_LOCK = threading.Lock()
# Global ceiling per IP across all tokens within the window. Set
# higher than the per-token MAX so legitimate multi-token use isn't
# blocked, but low enough to throttle cross-token brute force.
_VERIFY_IP_MAX_ATTEMPTS = max(DISCHARGE_ACCESS_MAX_ATTEMPTS * 3, 15)
_VERIFY_IP_WINDOW_SECONDS = DISCHARGE_ACCESS_LOCK_MINUTES * 60


def _is_verify_ip_throttled(ip: Optional[str]) -> bool:
    """Sliding-window per-IP throttle across all discharge tokens."""
    if not ip or ip == "unknown":
        return False
    now = time.time()
    with _VERIFY_IP_LOCK:
        attempts = [t for t in _VERIFY_IP_ATTEMPTS.get(ip, []) if now - t < _VERIFY_IP_WINDOW_SECONDS]
        _VERIFY_IP_ATTEMPTS[ip] = attempts
        return len(attempts) >= _VERIFY_IP_MAX_ATTEMPTS


def _record_verify_ip_attempt(ip: Optional[str]) -> None:
    if not ip or ip == "unknown":
        return
    now = time.time()
    with _VERIFY_IP_LOCK:
        bucket = [t for t in _VERIFY_IP_ATTEMPTS.get(ip, []) if now - t < _VERIFY_IP_WINDOW_SECONDS]
        bucket.append(now)
        _VERIFY_IP_ATTEMPTS[ip] = bucket

# ────────────────────────────────────────────────────────────────
#  THE SECOND FACTOR  (finding: pilot dry run, 2026-08-14)
#
#  Every discharge printed a summary URL and a QR code, and NOT ONE of them
#  could be opened: unlocking needs a stored date of birth, the admission path
#  never captured one, and 0 of 31 patients had one. The patient scanned a code
#  that could only ever answer "Incorrect Date of Birth".
#
#  DECISION — the factor stays the date of birth, and the LINK is what gives
#  way. The patient-facing portal (src/pages/DischargeAccess.tsx) asks for one
#  thing through an <input type="date">, so the only second factor it can carry
#  is a date. The two dates the hospital always holds — admission and discharge
#  — are printed on the same sheet as the QR and guessable within a handful of
#  tries against a 5-attempt lockout, which is not a second factor at all. So:
#  capture the date of birth at admission (optional, never blocking — see
#  services/admission_service), and where there is none on file, issue NO link
#  and say why. A code that cannot be opened is worse than no code: it sends the
#  patient away believing they have their record.
#
#  ONE definition, used at BOTH ends. `usable_dob` decides what the discharge
#  desk may print AND what verification will accept, so the two can never drift
#  into a link that issues but never opens — which is exactly the shape of the
#  original defect. A stored value the portal's date input cannot reproduce
#  ("01/02/1990", "1990-01-01 00:00:00" with a time on it) is normalized or, if
#  it cannot be, treated as absent.
# ────────────────────────────────────────────────────────────────
SECOND_FACTOR_FIELD = "date_of_birth"
NO_SECOND_FACTOR_REASON = (
    "No date of birth is on file for this patient, so a secure discharge link "
    "would have nothing to check the patient against and could never be opened. "
    "Record the patient's date of birth, then issue the link."
)


def usable_dob(value) -> Optional[str]:
    """Normalize a date of birth to the YYYY-MM-DD the portal can send, or None.

    None means "no usable second factor": absent, blank, or stored in a shape
    the patient's date picker cannot reproduce. Never raises — it is called on
    the discharge path, and a discharge must not fail over a date format.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Accept a stored timestamp ("1990-01-01 00:00:00" / "1990-01-01T00:00:00")
    # by taking its date part; anything else must already be a real ISO date.
    candidate = text[:10]
    try:
        parsed = datetime.strptime(candidate, "%Y-%m-%d").date()
    except ValueError:
        return None
    if parsed.year < 1900 or parsed > datetime.now(timezone.utc).date():
        return None
    return parsed.isoformat()


def second_factor_status(patient_id: str, db=None) -> Dict[str, Any]:
    """Can a discharge link be issued for this patient, and if not, why not?

    Returns {"available": bool, "factor": "date_of_birth"|None, "reason": str|None}.
    The reason is written for the person at the discharge desk, because they are
    the only one who can fix it. Sync; call via run_db from async.
    """
    def _lookup(conn):
        return conn.execute(
            "SELECT date_of_birth FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()

    row = _lookup(db) if db is not None else None
    if db is None:
        with db_session() as own:
            row = _lookup(own)

    if row is not None and usable_dob(row["date_of_birth"]):
        return {"available": True, "factor": SECOND_FACTOR_FIELD, "reason": None}
    return {"available": False, "factor": None, "reason": NO_SECOND_FACTOR_REASON}


def hash_token(raw_token: str) -> str:
    """SHA-256 hex digest of a presented raw discharge token.

    Discharge tokens are stored hashed only (finding: raw tokens were persisted
    as the token_id PK and leaked into audit_logs via correlation_id). New rows
    store the hash in both token_id and token_hash; legacy rows always had the
    correct hash in token_hash, and init_db() scrubs their plaintext token_id.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()

def get_token_record(db, token: str) -> Optional[Dict]:
    """Retrieve the token record by hashing the presented raw token."""
    row = db.execute(
        "SELECT * FROM discharge_access_tokens WHERE token_hash = ?",
        (hash_token(token),)
    ).fetchone()
    return dict(row) if row else None

def verify_discharge_access(token: str, dob: str, ip_address: str = None) -> Dict[str, Any]:
    """Verify a patient's access to their discharge summary using their DOB."""
    # All DB updates and audit correlation use the token HASH — the raw secret
    # must never be persisted (audit_logs is append-only and long-lived).
    token_id = hash_token(token)

    # Global per-IP throttle across ALL tokens (finding: cross-token
    # brute-force bypass of per-token lockout). Checked before any DB
    # work so a throttled IP can't probe token validity either.
    if _is_verify_ip_throttled(ip_address):
        log_audit_event(
            correlation_id=token_id,
            event_category="security",
            action_type="discharge_verification",
            outcome="failure",
            summary_message="Discharge verification IP-throttled",
            ip_address=ip_address,
        )
        return {"status": "error", "reason": "rate_limited"}

    # Every attempt (valid or not) counts toward the per-IP window.
    _record_verify_ip_attempt(ip_address)

    with db_session() as db:
        token_record = get_token_record(db, token)
        now = datetime.now(timezone.utc)

        log_audit_event(
            correlation_id=token_id,
            event_category="security",
            action_type="discharge_access_attempted",
            outcome="success",
            db=db,
            summary_message="Discharge verification attempted",
            ip_address=ip_address
        )

        if not token_record:
            log_audit_event(
                correlation_id=token_id,
                event_category="security",
                action_type="discharge_verification",
                outcome="failure",
                db=db,
                summary_message="Token not found",
                ip_address=ip_address
            )
            return {"status": "error", "reason": "invalid_token"}
            
        patient_id = token_record["patient_id"]

        if token_record["revoked_at"]:
            log_audit_event(correlation_id=token_id, event_category="security", action_type="discharge_verification", outcome="failure", db=db, summary_message="Token revoked", patient_id=patient_id, ip_address=ip_address)
            return {"status": "error", "reason": "revoked"}

        expires_at = datetime.fromisoformat(token_record["expires_at"])
        if now > expires_at:
            log_audit_event(correlation_id=token_id, event_category="security", action_type="discharge_link_expired", outcome="failure", db=db, summary_message="Token expired", patient_id=patient_id, ip_address=ip_address)
            return {"status": "error", "reason": "expired"}

        if token_record["locked_until"]:
            locked_until = datetime.fromisoformat(token_record["locked_until"])
            if now < locked_until:
                log_audit_event(correlation_id=token_id, event_category="security", action_type="discharge_verification", outcome="failure", db=db, summary_message="Token temporarily locked", patient_id=patient_id, ip_address=ip_address)
                return {"status": "error", "reason": "locked", "locked_until": locked_until.isoformat()}
            else:
                db.execute("UPDATE discharge_access_tokens SET locked_until = NULL, failed_attempts = 0 WHERE token_hash = ?", (token_id,))

        patient = db.execute("SELECT date_of_birth FROM patients WHERE id = ?", (patient_id,)).fetchone()

        # Constant-time DOB comparison (finding: non-constant-time `!=`
        # leaks length/prefix timing on a low-entropy second factor).
        #
        # Both sides go through usable_dob — the SAME function that decides
        # whether a link may be printed at all — so "issued" and "openable" mean
        # one thing. A patient with no usable DOB fails here exactly as a wrong
        # date does (reason 'invalid_dob', no hint that the record is the
        # problem): the response must never tell a token holder whether the
        # patient exists or what state their record is in.
        stored_dob = usable_dob(patient["date_of_birth"]) if patient else None
        presented_dob = usable_dob(dob)
        dob_ok = (
            stored_dob is not None
            and presented_dob is not None
            and hmac.compare_digest(stored_dob, presented_dob)
        )

        if not dob_ok:
            # Atomic increment (finding: read-then-write race lets parallel
            # guesses increment the counter only once). Increment in SQL and
            # read the authoritative value back within the same transaction.
            db.execute(
                "UPDATE discharge_access_tokens SET failed_attempts = failed_attempts + 1 WHERE token_hash = ?",
                (token_id,)
            )
            new_attempts_row = db.execute(
                "SELECT failed_attempts FROM discharge_access_tokens WHERE token_hash = ?",
                (token_id,)
            ).fetchone()
            new_attempts = new_attempts_row["failed_attempts"] if new_attempts_row else DISCHARGE_ACCESS_MAX_ATTEMPTS

            locked_until = None
            if new_attempts >= DISCHARGE_ACCESS_MAX_ATTEMPTS:
                locked_until = (now + timedelta(minutes=DISCHARGE_ACCESS_LOCK_MINUTES)).isoformat()
                db.execute(
                    "UPDATE discharge_access_tokens SET locked_until = ? WHERE token_hash = ?",
                    (locked_until, token_id)
                )
                log_audit_event(correlation_id=token_id, event_category="security", action_type="discharge_access_locked", outcome="success", db=db, summary_message=f"Locked for {DISCHARGE_ACCESS_LOCK_MINUTES}m after {new_attempts} failed attempts", patient_id=patient_id, ip_address=ip_address)

            log_audit_event(correlation_id=token_id, event_category="security", action_type="discharge_verification", outcome="failure", db=db, summary_message="DOB mismatch", patient_id=patient_id, ip_address=ip_address)

            if locked_until:
                return {"status": "error", "reason": "locked", "locked_until": locked_until}
            # Do NOT return attempts_remaining (finding: leaking the
            # remaining-attempt count aids the brute-force attacker).
            return {"status": "error", "reason": "invalid_dob"}

        # Success - mark verified
        db.execute(
            "UPDATE discharge_access_tokens SET verified_at = ?, failed_attempts = 0, locked_until = NULL WHERE token_hash = ?",
            (now.isoformat(), token_id)
        )
        
        log_audit_event(correlation_id=token_id, event_category="security", action_type="discharge_verification", outcome="success", db=db, summary_message="DOB verified", patient_id=patient_id, ip_address=ip_address)

        # Generate short-lived access grant (JWT) for this session (e.g. 1 hour).
        # Signed with DISCHARGE_JWT_SECRET, never the staff JWT_SECRET, so a leak
        # or rotation of one trust domain does not reach the other. The explicit
        # aud="discharge" claim stays as a second fence against conflation.
        payload = {
            "sub": "discharge_access",
            "aud": "discharge",
            "token_id": token_id,
            "patient_id": patient_id,
            "exp": now + timedelta(hours=1)
        }
        access_grant = jwt.encode(payload, DISCHARGE_JWT_SECRET, algorithm=JWT_ALGORITHM)
        
        return {
            "status": "success",
            "access_grant": access_grant,
            "patient_id": patient_id
        }

def get_verified_discharge_summary(token: str, access_grant: str, ip_address: str = None) -> Dict[str, Any]:
    """Retrieve the summary using a verified access grant."""
    # The grant carries (and audit correlation uses) the token HASH, never the raw secret.
    token_id = hash_token(token)
    try:
        # The discharge key AND the discharge audience: a staff token fails on
        # both, and a grant forged with the staff secret fails on the key.
        payload = jwt.decode(
            access_grant,
            DISCHARGE_JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience="discharge",
        )
    except jwt.ExpiredSignatureError:
        return {"status": "error", "reason": "grant_expired"}
    except jwt.InvalidTokenError:
        return {"status": "error", "reason": "invalid_grant"}

    if payload.get("sub") != "discharge_access" or payload.get("token_id") != token_id:
        return {"status": "error", "reason": "invalid_grant"}
        
    patient_id = payload["patient_id"]
    # Read on its own connection, before this session opens: the
    # reconciliation is a separate read and must not nest inside a session
    # that goes on to write the access stamp and audit row.
    rec = build_medication_reconciliation(patient_id)

    with db_session() as db:
        token_record = get_token_record(db, token)
        if not token_record or token_record["revoked_at"]:
             return {"status": "error", "reason": "revoked"}
             
        summary = db.execute(
            "SELECT * FROM discharge_summaries WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (patient_id,)
        ).fetchone()

        if not summary:
            return {"status": "error", "reason": "not_found"}

        # The MRN is patients.mrn. This used to select `p.id AS mrn`, so every
        # printed record carried the internal row id where the hospital number
        # belongs. The admission id is the encounter this summary closes.
        patient_row = db.execute(
            "SELECT p.mrn, p.name, p.age, p.sex, p.ward, p.bed, p.diagnosis, p.admission_date, "
            "       u.name as attending_doctor_name, "
            "       (SELECT a.id FROM admissions a WHERE a.patient_id = p.id "
            "         ORDER BY (a.status = 'active') DESC, a.created_at DESC, a.rowid DESC LIMIT 1) "
            "         AS admission_id "
            "FROM patients p LEFT JOIN users u ON p.attending_doctor_id = u.id WHERE p.id = ?",
            (patient_id,)
        ).fetchone()

        allergies, allergies_read = [], True
        try:
            allergies = [dict(a) for a in db.execute(
                "SELECT substance, reaction, severity FROM patient_allergies "
                "WHERE patient_id = ? AND status = 'active' ORDER BY substance",
                (patient_id,),
            ).fetchall()]
        except Exception:
            # Unreadable is not "none": the print view says it could not be read.
            allergies_read = False

        comorbidities = []
        try:
            prof = db.execute(
                "SELECT comorbidities FROM patient_profiles WHERE patient_id = ?", (patient_id,)
            ).fetchone()
            comorbidities = parse_comorbidities(prof["comorbidities"] if prof else None)
        except Exception:
            comorbidities = []

        # Discharge medicines are the reconciled CONTINUED list, and only the
        # ones a pharmacist need not verify first. Discontinued and unverified
        # AI-extracted orders used to be printed as the patient's prescription.
        medications = [
            {"name": m["name"], "drug_name": m["name"], "dosage": m["dosage"],
             "route": m["route"], "frequency": m["frequency"], "status": "prn" if m.get("prn") else "active"}
            for m in rec["continued"] if not m.get("needs_verification")
        ]
        unverified_count = sum(1 for m in rec["continued"] if m.get("needs_verification"))

        reviewed = is_reviewed(summary)
        reviewer_name = None
        if reviewed and summary["reviewed_by"]:
            reviewer = db.execute(
                "SELECT name FROM users WHERE id = ?", (summary["reviewed_by"],)
            ).fetchone()
            reviewer_name = reviewer["name"] if reviewer else None

        site_name = None
        try:
            from services.site_config_service import current_site_config
            site_name = current_site_config().site_name
        except Exception:
            site_name = None

        now = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE discharge_access_tokens SET last_accessed_at = ? WHERE token_hash = ?", (now, token_id))
        
        log_audit_event(
            correlation_id=token_id,
            event_category="clinical_access",
            action_type="discharge_summary_viewed",
            outcome="success",
            db=db,
            summary_message="Discharge summary accessed by patient via verified token",
            patient_id=patient_id,
            ip_address=ip_address
        )
        
        # The drafted text reaches the patient only once a clinician has marked
        # it reviewed. Until then the patient is told it is being finalised;
        # the recorded facts beside it (medicines, allergies) are not drafts.
        return {
            "status": "success",
            "summary": summary["summary"] if reviewed else None,
            "summary_status": "reviewed" if reviewed else "draft",
            "summary_message": None if reviewed else PENDING_REVIEW_MESSAGE,
            "reviewed_by_name": reviewer_name,
            "reviewed_at": summary["reviewed_at"] if reviewed else None,
            "discharged_at": summary["discharged_at"],
            "timeline": json.loads(summary["timeline_json"]) if summary["timeline_json"] else [],
            "patient": dict(patient_row) if patient_row else {},
            "allergies": allergies if allergies_read else None,
            "comorbidities": comorbidities,
            "medications": medications,
            "unverified_medication_count": unverified_count,
            "site_name": site_name,
        }
