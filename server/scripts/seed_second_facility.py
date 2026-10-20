#!/usr/bin/env python3
"""Seed a second facility on a verification copy, through the product.

WHY THIS EXISTS (multisite-18, W14-A, 20 October 2026)
Before a hospital group's second facility goes live on one Orb appliance, the
go-live drill in docs/MULTI_SITE.md adds a second facility to a COPY of the
database that the isolated verify stack serves, and then signs in as staff of
both facilities to check that each sees only its own patients. This script
builds that second facility from a plan file, and it does so only through the
product:

  * the facility through POST /api/facilities, which runs the create gate
    (services.facility_identity.can_create_facility);
  * its staff through User Management (POST /api/admin/users), which writes
    each department-less account's home facility row with its audit row;
  * its patients through the admission form's route (POST
    /api/admissions/admit), signed in as the new facility's own doctor, and
    a medicine for each through the drug chart's route;
  * departments have no route yet, so they are added through
    services.department_master, the departments list's only writer, as the
    signed-in administrator.

Nothing is written straight into a table, so the script can do nothing the
product would refuse. In particular it cannot add a second facility while the
create gate refuses, and the gate refuses until every screen group has been
taught to keep two facilities' patients apart (facility_fence.
FACILITY_BLIND_SURFACES empty), the audit facility index is built, facility 1
has its GSTIN, the signed-in administrator holds group-wide access and
ORB_MULTI_SITE is on.

HONEST WORDING
  * It refuses to run on a hospital's appliance: the prod profile, and the
    database at Orb's default location, are refused. On a real appliance a
    facility is added on the Facilities screen.
  * Every request it makes carries USER_AGENT, so every audit row that
    records the request's user agent (the sign-ins, the accounts, the
    departments) says this script made it, on a verification copy. Rows
    that record no user agent (facility_created, the admission's own rows)
    name the signed-in account only.
  * The plan's names (facility, departments, wards, staff, patients) may not
    carry a label such as demo, test or sample: what staff see in the app
    reads like a hospital's own records, with nothing on screen calling itself
    a demonstration.
  * It records no observation. An admission set entered by a script would be
    filed as charted by a person (the manual-entry seam), so the new patients
    have none until a nurse charts them during the drill.
  * Passwords of the accounts it makes are never printed. They go to a file of
    their own (--accounts-out), created 0600, never written over; shred it
    after the drill.

WHAT --apply DOES, IN ORDER
  1. Checks everything it can without a session and stops, having written
     nothing, at any problem: the copy, the switch, the plan, that every name
     it would add is free across the whole group (ruling MS-R4 (a)), the
     accounts file, and the create gate for --admin (the function the create
     route runs). The prod profile and the database at Orb's default location
     are refused before the database is opened at all. This comes before the
     sign-in because a sign-in writes to the database it signs in to: an
     audit row, and a refused password counts toward the account's lockout.
     A copy this script refuses is never signed in to.
  2. Signs in as --admin through POST /api/auth/login (the password is typed
     at the console, and a TOTP code when the account has one).
  3. Checks it all again as that session, the create gate now through GET
     /api/facilities/create-check as the Facilities screen asks, and stops at
     any problem. Nothing of the plan is written then; the sign-in's own
     audit row stays, and the script says so.
  4. Lists the departments facility 1's staff and admissions use that the
     departments list lacks: once a second facility exists, a ward account
     whose department the list does not hold sees no patient at all.
  5. Adds the facility, its departments and its staff, then signs in as its
     doctors and admits its patients, each with any allergy and medicine the
     plan gives.
  6. Reads back that every new account and patient is at the new facility,
     that every other ward account still works at a facility, and that no
     patient has admissions at two facilities.

Usage (from server/, with the verify stack's environment: ORB_DB_ENCRYPTION_KEY,
ORB_JWT_SECRET, ORB_DEPLOYMENT_PROFILE=dev and ORB_MULTI_SITE=1; never the
shipping .env of a hospital's appliance):
    python scripts/seed_second_facility.py --example-plan > drill-plan.json
    python scripts/seed_second_facility.py --db COPY --plan drill-plan.json --admin ADM001
    python scripts/seed_second_facility.py --db COPY --plan drill-plan.json --admin ADM001 \\
        --accounts-out drill-accounts.txt --apply

Exit codes:
    0  ready (the check, the default), or seeded (--apply)
    1  not ready, or refused, with the reasons printed; or a step the product
       refused part way through (what was written is printed; take a fresh copy)
    2  the command line, the copy or the plan could not be read, or no console
       to type the password at
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

SERVER_DIR = Path(__file__).resolve().parent.parent

#: The database Orb opens when ORB_DB_PATH is not set. Never seeded.
DEFAULT_DB = SERVER_DIR / "orb.db"

#: Sent with every request, so the audit trail says who really made each row.
USER_AGENT = "Orb seed_second_facility.py (two-facility drill on a verification copy)"

MAIN_FACILITY_ID = 1

#: The accounts the drill's browser pass signs in as, at the new facility.
DRILL_ROLES = ("doctor", "nurse", "admin", "pharmacist", "cashier")
#: Roles that work at their department's facility. The others are bound to the
#: new facility by a home facility row (department-less by design, F109).
DEPARTMENT_ROLES = frozenset({"doctor", "nurse"})

#: The identity fields POST /api/facilities takes (routes/facility_routes.py,
#: FacilityIdentityBody). Every one is checked again by the product.
FACILITY_FIELDS = (
    "code",
    "name",
    "short_name",
    "legal_name",
    "address_line1",
    "address_line2",
    "city",
    "district",
    "state",
    "state_code",
    "pincode",
    "phone",
    "email",
    "gstin",
    "pan",
    "hfr_id",
    "abdm_hip_id",
    "nabh_accreditation_no",
    "nabh_valid_until",
    "clinical_establishment_reg_no",
    "timezone",
)

#: Words that would label what staff see in the app as something other than a
#: hospital's own records. A plan name holding one is refused.
LABEL_WORDS = frozenset(
    {
        "demo",
        "demonstration",
        "mock",
        "test",
        "testing",
        "dummy",
        "fake",
        "sample",
        "example",
        "placeholder",
        "drill",
        "sandbox",
        "lorem",
        "ipsum",
        "seed",
        "seeded",
        "synthetic",
    }
)

_WORD = re.compile(r"[a-z]+")
_STAFF_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
_PASSWORD_BYTES = 18


# ═════════════════════════════════════════════════════════════════════════════
#  Errors
# ═════════════════════════════════════════════════════════════════════════════


def _problem(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


class SeedRefused(RuntimeError):
    """Nothing of the plan was written. `problems` lists {code, message} for
    the operator. A sign-in made before the refusal is on the audit trail, as
    every sign-in is."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__("; ".join(p["message"] for p in problems))
        self.problems = problems


