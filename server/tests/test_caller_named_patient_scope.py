"""Drift guard for the OTHER half of the scope surface: the caller NAMES the patient.

WHY THIS FILE EXISTS. Two guards already stand over patient scoping, and there
is a hole between them that is exactly the shape of the bug they were each
written for:

  * tests/test_patient_scope_routes.py — enumerates ``/{patient_id}`` routes but
    filters on ``"GET" in methods``. Reads only.
  * tests/test_nurse_write_scope.py    — enumerates ``/{patient_id}`` MUTATIONS.
    Writes, but only where the id is on the PATH.

Neither can see a mutation where the patient arrives in a BODY FIELD, a FORM
field or a QUERY param. permissions.enforce_patient_scope's docstring
(permissions.py:300) already names that spelling as the blind spot of the
2026-08-05 read sweep — ``/api/scribe/notes?patient_id=`` and friends — but the
mutation side never got the matching guard. So this file adds it.

STATUS, 2026-08-14: THE INVENTORY IS EMPTY AND THE RATCHET IS AT ZERO.
─────────────────────────────────────────────────────────────────────────────
All eight routes listed below as known-bare were scoped on 2026-08-14, plus the
two this enumeration could never see (POST /api/appointments and
POST /api/payments/refunds, both of which took an untyped `dict` body — they are
typed now, which is what made them visible). `_KNOWN_BARE_CALLER_NAMED_MUTATIONS`
is therefore an EMPTY set: from today a green run means the surface is clean,
not merely that nobody added a ninth. Any route that becomes bare — old or new —
fails here.

The historical list is kept in `_FORMERLY_BARE` and asserted route-by-route
(`test_the_eight_formerly_bare_routes_are_each_still_scoped`), because
"`bare` is empty" and "these specific eight are scoped" are different claims:
the first also goes green if the enumeration stops resolving a route at all,
which is exactly the failure mode this file has already had once.

EVIDENCE LEVEL, stated per entry rather than blurred: two of the eight were
PROBED live on 2026-08-07 (a request was actually made and a row actually
landed); the other six were STATIC findings — the endpoint names a patient and
its source contains no scope derivation. Static is enough to require a look; it
is not the same as demonstrated, and the inventory says which is which.

WHAT A GREEN RUN OF THIS FILE STILL DOES NOT PROVE. The sweep reads SOURCE for
the name of a derivation; it cannot tell a derivation that is applied from one
that is merely mentioned, and it says nothing about the id arriving through a
SURROGATE (`/orders/{order_id}/transition` names no patient but owns one). The
live cross-ward probes at the bottom of this file exist for exactly that reason:
they make an actual request and read the actual table.

THIS GUARD'S OWN BLIND SPOT, written down rather than left to be rediscovered.
It finds the patient by INSPECTING THE SIGNATURE — a parameter literally named
``patient_id``, or a Pydantic body model with a ``patient_id`` field. A route
that takes an UNTYPED body cannot be seen at all:

    POST /api/appointments  →  def book_appointment(data: dict, ...)

That route reads ``data.get("patient_id")`` and was PROBED CROSS-WARD ON
2026-08-07: a Cardiology doctor booked an appointment for an Oncology patient,
HTTP 200, row persisted. It is absent from the inventory below because the
enumeration cannot reach it, NOT because it is scoped. The same is true of any
future route that spells its body ``dict``. Typing a request body is therefore
a scoping-visibility decision here, not just a style one.
"""
import inspect
import typing

from pydantic import BaseModel


# Names that prove the endpoint derives scope from the authenticated user rather
# than from whatever the request said.
_SCOPE_DERIVATIONS = (
    "require_patient_access", "_authorize_patient_access", "enforce_patient_scope",
    "patient_in_scope", "department_scope", "require_patient_self",
    "scope_patient_ids_sql",
)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


