"""Drug identity governance (grn-6, ruling GRN-RS1 option (b), held for Samuel).

A drug's name, generic name, form and strength decide how the allergy
interlock, the interaction screen and the Sage lexicon recognise it. Pinned:
  * with the pharmacy store off every identity route answers 404;
  * a missing generic name is refused, and nothing is written;
  * a duplicate name is refused; the same generic name, strength and form is
    refused unless the pharmacist says it is another brand;
  * a new drug is a DRAFT with a pending request, audited (security,
    fail-closed: an audit outage writes nothing);
  * a draft cannot be ordered through create_order, insert_order, a Sage order
    match, the reconciler, a substitute or a stockout suggestion, yet the
    allergy interlock resolves it at once and a new penicillin brand is
    screened as a penicillin;
  * the Sage mutation lexicon has the new name without a restart;
  * only a DIFFERENT pharmacist confirms, against the identity they were
    shown; the approve tier cannot; the classification route cannot reach an
    identity request;
  * a refused or withdrawn draft is retired, never orderable;
  * a correction is for a confirmed drug nothing names yet, needs a second
    pharmacist, and is refused at approval once the drug is in use;
  * retiring takes a drug out of ordering at once, and a retired drug stays on
    its historical orders;
  * with the pharmacy store on, the formulary loader adds new rows as drafts
    and never rewrites a confirmed drug's identity.
"""
from __future__ import annotations

import random
import string
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from main import app
from services import allergy_interlock, helix_service
from services import pharmacy_master_service as masters
from services import pharmacy_stock_service as stock
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
LATER = (date.today() + timedelta(days=400)).isoformat()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)


def _word(n: int = 8) -> str:
    """A made-up brand word: letters only, so the Sage lexicon keeps it."""
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


def _user(role, department=None):
    uid = f"UDI{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, department, hash_password("secretpass")))
    return {"id": uid, "role": role,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


@pytest.fixture
def pharmacist():
    return _user("pharmacist")


@pytest.fixture
def checker():
    return _user("pharmacist")


@pytest.fixture
def admin():
    return _user("admin")


def _identity(brand=None, *, generic=None, strength="500 mg", form="Capsule"):
    brand = brand or _word().capitalize()
    return {"name": f"{brand} {strength} {form}", "generic_name": generic or f"{_word()}mol",
            "form": form, "strength": strength}


def _add(user, identity, **extra):
    return client.post(f"{BASE}/drugs", headers=user["hdr"], json=identity | extra)


def _added(user, identity, **extra) -> dict:
    resp = _add(user, identity, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _confirm(user, request_id, identity):
    return client.post(f"{BASE}/identity-requests/{request_id}/decision", headers=user["hdr"],
                       json={"decision": "approved", "expected": identity})


def _row(drug_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM helix_medication_master WHERE id = ?",
                               (drug_id,)).fetchone())


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


def _active_drug(identity=None) -> tuple[str, dict]:
    """A confirmed drug written straight to the master (as the seed does)."""
    identity = identity or _identity()
    drug_id = f"MEDDI{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (drug_id, identity["name"], identity["generic_name"], identity["form"],
                    identity["strength"]))
    return drug_id, identity


def _patient(allergy=None):
    pid = f"PATDI{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, bed, ward) "
                   "VALUES (?, ?, ?, 'admitted', 'B-1', 'GM-1')",
                   (pid, f"Identity Case {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:6]}"))
        db.execute("INSERT INTO admissions (patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, 'General Medicine', 'GM-1', 'B-1', 'active', "
                   "'system')", (pid,))
        if allergy:
            db.execute("INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
                       "status) VALUES (?, ?, 'anaphylaxis', 'severe', 'active')", (pid, allergy))
    return pid


def _lot(drug_id, units=20):
    with db_session(immediate=True) as conn:
        stock.open_or_merge_lot(conn, medication_id=drug_id, lot_number=f"B{_word(5)}",
                                expiration_date=LATER, units=units, actor_id="test")


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the tiers
# ═════════════════════════════════════════════════════════════════════════════