class SeedFailed(RuntimeError):
    """The product refused a step part way through. `done` lists, in words,
    what had been written by then (on the copy; take a fresh one)."""

    def __init__(self, message: str, done: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.done = list(done)


class SignInRefused(RuntimeError):
    """POST /api/auth/login did not issue a session."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


# ═════════════════════════════════════════════════════════════════════════════
#  The plan
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class StaffMember:
    id: str
    name: str
    role: str
    department: str | None


@dataclass(frozen=True)
class Medicine:
    drug_name: str
    dosage: str
    route: str
    frequency: str


@dataclass(frozen=True)
class PatientPlan:
    name: str
    department: str
    ward: str
    bed: str
    attending: str
    chief_complaint: str
    age: int | None = None
    sex: str | None = None
    mrn: str | None = None
    initial_diagnosis: str | None = None
    allergies: tuple[str, ...] = ()
    medicine: Medicine | None = None
    admission_source: str = "er"
    admission_type: str = "inpatient"


@dataclass(frozen=True)
class Plan:
    facility: dict[str, str]
    departments: tuple[str, ...]
    staff: tuple[StaffMember, ...]
    patients: tuple[PatientPlan, ...]

    def staff_ids(self) -> list[str]:
        return [m.id for m in self.staff]

    def wards(self) -> list[str]:
        seen: dict[str, str] = {}
        for p in self.patients:
            seen.setdefault(p.ward.casefold(), p.ward)
        return list(seen.values())


def _text(value: Any) -> str | None:
    """A plan value as text with its spacing folded, or None when blank."""
    if not isinstance(value, str):
        return None
    folded = " ".join(value.split())
    return folded or None


def label_words(value: str) -> list[str]:
    """The labelling words (LABEL_WORDS) a name holds, as whole words."""
    return sorted({w for w in _WORD.findall(value.casefold()) if w in LABEL_WORDS})


def _name_problems(where: str, value: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if "<" in value or ">" in value:
        out.append(_problem("plan_unfilled", f"{where} still holds a placeholder: fill it in."))
    words = label_words(value)
    if words:
        out.append(
            _problem(
                "plan_label_word",
                f"{where} '{value}' says {', '.join(words)}. Staff see this name in Orb, "
                "and nothing in Orb may present itself as a demonstration: choose a name "
                "a hospital would use.",
            )
        )
    return out


def _required_text(raw: dict, key: str, where: str, problems: list[dict[str, str]]) -> str:
    value = _text(raw.get(key))
    if value is None:
        problems.append(_problem("plan_incomplete", f"{where} needs '{key}'."))
        return ""
    problems.extend(_name_problems(f"{where} {key}", value))
    return value


def _optional_text(raw: dict, key: str, where: str, problems: list[dict[str, str]]) -> str | None:
    if raw.get(key) is not None and not isinstance(raw.get(key), str):
        problems.append(_problem("plan_invalid", f"{where} '{key}' must be text."))
        return None
    value = _text(raw.get(key))
    if value is not None:
        problems.extend(_name_problems(f"{where} {key}", value))
    return value


def _parse_facility(raw: Any, problems: list[dict[str, str]]) -> dict[str, str]:
    if not isinstance(raw, dict):
        problems.append(_problem("plan_incomplete", "The plan needs a 'facility' block."))
        return {}
    unknown = sorted(set(raw) - set(FACILITY_FIELDS))
    if unknown:
        problems.append(
            _problem(
                "plan_invalid",
                f"The facility block has fields Orb does not keep: {', '.join(unknown)}.",
            )
        )
    out: dict[str, str] = {}
    for key in FACILITY_FIELDS:
        if key not in raw or raw[key] is None:
            continue
        if not isinstance(raw[key], str):
            problems.append(_problem("plan_invalid", f"The facility's '{key}' must be text."))
            continue
        value = _text(raw[key])
        if value is None:
            continue
        problems.extend(_name_problems(f"The facility's {key}", value))
        out[key] = value
    for key in ("code", "name"):
        if key not in out:
            problems.append(_problem("plan_incomplete", f"The facility needs a '{key}'."))
    return out


def _parse_departments(raw: Any, problems: list[dict[str, str]]) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append(
            _problem("plan_incomplete", "The plan needs at least one department for the facility.")
        )
        return ()
    seen: dict[str, str] = {}
    for i, item in enumerate(raw, start=1):
        name = _text(item)
        if name is None:
            problems.append(_problem("plan_incomplete", f"Department {i} has no name."))
            continue
        problems.extend(_name_problems(f"Department {i}", name))
        if name.casefold() in seen:
            problems.append(_problem("plan_invalid", f"Department '{name}' is listed twice."))
            continue
        seen[name.casefold()] = name
    return tuple(seen.values())


def _parse_staff(
    raw: Any, departments: dict[str, str], problems: list[dict[str, str]]
) -> tuple[StaffMember, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append(_problem("plan_incomplete", "The plan needs the facility's staff."))
        return ()
    out: list[StaffMember] = []
    ids: set[str] = set()
    for i, item in enumerate(raw, start=1):
        where = f"Staff member {i}"
        if not isinstance(item, dict):
            problems.append(_problem("plan_invalid", f"{where} must be a block."))
            continue
        uid = _required_text(item, "id", where, problems)
        name = _required_text(item, "name", where, problems)
        role = _text(item.get("role")) or ""
        if role not in DRILL_ROLES:
            problems.append(
                _problem(
                    "plan_invalid",
                    f"{where} has role '{role}'. The drill's roles are {', '.join(DRILL_ROLES)}.",
                )
            )
        if uid and not _STAFF_ID.match(uid):
            problems.append(
                _problem(
                    "plan_invalid",
                    f"{where}'s sign-in id '{uid}' must be 2 to 32 letters, digits, - or _.",
                )
            )
        if uid.lower().startswith("bot_"):
            problems.append(_problem("plan_invalid", f"{where}'s id is kept for Orb's bots."))
        if uid and uid.casefold() in ids:
            problems.append(_problem("plan_invalid", f"Sign-in id '{uid}' is used twice."))
        ids.add(uid.casefold())
        dept = _text(item.get("department"))
        if role in DEPARTMENT_ROLES:
            if dept is None:
                problems.append(
                    _problem("plan_incomplete", f"{where} is a {role} and needs a department.")
                )
            elif dept.casefold() not in departments:
                problems.append(
                    _problem(
                        "plan_invalid",
                        f"{where}'s department '{dept}' is not one of the facility's departments "
                        "in the plan.",
                    )
                )
            else:
                dept = departments[dept.casefold()]
        elif dept is not None:
            problems.append(
                _problem(
                    "plan_invalid",
                    f"{where} is a {role}: that account has no department and works at the "
                    "facility through its home facility, so leave 'department' out.",
                )
            )
            dept = None
        out.append(StaffMember(id=uid, name=name, role=role, department=dept))
    missing = [r for r in DRILL_ROLES if r not in {m.role for m in out}]
    if missing:
        problems.append(
            _problem(
                "plan_incomplete",
                "The drill signs in as each of the facility's roles; the plan has no "
                f"{', '.join(missing)}.",
            )
        )
    return tuple(out)


def _parse_medicine(raw: Any, where: str, problems: list[dict[str, str]]) -> Medicine | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append(_problem("plan_invalid", f"{where}'s medicine must be a block."))
        return None
    drug = _required_text(raw, "drug_name", f"{where}'s medicine", problems)
    dosage = _required_text(raw, "dosage", f"{where}'s medicine", problems)
    frequency = _required_text(raw, "frequency", f"{where}'s medicine", problems)
    route = _optional_text(raw, "route", f"{where}'s medicine", problems) or "Oral"
    if not (drug and dosage and frequency):
        return None
    return Medicine(drug_name=drug, dosage=dosage, route=route, frequency=frequency)


def _parse_patients(
    raw: Any,
    departments: dict[str, str],
    doctors: set[str],
    problems: list[dict[str, str]],
) -> tuple[PatientPlan, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append(_problem("plan_incomplete", "The plan needs at least one patient."))
        return ()
    out: list[PatientPlan] = []
    beds: set[tuple[str, str]] = set()
    mrns: set[str] = set()
    for i, item in enumerate(raw, start=1):
        where = f"Patient {i}"
        if not isinstance(item, dict):
            problems.append(_problem("plan_invalid", f"{where} must be a block."))
            continue
        name = _required_text(item, "name", where, problems)
        dept = _required_text(item, "department", where, problems)
        if dept and dept.casefold() not in departments:
            problems.append(
                _problem(
                    "plan_invalid",
                    f"{where}'s department '{dept}' is not one of the facility's departments.",
                )
            )
        elif dept:
            dept = departments[dept.casefold()]
        ward = _required_text(item, "ward", where, problems)
        bed = _required_text(item, "bed", where, problems)
        attending = _text(item.get("attending")) or ""
        if attending not in doctors:
            problems.append(
                _problem(
                    "plan_invalid",
                    f"{where}'s attending '{attending}' is not one of the plan's doctors.",
                )
            )
        complaint = _required_text(item, "chief_complaint", where, problems)
        age = item.get("age")
        if age is not None and (not isinstance(age, int) or isinstance(age, bool)):
            problems.append(_problem("plan_invalid", f"{where}'s age must be a whole number."))
            age = None
        elif isinstance(age, int) and not 0 <= age <= 130:
            problems.append(_problem("plan_invalid", f"{where}'s age must be 0 to 130."))
            age = None
        allergies_raw = item.get("allergies") or []
        if not isinstance(allergies_raw, list):
            problems.append(_problem("plan_invalid", f"{where}'s allergies must be a list."))
            allergies_raw = []
        allergies = tuple(a for a in (_text(x) for x in allergies_raw) if a)
        for allergy in allergies:
            problems.extend(_name_problems(f"{where}'s allergy", allergy))
        mrn = _optional_text(item, "mrn", where, problems)
        if mrn is not None:
            if mrn.casefold() in mrns:
                problems.append(_problem("plan_invalid", f"Hospital number '{mrn}' is used twice."))
            mrns.add(mrn.casefold())
        if ward and bed:
            key = (ward.casefold(), bed.casefold())
            if key in beds:
                problems.append(_problem("plan_invalid", f"{where} shares a bed with another."))
            beds.add(key)
        out.append(
            PatientPlan(
                name=name,
                department=dept,
                ward=ward,
                bed=bed,
                attending=attending,
                chief_complaint=complaint,
                age=age if isinstance(age, int) else None,
                sex=_optional_text(item, "sex", where, problems),
                mrn=mrn,
                initial_diagnosis=_optional_text(item, "initial_diagnosis", where, problems),
                allergies=allergies,
                medicine=_parse_medicine(item.get("medicine"), where, problems),
                admission_source=_optional_text(item, "admission_source", where, problems) or "er",
                admission_type=(
                    _optional_text(item, "admission_type", where, problems) or "inpatient"
                ),
            )
        )
    return tuple(out)


def parse_plan(raw: Any) -> Plan:
    """The plan file's contents as a Plan. Raises SeedRefused listing every
    problem found (nothing is read from or written to the database here)."""
    problems: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        raise SeedRefused([_problem("plan_invalid", "The plan must be one JSON object.")])
    unknown = sorted(set(raw) - {"facility", "departments", "staff", "patients"})
    if unknown:
        problems.append(
            _problem("plan_invalid", f"The plan has sections this script does not read: {unknown}.")
        )
    facility = _parse_facility(raw.get("facility"), problems)
    departments = _parse_departments(raw.get("departments"), problems)
    by_key = {d.casefold(): d for d in departments}
    staff = _parse_staff(raw.get("staff"), by_key, problems)
    doctors = {m.id for m in staff if m.role == "doctor"}
    patients = _parse_patients(raw.get("patients"), by_key, doctors, problems)
    if problems:
        raise SeedRefused(problems)
    return Plan(facility=facility, departments=departments, staff=staff, patients=patients)


def load_plan(path: Path) -> Plan:
    """Read and parse a plan file. Raises OSError or ValueError when it cannot
    be read as JSON, SeedRefused when it reads but is not a usable plan."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_plan(raw)


def example_plan() -> dict[str, Any]:
    """A plan to start from. The facility's identity is the satellite's own
    and is left for the operator; the people and departments are invented,
    with names that hold no word a clinician types in a question."""
    dept = "Hadrel General Medicine"
    return {
        "facility": {
            "code": "SAT1",
            "name": "<the satellite's name, as staff and invoices show it>",
            "legal_name": "<the group's legal name, the same entity as facility 1>",
            "gstin": "<the satellite's GSTIN: the same PAN as facility 1's>",
            "address_line1": "<street address>",
            "city": "<city>",
            "state": "<state>",
            "pincode": "<six-digit PIN code>",
        },
        "departments": [dept],
        "staff": [
            {"id": "S1DOC01", "name": "Ravindar Okonkwo", "role": "doctor", "department": dept},
            {"id": "S1NUR01", "name": "Meherin Saldavka", "role": "nurse", "department": dept},
            {"id": "S1ADM01", "name": "Tovrin Acharyulu", "role": "admin"},
            {"id": "S1PHA01", "name": "Lusenda Pereiro", "role": "pharmacist"},
            {"id": "S1CSH01", "name": "Abhiroop Venkatoor", "role": "cashier"},
        ],
        "patients": [
            {
                "name": "Ishvani Rauthmore",
                "age": 58,
                "sex": "F",
                "department": dept,
                "ward": "Hadrel Ward A",
                "bed": "HA-01",
                "attending": "S1DOC01",
                "chief_complaint": "Fever and cough for four days",
                "initial_diagnosis": "Community acquired pneumonia",
                "allergies": ["Sulfonamides"],
                "medicine": {
                    "drug_name": "Amoxicillin",
                    "dosage": "500 mg",
                    "route": "Oral",
                    "frequency": "TDS",
                },
            },
            {
                "name": "Okelo Brannavik",
                "age": 64,
                "sex": "M",
                "department": dept,
                "ward": "Hadrel Ward A",
                "bed": "HA-02",
                "attending": "S1DOC01",
                "chief_complaint": "Swelling and pain of the left foot",
                "initial_diagnosis": "Type 2 diabetes with foot ulcer",
                "medicine": {
                    "drug_name": "Paracetamol",
                    "dosage": "650 mg",
                    "route": "Oral",
                    "frequency": "QDS",
                },
            },
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  The product, called in this process
# ═════════════════════════════════════════════════════════════════════════════


class Product:
    """The product's own routes, called in this process through its own
    FastAPI app (no network). Every call carries USER_AGENT and, once signed
    in, the session's bearer token; the browser's session cookie is never
    kept, so each call is the account the token names."""

    def __init__(self, client: Any, token: str | None = None) -> None:
        self.client = client
        self.token = token

    def signed_in(self, token: str) -> Product:
        return Product(self.client, token)

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        sent = {"User-Agent": USER_AGENT}
        if self.token:
            sent["Authorization"] = f"Bearer {self.token}"
        if headers:
            sent.update(headers)
        resp = self.client.request(method, path, json=body, headers=sent)
        cookies = getattr(self.client, "cookies", None)
        if cookies is not None:
            cookies.clear()
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return int(resp.status_code), data


def product_client() -> Any:
    """A client for the product's app in this process. Imported here, after
    main() has pointed ORB_DB_PATH at the copy."""
    import main as orb_main
    from fastapi.testclient import TestClient

    return TestClient(orb_main.app, base_url="http://localhost", raise_server_exceptions=False)


def _detail_words(data: Any) -> str:
    """What a refusal said, in its own words."""
    if isinstance(data, dict):
        detail = data.get("detail", data)
        if isinstance(detail, dict):
            words = detail.get("message") or detail.get("error") or json.dumps(detail)
            reasons = detail.get("reasons") or detail.get("errors")
            if isinstance(reasons, list) and reasons:
                listed = "; ".join(
                    str(r.get("message") or r) if isinstance(r, dict) else str(r) for r in reasons
                )
                return f"{words} ({listed})"
            return str(words)
        return str(detail)
    return str(data)[:500]


def sign_in(product: Product, user_id: str, password: str, totp_code: str | None = None) -> str:
    """A session token for the account, through POST /api/auth/login, exactly
    as the sign-in screen gets one. Raises SignInRefused."""
    body: dict[str, Any] = {"employee_id": user_id, "password": password}
    if totp_code:
        body["totp_code"] = totp_code
    status, data = Product(product.client).call("POST", "/api/auth/login", body)
    if status == 200 and isinstance(data, dict) and data.get("token"):
        return str(data["token"])
    raise SignInRefused(status, _detail_words(data))


def session_user(product: Product) -> dict[str, Any]:
    """The signed-in account as the server sees it (GET /api/auth/me)."""
    status, data = product.call("GET", "/api/auth/me")
    if status != 200 or not isinstance(data, dict):
        raise SeedRefused(
            [_problem("not_signed_in", f"The session was not accepted: {_detail_words(data)}")]
        )
    return {"id": data.get("id"), "role": data.get("role"), "department": data.get("department")}


# ═════════════════════════════════════════════════════════════════════════════
#  Readiness (reads only)
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class Readiness:
    """What a check found. Ready when `problems` is empty."""

    problems: list[dict[str, str]]
    main_departments: list[str]
    gate: list[dict[str, str]]
    facility_count: int

    @property
    def ready(self) -> bool:
        return not self.problems


_REQUIRED_TABLES = (
    "facilities",
    "departments",
    "user_facilities",
    "enterprise_access_grants",
    "users",
    "patients",
    "admissions",
)


def environment_problems() -> list[dict[str, str]]:
    """The profile, the switch and the sign-in policy the seed needs."""
    import config

    problems: list[dict[str, str]] = []
    if str(getattr(config, "DEPLOYMENT_PROFILE", "prod")).lower() == "prod":
        problems.append(
            _problem(
                "prod_profile",
                "This is the prod profile, which a hospital's appliance runs. This script "
                "seeds a verification copy only; on a hospital's appliance a facility is "
                "added on the Facilities screen. Run the verify stack's environment "
                "(ORB_DEPLOYMENT_PROFILE=dev).",
            )
        )
    try:
        db_path = Path(config.DB_PATH).resolve()
    except OSError:
        db_path = Path(config.DB_PATH)
    try:
        # A second name for the same file (a hard link) is not a copy of it.
        same_file = DEFAULT_DB.exists() and db_path.exists() and db_path.samefile(DEFAULT_DB)
    except OSError:
        same_file = False
    if db_path == DEFAULT_DB.resolve() or same_file:
        problems.append(
            _problem(
                "default_database",
                f"{db_path} is the database Orb opens by default. Seed a copy made with "
                "scripts/backup_db.py, never the database itself.",
            )
        )
    if not config.feature_enabled("multi_site"):
        problems.append(
            _problem(
                "switch_off",
                "The multi-site switch (ORB_MULTI_SITE) is off in this environment, so no "
                "facility can be added. Set ORB_MULTI_SITE=1 here and on the verify stack.",
            )
        )
    if "doctor" in getattr(config, "MFA_REQUIRED_ROLES", []):
        problems.append(
            _problem(
                "doctor_mfa_required",
                "ORB_MFA_REQUIRED_ROLES includes doctor, so the new facility's doctor could "
                "not sign in to admit its patients before enrolling a second factor. Run the "
                "seed with doctor left out of ORB_MFA_REQUIRED_ROLES on the verify stack.",
            )
        )
    return problems


#: Problems that mean the database is not a verification copy at all. It is
#: then not opened, even to read, and never signed in to.
NOT_A_COPY = frozenset({"prod_profile", "default_database"})


def not_a_copy(problems: list[dict[str, str]]) -> bool:
    """Whether `problems` say this is a hospital's own database, not a copy."""
    return any(p.get("code") in NOT_A_COPY for p in problems)


def _has_table(db: Any, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _schema_problems(db: Any) -> list[dict[str, str]]:
    missing = [t for t in _REQUIRED_TABLES if not _has_table(db, t)]
    if not missing:
        return []
    return [
        _problem(
            "schema_out_of_date",
            f"This copy has no {', '.join(missing)} table, so it has not been brought up to "
            "date. Start the verify stack on it once, then retry.",
        )
    ]


def _used_wards(db: Any) -> set[str]:
    wards: set[str] = set()
    for sql in (
        "SELECT DISTINCT LOWER(TRIM(ward)) FROM admissions WHERE ward IS NOT NULL",
        "SELECT DISTINCT LOWER(TRIM(ward)) FROM patients WHERE ward IS NOT NULL",
    ):
        wards.update(r[0] for r in db.execute(sql).fetchall() if r[0])
    if _has_table(db, "wards"):
        cols = {r[1] for r in db.execute("PRAGMA table_info(wards)").fetchall()}
        for column in ("code", "name"):
            if column in cols:
                wards.update(
                    r[0]
                    for r in db.execute(
                        f"SELECT DISTINCT LOWER(TRIM({column})) FROM wards "
                        f"WHERE {column} IS NOT NULL"
                    ).fetchall()
                    if r[0]
                )
    return wards


def department_in_use_on(db: Any, name: str) -> list[str]:
    """The tables whose department columns (department, or *_department)
    hold this name, ignoring capitals and outer spaces, plus Relay department
    rooms. The rule services.department_master.department_references applies,
    read here so that a virtual table this connection cannot open (the
    knowledge corpus's vector tables, which only the knowledge lane loads the
    extension for) is passed over instead of failing the check."""
    wanted = name.strip().lower()
    found: list[str] = []
    for table, sql in db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'departments' ORDER BY name"
    ).fetchall():
        if str(sql or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            continue
        try:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")').fetchall()]
        except Exception:
            continue
        for column in columns:
            if column != "department" and not column.endswith("_department"):
                continue
            hit = db.execute(
                f'SELECT 1 FROM "{table}" WHERE LOWER(TRIM("{column}")) = ? LIMIT 1', (wanted,)
            ).fetchone()
            if hit is not None:
                found.append(f"{table}.{column}")
        if table == "channels" and {"name", "type"} <= set(columns):
            hit = db.execute(
                "SELECT 1 FROM channels WHERE type = 'department' AND LOWER(TRIM(name)) = ? "
                "LIMIT 1",
                (wanted,),
            ).fetchone()
            if hit is not None:
                found.append("channels.name")
    return found


def plan_problems_on(db: Any, plan: Plan) -> list[dict[str, str]]:
    """Whether every name the plan would add is free across the whole group
    (ruling MS-R4 (a): a department, ward or account belongs to one facility)."""
    from services.site_config_service import looks_like_bed_prefix

    from services import department_master

    problems: list[dict[str, str]] = []
    code = plan.facility.get("code", "")
    if (
        code
        and db.execute("SELECT 1 FROM facilities WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
    ):
        problems.append(
            _problem(
                "facility_exists",
                f"This copy already holds facility {code.upper()}. Take a fresh copy for a "
                "new drill.",
            )
        )
    for name in plan.departments:
        clean = department_master.normalise_name(name) or name
        if looks_like_bed_prefix(clean):
            problems.append(
                _problem(
                    "department_looks_like_a_bed",
                    f"'{clean}' looks like a bed or ward label, not a department.",
                )
            )
        listed = db.execute(
            "SELECT 1 FROM departments WHERE LOWER(TRIM(name)) = ?", (clean.casefold(),)
        ).fetchone()
        used = department_in_use_on(db, clean)
        if listed is not None or used:
            problems.append(
                _problem(
                    "department_taken",
                    f"Department '{clean}' is already on this copy's departments list or in use. "
                    "A department name belongs to one facility of the group: choose another.",
                )
            )
    used_wards = _used_wards(db)
    for ward in plan.wards():
        if ward.strip().casefold() in used_wards:
            problems.append(
                _problem(
                    "ward_taken",
                    f"Ward '{ward}' is already in use on this copy. Ward names belong to one "
                    "facility of the group: choose another.",
                )
            )
    for uid in plan.staff_ids():
        if db.execute("SELECT 1 FROM users WHERE LOWER(id) = LOWER(?)", (uid,)).fetchone():
            problems.append(
                _problem("account_exists", f"There is already an account '{uid}' on this copy.")
            )
    for patient in plan.patients:
        if (
            patient.mrn
            and db.execute(
                "SELECT 1 FROM patients WHERE LOWER(mrn) = LOWER(?)", (patient.mrn,)
            ).fetchone()
        ):
            problems.append(
                _problem(
                    "mrn_in_use",
                    f"Hospital number '{patient.mrn}' is already on another record here.",
                )
            )
    return problems


#: Where a department name is in use: (label, table, the names). As
#: facility_service.facility_report reads them, but without patient portal
#: logins, whose department column holds the patient's ward (an admission
#: writes it there), and a ward is never a department.
_DEPARTMENT_SOURCES = (
    (
        "staff accounts",
        "users",
        "SELECT DISTINCT department FROM users WHERE COALESCE(is_active, 1) = 1 "
        "AND role != 'patient' AND department IS NOT NULL AND TRIM(department) != ''",
    ),
    (
        "admissions",
        "admissions",
        "SELECT DISTINCT department FROM admissions "
        "WHERE department IS NOT NULL AND TRIM(department) != ''",
    ),
    (
        "wards",
        "wards",
        "SELECT DISTINCT department FROM wards "
        "WHERE department IS NOT NULL AND TRIM(department) != ''",
    ),
)


def unlisted_departments_on(db: Any) -> list[dict[str, str]]:
    """[{where, name}] for every department name staff, admissions or wards
    use that no facility's list holds, compared the way the facility scope
    resolver compares them (outer spaces and capitals ignored)."""
    listed = {
        str(r[0]).strip().casefold()
        for r in db.execute("SELECT name FROM departments").fetchall()
        if r[0] is not None
    }
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for where, table, sql in _DEPARTMENT_SOURCES:
        if not _has_table(db, table):
            continue
        for (name,) in db.execute(sql).fetchall():
            key = str(name).strip().casefold()
            if key in listed or (where, key) in seen:
                continue
            seen.add((where, key))
            out.append({"where": where, "name": str(name).strip()})
    return out


def main_departments_on(db: Any) -> tuple[list[str], list[dict[str, str]]]:
    """Departments facility 1's staff, admissions and wards use that the
    departments list lacks (each to be listed at facility 1), and any that
    could not be listed as written."""
    from services.site_config_service import looks_like_bed_prefix

    from services import department_master, facility_service

    report = facility_service.facility_report(db)
    names: dict[str, str] = {}
    for row in unlisted_departments_on(db):
        names.setdefault(row["name"].casefold(), row["name"])
    problems: list[dict[str, str]] = []
    for name in names.values():
        if (
            looks_like_bed_prefix(name)
            or len(name) > department_master.MAX_NAME
            or department_master.normalise_name(name) != name
        ):
            problems.append(
                _problem(
                    "main_department_unlistable",
                    f"Facility 1's staff or admissions use the department '{name}', which the "
                    "departments list cannot hold as written. Correct those records on the copy "
                    "first: once a second facility exists, an account whose department is on "
                    "no list sees no patient.",
                )
            )
    if report.get("patients_at_several_facilities"):
        problems.append(
            _problem(
                "patients_at_several_facilities",
                f"{report['patients_at_several_facilities']} patient(s) already have admissions "
                "at more than one facility on this copy.",
            )
        )
    return sorted(names.values(), key=str.casefold), problems


def _gate_from_product(admin: Product) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(the create gate's reasons, problems reaching it), through GET
    /api/facilities/create-check, as the Facilities screen asks."""
    status, data = admin.call("GET", "/api/facilities/create-check")
    if status == 200 and isinstance(data, dict):
        reasons = [
            _problem(str(r.get("code")), str(r.get("message")))
            for r in data.get("reasons") or []
            if isinstance(r, dict)
        ]
        return reasons, []
    return [], [
        _problem(
            "create_check_unavailable",
            f"The create check answered {status}: {_detail_words(data)}",
        )
    ]


def _gate_for_account(db: Any, admin_id: str | None) -> list[dict[str, str]]:
    """The create gate's reasons for an account, read without signing in (the
    check). The same function the create route runs."""
    from services import facility_identity

    user: dict[str, Any] = {}
    if admin_id:
        row = db.execute(
            "SELECT id, role, department, COALESCE(is_active, 1) AS is_active FROM users "
            "WHERE id = ?",
            (admin_id,),
        ).fetchone()
        if row is not None:
            user = {"id": row["id"], "role": row["role"], "department": row["department"]}
    return list(facility_identity.can_create_facility(user, db=db))


def readiness(
    plan: Plan,
    *,
    admin: Product | None = None,
    admin_id: str | None = None,
    accounts_out: Path | None = None,
) -> Readiness:
    """Everything the seed needs, read without writing. With `admin` (a
    signed-in session) the create gate is asked through the product; without
    it, through the same function for the account `admin_id` names. On the
    prod profile, or at Orb's default database, the database is not opened."""
    problems = environment_problems()
    gate: list[dict[str, str]] = []
    main_departments: list[str] = []
    count = 0
    if not_a_copy(problems):
        return Readiness(
            problems=problems, main_departments=main_departments, gate=gate, facility_count=count
        )

    from database import db_session

    from services import facility_service

    with db_session() as db:
        schema = _schema_problems(db)
        problems.extend(schema)
        if not schema:
            count = facility_service.facility_count(db)
            problems.extend(plan_problems_on(db, plan))
            main_departments, main_problems = main_departments_on(db)
            problems.extend(main_problems)
            if admin_id:
                row = db.execute(
                    "SELECT role, COALESCE(is_active, 1) AS is_active FROM users WHERE id = ?",
                    (admin_id,),
                ).fetchone()
                if row is None or row["role"] != "admin" or not row["is_active"]:
                    problems.append(
                        _problem(
                            "not_an_administrator",
                            f"'{admin_id}' is not an active administrator account on this copy.",
                        )
                    )
            if admin is None:
                gate = _gate_for_account(db, admin_id)
    if not schema and admin is not None:
        gate, reach = _gate_from_product(admin)
        problems.extend(reach)
    problems.extend(
        _problem(f"create_gate_{g['code']}", f"The create gate refuses: {g['message']}")
        for g in gate
    )
    if accounts_out is not None:
        if accounts_out.exists():
            problems.append(
                _problem(
                    "accounts_file_exists",
                    f"{accounts_out} already exists. The accounts file is never written over.",
                )
            )
        elif not accounts_out.parent.is_dir():
            problems.append(
                _problem("accounts_folder_missing", f"There is no folder {accounts_out.parent}.")
            )
    return Readiness(
        problems=problems, main_departments=main_departments, gate=gate, facility_count=count
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Applying the plan
# ═════════════════════════════════════════════════════════════════════════════


@contextmanager
def _accounts_file(path: Path, db_label: str) -> Iterator[IO[str]]:
    """The accounts file, created 0600 and never over an existing file."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(
            "# Sign-in ids and passwords of the accounts seed_second_facility.py made on the\n"
            f"# verification copy {db_label} at {datetime.now(UTC).isoformat(timespec='seconds')}.\n"
            "# For the two-facility drill only. Shred this file when the drill is over.\n"
            "# id\trole\tpassword\n"
        )
        fh.flush()
        yield fh


def _request_meta() -> dict[str, str]:
    return {"request_path": "seed_second_facility.py", "user_agent": USER_AGENT}


def _list_department(name: str, facility_id: int, actor: dict[str, Any]) -> dict[str, Any]:
    from services import department_master

    try:
        return department_master.add_department(
            name, facility_id, actor=actor, request_meta=_request_meta()
        )
    except department_master.DepartmentError as e:
        raise SeedFailed(f"Listing department '{name}' was refused: {e.message}", []) from e


def _admission_body(patient: PatientPlan) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": patient.name,
        "admission_source": patient.admission_source,
        "admission_type": patient.admission_type,
        "department": patient.department,
        "ward": patient.ward,
        "bed": patient.bed,
        "attending_doctor_id": patient.attending,
        "chief_complaint": patient.chief_complaint,
        "allergies": list(patient.allergies),
    }
    for key in ("age", "sex", "mrn", "initial_diagnosis"):
        value = getattr(patient, key)
        if value is not None:
            body[key] = value
    return body


def verify_seeded(
    facility_id: int, staff_ids: list[str], patient_ids: list[str]
) -> list[dict[str, str]]:
    """Read back what the seed made. Problems, or [] when every new account
    and patient is at the new facility, every other ward account still works
    at a facility, and no patient has admissions at two facilities."""
    import permissions
    from database import db_session

    from services import facility_service

    facility_service.invalidate_cache()
    problems: list[dict[str, str]] = []
    with db_session() as db:
        report = facility_service.facility_report(db)
        if report["patients_at_several_facilities"]:
            problems.append(
                _problem(
                    "patients_at_several_facilities",
                    f"{report['patients_at_several_facilities']} patient(s) have admissions at "
                    "more than one facility.",
                )
            )
        for unlisted in unlisted_departments_on(db):
            problems.append(
                _problem(
                    "department_not_listed",
                    f"The department '{unlisted['name']}' ({unlisted['where']}) is on no "
                    "facility's list.",
                )
            )
        wanted = frozenset({facility_id})
        for uid in staff_ids:
            row = db.execute(
                "SELECT id, role, department FROM users WHERE id = ?", (uid,)
            ).fetchone()
            scope = (
                facility_service.facility_scope_for(dict(row), db=db) if row is not None else None
            )
            if scope != wanted:
                problems.append(
                    _problem(
                        "account_not_at_facility",
                        f"Account '{uid}' works at {sorted(scope) if scope else scope}, not at "
                        f"facility {facility_id}.",
                    )
                )
        for pid in patient_ids:
            got = db.execute(
                f"SELECT {permissions.FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = ?", (pid,)
            ).fetchone()
            if got is None or got[0] != facility_id:
                problems.append(
                    _problem(
                        "patient_not_at_facility",
                        f"Patient {pid} is at facility {got[0] if got else None}, not "
                        f"{facility_id}.",
                    )
                )
        for row in db.execute(
            "SELECT id, role, department FROM users WHERE COALESCE(is_active, 1) = 1 "
            "AND role != 'patient' AND department IS NOT NULL AND TRIM(department) != ''"
        ).fetchall():
            if row["id"] in staff_ids:
                continue
            if facility_service.facility_scope_for(dict(row), db=db) == frozenset():
                problems.append(
                    _problem(
                        "account_at_no_facility",
                        f"Account '{row['id']}' has the department '{row['department']}', which "
                        "no facility lists, so it sees no patient.",
                    )
                )
    return problems


def apply_plan(
    plan: Plan,
    product: Product,
    *,
    admin_token: str,
    accounts_out: Path,
    db_label: str = "",
) -> dict[str, Any]:
    """Seed the plan through the product, signed in with `admin_token` (an
    administrator holding group-wide access). Raises SeedRefused, having
    written nothing, unless every check passes and the create gate lets the
    facility through; SeedFailed when the product refuses a later step."""
    from services.drug_chart import drug_chart_enabled

    admin = product.signed_in(admin_token)
    actor = session_user(admin)
    ready = readiness(plan, admin=admin, admin_id=actor.get("id"), accounts_out=accounts_out)
    if not ready.ready:
        raise SeedRefused(ready.problems)

    done: list[str] = []
    accounts: list[dict[str, str]] = []
    patients: list[dict[str, Any]] = []
    medicines = 0
    with _accounts_file(accounts_out, db_label or "in use") as accounts_fh:
        try:
            for name in ready.main_departments:
                _list_department(name, MAIN_FACILITY_ID, actor)
                done.append(f"listed department '{name}' at facility 1")

            status, made = admin.call("POST", "/api/facilities", dict(plan.facility))
            if status != 200 or not isinstance(made, dict) or "id" not in made:
                raise SeedFailed(f"Adding the facility was refused: {_detail_words(made)}", done)
            facility_id = int(made["id"])
            done.append(f"added facility {made.get('code')} (id {facility_id})")

            for name in plan.departments:
                _list_department(name, facility_id, actor)
                done.append(f"listed department '{name}' at facility {made.get('code')}")

            passwords: dict[str, str] = {}
            for member in plan.staff:
                password = secrets.token_urlsafe(_PASSWORD_BYTES)
                body: dict[str, Any] = {
                    "id": member.id,
                    "name": member.name,
                    "role": member.role,
                    "password": password,
                }
                if member.department is not None:
                    body["department"] = member.department
                else:
                    body["facility_id"] = facility_id
                status, data = admin.call("POST", "/api/admin/users", body)
                if status != 201:
                    raise SeedFailed(
                        f"Adding the account '{member.id}' was refused: {_detail_words(data)}",
                        done,
                    )
                accounts_fh.write(f"{member.id}\t{member.role}\t{password}\n")
                accounts_fh.flush()
                passwords[member.id] = password
                accounts.append({"id": member.id, "role": member.role})
                done.append(f"added the {member.role} account '{member.id}'")

            doctors: dict[str, Product] = {}
            for patient in plan.patients:
                doctor = doctors.get(patient.attending)
                if doctor is None:
                    try:
                        token = sign_in(product, patient.attending, passwords[patient.attending])
                    except SignInRefused as e:
                        raise SeedFailed(
                            f"The doctor '{patient.attending}' could not sign in: {e.detail}", done
                        ) from e
                    doctor = doctors[patient.attending] = product.signed_in(token)
                status, admitted = doctor.call(
                    "POST",
                    "/api/admissions/admit",
                    _admission_body(patient),
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
                if status not in (200, 201) or not isinstance(admitted, dict):
                    raise SeedFailed(
                        f"Admitting '{patient.name}' was refused: {_detail_words(admitted)}", done
                    )
                pid = str(admitted["patient_id"])
                patients.append(
                    {
                        "id": pid,
                        "admission_id": admitted.get("admission_id"),
                        "mrn": admitted.get("mrn"),
                        "name": patient.name,
                    }
                )
                done.append(f"admitted patient {pid}")
                if patient.medicine is not None and drug_chart_enabled():
                    status, charted = doctor.call(
                        "POST",
                        f"/api/vigil/patients/{pid}/medications",
                        {
                            "drug_name": patient.medicine.drug_name,
                            "dosage": patient.medicine.dosage,
                            "route": patient.medicine.route,
                            "frequency": patient.medicine.frequency,
                        },
                        headers={"Idempotency-Key": str(uuid.uuid4())},
                    )
                    if status != 200:
                        # A 428 is the allergy or interaction screen asking for
                        # an acknowledgement. The seed never gives one: choose a
                        # medicine the patient's chart does not object to.
                        raise SeedFailed(
                            f"Charting {patient.medicine.drug_name} for patient {pid} was "
                            f"refused ({status}): {_detail_words(charted)}",
                            done,
                        )
                    medicines += 1
                    done.append(f"charted {patient.medicine.drug_name} for patient {pid}")
        except SeedFailed as failed:
            raise SeedFailed(failed.message, done) from failed
        except Exception as e:
            raise SeedFailed(f"Stopped on an unexpected error: {e!r}", done) from e

    checks = verify_seeded(
        facility_id, [m.id for m in plan.staff], [str(p["id"]) for p in patients]
    )
    return {
        "facility": {"id": facility_id, "code": made.get("code"), "name": made.get("name")},
        "departments_listed_at_facility_1": list(ready.main_departments),
        "departments": list(plan.departments),
        "accounts": accounts,
        "patients": patients,
        "medicines": medicines,
        "accounts_file": str(accounts_out),
        "checks": checks,
        "done": done,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  The command line
# ═════════════════════════════════════════════════════════════════════════════


def _print_problems(title: str, problems: list[dict[str, str]]) -> None:
    sys.stdout.flush()
    print(title, file=sys.stderr)
    for p in problems:
        print(f"  - {p['message']}", file=sys.stderr)


def _point_at_copy(db: Path) -> Path:
    """Use the copy for everything this process opens. Set before any of Orb's
    modules is imported, because config reads ORB_DB_PATH once."""
    resolved = db.expanduser().resolve()
    os.environ["ORB_DB_PATH"] = str(resolved)
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))
    return resolved


def _console_sign_in(product: Product, admin_id: str) -> str:
    """Sign in at the console: the password, then a TOTP code if asked."""
    password = getpass.getpass(f"Password for {admin_id}: ")
    try:
        return sign_in(product, admin_id, password)
    except SignInRefused as e:
        if e.status == 401 and e.detail == "mfa_required":
            code = input(f"TOTP code for {admin_id}: ").strip()
            return sign_in(product, admin_id, password, totp_code=code)
        raise


def _print_check(plan: Plan, ready: Readiness, db: Path) -> None:
    print(f"Copy: {db}.")
    if not not_a_copy(ready.problems):
        print(f"Facilities on record: {ready.facility_count}.")
    code = plan.facility.get("code", "").upper()
    print(
        f"Plan: facility {code} with {len(plan.departments)} department(s), "
        f"{len(plan.staff)} account(s) and {len(plan.patients)} patient(s)."
    )
    if ready.main_departments:
        print("Listed at facility 1 first (in use there, on no list yet):")
        for name in ready.main_departments:
            print(f"  - {name}")
    if ready.ready:
        print("Ready: the create gate lets a second facility through and every check passes.")


def _print_report(report: dict[str, Any]) -> None:
    fac = report["facility"]
    print(f"Added facility {fac['code']} (id {fac['id']}): {fac['name']}.")
    for name in report["departments_listed_at_facility_1"]:
        print(f"  Listed at facility 1: {name}")
    for name in report["departments"]:
        print(f"  Department: {name}")
    for account in report["accounts"]:
        print(f"  Account: {account['id']} ({account['role']})")
    for patient in report["patients"]:
        print(f"  Patient: {patient['id']} {patient['mrn']} {patient['name']}")
    print(f"Medicines charted: {report['medicines']}.")
    print(f"Passwords are in {report['accounts_file']} (0600). Shred it after the drill.")
    if report["checks"]:
        _print_problems("Read-back found problems:", report["checks"])
    else:
        print("Read-back: every new account and patient is at the new facility.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a second facility on a verification copy, through the product."
    )
    parser.add_argument("--db", type=Path, help="the verification copy (made with backup_db.py)")
    parser.add_argument("--plan", type=Path, help="the plan file (JSON)")
    parser.add_argument("--admin", help="the administrator with group-wide access to sign in as")
    parser.add_argument("--accounts-out", type=Path, help="where the new accounts' passwords go")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check only; write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="seed the copy")
    mode.add_argument("--example-plan", action="store_true", help="print a plan to start from")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    if args.example_plan:
        print(json.dumps(example_plan(), indent=2))
        return 0
    if args.db is None or args.plan is None:
        parser.error("--db and --plan are required")
    if not args.db.expanduser().is_file():
        print(f"There is no database file at {args.db}.", file=sys.stderr)
        return 2
    if args.apply and (not args.admin or args.accounts_out is None):
        parser.error("--apply needs --admin and --accounts-out")
    db = _point_at_copy(args.db)
    try:
        plan = load_plan(args.plan)
    except (OSError, ValueError) as e:
        print(f"The plan could not be read: {e}", file=sys.stderr)
        return 2
    except SeedRefused as refused:
        _print_problems("The plan needs correcting:", refused.problems)
        return 2

    try:
        if not args.apply:
            ready = readiness(plan, admin_id=args.admin)
            if args.json:
                print(json.dumps({**ready.__dict__, "ready": ready.ready}, indent=2))
            else:
                _print_check(plan, ready, db)
                if not ready.ready:
                    _print_problems("Not ready:", ready.problems)
            return 0 if ready.ready else 1

        if not sys.stdin.isatty():
            print(
                "Run this at the console: it asks for the administrator's password.",
                file=sys.stderr,
            )
            return 2
        accounts_out = args.accounts_out.expanduser().resolve()
        # Every check that needs no session runs before the sign-in, because a
        # sign-in writes to the database it signs in to: an audit row, and a
        # refused password counts toward the account's lockout (on a
        # hospital's appliance, its administrator's). A copy this script
        # refuses is never signed in to.
        ready = readiness(plan, admin_id=args.admin, accounts_out=accounts_out)
        if not ready.ready:
            _print_problems("Refused. Nothing was written:", ready.problems)
            return 1
        product = Product(product_client())
        try:
            token = _console_sign_in(product, args.admin)
        except SignInRefused as e:
            print(f"Sign-in refused ({e.status}): {e.detail}", file=sys.stderr)
            print(
                "The attempt is on the copy's audit trail, and a refused password counts "
                "toward the account's lockout. Nothing else was written.",
                file=sys.stderr,
            )
            return 1
        report = apply_plan(
            plan,
            product,
            admin_token=token,
            accounts_out=accounts_out,
            db_label=str(db),
        )
    except SeedRefused as refused:
        _print_problems(
            "Refused after signing in. Nothing of the plan was written; the sign-in "
            "itself is on the copy's audit trail:",
            refused.problems,
        )
        return 1
    except SeedFailed as failed:
        print(f"Stopped: {failed.message}", file=sys.stderr)
        if failed.done:
            _print_problems(
                "Already written on the copy (take a fresh copy before trying again):",
                [_problem("done", d) for d in failed.done],
            )
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return 1 if report["checks"] else 0


if __name__ == "__main__":
    sys.exit(main())
