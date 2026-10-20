"""Two-facility go-live drill (multisite-18, W14-A, 20 October 2026).

The drill docs/MULTI_SITE.md runs on the isolated verify stack, rehearsed here
end to end in a database this module owns (isolated_module_database):

  * Facility 1 is a working hospital before the drill: a ward doctor and nurse
    of its department, two patients admitted through the admission form's
    route, each with an allergy and a medicine charted through the drug chart.
    A department-less administrator holds group-wide access, granted at the
    console (scripts/grant_enterprise_access.py), facility 1 has its GSTIN
    (PATCH /api/facilities/1) and the audit facility index is built
    (scripts/build_audit_facility_index.py).
  * scripts/seed_second_facility.py then adds facility 2 through the product.
    With the facility fence as the code ships it (screen groups still blind)
    the create gate refuses, the seed stops before writing anything, and the
    one reason left is the fence: the create gate passes through the product
    only when every sweep is done. With every group cleared (the fence
    emptied, FACILITY_BLIND_SURFACES = ()) the same seed adds the facility
    through POST /api/facilities, its department, its doctor, nurse,
    administrator, pharmacist and cashier through User Management, and two
    patients through the admission form signed in as its own doctor.
  * The browser pass, one role at a time (facility 1's nurse, facility 2's
    nurse, administrator, pharmacist and cashier, and the group-wide
    administrator): every GET route the application serves is called with the
    fence opened, once with the role's own facility's ids in the path and
    query and once with the other facility's, and no answer may name a
    patient of the other facility (strict; the group-wide administrator must
    see both). The same pass runs again after a boot with ORB_MULTI_SITE off:
    isolation is keyed on data (ruling M2), so turning the switch off stops
    growth and nothing else. Test-only plants give the pharmacist's queue and
    Sage's proactive insights a row on each side.
  * One disclosure is known: the rehearsal on a copy of the dev database
    found GET /api/sage/insights naming the other facility's patients
    (multisite-10, left open in wave 12). It is tolerated in the pass only
    while the shipped fence holds /api/sage shut (pinned: 503 for every
    account), and its own test is an expected failure until it is fixed.
  * The go-live criterion itself (FACILITY_BLIND_SURFACES empty, so the leak
    sweep is strict everywhere and the gate opens as the code ships) is pinned
    as a strict xfail: it fails the day the fence is emptied, so whoever
    empties it removes the mark and the drill runs for real.

Test patients and staff carry invented names (never a word a clinician types).
Run with the test env, never the shipping .env.
"""

from __future__ import annotations

import io
import json
import re
import stat
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import config
import facility_fence as F
import main
import pytest
from auth import create_token, hash_password
from database import db_session
from fastapi.testclient import TestClient

from scripts import build_audit_facility_index as INDEX
from scripts import grant_enterprise_access as GRANT
from scripts import seed_second_facility as S
from services import audit_facility as AF
from services import facility_service as FS

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
SAT = f"DR{TAG[:4]}"

F1_DEPT = f"Velquorin Medicine {TAG}"
F2_DEPT = f"Ostravel Medicine {TAG}"
F1_WARD = f"Velquorin Ward {TAG}"
F2_WARD = f"Ostravel Ward {TAG}"

GSTIN_MAIN = "27AAPFU0939F1ZV"  # PAN AAPFU0939F, Maharashtra
GSTIN_SAT = "29AAPFU0939F1ZR"  # the same PAN, Karnataka

ADMIN = f"GADM{TAG}"
F1_DOCTOR = f"F1D{TAG}"
F1_NURSE = f"F1N{TAG}"
F2_DOCTOR = f"F2D{TAG}"
F2_NURSE = f"F2N{TAG}"
F2_ADMIN = f"F2A{TAG}"
F2_PHARMACIST = f"F2P{TAG}"
F2_CASHIER = f"F2C{TAG}"

#: Passwords of the accounts made before the drill (bcrypt-hashed below).
_PASSWORD = {uid: uuid.uuid4().hex + "Aa1!" for uid in (ADMIN, F1_DOCTOR, F1_NURSE)}

#: One entry per patient: the words only that patient's record holds.
F1_PATIENTS = (
    {
        "name": "Quessaly Vorntrapp",
        "mrn": f"DRMA{TAG}",
        "diagnosis": "Brevantine fever",
        "allergen": "Thalvorate",
        "drug": "Plinquorex",
        "complaint": "Zelthran cough",
        "bed": "VQ-01",
        "queued": "Harvenzolam",
    },
    {
        "name": "Iddrena Holcombrey",
        "mrn": f"DRMB{TAG}",
        "diagnosis": "Skorvellic ulcer",
        "allergen": "Mennastrine",
        "drug": "Quovadrel",
        "complaint": "Prenwick swelling",
        "bed": "VQ-02",
    },
)
F2_PATIENTS = (
    {
        "name": "Marrowen Tsevlikar",
        "mrn": f"DRMC{TAG}",
        "diagnosis": "Corvalent rash",
        "allergen": "Pexirane",
        "drug": "Dravomycin",
        "complaint": "Murrowind ache",
        "bed": "OS-01",
        "queued": "Kestrolavine",
    },
    {
        "name": "Yuvanta Ploskirev",
        "mrn": f"DRMD{TAG}",
        "diagnosis": "Thessaloid cyst",
        "allergen": "Gorvandine",
        "drug": "Felquinast",
        "complaint": "Ombrevit dizziness",
        "bed": "OS-02",
    },
)

#: The browser pass: (label, account, the facility it works at, or None for
#: group-wide).
ROLES = (
    ("facility 1 nurse", F1_NURSE, 1),
    ("facility 2 nurse", F2_NURSE, 2),
    ("facility 2 administrator", F2_ADMIN, 2),
    ("facility 2 pharmacist", F2_PHARMACIST, 2),
    ("facility 2 cashier", F2_CASHIER, 2),
    ("group-wide administrator", ADMIN, None),
)
BOUND_ROLES = tuple(r for r in ROLES if r[2] is not None)

ROLE_OF = {
    ADMIN: "admin",
    F1_NURSE: "nurse",
    F2_NURSE: "nurse",
    F2_ADMIN: "admin",
    F2_PHARMACIST: "pharmacist",
    F2_CASHIER: "cashier",
}