# ─────────────────────────────────────────────────────────────────────────────
# The inventory, 2026-08-07 → CLOSED 2026-08-14. [PROBED] = a cross-ward request
# was actually made and a row actually landed. [STATIC] = names a patient, no
# scope derivation in the endpoint source; not demonstrated end to end.
#
# Each entry now records WHERE the derivation went, so a reviewer can check the
# claim in one hop rather than trusting this comment.
# ─────────────────────────────────────────────────────────────────────────────
_FORMERLY_BARE = {
    # [PROBED] outcome.record — held by NURSE, doctor and admin. Writes the
    # pilot's GROUND TRUTH (a confirmed death / ICU transfer / arrest) against
    # any patient id in the body. A Cardiology nurse logged an `icu_transfer` on
    # an Oncology patient: HTTP 201, shadow_event row created. This does not
    # merely cross a ward boundary — it corrupts the denominator of the metric
    # the pilot is judged on, from an account deliberately denied the scorecard
    # (see the OUTCOME_RECORD comment in permissions.py).
    #   → routes/shadow_routes.py post_outcome, after the enum validation so a
    #     malformed body keeps its 422 instead of being masked by a 404.
    ("POST", "/api/admin/shadow/outcome"),
    # [PROBED] order.write — held by nurse and doctor. A Cardiology nurse
    # created a nursing order on an Oncology patient: HTTP 200, row persisted.
    #   → routes/orders_routes.py create_order. Probed live again on 2026-08-14
    #     by test_a_clinician_cannot_create_an_order_for_another_wards_patient.
    ("POST", "/api/orders"),
    ("POST", "/api/orders/"),
    # [STATIC] helix.write — pharmacist/admin. Approves a pharmacy order against
    # a body-named patient.
    #   → routes/helix_routes.py approve_helix_order, after name→id resolution
    #     so both spellings of "which patient" are covered.
    ("POST", "/api/helix/orders/approve"),
    # [STATIC] lens.analyze — nurse and doctor. Attaches an uploaded image to a
    # caller-named patient, and posts to that patient's Relay case room.
    #   → routes/lens_routes.py analyze_image / assign_to_patient (run_db: both
    #     are async def).
    ("POST", "/api/lens/analyze"),
    ("PATCH", "/api/lens/analyses/{analysis_id}/assign"),
    # [STATIC] scribe.create_draft — nurse and doctor. Transcribes audio against
    # a caller-named patient.
    #   → routes/scribe_routes.py transcribe / transcribe_chunk. Note these read
    #     the patient's diagnosis and drug list to bias the ASR decoder, so
    #     unscoped they were a chart READ as well as a write.
    ("POST", "/api/scribe/transcribe"),
    ("POST", "/api/scribe/transcribe-chunk"),
}

# EMPTY, AND MEANT TO STAY EMPTY. The ratchet no longer tolerates anything: a
# bare caller-named mutation fails on the day it lands, whether it is new or a
# regression of one of the eight above. Do not re-add entries here to make a red
# run green — scope the route.
_KNOWN_BARE_CALLER_NAMED_MUTATIONS: set[tuple[str, str]] = set()


def _caller_named_patient_mutations():
    """Live mutation routes where the caller names a patient OFF the path.

    Returns (bare, seen) — `seen` is every route the enumeration RESOLVED as
    naming a patient, `bare` the subset with no scope derivation in the endpoint
    source. They are returned separately so a test can assert a route is still
    being LOOKED AT without also asserting it is still broken.
    """
    from main import app as live_app

    bare, seen = set(), set()
    for route in live_app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set()) & _MUTATING
        endpoint = getattr(route, "endpoint", None)
        if not methods or endpoint is None:
            continue
        if "{patient_id}" in path:
            continue        # tests/test_nurse_write_scope.py owns the path form
        try:
            sig = inspect.signature(endpoint)
        except (TypeError, ValueError):
            continue

        # RESOLVE STRING ANNOTATIONS. Route modules that use
        # `from __future__ import annotations` hand back the STRING "OutcomeIn"
        # rather than the class, so reading ``param.annotation`` raw skips every
        # such route silently. That is not hypothetical: the first cut of this
        # enumeration missed POST /api/admin/shadow/outcome — the single worst
        # entry in the inventory — for exactly that reason. A guard that fails
        # open on a syntax detail is worse than no guard, because it reports
        # green.
        try:
            hints = typing.get_type_hints(endpoint)
        except Exception:
            hints = {}

        names_a_patient = False
        for pname, param in sig.parameters.items():
            if pname == "patient_id":
                names_a_patient = True
            annotation = hints.get(pname, param.annotation)
            if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
                if "patient_id" in annotation.model_fields:
                    names_a_patient = True
        if not names_a_patient:
            continue

        try:
            src = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        seen |= {(m, path) for m in methods}
        if not any(g in src for g in _SCOPE_DERIVATIONS):
            bare |= {(m, path) for m in methods}
    return bare, seen


