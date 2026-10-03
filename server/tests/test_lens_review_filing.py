"""Lens: a read reaches a record only as the text a clinician confirmed (F041).

THE FAILURE. The assign route moved patient_id and nothing else: the reviewed
text went to the case room, while the row kept the raw model read, so the chart,
the discharge timeline, Sage and the patient portal all showed the unedited
draft. /analyze also filed a draft straight onto a chart when the form named a
patient.

NOW. Assign refuses without reviewed text, stores reviewed_by / reviewed_at /
reviewed_text (migration 45) and audits the filing; only the analysing
clinician can file an unfiled read; Sage and the discharge timeline read
reviewed rows and reviewed_text only. The portal half is in
tests/test_portal_labs_and_requests.py.
"""
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


def _user(role="doctor") -> str:
    uid = f"LENSREV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        # No department: unscoped, so ward scoping cannot mask the behaviour here.
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
            (uid, f"Dr Lens {uid[-4:]}", role),
        )
    return uid


def _patient() -> str:
    pid = f"PLENS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status, age, sex) "
            "VALUES (?, 'Lens Review Test', ?, 'GM', 'B-1', 'Pneumonia', 'admitted', 60, 'M')",
            (pid, f"MRN-{pid}"),
        )
    return pid


def _unfiled_read(author: str, analysis: str = "RAW MODEL READ: consolidation") -> int:
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO lens_analyses (patient_id, author_id, image_path, image_type, "
            "clinical_context, analysis) VALUES (NULL, ?, '/tmp/lens_x.png', 'xray', 'Cough', ?)",
            (author, analysis),
        )
        return int(cur.lastrowid)


def _auth(uid: str, role: str = "doctor") -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _row(analysis_id: int):
    with db_session() as db:
        return db.execute("SELECT * FROM lens_analyses WHERE id = ?", (analysis_id,)).fetchone()


def test_filing_without_reviewed_text_is_refused_and_files_nothing():
    doc, pid = _user(), _patient()
    aid = _unfiled_read(doc)
    for text in (None, "", "   "):
        data = {"patient_id": pid}
        if text is not None:
            data["reviewed_text"] = text
        r = client.patch(f"/api/lens/analyses/{aid}/assign", data=data, headers=_auth(doc))
        assert r.status_code == 422, r.text
    row = _row(aid)
    assert row["patient_id"] is None
    assert row["reviewed_at"] is None


def test_filing_stores_the_confirmed_text_who_and_when_and_audits_it():
    doc, pid = _user(), _patient()
    aid = _unfiled_read(doc)
    r = client.patch(
        f"/api/lens/analyses/{aid}/assign",
        data={"patient_id": pid, "reviewed_text": "  Right lower lobe opacity, confirmed  "},
        headers=_auth(doc),
    )
    assert r.status_code == 200, r.text
    row = _row(aid)
    assert row["patient_id"] == pid
    assert row["reviewed_by"] == doc
    assert row["reviewed_at"]
    assert row["reviewed_text"] == "Right lower lobe opacity, confirmed"
    # The raw read is kept, untouched: it is the evidence of what the model said.
    assert row["analysis"].startswith("RAW MODEL READ")
    with db_session() as db:
        audit = db.execute(
            "SELECT actor_user_id, patient_id FROM audit_logs "
            "WHERE action_type = 'lens_read_filed' AND target_id = ?",
            (str(aid),),
        ).fetchone()
    assert audit is not None and audit["actor_user_id"] == doc and audit["patient_id"] == pid


def test_another_clinicians_unfiled_read_cannot_be_filed():
    author, other, pid = _user(), _user(), _patient()
    aid = _unfiled_read(author)
    r = client.patch(
        f"/api/lens/analyses/{aid}/assign",
        data={"patient_id": pid, "reviewed_text": "Looks fine"},
        headers=_auth(other),
    )
    assert r.status_code == 404
    assert _row(aid)["patient_id"] is None


def test_a_filed_read_is_not_moved_to_another_patient():
    doc, first, second = _user(), _patient(), _patient()
    aid = _unfiled_read(doc)
    ok = client.patch(f"/api/lens/analyses/{aid}/assign",
                      data={"patient_id": first, "reviewed_text": "Confirmed"},
                      headers=_auth(doc))
    assert ok.status_code == 200
    again = client.patch(f"/api/lens/analyses/{aid}/assign",
                         data={"patient_id": second, "reviewed_text": "Confirmed"},
                         headers=_auth(doc))
    assert again.status_code == 409
    assert _row(aid)["patient_id"] == first