def _hdr(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _plan_raw() -> dict:
    """Facility 2 as the drill's plan file gives it."""
    return {
        "facility": {
            "code": SAT,
            "name": "Ostravel Hospital",
            "legal_name": "Velquorin Health Private Limited",
            "gstin": GSTIN_SAT,
            "address_line1": "4 Irwin Road",
            "city": "Mysuru",
            "state": "Karnataka",
            "pincode": "570001",
        },
        "departments": [F2_DEPT],
        "staff": [
            {"id": F2_DOCTOR, "name": "Brennick Taluvar", "role": "doctor", "department": F2_DEPT},
            {"id": F2_NURSE, "name": "Ysmira Kelloway", "role": "nurse", "department": F2_DEPT},
            {"id": F2_ADMIN, "name": "Corvith Danmeret", "role": "admin"},
            {"id": F2_PHARMACIST, "name": "Halvessa Prunetti", "role": "pharmacist"},
            {"id": F2_CASHIER, "name": "Torvane Esquilo", "role": "cashier"},
        ],
        "patients": [
            {
                "name": p["name"],
                "age": 47 + i,
                "sex": "F" if i else "M",
                "mrn": p["mrn"],
                "department": F2_DEPT,
                "ward": F2_WARD,
                "bed": p["bed"],
                "attending": F2_DOCTOR,
                "chief_complaint": p["complaint"],
                "initial_diagnosis": p["diagnosis"],
                "allergies": [p["allergen"]],
                "medicine": {
                    "drug_name": p["drug"],
                    "dosage": "5 mg",
                    "route": "Oral",
                    "frequency": "OD",
                },
            }
            for i, p in enumerate(F2_PATIENTS)
        ],
    }


def _counts() -> dict:
    tables = (
        "facilities",
        "departments",
        "users",
        "user_facilities",
        "patients",
        "admissions",
        "medications",
    )
    with db_session() as db:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _admit_facility_one(client: TestClient) -> list[dict]:
    """Facility 1's patients, admitted the way its ward admits them."""
    doctor = _hdr(F1_DOCTOR, "doctor")
    made = []
    for i, p in enumerate(F1_PATIENTS):
        resp = client.post(
            "/api/admissions/admit",
            headers=doctor,
            json={
                "name": p["name"],
                "age": 52 + i,
                "sex": "M" if i else "F",
                "mrn": p["mrn"],
                "admission_source": "er",
                "admission_type": "inpatient",
                "department": F1_DEPT,
                "ward": F1_WARD,
                "bed": p["bed"],
                "attending_doctor_id": F1_DOCTOR,
                "chief_complaint": p["complaint"],
                "initial_diagnosis": p["diagnosis"],
                "allergies": [p["allergen"]],
            },
        )
        assert resp.status_code in (200, 201), resp.text
        pid = resp.json()["patient_id"]
        med = client.post(
            f"/api/vigil/patients/{pid}/medications",
            headers=doctor,
            json={"drug_name": p["drug"], "dosage": "5 mg", "route": "Oral", "frequency": "OD"},
        )
        assert med.status_code == 200, med.text
        made.append({**p, "id": pid, "admission_id": resp.json()["admission_id"]})
    return made


def _stub_models(mp: pytest.MonkeyPatch) -> None:
    """The model lanes answer with the context Orb assembled for them, so a
    patient a lane should never have read shows up in the answer, and no
    request waits on a model server."""
    import routes.sage_routes as sage_routes
    import routes.surgery_routes as surgery_routes

    async def echo(prompt, context="", conversation_history=None, scope="simple", **kwargs):
        info = kwargs.get("route_info")
        if info is not None:
            info.update(lane="local", provider="ollama", model="drill-echo")
        yield "NOACT|Context I was given: " + " ".join(str(context or "").split())[:8000]

    async def risk_echo(surgery_id, detail=None):
        yield "data: " + json.dumps({"context": detail}, default=str) + "\n\n"

    mp.setattr(sage_routes, "query_ollama", echo)
    mp.setattr(sage_routes, "_save_turn", lambda *a, **k: None)
    mp.setattr(surgery_routes, "generate_risk_assessment", risk_echo)


def _forget() -> None:
    FS.invalidate_cache()
    AF.invalidate_cache()
    F._audit_last.clear()


# ── the drill ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def drill(isolated_module_database, tmp_path_factory):
    from services.helix_db import init_helix_db

    init_helix_db()
    out = tmp_path_factory.mktemp("drill")
    with pytest.MonkeyPatch.context() as mp:
        # The prod suite run must not make the seed refuse its own world: the
        # drill runs on the verify stack's dev profile.
        mp.setattr(config, "DEPLOYMENT_PROFILE", "dev")
        mp.setattr(config, "MULTI_SITE_ENABLED", True)
        _stub_models(mp)
        _forget()
        client = TestClient(main.app, raise_server_exceptions=False)
        with db_session() as db:
            for uid, role, dept, name in (
                (ADMIN, "admin", None, "Ilvarra Quensted"),
                (F1_DOCTOR, "doctor", F1_DEPT, "Dovrell Ashkenrath"),
                (F1_NURSE, "nurse", F1_DEPT, "Prithvel Orrenshaw"),
            ):
                db.execute(
                    "INSERT INTO users (id, name, role, department, password_hash, is_active, "
                    "token_version) VALUES (?, ?, ?, ?, ?, 1, 0)",
                    (uid, name, role, dept, hash_password(_PASSWORD[uid])),
                )
            # The baseline accounts the suite's conftest seeds before every
            # test (department General Medicine), here before the drill as a
            # hospital's existing staff are, so the seed finds their department.
            db.executemany(
                "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, 'General Medicine', ?)",
                [
                    (uid, name, role, hash_password(uuid.uuid4().hex))
                    for uid, name, role in (
                        ("D001", "Dr. Test", "doctor"),
                        ("D002", "Dr. Test2", "doctor"),
                        ("N001", "Nurse Test", "nurse"),
                        ("DOC001", "Dr. Legacy", "doctor"),
                    )
                ],
            )
        f1 = _admit_facility_one(client)
        granted = GRANT.grant(ADMIN, "Group CIO for the two-facility go-live drill")
        patched = client.patch(
            "/api/facilities/1", headers=_hdr(ADMIN, "admin"), json={"gstin": GSTIN_MAIN}
        )
        assert patched.status_code == 200, patched.text
        index = INDEX.run(None, do_build=True, yes=True)
        _forget()

        plan = S.parse_plan(_plan_raw())
        product = S.Product(TestClient(main.app, raise_server_exceptions=False))
        token = S.sign_in(product, ADMIN, _PASSWORD[ADMIN])

        # 1. The fence as the code ships it: refused, nothing written.
        before = _counts()
        refused: list | None = None
        try:
            S.apply_plan(plan, product, admin_token=token, accounts_out=out / "refused.txt")
        except S.SeedRefused as e:
            refused = e.problems
        after_refusal = _counts()

        # 2. Every screen group cleared: the gate passes, through the product.
        with pytest.MonkeyPatch.context() as fence:
            fence.setattr(F, "FACILITY_BLIND_SURFACES", ())
            check = S.readiness(
                plan,
                admin=product.signed_in(token),
                admin_id=ADMIN,
                accounts_out=out / "accounts.txt",
            )
            report = S.apply_plan(
                plan, product, admin_token=token, accounts_out=out / "accounts.txt"
            )
        _forget()
        f2_id = int(report["facility"]["id"])
        f2 = [
            {**p, **next(r for r in report["patients"] if r["mrn"] == p["mrn"])}
            for p in F2_PATIENTS
        ]
        # A medicine read from a note awaiting the pharmacist, on one patient
        # of each facility, so the pharmacist's queue has a row on both sides
        # (test plant only: the seed itself writes through the product).
        # And one proactive Sage insight on each side, addressed to nobody in
        # particular: the realistic copy's rehearsal (docs/MULTI_SITE.md)
        # found GET /api/sage/insights naming another facility's patients.
        with db_session() as db:
            for p, doctor in ((f1[0], F1_DOCTOR), (f2[0], F2_DOCTOR)):
                db.execute(
                    "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
                    "status, prescribed_by, source) VALUES (?, ?, '5 mg', 'oral', 'OD', "
                    "'active', ?, 'llm_extracted')",
                    (p["id"], p["queued"], doctor),
                )
                db.execute(
                    "INSERT INTO sage_insights (id, patient_id, insight_type, severity, title, "
                    "detail, state) VALUES (?, ?, 'trend', 'warning', 'Heart rate rising', "
                    "'Three readings in a row above the last', 'active')",
                    (f"INS-{p['id']}", p["id"]),
                )
        try:
            yield {
                "client": client,
                "product": product,
                "token": token,
                "plan": plan,
                "out": out,
                "granted": granted,
                "index": index,
                "before": before,
                "refused": refused,
                "after_refusal": after_refusal,
                "refused_file": out / "refused.txt",
                "check": check,
                "report": report,
                "f2_id": f2_id,
                "patients": {1: f1, 2: f2},
            }
        finally:
            _forget()
    _forget()


@pytest.fixture()
def fence_open(monkeypatch):
    """The blind list as it will stand once every screen is swept."""
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture(autouse=True)
def _fresh_caches():
    _forget()
    yield
    _forget()


# ── 1. The create gate ───────────────────────────────────────────────────────


def test_with_the_fence_as_shipped_the_gate_refuses_and_the_seed_writes_nothing(drill):
    """Everything else is in place (switch, grant, GSTIN, index), so the one
    reason left is that screen groups are still blind."""
    assert F.FACILITY_BLIND_SURFACES, "the fence is empty: remove the xfail mark below"
    assert drill["refused"] is not None
    codes = {p["code"] for p in drill["refused"]}
    assert codes == {"create_gate_screens_not_ready"}, drill["refused"]
    words = " ".join(p["message"] for p in drill["refused"])
    assert str(len(F.FACILITY_BLIND_SURFACES)) in words
    assert drill["after_refusal"] == drill["before"]
    assert not drill["refused_file"].exists()


def test_the_preconditions_were_made_through_the_console_tools_and_the_product(drill):
    assert drill["granted"]["user_id"] == ADMIN
    assert drill["index"]["built"] is True and drill["index"]["index_exists"] is True
    with db_session() as db:
        assert db.execute("SELECT gstin FROM facilities WHERE id = 1").fetchone()[0] == GSTIN_MAIN
        grant = db.execute(
            "SELECT granted_by FROM enterprise_access_grants WHERE user_id = ? "
            "AND revoked_at IS NULL",
            (ADMIN,),
        ).fetchone()
        assert grant["granted_by"] == "console"


def test_with_every_group_cleared_the_gate_passes_through_the_product(drill):
    assert drill["check"].ready, drill["check"].problems
    assert drill["check"].gate == []
    report = drill["report"]
    assert report["facility"]["code"] == SAT
    assert report["checks"] == []
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM facilities WHERE code = ? COLLATE NOCASE", (SAT,)
        ).fetchone()
        assert row is not None and row["id"] == drill["f2_id"]
        assert row["name"] == "Ostravel Hospital" and row["gstin"] == GSTIN_SAT
        assert row["created_by"] == ADMIN and row["audit_epoch_id"] is not None
        made = db.execute(
            "SELECT actor_user_id, user_agent FROM audit_logs "
            "WHERE action_type = 'facility_created' AND target_id = ?",
            (str(drill["f2_id"]),),
        ).fetchall()
    assert len(made) == 1
    assert made[0]["actor_user_id"] == ADMIN
    # The rows that record the request's user agent name the script.
    with db_session() as db:
        agents = {
            r[0]
            for r in db.execute(
                "SELECT user_agent FROM audit_logs WHERE action_type = 'user_created' "
                "AND target_id IN (?, ?, ?, ?, ?)",
                (F2_DOCTOR, F2_NURSE, F2_ADMIN, F2_PHARMACIST, F2_CASHIER),
            ).fetchall()
        }
    assert agents == {S.USER_AGENT}
    FS.invalidate_cache()
    assert FS.multi_facility_data() is True