def test_every_identity_route_is_404_with_the_module_off(monkeypatch, pharmacist):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in (("post", "/drugs/preview"), ("post", "/drugs"),
                         ("get", "/identity-requests"),
                         ("post", "/identity-requests/1/decision"),
                         ("post", "/identity-requests/1/withdraw"),
                         ("post", "/drugs/X/identity-correction"),
                         ("post", "/drugs/X/retire")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=pharmacist["hdr"],
                                       **({} if method == "get" else {"json": {}}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_the_approve_tier_neither_adds_nor_confirms(admin, pharmacist):
    assert _add(admin, _identity()).status_code == 403
    out = _added(pharmacist, _identity())
    assert _confirm(admin, out["request"]["id"], {
        k: out["drug"][k] for k in masters.IDENTITY_FIELDS}).status_code == 403
    # The approve tier's classification route does not even see it.
    resp = client.post(f"{BASE}/classification-requests/{out['request']['id']}/decision",
                       headers=admin["hdr"], json={"decision": "approved"})
    assert resp.status_code == 404, resp.text
    listed = client.get(f"{BASE}/classification-requests", headers=admin["hdr"]).json()
    assert out["request"]["id"] not in {r["id"] for r in listed["requests"]}
    assert _row(out["drug"]["id"])["identity_status"] == "draft"


# ═════════════════════════════════════════════════════════════════════════════
#  Adding a drug
# ═════════════════════════════════════════════════════════════════════════════

def test_a_missing_generic_name_is_refused_and_nothing_is_written(pharmacist):
    identity = _identity() | {"generic_name": "   "}
    resp = _add(pharmacist, identity)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "missing_field" and detail["field"] == "generic_name"
    assert "allergy and interaction screens" in detail["message"]
    with db_session() as db:
        assert db.execute("SELECT 1 FROM helix_medication_master WHERE name = ?",
                          (identity["name"],)).fetchone() is None
    for field in ("name", "form", "strength"):
        resp = _add(pharmacist, _identity() | {field: ""})
        assert resp.status_code == 422 and resp.json()["detail"]["field"] == field


def test_a_duplicate_name_or_identity_is_refused(pharmacist):
    first = _added(pharmacist, _identity())["drug"]
    same_name = {k: first[k] for k in masters.IDENTITY_FIELDS} | {"generic_name": "othermol"}
    resp = _add(pharmacist, same_name)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "already_exists"

    twin = _identity(generic=first["generic_name"].upper(), strength="500mg",
                     form=first["form"].lower())
    resp = _add(pharmacist, twin)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "same_identity_exists"
    assert [r["id"] for r in detail["same_identity"]] == [first["id"]]
    # Said to be another brand of the same medicine: saved, and the audit
    # row says so.
    second = _added(pharmacist, twin, another_brand=True)["drug"]
    meta = _audits("pharmacy_drug_created", second["id"])[0]["metadata_json"]
    assert first["id"] in meta and '"acknowledged_same_identity": true' in meta


def test_a_new_drug_is_an_audited_draft_and_the_lexicon_learns_it(pharmacist):
    from services import sage_service

    brand = _word(9)
    out = _added(pharmacist, _identity(brand.capitalize(), generic="Amoxicillin"),
                 another_brand=True)
    drug, request = out["drug"], out["request"]
    assert drug["identity_status"] == "draft"
    assert drug["identity_label"] == "Waiting for a second pharmacist"
    assert drug["orderable_identity"] is False
    assert request["change_kind"] == "identity_create" and request["decision"] == "pending"
    assert request["identity"]["generic_name"] == "Amoxicillin"
    assert "penicillins" in out["screening"]["classes"]
    rows = _audits("pharmacy_drug_created", drug["id"])
    assert len(rows) == 1 and rows[0]["event_category"] == "security"
    assert rows[0]["target_id"] == drug["id"]
    # The Sage mutation gate knows the brand now, without a restart.
    assert brand in sage_service._FORMULARY_TERMS


def test_the_preview_says_how_a_new_penicillin_brand_is_screened(pharmacist):
    identity = _identity(_word().capitalize(), generic="Amoxicillin")
    resp = client.post(f"{BASE}/drugs/preview", headers=pharmacist["hdr"], json=identity)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "penicillins" in body["screening"]["classes"]
    assert "Amoxicillin" in body["screening"]["screened_as"]
    assert body["same_name"] == [] and isinstance(body["screening"]["interaction_recognised"],
                                                  (bool, type(None)))
    with db_session() as db:
        assert db.execute("SELECT 1 FROM helix_medication_master WHERE name = ?",
                          (identity["name"],)).fetchone() is None, "a preview saves nothing"


def test_an_audit_outage_adds_nothing(monkeypatch, pharmacist):
    from services import audit_service

    def broken(*args, **kwargs):
        raise audit_service.AuditWriteError("audit store down")

    monkeypatch.setattr(audit_service, "log_audit_event", broken)
    identity = _identity()
    resp = _add(pharmacist, identity)
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    with db_session() as db:
        assert db.execute("SELECT 1 FROM helix_medication_master WHERE name = ?",
                          (identity["name"],)).fetchone() is None


# ═════════════════════════════════════════════════════════════════════════════
#  A draft cannot be ordered, yet every safety screen sees it
# ═════════════════════════════════════════════════════════════════════════════

def test_a_draft_reaches_no_ordering_path(pharmacist):
    from routes.sage_routes import _match_medication
    from services.rxnorm_reconciler import reconcile_medication

    brand = _word(9).capitalize()
    drug = _added(pharmacist, _identity(brand, generic=f"{_word()}zole"))["drug"]
    pid = _patient()
    with pytest.raises(ValueError):
        helix_service.create_order(pid, brand, "1 cap", "PO", "BD", pharmacist["id"])
    with db_session() as db, pytest.raises(ValueError, match="second pharmacist"):
        helix_service.insert_order(db, pid, drug["id"], "1 cap", "PO", "BD", pharmacist["id"])
    assert _match_medication(brand) is None
    matched, _score = reconcile_medication(drug["name"])
    assert matched is None or matched["id"] != drug["id"]
    assert drug["id"] not in {d["id"] for d in
                              helix_service.get_medication_catalog(orderable_only=True)}
    # The procurement boundary screen still sees it: every row by default.
    assert drug["id"] in {d["id"] for d in helix_service.get_medication_catalog()}
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM helix_patient_med_orders WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0


def test_the_sage_handoff_orders_the_confirmed_drug_never_a_draft(pharmacist):
    """The handoff finds a drug by name as create_order does. A draft whose
    name also matches, added after the confirmed drug, never shadows it, and a
    name only a draft carries is charted for review but never ordered."""
    from services.sage_handoff_service import _write_helix

    word = _word(9).capitalize()
    # The draft first, so a lookup that ignored identity would meet it first.
    _added(pharmacist, _identity(f"{word}x", generic=f"{_word()}vir"))
    confirmed, _ident = _active_drug(_identity(word, generic=f"{_word()}vir"))
    pid = _patient()
    screened = {"allergy_alerts": [], "interaction_alerts": [], "blocking": False}
    action = {"patient_id": pid, "target": word}
    with db_session() as db:
        ref, _safety, note = _write_helix(db, action, pharmacist["id"], screened)
    assert note is None and ref != "charted-only"
    with db_session() as db:
        assert db.execute("SELECT medication_id FROM helix_patient_med_orders WHERE id = ?",
                          (ref,)).fetchone()[0] == confirmed

    only_draft = _word(9).capitalize()
    _added(pharmacist, _identity(only_draft))
    with db_session() as db:
        ref, _safety, note = _write_helix(db, {"patient_id": pid, "target": only_draft},
                                          pharmacist["id"], screened)
    assert ref == "charted-only" and "not sent to pharmacy" in note


def test_a_draft_is_never_a_substitute_or_a_stockout_suggestion(pharmacist, checker):
    generic = f"{_word()}pril"
    wanted, ident = _active_drug(_identity(generic=generic, strength="5 mg", form="Tablet"))
    draft = _added(pharmacist, _identity(generic=generic, strength="5 mg", form="Tablet"),
                   another_brand=True)
    _lot(draft["drug"]["id"])
    # Out of stock, and the only equivalent in stock is a draft: nothing.
    assert helix_service.pre_flight_alternative_suggestion(wanted) is None

    pid = _patient()
    order = helix_service.create_order(pid, ident["name"], "5 mg", "PO", "OD", pharmacist["id"])
    helix_service.transition_order_status(order["id"], "reviewing", pharmacist["id"])
    with pytest.raises(ValueError, match="second pharmacist"):
        helix_service.transition_screen_target(order["id"], "substitute_suggested",
                                               substitute_medication_id=draft["drug"]["id"])
    with pytest.raises(ValueError, match="second pharmacist"):
        helix_service.transition_order_status(order["id"], "substitute_suggested",
                                              pharmacist["id"],
                                              substitute_medication_id=draft["drug"]["id"])
    with db_session() as db:
        row = db.execute("SELECT status, substitute_medication_id FROM helix_patient_med_orders "
                         "WHERE id = ?", (order["id"],)).fetchone()
    assert (row["status"], row["substitute_medication_id"]) == ("reviewing", None)

    # Once a second pharmacist confirms it, it is the suggestion.
    assert _confirm(checker, draft["request"]["id"], draft["request"]["identity"]).status_code == 200
    suggestion = helix_service.pre_flight_alternative_suggestion(wanted)
    assert suggestion and suggestion["id"] == draft["drug"]["id"]


def test_the_allergy_interlock_screens_a_draft_brand_as_a_penicillin(pharmacist):
    brand = _word(9).capitalize()
    _added(pharmacist, _identity(brand, generic="Amoxicillin"), another_brand=True)
    pid = _patient(allergy="Penicillin")
    with db_session() as db:
        result = allergy_interlock.screen(db, pid, brand)
    assert "amoxicillin" in [i.lower() for i in result["ingredients"]]
    assert result["decision"] == allergy_interlock.BLOCK
    assert any("penicillin" in (a.get("matched_on") or "").lower() or a["match"] == "direct"
               for a in result["blocking_alerts"])


# ═════════════════════════════════════════════════════════════════════════════
#  The second pharmacist
# ═════════════════════════════════════════════════════════════════════════════

def test_only_a_different_pharmacist_confirms_what_they_were_shown(pharmacist, checker):
    brand = _word(9).capitalize()
    out = _added(pharmacist, _identity(brand))
    rid, identity = out["request"]["id"], out["request"]["identity"]

    resp = _confirm(pharmacist, rid, identity)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "same_person"
    resp = client.post(f"{BASE}/identity-requests/{rid}/decision", headers=checker["hdr"],
                       json={"decision": "approved"})
    assert resp.status_code == 422, "confirming names the identity that was checked"
    resp = _confirm(checker, rid, identity | {"strength": "250 mg"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "request_stale"
    assert _row(out["drug"]["id"])["identity_status"] == "draft"

    listed = client.get(f"{BASE}/identity-requests", headers=checker["hdr"]).json()["requests"]
    mine = next(r for r in listed if r["id"] == rid)
    assert mine["screening"]["screened_as"] and mine["identity"] == identity

    resp = _confirm(checker, rid, identity)
    assert resp.status_code == 200, resp.text
    assert resp.json()["drug"]["identity_status"] == "active"
    assert resp.json()["request"]["decided_by"] == checker["id"]
    assert len(_audits("pharmacy_drug_identity_confirmed", out["drug"]["id"])) == 1
    # Now it can be ordered.
    order = helix_service.create_order(_patient(), brand, "1 cap", "PO", "BD", pharmacist["id"])
    assert order["medication_id"] == out["drug"]["id"]
    resp = _confirm(checker, rid, identity)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "decided"


def test_a_refused_or_withdrawn_draft_is_retired(pharmacist, checker):
    refused = _added(pharmacist, _identity())
    rid = refused["request"]["id"]
    resp = client.post(f"{BASE}/identity-requests/{rid}/decision", headers=checker["hdr"],
                       json={"decision": "refused"})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "missing_reason"
    resp = client.post(f"{BASE}/identity-requests/{rid}/decision", headers=checker["hdr"],
                       json={"decision": "refused", "reason": "Wrong strength on the pack"})
    assert resp.status_code == 200, resp.text
    assert _row(refused["drug"]["id"])["identity_status"] == "retired"
    with db_session() as db, pytest.raises(ValueError, match="retired"):
        helix_service.insert_order(db, _patient(), refused["drug"]["id"], "1", "PO", "OD",
                                   pharmacist["id"])

    withdrawn = _added(pharmacist, _identity())
    wid = withdrawn["request"]["id"]
    resp = client.post(f"{BASE}/identity-requests/{wid}/withdraw", headers=checker["hdr"])
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "not_requester"
    resp = client.post(f"{BASE}/identity-requests/{wid}/withdraw", headers=pharmacist["hdr"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["request"]["decision"] == "withdrawn"
    assert _row(withdrawn["drug"]["id"])["identity_status"] == "retired"
    # A retired drug's name is free again: the corrected drug can take it.
    again = {k: withdrawn["drug"][k] for k in masters.IDENTITY_FIELDS}
    assert _add(pharmacist, again).status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
#  Correction, retire and replace
# ═════════════════════════════════════════════════════════════════════════════

def test_a_drug_in_use_is_retired_and_replaced_never_rewritten(pharmacist):
    drug_id, identity = _active_drug()
    pid = _patient()
    helix_service.create_order(pid, identity["name"], "1", "PO", "OD", pharmacist["id"])
    resp = client.post(f"{BASE}/drugs/{drug_id}/identity-correction", headers=pharmacist["hdr"],
                       json=identity | {"generic_name": "othermol", "reason": "Typo at entry"})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "identity_locked"
    assert _row(drug_id)["generic_name"] == identity["generic_name"]
    # The field editor refuses identity too.
    resp = client.patch(f"{BASE}/drugs/{drug_id}", headers=pharmacist["hdr"],
                        json={"generic_name": "othermol"})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "identity_locked"

    resp = client.post(f"{BASE}/drugs/{drug_id}/retire", headers=pharmacist["hdr"], json={})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "missing_reason"
    replacement = _added(pharmacist, _identity())["drug"]
    resp = client.post(f"{BASE}/drugs/{drug_id}/retire", headers=pharmacist["hdr"],
                       json={"reason": "Generic name entered wrongly",
                             "replacement_drug_id": replacement["id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["drug"]["identity_status"] == "retired"
    meta = _audits("pharmacy_drug_retired", drug_id)[0]["metadata_json"]
    assert replacement["id"] in meta and '"in_use": true' in meta
    # Nothing new can order it; the historical order still names it.
    with pytest.raises(ValueError):
        helix_service.create_order(pid, identity["name"], "1", "PO", "OD", pharmacist["id"])
    history = [o for o in helix_service.get_all_orders() if o["patient_id"] == pid]
    assert [o["medication_name"] for o in history] == [identity["name"]]
    resp = client.post(f"{BASE}/drugs/{drug_id}/retire", headers=pharmacist["hdr"],
                       json={"reason": "again"})
    assert resp.status_code == 409


def test_a_correction_needs_a_second_pharmacist_and_no_use(pharmacist, checker):
    drug_id, identity = _active_drug()
    fixed = identity | {"strength": "250 mg", "name": f"{identity['name']} corrected"}
    resp = client.post(f"{BASE}/drugs/{drug_id}/identity-correction", headers=pharmacist["hdr"],
                       json=fixed)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "missing_reason"
    resp = client.post(f"{BASE}/drugs/{drug_id}/identity-correction", headers=pharmacist["hdr"],
                       json=fixed | {"reason": "Strength typed wrongly"})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["request"]["id"]
    assert _row(drug_id)["strength"] == identity["strength"], "unchanged until approved"
    again = client.post(f"{BASE}/drugs/{drug_id}/identity-correction", headers=pharmacist["hdr"],
                        json=fixed | {"reason": "Strength typed wrongly"})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "request_pending"

    assert _confirm(pharmacist, rid, fixed).status_code == 403
    resp = _confirm(checker, rid, fixed)
    assert resp.status_code == 200, resp.text
    row = _row(drug_id)
    assert (row["name"], row["strength"]) == (fixed["name"], "250 mg")
    assert len(_audits("pharmacy_drug_identity_corrected", drug_id)) == 1


def test_a_correction_is_refused_once_the_drug_is_in_use(pharmacist, checker):
    drug_id, identity = _active_drug()
    fixed = identity | {"generic_name": f"{identity['generic_name']}x"}
    resp = client.post(f"{BASE}/drugs/{drug_id}/identity-correction", headers=pharmacist["hdr"],
                       json=fixed | {"reason": "Generic name typed wrongly"})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["request"]["id"]
    _lot(drug_id)
    resp = _confirm(checker, rid, fixed)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "identity_locked"
    assert _row(drug_id)["generic_name"] == identity["generic_name"]


def test_a_draft_is_not_corrected_or_retired_in_place(pharmacist):
    out = _added(pharmacist, _identity())
    drug_id = out["drug"]["id"]
    resp = client.post(f"{BASE}/drugs/{drug_id}/identity-correction", headers=pharmacist["hdr"],
                       json=_identity() | {"reason": "Changed my mind"})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "identity_locked"
    resp = client.post(f"{BASE}/drugs/{drug_id}/retire", headers=pharmacist["hdr"],
                       json={"reason": "Not needed"})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "identity_locked"


# ═════════════════════════════════════════════════════════════════════════════
#  The seed's identities
# ═════════════════════════════════════════════════════════════════════════════

def test_every_seeded_brand_carries_its_true_molecule():
    """The seed once tagged two brands with another drug's molecule so a
    substitution demo would fire, and the drug-safety screen, which reads
    generic_name, then saw a cephalosporin as amoxicillin. Fixed before the
    3 October baseline; pinned here because the store's same-ingredient
    checks (the stockout suggestion, a confirmed brand twin, medication-order
    binding) all trust generic_name. Every seeded row names its own molecule,
    the screen classes it at least as its molecule does, and only true brand
    twins share a generic name, strength and form."""
    from scripts.seed_helix import DRUGS
    from services import drug_safety_service as dss

    brands = {"Mox 500mg Capsule": "Amoxicillin", "Dolo 650mg Tablet": "Paracetamol",
              "Monocef 1g Injection": "Ceftriaxone",
              "Augmentin 625 DUO Tablet": "Amoxicillin/Clavulanate"}
    groups: dict[tuple, list[str]] = {}
    for med_id, name, generic, form, strength in DRUGS:
        if name in brands:
            assert generic == brands[name], (med_id, generic)
        else:
            assert generic.split()[0].lower() in name.lower(), (med_id, name, generic)
        screened = set(dss.classify_drug(helix_service._screen_name(
            {"name": name, "generic_name": generic})))
        assert set(dss.classify_drug(generic)) <= screened, (med_id, screened)
        assert set(dss.classify_drug(name)) <= screened, (med_id, screened)
        groups.setdefault((masters._key(generic), masters._key(strength), masters._key(form)),
                          []).append(med_id)
    twins = sorted(sorted(ids) for ids in groups.values() if len(ids) > 1)
    assert twins == [["MED-AMOX-500", "MED-MOX-500"], ["MED-DOLO", "MED-PARA"]]


# ═════════════════════════════════════════════════════════════════════════════
#  The formulary loader with the pharmacy store on
# ═════════════════════════════════════════════════════════════════════════════

def test_the_loader_drafts_new_rows_and_leaves_confirmed_identity_alone(tmp_path: Path,
                                                                       checker):
    from scripts.load_rxnorm_formulary import load_file_report

    confirmed, ident = _active_drug()
    confirmed_cui = f"GOV{uuid.uuid4().hex[:6]}"
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET id = ? WHERE id = ?",
                   (f"RX-{confirmed_cui}", confirmed))
    new_cui, draft_cui = f"NEW{uuid.uuid4().hex[:6]}", f"DRF{uuid.uuid4().hex[:6]}"
    new_name, draft_name = f"{_word().capitalize()} 10 mg Tablet", f"{_word().capitalize()} Syrup"
    first = tmp_path / "first.csv"
    first.write_text("rxcui,name,generic_name,form,strength\n"
                     f"{new_cui},{new_name},newmol,Tablet,10 mg\n"
                     f"{draft_cui},{draft_name},draftmol,Syrup,5 mg/5 mL\n"
                     f"{confirmed_cui},{ident['name']},changedmol,{ident['form']},"
                     f"{ident['strength']}\n", encoding="utf-8")
    report = load_file_report(first)
    assert sorted(report["drafted"]) == sorted([f"RX-{new_cui}", f"RX-{draft_cui}"])
    assert [g["id"] for g in report["governed"]] == [f"RX-{confirmed_cui}"]
    assert _row(f"RX-{confirmed_cui}")["generic_name"] == ident["generic_name"]
    new_row = _row(f"RX-{new_cui}")
    assert new_row["identity_status"] == "draft"
    with pytest.raises(ValueError):
        helix_service.create_order(_patient(), new_name, "1", "PO", "OD", checker["id"])

    # A second file corrects the still-draft row, and its request follows.
    second = tmp_path / "second.csv"
    second.write_text("rxcui,name,generic_name,form,strength\n"
                      f"{draft_cui},{draft_name},draftmol,Syrup,10 mg/5 mL\n", encoding="utf-8")
    load_file_report(second)
    assert _row(f"RX-{draft_cui}")["strength"] == "10 mg/5 mL"
    pending = [r for r in masters.list_identity_requests()
               if r["helix_medication_id"] == f"RX-{draft_cui}"]
    assert len(pending) == 1 and pending[0]["identity"]["strength"] == "10 mg/5 mL"
    assert pending[0]["requested_by_loader"] is True

    # Any pharmacist confirms a loader draft against what is on the row.
    resp = _confirm(checker, pending[0]["id"], pending[0]["identity"])
    assert resp.status_code == 200, resp.text
    assert _row(f"RX-{draft_cui}")["identity_status"] == "active"
    with db_session() as db:
        meta = db.execute("SELECT metadata_json FROM audit_logs WHERE action_type = "
                          "'pharmacy_formulary_loaded' AND target_id = ? ORDER BY id DESC",
                          ("first.csv",)).fetchone()[0]
    assert "drafted_ids" in meta and "governed_ids" in meta
