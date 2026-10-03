"""Item 10 regressions — Scribe sign acknowledgement, transcript-read audit, ASR probe.

Three invariants, each of which was structurally false before the fix:

  1. SIGN GATE, DEFAULT CONFIG. With verification OFF (the shipped default) a
     fresh note carries {"status": "not_requested"} and the sign gate 428s —
     that is correct and fail-closed, and must stay. What must ALSO hold: an
     acknowledged sign persists WHO acknowledged and WHEN on the note row
     itself (scribe_notes.unverified_ack_by / unverified_ack_at — its own
     columns since migration 19, because verification_json is rewritten
     wholesale by the verifier and copied onto revision drafts), and if that
     write fails the note is NOT signed.

  1b. AN ACKNOWLEDGEMENT BELONGS TO ONE NOTE. Revising a signed note produces a
     new DRAFT that nobody has acknowledged: it inherits neither the ack columns
     nor any legacy in-JSON marker.

  1c. THE SSE STRUCTURE ROUTE SAVES OFF THE LOOP. The generator's note save is a
     SQLCipher write; run on the event loop it is the failure class behind all
     three of Orb's production freezes, and the discipline scan cannot see it
     through the service call.

  2. TRANSCRIPT SERVING IS AUDITED. GET /api/scribe/notes and
     GET /api/scribe/notes/{id} serve verbatim consultation transcripts; every
     serve writes a fail-closed ``phi_access`` row BEFORE content is returned,
     and a failed audit write blocks the read (services/audit_service.py:36).

  3. ASR PROBE IS HONEST. /api/scribe/asr/status says "unavailable" only when
     neither the in-venv runtime nor the warm daemon can transcribe, and
     "available" when the subprocess worker path is whole.
"""
import asyncio
import json

from fastapi.testclient import TestClient

from main import app
from database import db_session

client = TestClient(app)


def _login(user_id: str, password: str) -> str:
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _doctor_token() -> str:
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("I10-DOC", "Dr I10", "doctor", hash_password("docpass123")),
        )
    return _login("I10-DOC", "docpass123")


def _make_note(verification=None, raw_transcript="patient reports chest pain since morning") -> int:
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note, note_format, status, verification_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (None, "I10-DOC", raw_transcript, "S: note", "soap", "draft",
             json.dumps(verification) if verification else None),
        )
        return cur.lastrowid


def _load_note_row(note_id: int) -> dict:
    with db_session() as db:
        row = db.execute("SELECT * FROM scribe_notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


def _audit_rows(action_type: str, target_id: str = None) -> list:
    q = "SELECT * FROM audit_logs WHERE event_category='phi_access' AND action_type=?"
    params = [action_type]
    if target_id is not None:
        q += " AND target_id=?"
        params.append(target_id)
    with db_session() as db:
        rows = db.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def _sign_audit_metadata(note_id: int) -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE event_category='scribe' "
            "AND action_type='scribe_draft_signed' AND target_id=? ORDER BY id DESC LIMIT 1",
            (str(note_id),),
        ).fetchone()
    assert row, f"no scribe_draft_signed audit row for note {note_id}"
    return json.loads(row["metadata_json"])


# ── 1. Sign gate: default config + persisted acknowledgement ─────────────────

def test_default_config_note_428s_without_ack():
    """A note created under the shipped default (verify disabled →
    {"status": "not_requested"}) must hit the fail-closed gate, never
    one-click sign."""
    token = _doctor_token()
    note_id = _make_note(verification={"status": "not_requested"})
    resp = client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["requires"] == "acknowledge_unverified"