def test_facility_one_departments_were_listed_before_the_second_facility(drill):
    assert F1_DEPT in drill["report"]["departments_listed_at_facility_1"]
    with db_session() as db:
        assert (
            db.execute("SELECT facility_id FROM departments WHERE name = ?", (F1_DEPT,)).fetchone()[
                0
            ]
            == 1
        )
        assert (
            db.execute("SELECT facility_id FROM departments WHERE name = ?", (F2_DEPT,)).fetchone()[
                0
            ]
            == drill["f2_id"]
        )
        listed = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'department_created' "
            "AND user_agent = ?",
            (S.USER_AGENT,),
        ).fetchone()[0]
    assert listed == len(drill["report"]["departments_listed_at_facility_1"]) + 1


def test_the_seeded_staff_work_at_the_new_facility(drill):
    f2 = drill["f2_id"]
    with db_session() as db:
        for uid in (F2_ADMIN, F2_PHARMACIST, F2_CASHIER):
            home = db.execute(
                "SELECT facility_id, granted_by FROM user_facilities WHERE user_id = ? "
                "AND kind = 'home' AND revoked_at IS NULL",
                (uid,),
            ).fetchone()
            assert home is not None and home["facility_id"] == f2, uid
            assert home["granted_by"] == ADMIN
        for uid in (F2_DOCTOR, F2_NURSE):
            assert (
                db.execute("SELECT department FROM users WHERE id = ?", (uid,)).fetchone()[0]
                == F2_DEPT
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM user_facilities WHERE user_id = ?", (uid,)
                ).fetchone()[0]
                == 0
            )
        assigned = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'user_facility_assigned' "
            "AND user_agent = ?",
            (S.USER_AGENT,),
        ).fetchone()[0]
        assert assigned == 3
        for uid in (F2_DOCTOR, F2_NURSE, F2_ADMIN, F2_PHARMACIST, F2_CASHIER):
            user = dict(
                db.execute("SELECT id, role, department FROM users WHERE id = ?", (uid,)).fetchone()
            )
            assert FS.facility_scope_for(user, db=db) == frozenset({f2}), uid
        for uid in (F1_DOCTOR, F1_NURSE):
            user = dict(
                db.execute("SELECT id, role, department FROM users WHERE id = ?", (uid,)).fetchone()
            )
            assert FS.facility_scope_for(user, db=db) == frozenset({1}), uid
        admin = dict(
            db.execute("SELECT id, role, department FROM users WHERE id = ?", (ADMIN,)).fetchone()
        )
        assert FS.facility_scope_for(admin, db=db) is None