def test_no_new_mutation_route_lets_the_caller_name_the_patient_unscoped():
    """A new bare caller-named mutation route fails here on the day it lands."""
    bare, seen = _caller_named_patient_mutations()

    # Non-vacuity. If the enumeration silently stops finding routes — a FastAPI
    # upgrade changing `.routes`, an annotation-resolution regression like the
    # one described above — this test would pass forever while guarding nothing.
    # The floor was 8 while the inventory was the point; it is 24 now (26 routes
    # resolve as of 2026-08-14, after typing the two `dict` bodies) because with
    # `bare` expected EMPTY, a broken enumeration is the only remaining way for
    # this assertion to be trivially satisfied.
    assert len(seen) >= 24, (
        f"only {len(seen)} caller-named-patient mutation routes enumerated; the "
        f"enumeration is broken, not the app"
    )

    new = bare - _KNOWN_BARE_CALLER_NAMED_MUTATIONS
    assert not new, (
        "these mutation routes let the CALLER name the patient (body/form/query) "
        "and derive no ward scope from the authenticated user — the same defect "
        "class as the nurse observation write, in the spelling the path-based "
        "guards cannot see:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in sorted(new))
    )


def test_the_annotation_resolution_actually_resolves_a_string_annotation():
    """Guards the guard.

    ``POST /api/admin/shadow/outcome`` declares its body as ``body: OutcomeIn``
    inside a module using `from __future__ import annotations`. If the
    enumeration ever reverts to reading ``param.annotation`` raw, this route
    vanishes from the sweep and the ratchet quietly narrows. Assert it is still
    being SEEN — independently of whether it is bare.
    """
    _, seen = _caller_named_patient_mutations()
    assert ("POST", "/api/admin/shadow/outcome") in seen, (
        "the sweep no longer SEES the shadow outcome route — its body model is "
        "declared as a string annotation, so this is the signature of the "
        "resolution regression described in _caller_named_patient_mutations"
    )

    # And prove the resolution path works on the real endpoint, not just in
    # aggregate, so the failure above can be told apart from a routing change.
    from routes.shadow_routes import post_outcome
    hints = typing.get_type_hints(post_outcome)
    body = hints.get("body")
    assert inspect.isclass(body) and issubclass(body, BaseModel), (
        "string annotation no longer resolves to a model — the enumeration in "
        "this file is now blind to every body-named patient_id route"
    )
    assert "patient_id" in body.model_fields


def test_the_nurse_routes_are_not_in_this_inventory():
    """The routes this sweep OWNS must never appear in a known-bare list.

    Stated separately so the ratchet above can never be widened to re-admit
    them by someone pasting a fresh enumeration over the inventory.
    """
    owned = {("POST", "/api/nurse/patients/{patient_id}/observations"),
             ("POST", "/api/nurse/patients/{patient_id}/news2-preview")}
    assert not (owned & _KNOWN_BARE_CALLER_NAMED_MUTATIONS)


def test_the_eight_formerly_bare_routes_are_each_still_scoped():
    """Named individually, because "bare is empty" is a weaker claim.

    `bare` also goes empty if the enumeration stops RESOLVING a route — the
    exact regression this file has already survived once, when a string
    annotation hid the worst entry in the inventory. So each of the eight is
    asserted to be BOTH still seen AND not bare, and the failure message says
    which of the two happened.
    """
    bare, seen = _caller_named_patient_mutations()

    unseen = sorted(_FORMERLY_BARE - seen)
    assert not unseen, (
        "the sweep no longer SEES these routes at all, so it can no longer tell "
        "whether they are scoped. Either they were renamed/removed (update this "
        "set) or the enumeration regressed (fix it — do NOT delete the entry):\n  "
        + "\n  ".join(f"{m} {p}" for m, p in unseen)
    )

    regressed = sorted(_FORMERLY_BARE & bare)
    assert not regressed, (
        "these routes were scoped on 2026-08-14 and have gone bare again — the "
        "caller can once more name any patient in a body/form/query field and "
        "have the mutation applied to them:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in regressed)
    )


def test_the_two_dict_bodied_routes_are_now_visible_to_this_sweep():
    """The blind spot named in the module docstring, closed.

    `POST /api/appointments` and `POST /api/payments/refunds` both declared
    their body as `dict`, so no signature inspection could ever find the patient
    in them — the appointments one was PROBED CROSS-WARD on 2026-08-07 while
    this file ran green. They are typed now. If either reverts to `dict` it
    disappears from `seen` and this fails, which is the only way to keep typing
    a body from silently becoming optional again.
    """
    _, seen = _caller_named_patient_mutations()
    for route in (("POST", "/api/appointments"), ("POST", "/api/payments/refunds")):
        assert route in seen, (
            f"{route[0]} {route[1]} is invisible to the sweep again — its request "
            f"body has most likely reverted to an untyped `dict`, which opts the "
            f"route out of this guard entirely"
        )