def test_the_disclaimer_an_old_client_sends_is_not_stored_as_findings():
    from routes.lens_routes import _DISCLAIMER_LINE
    doc, pid = _user(), _patient()
    aid = _unfiled_read(doc)
    r = client.patch(f"/api/lens/analyses/{aid}/assign",
                     data={"patient_id": pid, "reviewed_text": f"Clear lungs\n---\n{_DISCLAIMER_LINE}"},
                     headers=_auth(doc))
    assert r.status_code == 200
    assert _row(aid)["reviewed_text"] == "Clear lungs"


def _filed(pid: str, author: str, *, reviewed: bool, image_type: str = "xray",
           text: str = "Reviewed: small effusion", analysis: str = "DRAFT ONLY") -> int:
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO lens_analyses (patient_id, author_id, image_path, image_type, "
            "clinical_context, analysis, reviewed_by, reviewed_at, reviewed_text) "
            "VALUES (?, ?, '', ?, 'ctx', ?, ?, ?, ?)",
            (pid, author, image_type, analysis,
             author if reviewed else None,
             "2026-10-02T09:00:00+00:00" if reviewed else None,
             text if reviewed else None),
        )
        return int(cur.lastrowid)


def test_sage_reads_only_the_reviewed_text_and_never_a_draft():
    from services.sage_service import _assemble_patient_context

    doc, pid = _user(), _patient()
    _filed(pid, doc, reviewed=False, analysis="DRAFT ONLY: pneumothorax")
    with db_session() as db:
        ctx = _assemble_patient_context(db, pid, None)
    assert "imaging" not in ctx or "DRAFT ONLY" not in ctx.get("imaging", "")
    assert "No recent imaging" in ctx["completeness"]["missing"]

    _filed(pid, doc, reviewed=True, text="Reviewed: small effusion")
    with db_session() as db:
        ctx = _assemble_patient_context(db, pid, None)
    assert ctx["imaging"] == "XRAY: Reviewed: small effusion"


def test_the_lab_bots_lens_copy_never_reaches_sage_as_imaging():
    from services.sage_service import _assemble_patient_context

    doc, pid = _user(), _patient()
    _filed(pid, doc, reviewed=True, image_type="lab", text="CBC copy")
    with db_session() as db:
        ctx = _assemble_patient_context(db, pid, None)
    assert "CBC copy" not in (ctx.get("imaging") or "")


def test_the_discharge_timeline_carries_reviewed_reads_only():
    from services.discharge_service import build_patient_timeline

    doc, pid = _user(), _patient()
    _filed(pid, doc, reviewed=False, analysis="DRAFT ONLY: mass")
    _filed(pid, doc, reviewed=True, text="Reviewed: no mass")
    imaging = [e for e in build_patient_timeline(pid) if e["type"] == "imaging"]
    assert [e["detail"] for e in imaging] == ["Reviewed: no mass"]


def test_the_chart_gets_an_authenticated_image_url_not_a_disk_path():
    from services.lens_service import decorate_analysis, image_url_for

    assert image_url_for("/Users/orb/server/uploads/lens_abc.png") == "/uploads/lens_abc.png"
    assert image_url_for("/uploads/mock_ct.png") == "/uploads/mock_ct.png"
    assert image_url_for("") is None
    assert decorate_analysis({"image_path": "/a/b/lens_1.png", "id": 1})["image_url"] == "/uploads/lens_1.png"


def test_analyze_never_files_the_draft_even_when_a_patient_is_named(monkeypatch):
    """The SSE body saves the row with patient_id None whatever the form said."""
    import routes.lens_routes as lr

    saved = {}

    def fake_save(**kwargs):
        saved.update(kwargs)
        return 4242

    async def fake_stream(*a, **k):
        yield "FINDINGS: none"

    monkeypatch.setattr(lr, "save_lens_analysis", fake_save)
    monkeypatch.setattr(lr, "analyze_image_stream", fake_stream)
    monkeypatch.setattr("config.LENS_ENABLED", True)
    monkeypatch.setattr("services.crypto_service.write_encrypted", lambda path, data: None)

    from contextlib import contextmanager

    @contextmanager
    def passthrough(path):
        yield path

    monkeypatch.setattr("services.crypto_service.materialize_plaintext", passthrough)

    doc, pid = _user(), _patient()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    r = client.post(
        "/api/lens/analyze",
        data={"patient_id": pid, "image_type": "xray"},
        files={"image": ("scan.png", png, "image/png")},
        headers=_auth(doc),
    )
    assert r.status_code == 200, r.text
    assert '"analysis_id": 4242' in r.text
    assert saved["patient_id"] is None
    assert saved["author_id"] == doc