def test_the_seeded_patients_are_admitted_at_the_new_facility_with_no_observation(drill):
    import permissions as P

    f2 = drill["f2_id"]
    seeded = drill["patients"][2]
    assert len(seeded) == 2 and drill["report"]["medicines"] == 2
    with db_session() as db:
        for p in seeded:
            adm = db.execute(
                "SELECT facility_id, department, ward, admitted_by FROM admissions "
                "WHERE patient_id = ?",
                (p["id"],),
            ).fetchall()
            assert [(a["facility_id"], a["department"], a["ward"]) for a in adm] == [
                (f2, F2_DEPT, F2_WARD)
            ]
            assert adm[0]["admitted_by"] == F2_DOCTOR
            row = db.execute(
                f"SELECT registered_facility_id, {P.FACILITY_PATIENT_SQL} AS fid "
                "FROM patients p WHERE p.id = ?",
                (p["id"],),
            ).fetchone()
            assert row["registered_facility_id"] == f2 and row["fid"] == f2
            meds = db.execute(
                "SELECT drug_name, prescribed_by FROM medications WHERE patient_id = ? "
                "AND source != 'llm_extracted'",
                (p["id"],),
            ).fetchall()
            assert [(m["drug_name"], m["prescribed_by"]) for m in meds] == [(p["drug"], F2_DOCTOR)]
            allergies = db.execute(
                "SELECT COUNT(*) FROM patient_allergies WHERE patient_id = ? "
                "AND LOWER(substance) = LOWER(?)",
                (p["id"], p["allergen"]),
            ).fetchone()[0]
            assert allergies == 1
            # Honest wording: a script never files an observation as charted.
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM vitals WHERE patient_id = ?", (p["id"],)
                ).fetchone()[0]
                == 0
            )
        report = FS.facility_report(db)
        unlisted = S.unlisted_departments_on(db)
        # The doctor the seed admitted them as signed in through the product.
        agents = {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT user_agent FROM audit_logs WHERE action_type = 'login_success' "
                "AND actor_user_id = ?",
                (F2_DOCTOR,),
            ).fetchall()
        }
    assert report["patients_at_several_facilities"] == 0
    assert unlisted == []
    assert agents == {S.USER_AGENT}


def test_every_account_of_the_pass_is_shown_at_its_own_facility(drill, fence_open):
    """What the shell's role line shows (GET /api/auth/me, UserOut.facility)."""
    client = drill["client"]
    for uid, want in (
        (F1_NURSE, (1, "MAIN")),
        (F2_NURSE, (drill["f2_id"], SAT)),
        (F2_DOCTOR, (drill["f2_id"], SAT)),
        (F2_ADMIN, (drill["f2_id"], SAT)),
        (F2_PHARMACIST, (drill["f2_id"], SAT)),
        (F2_CASHIER, (drill["f2_id"], SAT)),
    ):
        role = ROLE_OF.get(uid, "doctor")
        me = client.get("/api/auth/me", headers=_hdr(uid, role))
        assert me.status_code == 200, me.text
        facility = me.json()["facility"]
        assert (facility["id"], facility["code"]) == want, uid
    stock = client.get("/api/helix/inventory", headers=_hdr(F2_PHARMACIST, "pharmacist"))
    assert stock.status_code == 200, stock.text
    assert stock.json()["facility"]["code"] == SAT


def test_the_accounts_file_holds_working_passwords_and_nothing_else_shows_them(drill):
    path = drill["out"] / "accounts.txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    rows = [ln.split("\t") for ln in lines]
    assert [r[0] for r in rows] == [F2_DOCTOR, F2_NURSE, F2_ADMIN, F2_PHARMACIST, F2_CASHIER]
    shown = json.dumps(drill["report"], default=str)
    for uid, _role, password in rows:
        assert len(password) >= 20 and password not in shown
        token = S.sign_in(drill["product"], uid, password)
        assert token
    # The file is never written over.
    again = S.readiness(drill["plan"], admin_id=ADMIN, accounts_out=path)
    assert "accounts_file_exists" in {p["code"] for p in again.problems}


def test_a_second_run_on_the_same_copy_is_refused_and_writes_nothing(drill):
    before = _counts()
    with pytest.MonkeyPatch.context() as fence:
        fence.setattr(F, "FACILITY_BLIND_SURFACES", ())
        with pytest.raises(S.SeedRefused) as refused:
            S.apply_plan(
                drill["plan"],
                drill["product"],
                admin_token=drill["token"],
                accounts_out=drill["out"] / "second.txt",
            )
    codes = {p["code"] for p in refused.value.problems}
    assert {
        "facility_exists",
        "department_taken",
        "ward_taken",
        "account_exists",
        "mrn_in_use",
    } <= codes
    assert _counts() == before
    assert not (drill["out"] / "second.txt").exists()