# ═════════════════════════════════════════════════════════════════════════════
#  LIVE CROSS-WARD PROBES
# ═════════════════════════════════════════════════════════════════════════════
# The sweep above reads SOURCE. It cannot tell a derivation that is APPLIED from
# one that is merely present in the text, and this repo's own history is the
# argument for not trusting that difference: a green suite has certified a live
# bug here before. So the three mutations that matter most are exercised for
# real — a request is made, the response code is asserted, AND the table is read
# to prove nothing was written.
#
# THE WRONG-COLUMN TRAP is reproduced from tests/test_nurse_write_scope.py: a
# patient's department lives on ``admissions.department``, while
# ``patients.ward`` is a free-text bed label nothing constrains. The fixtures
# below CROSS the two, so a "fix" that scoped ``patients.ward`` would fail in
# both directions — letting the cross-ward write through AND refusing the
# legitimate one.

import uuid                                                        # noqa: E402
from datetime import date, timedelta                               # noqa: E402

import pytest                                                      # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

from auth import create_token                                      # noqa: E402
from database import db_session                                    # noqa: E402
from main import app as _live_app                                  # noqa: E402

_client = TestClient(_live_app)

DEPT_A = "Caller Named Scope Cardiology"
DEPT_B = "Caller Named Scope Oncology"


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department):
    uid = f"CNS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Caller Named Scope {role}", role, department),
        )
    return uid


def _mk_patient(department, *, ward_label):
    pid = f"CNSP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('CNSADM', 'Caller Named Scope Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, ?, ?, 'B-2', ?, 58, 'M', 'Caller named scope fixture', 'admitted')",
            (pid, f"CNS {department}", f"MRN-{uuid.uuid4().hex[:8].upper()}", ward_label),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', 'CNSADM')",
            (f"ADM-CNS-{pid}", pid, department, ward_label),
        )
    return pid


@pytest.fixture()
def world():
    """Two wards with CROSSED ward labels, and the accounts that cross them.

      p_a.admissions.department = DEPT_A  but  p_a.patients.ward = DEPT_B
      p_b.admissions.department = DEPT_B  but  p_b.patients.ward = DEPT_A
    """
    return {
        "doctor_a": _mk_user("doctor", DEPT_A),
        "admin_a": _mk_user("admin", DEPT_A),
        "p_a": _mk_patient(DEPT_A, ward_label=DEPT_B),
        "p_b": _mk_patient(DEPT_B, ward_label=DEPT_A),
    }


def _order_count(patient_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE patient_id = ?", (patient_id,)
        ).fetchone()["n"]


def test_a_clinician_cannot_create_an_order_for_another_wards_patient(world):
    """THE CLINICAL ONE. PROBED on 2026-08-07: a Cardiology nurse created a
    nursing order on an Oncology patient — 200, row persisted.

    An order is an INSTRUCTION. It appears on a chart the ordering clinician
    cannot open, so she can neither see what she did nor undo it, and the nurse
    who acts on it has no way to know it came from off the ward.
    """
    doc, p_b = world["doctor_a"], world["p_b"]
    before = _order_count(p_b)

    resp = _client.post(
        "/api/orders", headers=_auth(doc, "doctor"),
        json={"patient_id": p_b, "order_type": "nursing",
              "description": "Cross-ward probe — hourly observations"},
    )

    assert resp.status_code == 404, (
        f"a {DEPT_A} doctor created an order on a {DEPT_B} patient "
        f"({resp.status_code}) — the caller-named patient is unscoped again"
    )
    # 404 and NOT 403: a 403 confirms the patient exists, which is itself the
    # disclosure the whole scoping law exists to prevent.
    assert resp.json()["detail"] == "Patient not found"
    assert _order_count(p_b) == before, (
        "the request was refused but the order was written anyway — the check is "
        "in the wrong place, after the service call"
    )


def test_the_same_clinician_can_still_order_for_her_own_ward(world):
    """NON-VACUITY, and the wrong-column half of the trap.

    Without this, `create_order` could satisfy the test above by refusing
    everything — and a fix that scoped `patients.ward` (which for p_a holds the
    OTHER department's name) would fail exactly here.
    """
    doc, p_a = world["doctor_a"], world["p_a"]
    resp = _client.post(
        "/api/orders", headers=_auth(doc, "doctor"),
        json={"patient_id": p_a, "order_type": "nursing",
              "description": "In-ward control — hourly observations"},
    )
    assert resp.status_code == 200, resp.text
    assert _order_count(p_a) == 1