def test_acknowledged_sign_persists_who_and_when_on_the_note():
    """WHO/WHEN land in the note's OWN columns.

    Not in verification_json: the background verifier and
    POST /notes/{id}/verify both replace that blob wholesale, so an attestation
    stored inside it can be erased after the fact by a retry.
    """
    token = _doctor_token()
    note_id = _make_note(verification={"status": "not_requested"})
    resp = client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={"acknowledge_unverified": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "signed"

    row = _load_note_row(note_id)
    assert row["status"] == "signed"
    assert row["unverified_ack_by"] == "I10-DOC"
    assert row["unverified_ack_at"], "acknowledgement must carry a timestamp"
    # The verification report is left exactly as the verifier wrote it — the
    # legacy in-JSON marker is no longer produced (nothing read it).
    assert "signer_acknowledged_unverified" not in (row["verification_json"] or "")

    # WHY the override was needed is on the hash chain, which is where an
    # immutable record of it belongs.
    meta = _sign_audit_metadata(note_id)
    assert meta["acknowledged_unverified"] is True
    assert isinstance(meta["verification_reasons"], list) and meta["verification_reasons"]


def test_clean_verified_sign_records_no_acknowledgement():
    token = _doctor_token()
    note_id = _make_note(
        verification={"verified": True, "verification_status": "verified", "warnings": [], "omissions": []}
    )
    resp = client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 200, resp.text
    row = _load_note_row(note_id)
    assert row["unverified_ack_by"] is None
    assert row["unverified_ack_at"] is None
    v = json.loads(row["verification_json"])
    assert "signer_acknowledged_unverified" not in v


def test_sign_refused_when_ack_cannot_be_persisted(monkeypatch):
    """Fail-closed: if the acknowledgement write fails, the note stays a draft."""
    token = _doctor_token()
    note_id = _make_note(verification={"status": "not_requested"})
    monkeypatch.setattr(
        "routes.scribe_routes.record_unverified_ack", lambda *a, **kw: False
    )
    resp = client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={"acknowledge_unverified": True},
    )
    assert resp.status_code == 500, resp.text
    assert _load_note_row(note_id)["status"] == "draft"


# ── 1b. An acknowledgement belongs to exactly one note ───────────────────────