# ── 2. The browser pass, one role at a time ──────────────────────────────────


def _get_routes() -> list:
    out = []
    for r in main.app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if "GET" not in methods or path.startswith("/socket.io"):
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.path)


GET_ROUTES = _get_routes()

SEARCH_PARAMS = {"q", "query", "search", "term", "text", "name"}
DAY_PARAMS = {"day", "date", "visit_date", "on", "scheduled_date"}


def _required(field) -> bool:
    req = getattr(field, "required", None)
    if isinstance(req, bool):
        return req
    info = getattr(field, "field_info", None)
    return bool(info is not None and info.is_required())


def _values(drill, facility_id: int) -> dict:
    """Path and query values naming one facility's first patient."""
    fid = drill["f2_id"] if facility_id == 2 else 1
    patient = drill["patients"][facility_id][0]
    with db_session() as db:
        med = db.execute(
            "SELECT id FROM medications WHERE patient_id = ? ORDER BY id LIMIT 1", (patient["id"],)
        ).fetchone()
        channel = db.execute(
            "SELECT id FROM channels WHERE patient_id = ? ORDER BY id LIMIT 1", (patient["id"],)
        ).fetchone()
    return {
        "patient_id": patient["id"],
        "pid": patient["id"],
        "admission_id": patient["admission_id"],
        "mrn": patient["mrn"],
        "uhid": patient["mrn"],
        "med_id": str(med["id"]) if med else "1",
        "medication_id": str(med["id"]) if med else "1",
        "channel_id": channel["id"] if channel else "1",
        "ward": F2_WARD if facility_id == 2 else F1_WARD,
        "facility_id": str(fid),
        "user_id": F2_DOCTOR if facility_id == 2 else F1_DOCTOR,
        "filename": "nothing.png",
        "search": patient["mrn"],
    }


def _url(route, values: dict) -> str:
    def fill(m):
        name = m.group(1).split(":")[0]
        return str(values.get(name, "1"))

    path = re.sub(r"\{([^}]+)\}", fill, route.path)
    query = {}
    dependant = getattr(route, "dependant", None)
    for field in getattr(dependant, "query_params", None) or []:
        name = getattr(field, "alias", None) or field.name
        if name in ("patient_id", "pid"):
            query[name] = values["patient_id"]
        elif name in ("uhid", "mrn") or name in SEARCH_PARAMS:
            query[name] = values["search"]
        elif name == "ward":
            query[name] = values["ward"]
        elif name in DAY_PARAMS:
            query[name] = date.today().isoformat()
        elif _required(field):
            query[name] = "1"
    return path + ("?" + urlencode(query) if query else "")


def _words(patients: list[dict]) -> tuple[str, ...]:
    """What only these patients' records hold."""
    out: list[str] = []
    for p in patients:
        out += [p["name"], *p["name"].split(), p["mrn"], p["id"], str(p["admission_id"])]
        out += [p["diagnosis"].split()[0], p["allergen"], p["drug"], p["complaint"].split()[0]]
        if p.get("queued"):
            out.append(p["queued"])
    return tuple(out)


def _hits(text: str, sent: str, words: tuple[str, ...]) -> list[str]:
    low, sent_low = text.lower(), sent.lower()
    return sorted({w for w in words if w.lower() in low and w.lower() not in sent_low})


def _pass(client: TestClient, headers: dict, values: dict, words: dict) -> dict:
    """{facility: {url: [its words found]}} over every GET route, one request
    per route."""
    found: dict[int, dict] = {fid: {} for fid in words}
    for route in GET_ROUTES:
        url = _url(route, values)
        resp = client.get(url, headers=headers)
        text = resp.content.decode("utf-8", "ignore")
        for fid, facility_words in words.items():
            hits = _hits(text, url, facility_words)
            if hits:
                found[fid][url] = hits
    return found


#: The fence as the code ships it (the tests open it by patching the module).
SHIPPED_BLIND = F.FACILITY_BLIND_SURFACES

#: Routes known to show another facility's patients with the fence opened,
#: each with its owner. One is tolerated only while the shipped fence still
#: holds its route group shut (503 for everyone once two facilities exist);
#: the day its group is cleared, a disclosure there fails the pass.
KNOWN_BLIND_DISCLOSURES = {
    "/api/sage/insights": (
        "multisite-10 (W12-B deferred): sage_proactive_service.get_active_insights reads "
        "every facility's insights at HEAD (an administrator sees all, anyone else every "
        "insight addressed to nobody)"
    ),
}


def _unexpected(leaks: dict) -> dict:
    """The leaks the go-live drill cannot accept today: any on a route the
    shipped fence lets through, and any not listed in KNOWN_BLIND_DISCLOSURES."""
    out = {}
    for url, hits in leaks.items():
        path = url.split("?", 1)[0]
        held_shut = F.is_fenced(path, blind=SHIPPED_BLIND) if SHIPPED_BLIND else False
        if path in KNOWN_BLIND_DISCLOSURES and held_shut:
            continue
        out[url] = hits
    return out


def _role_pass(drill, uid: str, facility: int | None) -> tuple[dict, dict, dict]:
    """(facility 1 words seen, facility 2 words seen, leaks) for one account:
    every GET route with its own facility's ids, then with the other's."""
    client = drill["client"]
    headers = _hdr(uid, ROLE_OF[uid])
    words = {1: _words(drill["patients"][1]), 2: _words(drill["patients"][2])}
    seen: dict[int, dict] = {1: {}, 2: {}}
    leaks: dict[str, list] = {}
    for target in (1, 2):
        found = _pass(client, headers, _values(drill, target), words)
        for fid in (1, 2):
            seen[fid].update(found[fid])
            if facility is not None and fid != facility:
                leaks.update(found[fid])
    return seen[1], seen[2], leaks