def test_a_clinician_cannot_cancel_another_wards_order(world):
    """THE SURROGATE-ID SPELLING, which no enumeration in this file can reach.

    `POST /orders/{order_id}/transition` names no patient — it names an ORDER,
    and the order names the patient. So it is invisible to the signature sweep
    above AND to tests/test_nurse_write_scope.py's path sweep, while being able
    to CANCEL a live instruction on a ward the caller cannot see. Cancelling a
    stranger's order is the quieter half of the same harm as creating one: the
    drug simply stops, and nobody on that ward can account for why.
    """
    from services import orders_service

    p_b = world["p_b"]
    order = orders_service.create_order(
        patient_id=p_b, order_type="nursing",
        description="Existing Oncology order", ordered_by="CNSADM",
    )

    resp = _client.post(
        f"/api/orders/{order['id']}/transition",
        headers=_auth(world["doctor_a"], "doctor"),
        json={"to_status": "cancelled"},
    )
    assert resp.status_code == 404, (
        f"a {DEPT_A} doctor drove the state machine on a {DEPT_B} patient's "
        f"order ({resp.status_code})"
    )
    # "Order not found", NOT "patient not found": naming the patient would
    # confirm there is one behind this id.
    assert resp.json()["detail"] == "Order not found"
    assert orders_service.get_order(order["id"])["status"] == "draft", (
        "refused with a 404 and cancelled the order anyway"
    )


def test_a_clinician_cannot_book_an_appointment_for_another_wards_patient(world):
    """PROBED CROSS-WARD on 2026-08-07 — 200, row persisted — while the sweep in
    this file ran green, because the body was an untyped `dict`."""
    doc, p_b = world["doctor_a"], world["p_b"]
    day = (date.today() + timedelta(days=4)).isoformat()

    resp = _client.post(
        "/api/appointments", headers=_auth(doc, "doctor"),
        json={"patient_id": p_b, "scheduled_date": day, "scheduled_time": "10:30"},
    )
    assert resp.status_code == 404, (
        f"a {DEPT_A} doctor booked an appointment for a {DEPT_B} patient "
        f"({resp.status_code})"
    )
    assert resp.json()["detail"] == "Patient not found"
    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM appointments WHERE patient_id = ?", (p_b,)
        ).fetchone()["n"]
    assert n == 0, "refused with a 404 and booked the slot anyway"

    # The same booking on her own ward still works — otherwise the assertion
    # above is satisfied by a route that refuses everyone.
    ok = _client.post(
        "/api/appointments", headers=_auth(doc, "doctor"),
        json={"patient_id": world["p_a"], "scheduled_date": day, "scheduled_time": "10:30"},
    )
    assert ok.status_code == 200, ok.text


def test_a_ward_scoped_account_cannot_refund_against_another_wards_patient(world):
    """`POST /api/payments/refunds` — the one whose own siblings shamed it.

    `GET /refunds/limits?patient_id=` and `GET /refunds?patient_id=` have both
    been scoped since 2026-08-12, so the caller could be refused permission to
    ASK what was refundable for a patient while still being allowed to REQUEST
    the refund. The difference was not a decision: the write took a `dict` body
    and was invisible to this file's sweep.

    NOTE the in-ward control asserts NOT-404 rather than 200 — there is nothing
    refundable for a fresh fixture patient, so the honest answer is the money
    rule's 422. Reaching it proves the scope gate opened.
    """
    admin, p_a, p_b = world["admin_a"], world["p_a"], world["p_b"]
    body = {"amount_paise": 1000, "reason": "cross-ward probe"}

    denied = _client.post("/api/payments/refunds", headers=_auth(admin, "admin"),
                          json={**body, "patient_id": p_b})
    assert denied.status_code == 404, (
        f"a {DEPT_A} finance account raised a refund against a {DEPT_B} patient "
        f"({denied.status_code})"
    )
    assert denied.json()["detail"] == "Patient not found"

    allowed = _client.post("/api/payments/refunds", headers=_auth(admin, "admin"),
                           json={**body, "patient_id": p_a})
    assert allowed.status_code != 404, (
        "the scope gate refused the caller's OWN ward — check the derivation "
        "reads admissions.department and not patients.ward"
    )