def test_revision_of_an_acknowledged_note_carries_no_acknowledgement():
    """Revising a signed note must produce a draft nobody has acknowledged.

    Both halves matter: the ack COLUMNS are not copied, and a legacy in-JSON
    marker on the original is stripped on the way across — otherwise the new
    draft would claim an attestation that was made for a different note.
    """
    token = _doctor_token()
    note_id = _make_note(verification={"status": "not_requested"})
    assert client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={"acknowledge_unverified": True},
    ).status_code == 200

    # A note signed before migration 19 also carries the marker in its report.
    with db_session() as db:
        db.execute(
            "UPDATE scribe_notes SET verification_json = ? WHERE id = ?",
            (json.dumps({
                "status": "not_requested",
                "signer_acknowledged_unverified": {
                    "by": "I10-DOC", "at": "2026-08-01T09:00:00+00:00",
                    "note_id": note_id, "reasons": ["verified=None"],
                },
            }), note_id),
        )

    resp = client.post(
        f"/api/scribe/notes/{note_id}/revise",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    revision = _load_note_row(resp.json()["new_note_id"])

    assert revision["status"] == "draft"
    assert revision["unverified_ack_by"] is None
    assert revision["unverified_ack_at"] is None
    assert "signer_acknowledged_unverified" not in (revision["verification_json"] or ""), (
        "a revision must not inherit an acknowledgement made on the original"
    )
    # The rest of the report still crosses over — this strips one key, not the
    # verification result the next signer needs to see.
    assert json.loads(revision["verification_json"])["status"] == "not_requested"

    # And the original keeps its own attestation.
    assert _load_note_row(note_id)["unverified_ack_by"] == "I10-DOC"


# ── 1c. The SSE structure route saves the note OFF the event loop ────────────

def _stub_draft_stream(monkeypatch, text: str = "S: chest pain since morning"):
    async def _fake_stream(transcript, format_type, **_audit_labels):
        yield text
    monkeypatch.setattr("routes.scribe_routes.structure_transcript_stream", _fake_stream)


def test_structure_route_saves_the_note_off_the_event_loop(monkeypatch):
    """The save must run in a worker thread, not on the loop.

    Proven the only way that cannot be faked by a name check: the stand-in save
    asks for a RUNNING event loop in its own thread. On the loop that call
    succeeds; off the loop it raises RuntimeError. `run_db` is what makes the
    second true.
    """
    token = _doctor_token()
    seen = {}

    def _fake_save(**kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return 4242

    _stub_draft_stream(monkeypatch)
    monkeypatch.setattr("routes.scribe_routes.save_scribe_note", _fake_save)

    resp = client.post(
        "/api/scribe/structure",
        headers={"Authorization": f"Bearer {token}"},
        json={"transcript": "chest pain since morning", "format": "soap"},
    )
    assert resp.status_code == 200, resp.text
    assert '"id": 4242' in resp.text or '"id":4242' in resp.text
    assert seen.get("on_loop") is False, "save_scribe_note ran ON the event loop"


def test_structure_route_emits_an_error_event_when_the_save_fails(monkeypatch):
    """Contract on save failure, unchanged by moving the write off-loop: the
    stream ends with a single error event and no done event — the client must
    not be told a note id that does not exist."""
    token = _doctor_token()

    def _explode(**kwargs):
        raise RuntimeError("database is locked")

    _stub_draft_stream(monkeypatch)
    monkeypatch.setattr("routes.scribe_routes.save_scribe_note", _explode)

    resp = client.post(
        "/api/scribe/structure",
        headers={"Authorization": f"Bearer {token}"},
        json={"transcript": "chest pain since morning", "format": "soap"},
    )
    assert resp.status_code == 200, resp.text
    assert '"error": "database is locked"' in resp.text
    assert '"done"' not in resp.text


# ── 2. Transcript serving writes a fail-closed phi_access row ────────────────

def test_get_note_writes_phi_access_audit_row():
    token = _doctor_token()
    note_id = _make_note()
    before = len(_audit_rows("scribe_note_viewed", str(note_id)))
    resp = client.get(
        f"/api/scribe/notes/{note_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["raw_transcript"]
    rows = _audit_rows("scribe_note_viewed", str(note_id))
    assert len(rows) == before + 1, "serving a transcript must write a phi_access row"
    assert rows[-1]["actor_user_id"] == "I10-DOC"


def test_list_notes_writes_phi_access_audit_row():
    token = _doctor_token()
    _make_note()
    before = len(_audit_rows("scribe_notes_listed"))
    resp = client.get("/api/scribe/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    rows = _audit_rows("scribe_notes_listed")
    assert len(rows) == before + 1
    assert rows[-1]["actor_user_id"] == "I10-DOC"


def test_transcript_not_served_when_audit_write_fails(monkeypatch):
    """phi_access is fail-closed: a failed audit write must block the read —
    the transcript must not leave the process unaudited."""
    from services.audit_service import AuditWriteError

    token = _doctor_token()
    secret = "unaudited-transcript-canary-72h"
    note_id = _make_note(raw_transcript=secret)

    def _refuse(**kwargs):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr("routes.scribe_routes.log_phi_read", _refuse)
    local = TestClient(app, raise_server_exceptions=False)
    local.cookies = client.cookies
    resp = local.get(
        f"/api/scribe/notes/{note_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code >= 500, resp.text
    assert secret not in resp.text


# ── 3. ASR availability probe ────────────────────────────────────────────────

def test_asr_status_reports_unavailable_when_runtime_and_daemon_absent(monkeypatch):
    token = _doctor_token()
    monkeypatch.setattr("routes.scribe_routes._asr_runtime_importable", lambda: False)
    monkeypatch.setattr("routes.scribe_routes._asr_daemon_responds", lambda: False)
    resp = client.get("/api/scribe/asr/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert "lightning_whisper_mlx" in body["reason"]
    assert "INSTALL_RUNBOOK" in body["runbook"]


def test_asr_status_reports_available_when_worker_path_whole(monkeypatch):
    token = _doctor_token()
    monkeypatch.setattr("routes.scribe_routes._asr_runtime_importable", lambda: True)
    # WORKER_SCRIPT exists in the repo, so no daemon probe should be needed —
    # make the daemon path an assertion failure to prove it is not consulted.
    monkeypatch.setattr(
        "routes.scribe_routes._asr_daemon_responds",
        lambda: (_ for _ in ()).throw(AssertionError("daemon probed despite whole worker path")),
    )
    resp = client.get("/api/scribe/asr/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["reason"] is None