@pytest.mark.parametrize("label, uid, facility", ROLES, ids=[r[0] for r in ROLES])
def test_the_browser_pass_finds_no_other_facility_patient(
    drill, label, uid, facility, fence_open, monkeypatch
):
    _stub_models(monkeypatch)
    seen1, seen2, leaks = _role_pass(drill, uid, facility)
    unexpected = _unexpected(leaks)
    assert unexpected == {}, f"{label} was shown the other facility's patients: {unexpected}"
    if facility is None:
        assert seen1 and seen2, f"{label} sees facility 1: {bool(seen1)}, 2: {bool(seen2)}"
    else:
        own = seen1 if facility == 1 else seen2
        assert own, f"{label} sees none of its own facility's patients on any screen"


# ── 3. A boot with the switch off keeps isolation ────────────────────────────


@pytest.fixture()
def switched_off(drill, monkeypatch):
    """Orb restarted with ORB_MULTI_SITE off and two facilities on record:
    the boot's facility steps (main.lifespan: init_db, assert_facility_one,
    prime_cache)."""
    from database import init_db

    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", False)
    init_db()
    FS.assert_facility_one()
    assert FS.prime_cache() is True
    return drill


def test_after_a_switched_off_boot_the_health_probe_says_two_facilities(switched_off):
    resp = switched_off["client"].get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["platform"] == {"multi_site": False, "multi_facility_data": True}


def test_after_a_switched_off_boot_every_account_keeps_its_facility(switched_off):
    f2 = switched_off["f2_id"]
    with db_session() as db:
        for uid, want in (
            (F1_NURSE, frozenset({1})),
            (F1_DOCTOR, frozenset({1})),
            (F2_NURSE, frozenset({f2})),
            (F2_DOCTOR, frozenset({f2})),
            (F2_ADMIN, frozenset({f2})),
            (F2_PHARMACIST, frozenset({f2})),
            (F2_CASHIER, frozenset({f2})),
            (ADMIN, None),
        ):
            user = dict(
                db.execute("SELECT id, role, department FROM users WHERE id = ?", (uid,)).fetchone()
            )
            assert FS.facility_scope_for(user, db=db) == want, uid


def test_after_a_switched_off_boot_the_fence_still_holds_blind_screens_shut(switched_off):
    client = switched_off["client"]
    resp = client.get("/api/vigil/patients", headers=_hdr(F2_NURSE, "nurse"))
    assert resp.status_code == 503 and resp.json()["error"] == "facility_fence"
    sock = TestClient(main.socket_app, base_url="http://localhost")
    polled = sock.get("/socket.io/?EIO=4&transport=polling")
    assert polled.status_code == 503 and polled.json()["error"] == "facility_fence"


def test_after_a_switched_off_boot_a_chart_of_the_other_facility_is_not_found(
    switched_off, fence_open
):
    client = switched_off["client"]
    p1 = switched_off["patients"][1][0]["id"]
    p2 = switched_off["patients"][2][0]["id"]
    assert client.get(f"/api/vigil/patients/{p1}", headers=_hdr(F2_NURSE, "nurse")).status_code == (
        404
    )
    assert client.get(f"/api/vigil/patients/{p2}", headers=_hdr(F1_NURSE, "nurse")).status_code == (
        404
    )
    own = client.get(f"/api/vigil/patients/{p2}", headers=_hdr(F2_NURSE, "nurse"))
    assert own.status_code == 200, own.text
    assert switched_off["patients"][2][0]["diagnosis"] in own.text


def test_after_a_switched_off_boot_growth_is_refused(switched_off, fence_open):
    client = switched_off["client"]
    admin = _hdr(ADMIN, "admin")
    check = client.get("/api/facilities/create-check", headers=admin)
    assert check.status_code == 404 and check.json()["detail"]["error"] == "feature_disabled"
    made = client.post("/api/facilities", headers=admin, json={"code": "DRX", "name": "Oshkivar"})
    assert made.status_code == 404
    with pytest.raises(GRANT.GrantRefused):
        GRANT.grant(F2_ADMIN, "A second group-wide administrator")
    codes = {p["code"] for p in S.environment_problems()}
    assert "switch_off" in codes
    assert _counts()["facilities"] == 2


@pytest.mark.parametrize("label, uid, facility", BOUND_ROLES, ids=[r[0] for r in BOUND_ROLES])
def test_after_a_switched_off_boot_the_browser_pass_still_finds_no_other_facility_patient(
    switched_off, label, uid, facility, fence_open, monkeypatch
):
    _stub_models(monkeypatch)
    _, _, leaks = _role_pass(switched_off, uid, facility)
    unexpected = _unexpected(leaks)
    assert unexpected == {}, f"{label} was shown the other facility's patients: {unexpected}"


# ── 4. What the realistic copy found ─────────────────────────────────────────


def test_every_known_disclosure_is_on_a_route_the_shipped_fence_holds_shut(drill):
    """With two facilities on record the fence answers 503 there, to every
    account, before the handler runs: nothing leaks in the product today."""
    client = drill["client"]
    for path in KNOWN_BLIND_DISCLOSURES:
        assert F.is_fenced(path), path
        for uid, _facility in ((F2_ADMIN, 2), (F2_NURSE, 2), (F1_NURSE, 1)):
            resp = client.get(path, headers=_hdr(uid, ROLE_OF[uid]))
            assert resp.status_code == 503 and resp.json()["error"] == "facility_fence", (
                path,
                uid,
            )


@pytest.mark.xfail(
    strict=False,
    reason=KNOWN_BLIND_DISCLOSURES["/api/sage/insights"],
)
def test_the_sage_insight_list_keeps_to_the_readers_facility(drill, fence_open):
    resp = drill["client"].get("/api/sage/insights", headers=_hdr(F2_ADMIN, "admin"))
    assert resp.status_code == 200, resp.text
    named = {i.get("patient_id") for i in resp.json()["insights"]}
    assert drill["patients"][2][0]["id"] in named
    assert drill["patients"][1][0]["id"] not in named


# ── 5. The go-live criterion ─────────────────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Go-live criterion of multisite-18: FACILITY_BLIND_SURFACES must be empty (every "
        "sweep cleared and the fence regenerated from tests/facility_sql_sites), so the "
        "leak sweep is strict everywhere and the create gate opens as the code ships. "
        "Waves 12 and 13 left the regeneration as its own reviewed step. When this "
        "passes, remove this mark: the drill above then runs with the real fence."
    ),
)
def test_go_live_every_screen_group_is_cleared_and_the_gate_opens_as_shipped(drill):
    assert F.FACILITY_BLIND_SURFACES == ()
    assert [r.path for r in GET_ROUTES if F.is_fenced(r.path)] == []
    status, data = (
        drill["product"].signed_in(drill["token"]).call("GET", "/api/facilities/create-check")
    )
    assert status == 200 and data["allowed"] is True, data


# ── 6. The plan and the copy ─────────────────────────────────────────────────


def _codes(raw: Any) -> set:
    with pytest.raises(S.SeedRefused) as refused:
        S.parse_plan(raw)
    return {p["code"] for p in refused.value.problems}


def test_a_plan_needs_every_role_of_the_drill():
    raw = _plan_raw()
    raw["staff"] = [m for m in raw["staff"] if m["role"] != "cashier"]
    assert _codes(raw) == {"plan_incomplete"}


def test_a_plan_keeps_departments_to_ward_roles():
    raw = _plan_raw()
    raw["staff"][0].pop("department")
    raw["staff"][2]["department"] = F2_DEPT
    raw["staff"][3]["role"] = "receptionist"
    assert _codes(raw) == {"plan_incomplete", "plan_invalid"}


def test_a_plan_name_may_not_label_itself_or_hold_a_placeholder():
    raw = _plan_raw()
    raw["facility"]["name"] = "Ostravel Demo Hospital"
    raw["patients"][0]["name"] = "Test Kelvorrin"
    raw["departments"] = [f"{F2_DEPT} sandbox"]
    raw["staff"][0]["department"] = raw["departments"][0]
    raw["staff"][1]["department"] = raw["departments"][0]
    for p in raw["patients"]:
        p["department"] = raw["departments"][0]
    raw["facility"]["gstin"] = "<the satellite's GSTIN>"
    assert _codes(raw) == {"plan_label_word", "plan_unfilled"}
    assert S.label_words("Ward 3 (Mock)") == ["mock"]
    assert S.label_words("Testimony Ward") == []


def test_a_plan_names_known_departments_doctors_and_beds_once():
    raw = _plan_raw()
    raw["patients"][0]["department"] = "Ombrevane Surgery"
    raw["patients"][1]["attending"] = F2_NURSE
    raw["patients"][1]["bed"] = raw["patients"][0]["bed"]
    raw["patients"][1]["mrn"] = raw["patients"][0]["mrn"]
    raw["facility"]["colour"] = "teal"
    raw["staff"][1]["id"] = raw["staff"][0]["id"]
    assert _codes(raw) == {"plan_invalid"}
    assert _codes([]) == {"plan_invalid"}
    assert _codes({}) == {"plan_incomplete"}


def test_the_example_plan_is_honest_and_only_needs_the_facility_filled_in():
    raw = S.example_plan()
    assert _codes(raw) == {"plan_unfilled"}
    raw["facility"] = {"code": "SAT1", "name": "Ostravel Hospital", "gstin": GSTIN_SAT}
    plan = S.parse_plan(raw)
    assert {m.role for m in plan.staff} == set(S.DRILL_ROLES)
    ordinary = {"patient", "bed", "ward", "live", "new", "admission", "doctor", "nurse", "the"}
    names = [m.name for m in plan.staff] + [p.name for p in plan.patients]
    for name in names:
        assert not S.label_words(name), name
        assert not ordinary & set(name.casefold().split()), name


def test_the_copy_is_refused_on_the_prod_profile_or_at_the_default_location(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "prod")
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    assert {p["code"] for p in S.environment_problems()} == {"prod_profile"}
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(config, "DB_PATH", S.DEFAULT_DB)
    assert {p["code"] for p in S.environment_problems()} == {"default_database"}


def test_the_seed_refuses_while_doctors_must_enrol_a_second_factor(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    monkeypatch.setattr(config, "MFA_REQUIRED_ROLES", ["admin", "doctor"])
    assert {p["code"] for p in S.environment_problems()} == {"doctor_mfa_required"}


def test_the_check_on_the_command_line_reports_and_writes_nothing(
    drill, tmp_path, monkeypatch, capsys
):
    import database

    monkeypatch.setenv("ORB_DB_PATH", str(database.DB_PATH))
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_plan_raw()))
    before = _counts()
    code = S.main(["--db", str(database.DB_PATH), "--plan", str(plan), "--admin", ADMIN, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ready"] is False
    assert {"facility_exists", "account_exists"} <= {p["code"] for p in out["problems"]}
    assert _counts() == before


class _Console(io.StringIO):
    """Standard input as a console gives it, so --apply goes on to sign in."""

    def isatty(self) -> bool:
        return True


def _trail() -> tuple:
    """What a sign-in writes: the audit trail, and the accounts (a refused
    password counts toward the account's lockout, a right one clears it)."""
    with db_session() as db:
        audit = tuple(db.execute("SELECT COUNT(*), MAX(id) FROM audit_logs").fetchone())
        users = [tuple(r) for r in db.execute("SELECT * FROM users ORDER BY id").fetchall()]
    return audit, users


def _apply_at_the_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, password: str
) -> tuple[int, list[str]]:
    """main() with --apply as the operator runs it at the console, typing
    `password` at every prompt. The exit code and the prompts asked."""
    import database

    monkeypatch.setenv("ORB_DB_PATH", str(database.DB_PATH))
    monkeypatch.setattr(sys, "stdin", _Console(""))
    asked: list[str] = []

    def typed(prompt: str = "") -> str:
        asked.append(prompt)
        return password

    monkeypatch.setattr(S.getpass, "getpass", typed)
    monkeypatch.setattr("builtins.input", typed)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_plan_raw()))
    code = S.main(
        [
            "--db",
            str(database.DB_PATH),
            "--plan",
            str(plan),
            "--admin",
            ADMIN,
            "--accounts-out",
            str(tmp_path / "accounts.txt"),
            "--apply",
        ]
    )
    return code, asked


@pytest.mark.parametrize(
    "refusal, password",
    [
        ("prod profile", "wrong"),
        ("prod profile", "right"),
        ("default location", "wrong"),
        ("default location", "right"),
        ("plan and gate", "right"),
    ],
)
def test_apply_on_a_copy_it_refuses_never_signs_in_and_writes_nothing(
    drill, refusal, password, tmp_path, monkeypatch, capsys
):
    """A sign-in writes to the database it signs in to (an audit row, and a
    refused password counts toward the account's lockout, which on a
    hospital's appliance would be its administrator's). So every check that
    needs no session runs first, and a refused copy is never signed in to."""
    import database

    monkeypatch.setattr(
        config, "DEPLOYMENT_PROFILE", "prod" if refusal == "prod profile" else "dev"
    )
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    if refusal == "default location":
        monkeypatch.setattr(S, "DEFAULT_DB", Path(database.DB_PATH))
    typed = _PASSWORD[ADMIN] if password == "right" else uuid.uuid4().hex
    trail, counts = _trail(), _counts()
    code, asked = _apply_at_the_console(monkeypatch, tmp_path, typed)
    err = capsys.readouterr().err
    assert code == 1
    assert asked == [], "the password was asked for, so the copy was signed in to"
    assert _trail() == trail
    assert _counts() == counts
    assert not (tmp_path / "accounts.txt").exists()
    assert err.startswith("Refused. Nothing was written:"), err
    reason = {
        "prod profile": "This is the prod profile",
        "default location": "is the database Orb opens by default",
        "plan and gate": "The create gate refuses",
    }[refusal]
    assert reason in err, err
    if refusal != "plan and gate":
        # Not a verification copy: the database is not even read.
        assert "The create gate refuses" not in err and "already" not in err, err


@pytest.mark.parametrize("password", ["wrong", "right"])
def test_apply_stopped_after_signing_in_says_the_sign_in_is_on_record(
    drill, password, tmp_path, monkeypatch, capsys
):
    """Should the checks pass before signing in and the product refuse after
    (the copy changed in between, say), the operator is told that the
    sign-in is on the audit trail, never that nothing was written."""
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    real = S.readiness

    def passes_before_signing_in(plan: Any, *, admin: Any = None, **kwargs: Any) -> Any:
        if admin is None:
            return S.Readiness(problems=[], main_departments=[], gate=[], facility_count=2)
        return real(plan, admin=admin, **kwargs)

    monkeypatch.setattr(S, "readiness", passes_before_signing_in)
    typed = _PASSWORD[ADMIN] if password == "right" else uuid.uuid4().hex
    ((_, last), _users), counts = _trail(), _counts()
    code, asked = _apply_at_the_console(monkeypatch, tmp_path, typed)
    err = capsys.readouterr().err
    assert code == 1
    assert asked == [f"Password for {ADMIN}: "]
    assert "Nothing was written" not in err, err
    assert "audit trail" in err, err
    assert _counts() == counts
    assert not (tmp_path / "accounts.txt").exists()
    with db_session() as db:
        rows = db.execute(
            "SELECT action_type, user_agent FROM audit_logs WHERE id > ? ORDER BY id",
            (last or 0,),
        ).fetchall()
    actions = [r["action_type"] for r in rows]
    assert ("login_success" if password == "right" else "login_failure") in actions, actions
    assert {r["user_agent"] for r in rows} == {S.USER_AGENT}
    if password == "right":
        assert err.startswith("Refused after signing in."), err
        assert "facility_exists" not in err and "already" in err, err
    else:
        assert err.startswith("Sign-in refused (401)"), err


def test_a_copy_that_is_not_one_is_never_opened(tmp_path, monkeypatch, capsys):
    """The prod profile, or the database at Orb's default location: the check
    reports it without opening the database, even to read."""
    import database

    def opened(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the database was opened")

    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "prod")
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    path = Path(database.DB_PATH)
    monkeypatch.setattr(database, "db_session", opened)
    plan = S.parse_plan(_plan_raw())
    assert [p["code"] for p in S.readiness(plan, admin_id=ADMIN).problems] == ["prod_profile"]

    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(S, "DEFAULT_DB", path)
    assert [p["code"] for p in S.readiness(plan, admin_id=ADMIN).problems] == ["default_database"]

    raw = tmp_path / "plan.json"
    raw.write_text(json.dumps(_plan_raw()))
    monkeypatch.setenv("ORB_DB_PATH", str(path))
    assert S.main(["--db", str(path), "--plan", str(raw), "--admin", ADMIN]) == 1
    printed = capsys.readouterr()
    assert "Facilities on record" not in printed.out
    assert "is the database Orb opens by default" in printed.err


def test_a_hard_link_to_the_default_database_is_the_default_database(tmp_path, monkeypatch):
    """A second name for the same file is not a copy of it."""
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    default = tmp_path / "orb.db"
    default.write_bytes(b"")
    linked = tmp_path / "copy.db"
    linked.hardlink_to(default)
    monkeypatch.setattr(S, "DEFAULT_DB", default)
    monkeypatch.setattr(config, "DB_PATH", linked)
    assert {p["code"] for p in S.environment_problems()} == {"default_database"}
    copied = tmp_path / "real-copy.db"
    copied.write_bytes(default.read_bytes())
    monkeypatch.setattr(config, "DB_PATH", copied)
    assert S.environment_problems() == []


def test_the_command_line_refuses_what_it_cannot_read(tmp_path, monkeypatch, capsys):
    assert S.main(["--example-plan"]) == 0
    assert json.loads(capsys.readouterr().out)["facility"]["code"] == "SAT1"
    assert S.main(["--db", str(tmp_path / "absent.db"), "--plan", str(tmp_path / "p.json")]) == 2
    copy = tmp_path / "copy.db"
    copy.write_bytes(b"")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("ORB_DB_PATH", str(copy))
    assert S.main(["--db", str(copy), "--plan", str(bad)]) == 2
    unusable = tmp_path / "unusable.json"
    unusable.write_text(json.dumps({"facility": {}}))
    assert S.main(["--db", str(copy), "--plan", str(unusable)]) == 2
    with pytest.raises(SystemExit):
        S.main(["--db", str(copy), "--plan", str(unusable), "--apply"])
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_plan_raw()))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert (
        S.main(
            [
                "--db",
                str(copy),
                "--plan",
                str(good),
                "--admin",
                ADMIN,
                "--accounts-out",
                str(tmp_path / "a.txt"),
                "--apply",
            ]
        )
        == 2
    )
    assert not (tmp_path / "a.txt").exists()


def test_the_drill_files_hold_no_em_dash():
    root = Path(__file__).resolve().parents[2]
    for rel in (
        "server/scripts/seed_second_facility.py",
        "server/tests/test_two_facility_e2e.py",
        "docs/MULTI_SITE.md",
    ):
        path = root / rel
        if path.exists():
            assert chr(0x2014) not in path.read_text(encoding="utf-8"), rel
