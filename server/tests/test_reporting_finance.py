"""Real-schema, synthetic-only reporting and finance boundary regressions."""
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import database
from database import db_session
from db_backend import discard_pooled
from services import billing_masters as masters, billing_service as billing, payments_service as payments
from services import facility_service as facilities
from services import reporting_service as reporting, finance_tools_service as finance
from services import reporting_finance_common as common
from services.hospital_time import hospital_today
from services.reporting_finance_common import WorkbenchError

ADMIN = {"id": "RF-ADMIN"}
APPROVER = {"id": "RF-APPROVER"}
DOCTOR = {"id": "RF-DOCTOR"}
PERIOD = lambda: {"facility_id": 1, "from_day": hospital_today().isoformat(), "to_day": hospital_today().isoformat()}


@pytest.fixture(autouse=True)
def owned_database(tmp_path, monkeypatch):
    """Each test owns the complete DB; no prior suite census or site settings."""
    path = tmp_path / "orb_test_reporting_finance.db"
    monkeypatch.setenv("ORB_DB_PATH", str(path))
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "REPORTING_ENABLED", True)
    monkeypatch.setattr(config, "FINANCE_TOOLS_ENABLED", True)
    facilities.invalidate_cache()
    database.init_db()
    with db_session() as db:
        db.executemany("INSERT INTO users(id,name,role,department,password_hash) VALUES (?,?,?,?,?)", [
            ("RF-ADMIN", "Synthetic creator", "admin", None, "not-a-login"),
            ("RF-APPROVER", "Synthetic approver", "admin", None, "not-a-login"),
            ("RF-DOCTOR", "Synthetic doctor", "doctor", "General Medicine", "not-a-login"),
            ("RF-NURSE", "Synthetic nurse", "nurse", "ICU", "not-a-login"),
        ])
        db.executemany("INSERT INTO patients(id,name,mrn,sex,age,status) VALUES (?,?,?,?,?,'registered')", [
            ("RF-P1", "=Synthetic Patient", "RF-1", "female", 44),
            ("RF-P2", "Other Synthetic", "RF-2", "male", 71),
        ])
    yield
    discard_pooled(database.get_db())
    facilities.invalidate_cache()


def count(table):
    with db_session() as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def source(*, patient="RF-P1", gross=100001, discount=10001):
    masters.upsert_service_item(code="RF-CONSULT", name="Synthetic consultation", category="consultation", gst_treatment="exempt", gst_rate_bp=0)
    charge = billing.add_charge(patient_id=patient, service_code="RF-CONSULT", unit_rate_paise=gross, discount_paise=discount, encounter_type="op", charged_by="RF-DOCTOR")
    invoice = billing.create_invoice(patient_id=patient, charge_ids=[charge["id"]], encounter_type="op", issue=True, round_off=False, created_by=ADMIN["id"])
    return charge, invoice


def map_accounts():
    for role in finance.ACCOUNT_ROLES:
        finance.set_mapping(user=ADMIN, facility_id=1, account_role=role, account_code=f"TEST-{role}", account_name=f"Synthetic {role}")


def attributed(*, recognition="billed", mode="percentage", basis="net_ex_tax", percent_bp=2500, fixed_paise=None):
    charge, invoice = source()
    finance.attribute_charge(user=ADMIN, facility_id=1, charge_id=charge["id"], doctor_id=DOCTOR["id"], reason="Synthetic signed attribution")
    finance.create_rule(user=ADMIN, facility_id=1, doctor_id=DOCTOR["id"], selector_type="all", selector_value="", mode=mode, basis=basis, recognition=recognition,
                        percent_bp=percent_bp, fixed_paise=fixed_paise, effective_from=hospital_today().isoformat())
    return charge, invoice


def error(code, fn, **kwargs):
    with pytest.raises(WorkbenchError) as caught:
        fn(**kwargs)
    assert caught.value.code == code
    return caught.value


def test_modules_default_off_and_direct_calls_honor_flag(monkeypatch):
    monkeypatch.setattr(config, "REPORTING_ENABLED", False)
    assert error("module_disabled", reporting.catalog, user=ADMIN).status == 404
    monkeypatch.setattr(config, "FINANCE_TOOLS_ENABLED", False)
    assert error("module_disabled", finance.metadata, user=ADMIN).status == 404


@pytest.mark.parametrize("identity", [{"id": "missing", "role": "admin"}, {"id": "RF-NURSE", "role": "admin"}, {"id": "RF-DOCTOR", "role": "admin"}])
def test_stored_identity_prevents_forged_role(identity):
    assert error("authentication_required" if identity["id"] == "missing" else "permission_denied", reporting.catalog, user=identity).status in (401, 403)
    error("authentication_required" if identity["id"] == "missing" else "permission_denied", finance.metadata, user=identity)
    assert count("finance_payout_rules") == 0


def test_inactive_and_multifacility_fail_closed():
    with db_session() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (ADMIN["id"],))
    assert error("unauthenticated", reporting.catalog, user=ADMIN).status == 401
    with db_session() as db:
        db.execute("UPDATE users SET is_active=1 WHERE id=?", (ADMIN["id"],))
        db.execute("INSERT INTO facilities(id,uuid,code,name) VALUES (2,'00000000-0000-0000-0000-000000000002','TEST2','Synthetic satellite')")
    facilities.invalidate_cache()
    assert error("facility_fenced", finance.metadata, user=ADMIN).status == 503
    assert error("facility_fenced", reporting.catalog, user=ADMIN).status == 503


@pytest.mark.parametrize("query,code", [
    ({"dataset": "patients; DROP TABLE users"}, "invalid_dataset"),
    ({"dataset": "patients", "sql": "SELECT *"}, "invalid_query"),
    ({"dataset": "patients", "columns": ["name FROM users --"]}, "invalid_field"),
    ({"dataset": "patients", "columns": [["patient_id"]]}, "invalid_field"),
    ({"dataset": "patients", "mode": []}, "invalid_query"),
    ({"dataset": "patients", "limit": True}, "invalid_input"),
    ({"dataset": "patients", "limit": 501}, "invalid_input"),
    ({"dataset": "patients", "mode": "summary", "group_by": ["patient_name"]}, "invalid_group"),
    ({"dataset": "patients", "filters": [{"field": "patient_name", "op": [], "value": "x"}]}, "invalid_filter"),
    ({"dataset": "patients", "filters": [{"field": "charted_age", "op": "eq", "value": "44"}]}, "invalid_input"),
])
def test_query_identifiers_shapes_and_types_are_constrained(query, code):
    error(code, reporting.run, user=ADMIN, query=query)
    assert count("users") >= 4


def test_parameterized_filters_and_formula_safe_csv():
    injected = reporting.run(user=ADMIN, query={"dataset": "patients", "filters": [{"field": "patient_name", "op": "eq", "value": "' OR 1=1 --"}]})
    assert injected["rows"] == []
    result = reporting.run(user=ADMIN, query={"dataset": "patients", "filters": [{"field": "charted_age", "op": "gte", "value": 50}]})
    assert [row["patient_id"] for row in result["rows"]] == ["RF-P2"]
    content = reporting.export_csv(user=ADMIN, query={"dataset": "patients", "columns": ["patient_name", "charted_age"]})
    assert list(csv.DictReader(io.StringIO(content)))[0]["patient_name"] == "'=Synthetic Patient"
    assert common.csv_text(["text", "money"], [{"text": " @SUM(1)", "money": -100}]).splitlines()[1] == "' @SUM(1),-100"


def test_summary_and_row_limits_never_export_partial_totals():
    query = {"dataset": "patients", "limit": 1}
    assert reporting.run(user=ADMIN, query=query)["truncated"] is True
    error("result_too_large", reporting.export_csv, user=ADMIN, query=query)
    error("result_too_large", reporting.run, user=ADMIN, query={**query, "mode": "summary", "group_by": ["sex"]})
    assert reporting.run(user=ADMIN, query={"dataset": "patients", "mode": "summary"})["rows"] == [{"count": 2}]


def test_scope_precedes_grouping_and_opd_uses_visit_department(monkeypatch):
    with db_session() as db:
        db.execute("INSERT INTO admissions(id,patient_id,admitted_by,department,status) VALUES ('RF-A1','RF-P1',?,'General Medicine','active')", (ADMIN["id"],))
        db.execute("INSERT INTO admissions(id,patient_id,admitted_by,department,status) VALUES ('RF-A2','RF-P2',?,'ICU','active')", (ADMIN["id"],))
        for vid, pid, dept in [("RF-V1", "RF-P1", "ICU"), ("RF-V2", "RF-P2", "General Medicine")]:
            db.execute("INSERT INTO opd_visits(id,patient_id,department,visit_date,token_number,registered_at) VALUES (?,?,?,?,1,?)", (vid, pid, dept, hospital_today().isoformat(), common.stamp()))
    # Exercise canonical department SQL for a future explicitly delegated role,
    # without weakening the production admin-only grants.
    monkeypatch.setattr(common, "department_scope", lambda actor: "General Medicine")
    result = reporting.run(user=ADMIN, query={"dataset": "patients", "mode": "summary", "limit": 1})
    assert result["rows"] == [{"count": 1}]
    visits = reporting.run(user=ADMIN, query={"dataset": "opd_visits", "columns": ["visit_id"]})
    assert visits["rows"] == [{"visit_id": "RF-V2"}]


def test_saved_queries_owner_version_and_no_identifying_filter_values():
    saved = reporting.save_definition(user=ADMIN, name="Synthetic cohort", query={"dataset": "patients", "mode": "summary"})
    assert reporting.definitions(user=APPROVER) == []
    error("not_found", reporting.delete_definition, user=APPROVER, definition_id=saved["id"])
    error("stale_definition", reporting.save_definition, user=ADMIN, name="new", query={"dataset": "patients"}, definition_id=saved["id"], version=2)
    updated = reporting.save_definition(user=ADMIN, name="new", query={"dataset": "patients"}, definition_id=saved["id"], version=1)
    assert updated["version"] == 2
    error("identifying_saved_filter", reporting.save_definition, user=ADMIN, name="unsafe", query={"dataset": "patients", "filters": [{"field": "patient_name", "op": "eq", "value": "Synthetic"}]})
    assert count("report_definitions") == 1


def test_journal_balances_invoice_overpayment_deposit_and_reversal():
    _, invoice = source()
    receipt = payments.record_payment(patient_id="RF-P1", amount_paise=100000, method="cash", invoice_id=invoice["id"], received_by=ADMIN["id"])
    preview = finance.journal_preview(user=ADMIN, **PERIOD())
    assert preview["debit_paise"] == preview["credit_paise"] == 200001
    row = {r["account_role"]: r for r in preview["rows"] if r["entry_key"] == f"payment:{receipt['id']}"}
    assert row["receivable"]["credit_paise"] == 90000
    assert row["patient_advances"]["credit_paise"] == 10000
    assert preview["can_export"] is False
    reversal = payments.reverse_payment(receipt["id"], reason="Synthetic reversal", reversed_by=APPROVER["id"])
    preview = finance.journal_preview(user=ADMIN, **PERIOD())
    original = [(r["account_role"], r["debit_paise"] - r["credit_paise"]) for r in preview["rows"] if r["entry_key"] == f"payment:{receipt['id']}"]
    reversed_rows = [(r["account_role"], r["debit_paise"] - r["credit_paise"]) for r in preview["rows"] if r["entry_key"] == f"payment:{reversal['reversal']['id']}"]
    assert reversed_rows == [(role, -amount) for role, amount in original]


def test_journal_export_snapshot_is_reproducible_idempotent_and_patient_owned():
    source()
    map_accounts()
    out = finance.create_journal_export(user=ADMIN, **PERIOD(), idempotency_key="synthetic-export")
    before = finance.journal_csv(user=ADMIN, export_id=out["id"])
    finance.set_mapping(user=ADMIN, facility_id=1, account_role="revenue", account_code="CHANGED", account_name="Changed")
    assert finance.journal_csv(user=APPROVER, export_id=out["id"]) == before
    assert finance.create_journal_export(user=ADMIN, **PERIOD(), idempotency_key="synthetic-export") == {**out, "replayed": True}
    assert count("finance_journal_exports") == 1
    with db_session() as db:
        assert [r[0] for r in db.execute("SELECT patient_id FROM finance_journal_export_patients WHERE export_id=?", (out["id"],))] == ["RF-P1"]
    error("idempotency_conflict", finance.create_journal_export, user=ADMIN, **{**PERIOD(), "from_day": "2020-01-01"}, idempotency_key="synthetic-export")


def test_journal_rejects_unbalanced_source_and_unknown_accounts():
    _, invoice = source()
    error("journal_not_ready", finance.create_journal_export, user=ADMIN, **PERIOD(), idempotency_key="not-ready")
    with db_session() as db:
        db.execute("UPDATE invoices SET subtotal_paise=subtotal_paise+1 WHERE id=?", (invoice["id"],))
    error("source_totals_mismatch", finance.journal_preview, user=ADMIN, **PERIOD())
    assert count("finance_journal_exports") == 0


def test_payout_never_infers_doctor_from_charged_by():
    source()
    result = finance.payout_preview(user=ADMIN, **PERIOD())
    assert result["lines"] == []
    assert result["issues"][0]["code"] == "doctor_not_attributed"
    error("payout_not_ready", finance.create_statement, user=ADMIN, **PERIOD(), idempotency_key="unattributed")
    assert count("finance_payout_statements") == 0


@pytest.mark.parametrize("basis,mode,percent,fixed,expected", [
    ("net_ex_tax", "percentage", 2500, None, 22500),
    ("gross_ex_tax", "percentage", 2500, None, 25000),
    ("net_ex_tax", "fixed", None, 12345, 12345),
])
def test_payout_explicit_integer_rules(basis, mode, percent, fixed, expected):
    attributed(basis=basis, mode=mode, percent_bp=percent, fixed_paise=fixed)
    result = finance.payout_preview(user=ADMIN, **PERIOD())
    assert result["total_paise"] == expected
    assert result["can_create"] is True


def test_rule_overlap_and_specificity_are_explicit():
    attributed()
    args = dict(user=ADMIN, facility_id=1, doctor_id=DOCTOR["id"], selector_type="all", selector_value="", mode="percentage", basis="net_ex_tax", recognition="billed", percent_bp=1000, effective_from=hospital_today().isoformat())
    error("overlapping_rule", finance.create_rule, **args)
    finance.create_rule(**{**args, "selector_type": "category", "selector_value": "consultation", "percent_bp": 2000})
    finance.create_rule(**{**args, "selector_type": "service", "selector_value": "RF-CONSULT", "percent_bp": 3000})
    assert finance.payout_preview(user=ADMIN, **PERIOD())["total_paise"] == 27000


def test_fully_collected_rule_waits_for_live_allocation_and_reversal_removes_eligibility():
    _, invoice = attributed(recognition="fully_collected")
    assert finance.payout_preview(user=ADMIN, **PERIOD())["excluded"][0]["code"] == "not_fully_collected"
    receipt = payments.record_payment(patient_id="RF-P1", amount_paise=90000, method="cash", invoice_id=invoice["id"], received_by=ADMIN["id"])
    assert finance.payout_preview(user=ADMIN, **PERIOD())["can_create"] is True
    payments.reverse_payment(receipt["id"], reason="Synthetic", reversed_by=APPROVER["id"])
    assert finance.payout_preview(user=ADMIN, **PERIOD())["can_create"] is False


def test_statement_requires_second_person_and_claims_source_once():
    charge, _ = attributed()
    first = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="first", deduction_paise=500, deduction_reason="Explicit synthetic deduction")
    replay = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="first", deduction_paise=500, deduction_reason="Explicit synthetic deduction")
    assert replay["id"] == first["id"] and replay["replayed"] is True
    assert first["net_paise"] == 22000
    second = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="second")
    error("second_person_required", finance.approve_statement, user=ADMIN, statement_id=first["id"])
    approved = finance.approve_statement(user=APPROVER, statement_id=first["id"])
    assert approved["status"] == "approved"
    error("statement_changed", finance.approve_statement, user=APPROVER, statement_id=second["id"])
    error("attribution_locked", finance.attribute_charge, user=ADMIN, facility_id=1, charge_id=charge["id"], doctor_id=DOCTOR["id"], reason="new", version=1)
    paid = finance.mark_statement_paid(user=APPROVER, statement_id=first["id"], payment_reference="SYNTHETIC-BANK-REF")
    assert paid["status"] == "paid" and count("finance_payout_claims") == 1
    error("payment_reference_conflict", finance.mark_statement_paid, user=APPROVER, statement_id=first["id"], payment_reference="OTHER")


def test_approval_revalidates_changed_attribution_and_rules():
    charge, _ = attributed()
    draft = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="draft")
    finance.attribute_charge(user=ADMIN, facility_id=1, charge_id=charge["id"], doctor_id=DOCTOR["id"], reason="New signed reason", version=1)
    error("statement_changed", finance.approve_statement, user=APPROVER, statement_id=draft["id"])
    assert count("finance_payout_claims") == 0


def test_audit_failure_rolls_back_parent_lines_and_claims(monkeypatch):
    attributed()
    draft = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="before")
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit sink failure")
    monkeypatch.setattr(common, "log_audit_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit"):
        finance.approve_statement(user=APPROVER, statement_id=draft["id"])
    assert count("finance_payout_claims") == 0
    with db_session() as db:
        assert db.execute("SELECT status FROM finance_payout_statements WHERE id=?", (draft["id"],)).fetchone()[0] == "draft"
    with pytest.raises(RuntimeError):
        finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="rolled-back")
    assert count("finance_payout_statements") == 1
    with pytest.raises(RuntimeError):
        reporting.save_definition(user=ADMIN, name="Rollback", query={"dataset": "patients"})
    assert count("report_definitions") == 0


def test_concurrent_approvals_cannot_claim_twice():
    attributed()
    drafts = [finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key=f"parallel-{i}")["id"] for i in range(2)]
    def approve(sid):
        try:
            return finance.approve_statement(user=APPROVER, statement_id=sid)["status"]
        except WorkbenchError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, drafts)) == ["approved", "statement_changed"]
    assert count("finance_payout_claims") == 1


def test_http_csv_route_not_shadowed_and_unexpected_failures_are_generic(monkeypatch):
    from routes import reporting_routes, finance_tools_routes
    from auth import get_current_user
    app = FastAPI()
    app.include_router(reporting_routes.router)
    app.include_router(finance_tools_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {**ADMIN, "role": "admin", "department": None}
    monkeypatch.setattr(finance, "statement_csv", lambda **kwargs: "amount_paise\r\n100\r\n")
    client = TestClient(app)
    reply = client.get("/api/finance-tools/payout-statements/synthetic.csv")
    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    monkeypatch.setattr(reporting, "run", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("/private/secret-patient.db")))
    reply = client.post("/api/reporting/query", json={"dataset": "patients"})
    assert reply.status_code == 500
    assert "secret" not in reply.text and "patient.db" not in reply.text


def test_migration_is_idempotent_and_seeds_no_commercial_policy():
    from migrations_his import m0082_reporting_finance as migration
    with db_session() as db:
        migration.apply(db)
        migration.apply(db)
        columns = {row[1] for row in db.execute("PRAGMA table_info(finance_payout_lines)")}
    assert "created_at" in columns
    assert count("finance_payout_rules") == count("finance_account_mappings") == count("finance_charge_attributions") == 0


def test_problem_cohort_uses_recorded_active_code_only():
    with db_session() as db:
        db.execute("INSERT INTO problem_list(patient_id,problem,icd10_code,status) VALUES ('RF-P1','Synthetic active problem','E11','active')")
        db.execute("INSERT INTO problem_list(patient_id,problem,icd10_code,status) VALUES ('RF-P2','Synthetic resolved problem','E11','resolved')")
    query = {"dataset": "patients", "columns": ["patient_id"], "filters": [{"field": "problem_icd10", "op": "eq", "value": "E11"}]}
    assert reporting.run(user=ADMIN, query=query)["rows"] == [{"patient_id": "RF-P1"}]
    assert reporting.run(user=ADMIN, query={**query, "filters": [{"field": "problem_icd10", "op": "eq", "value": "unknown-code"}]})["rows"] == []
    error("invalid_filter", reporting.run, user=ADMIN, query={**query, "filters": [{"field": "problem_icd10", "op": "contains", "value": "E"}]})


def test_dataset_grant_is_required_in_addition_to_reporting(monkeypatch):
    from permissions import ROLE_PERMISSIONS
    monkeypatch.setitem(ROLE_PERMISSIONS, "admin", {"reporting.read", "reporting.write", "reporting.export"})
    assert reporting.catalog(user=ADMIN)["datasets"] == []
    error("permission_denied", reporting.run, user=ADMIN, query={"dataset": "payments"})
    error("permission_denied", reporting.run, user=ADMIN, query={"dataset": "patients"})


@pytest.mark.parametrize("from_day,to_day", [("not-a-date", "2026-10-11"), ("2026-10-12", "2026-10-11"), ("2020-01-01", "2026-10-11"), ("2099-01-01", "2099-01-01")])
def test_periods_are_bounded_completed_hospital_days(from_day, to_day):
    error("invalid_period", reporting.run, user=ADMIN, query={"dataset": "invoices", "from_day": from_day, "to_day": to_day})


def test_invoice_date_filter_uses_hospital_midnight_not_utc_midnight():
    _, invoice = source()
    from services.hospital_time import local_day_bounds_utc
    start, end = local_day_bounds_utc(hospital_today())
    with db_session() as db:
        db.execute("UPDATE invoices SET issued_at=? WHERE id=?", (start.isoformat(), invoice["id"]))
    query = {"dataset": "invoices", "from_day": hospital_today().isoformat(), "to_day": hospital_today().isoformat(), "columns": ["invoice_id"]}
    assert reporting.run(user=ADMIN, query=query)["rows"] == [{"invoice_id": invoice["id"]}]
    with db_session() as db:
        db.execute("UPDATE invoices SET issued_at=? WHERE id=?", (end.isoformat(), invoice["id"]))
    assert reporting.run(user=ADMIN, query=query)["rows"] == []


def test_journal_cancellation_is_an_opposing_event():
    _, invoice = source()
    billing.cancel_invoice(invoice["id"], reason="Synthetic cancellation", actor=ADMIN["id"])
    result = finance.journal_preview(user=ADMIN, **PERIOD())
    invoice_rows = [r for r in result["rows"] if r["source_type"] == "invoice"]
    cancelled = [r for r in result["rows"] if r["source_type"] == "invoice_cancellation"]
    assert invoice_rows and cancelled
    assert [(r["account_role"], r["debit_paise"] - r["credit_paise"]) for r in cancelled] == [(r["account_role"], -(r["debit_paise"] - r["credit_paise"])) for r in invoice_rows]
    assert sum(r["debit_paise"] - r["credit_paise"] for r in result["rows"]) == 0


def test_deposit_application_never_adds_cash_to_journal():
    _, invoice = source()
    payments.record_payment(patient_id="RF-P1", amount_paise=90000, method="cash", purpose="advance", received_by=ADMIN["id"])
    applied = payments.apply_deposit_to_invoice(patient_id="RF-P1", invoice_id=invoice["id"], amount_paise=90000, applied_by=ADMIN["id"])
    result = finance.journal_preview(user=ADMIN, **PERIOD())
    assert sum(r["debit_paise"] for r in result["rows"] if r["account_role"] == "funds:cash") == 90000
    adjustments = [r for r in result["rows"] if r["source_id"] == applied["id"] and r["source_type"] == "receipt"]
    assert {r["account_role"] for r in adjustments} == {"patient_advances", "receivable"}


def test_credit_note_offsets_exact_approved_earning_not_a_new_rule():
    _, invoice = attributed()
    draft = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="original")
    finance.approve_statement(user=APPROVER, statement_id=draft["id"])
    billing.create_credit_note(invoice["id"], reason="Synthetic full credit", actor=ADMIN["id"])
    finance.retire_rule(user=ADMIN, rule_id=draft["lines"][0]["rule_id"])
    result = finance.payout_preview(user=ADMIN, **PERIOD())
    assert len(result["lines"]) == 1
    assert result["lines"][0]["source_kind"] == "credit"
    assert result["total_paise"] == -22500
    assert result["lines"][0]["rule_id"] == draft["lines"][0]["rule_id"]
    assert result["can_create"] is False
    assert result["issues"] == [{"code": "negative_payout_requires_offset"}]
    error("payout_not_ready", finance.create_statement, user=ADMIN, **PERIOD(), idempotency_key="negative-alone")
    journal = finance.journal_preview(user=ADMIN, **PERIOD())
    assert journal["balanced"] is True
    assert {row["source_type"] for row in journal["rows"]} == {"invoice"}


def test_credit_before_approval_never_pays_the_reversed_charge():
    _, invoice = attributed()
    billing.create_credit_note(invoice["id"], reason="Synthetic credit before payment", actor=ADMIN["id"])
    result = finance.payout_preview(user=ADMIN, **PERIOD())
    assert result["lines"] == [] and result["can_create"] is False
    assert {i["code"] for i in result["excluded"]} == {"source_credited", "credit_without_claimed_earning"}


def test_credit_can_offset_later_unclaimed_earnings_without_double_claiming():
    _, invoice = attributed()
    original = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="original")
    finance.approve_statement(user=APPROVER, statement_id=original["id"])
    billing.create_credit_note(invoice["id"], reason="Synthetic full credit", actor=ADMIN["id"])
    charge, _ = source(gross=200000, discount=0)
    finance.attribute_charge(user=ADMIN, facility_id=1, charge_id=charge["id"], doctor_id=DOCTOR["id"], reason="Synthetic later earning")
    result = finance.payout_preview(user=ADMIN, **PERIOD())
    assert result["total_paise"] == 27500 and result["can_create"]
    replacement = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="offset")
    finance.approve_statement(user=APPROVER, statement_id=replacement["id"])
    assert count("finance_payout_claims") == 3


def test_snapshot_hash_detects_corruption_and_scope_checked_on_retry(monkeypatch):
    source(); map_accounts()
    created = finance.create_journal_export(user=ADMIN, **PERIOD(), idempotency_key="scoped")
    with db_session() as db:
        db.execute("INSERT INTO admissions(id,patient_id,admitted_by,department,status) VALUES ('RF-A1','RF-P1',?,'ICU','active')", (ADMIN["id"],))
    monkeypatch.setattr(common, "department_scope", lambda actor: "General Medicine")
    error("not_found", finance.journal_csv, user=ADMIN, export_id=created["id"])
    error("not_found", finance.create_journal_export, user=ADMIN, **PERIOD(), idempotency_key="scoped")
    monkeypatch.setattr(common, "department_scope", lambda actor: None)
    with db_session() as db:
        row = db.execute("SELECT rows_json FROM finance_journal_exports WHERE id=?", (created["id"],)).fetchone()
        rows = json.loads(row[0]); rows[0]["account_code"] = "CORRUPTED"
        db.execute("UPDATE finance_journal_exports SET rows_json=? WHERE id=?", (json.dumps(rows), created["id"]))
    error("snapshot_integrity", finance.journal_csv, user=ADMIN, export_id=created["id"])


def test_export_audit_failure_rolls_back_snapshot_and_patient_index(monkeypatch):
    source(); map_accounts()
    monkeypatch.setattr(common, "log_audit_event", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Synthetic audit failure")))
    with pytest.raises(RuntimeError):
        finance.create_journal_export(user=ADMIN, **PERIOD(), idempotency_key="rollback")
    assert count("finance_journal_exports") == count("finance_journal_export_patients") == 0


def test_statement_export_includes_manual_deduction_and_net():
    attributed()
    draft = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="with-deduction", deduction_paise=500, deduction_reason="Synthetic explicit deduction")
    csv_rows = list(csv.DictReader(io.StringIO(finance.statement_csv(user=ADMIN, statement_id=draft["id"]))))
    assert csv_rows[0]["statement_total_paise"] == "22500"
    assert csv_rows[0]["statement_deduction_paise"] == "500"
    assert csv_rows[0]["statement_net_paise"] == "22000"


def test_concurrent_idempotent_creation_returns_one_statement():
    attributed()
    def create(_):
        return finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="same-concurrent-request")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert len({r["id"] for r in results}) == 1
    assert sorted(r["replayed"] for r in results) == [False, True]
    assert count("finance_payout_statements") == count("finance_payout_lines") == 1


def test_export_history_applies_snapshot_scope_before_limit_even_without_child_index(monkeypatch):
    source(); map_accounts()
    created = finance.create_journal_export(user=ADMIN, **PERIOD(), idempotency_key="history")
    assert [row["id"] for row in finance.list_journal_exports(user=ADMIN, facility_id=1)] == [created["id"]]
    with db_session() as db:
        db.execute("DELETE FROM finance_journal_export_patients")
        db.execute("INSERT INTO admissions(id,patient_id,admitted_by,department,status) VALUES ('RF-A1','RF-P1',?,'ICU','active')", (ADMIN["id"],))
    monkeypatch.setattr(common, "department_scope", lambda actor: "General Medicine")
    assert finance.list_journal_exports(user=ADMIN, facility_id=1) == []


def test_payout_rule_metadata_scopes_doctors_before_row_limit():
    own = finance.create_rule(user=ADMIN, facility_id=1, doctor_id=DOCTOR["id"], selector_type="all", selector_value="", mode="fixed", basis="net_ex_tax", recognition="billed", fixed_paise=123, effective_from=hospital_today().isoformat())
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,department,password_hash) VALUES ('RF-FOREIGN-DOCTOR','Other synthetic doctor','doctor','ICU','not-a-login')")
        # Historical retired rules are valid rows. Put enough later foreign
        # history ahead of the one own rule to prove SQL scope precedes LIMIT.
        db.executemany("INSERT INTO finance_payout_rules(facility_id,doctor_id,selector_type,selector_value,mode,basis,recognition,fixed_paise,effective_from,is_active,created_by,created_at) VALUES (1,'RF-FOREIGN-DOCTOR','all','','fixed','net_ex_tax','billed',999,?,0,?,?)",
                       [(hospital_today().isoformat(), ADMIN["id"], common.stamp()) for _ in range(501)])
    assert len(finance.metadata(user=ADMIN)["rules"]) == 500
    with db_session() as db:
        db.execute("UPDATE users SET department='General Medicine' WHERE id=?", (ADMIN["id"],))
    result = finance.metadata(user=ADMIN)
    assert [rule["id"] for rule in result["rules"]] == [own["id"]]
    assert {doctor["id"] for doctor in result["doctors"]} == {DOCTOR["id"]}
    assert error("not_found", finance.retire_rule, user=ADMIN, rule_id=own["id"] + 1).status == 404


def test_doctor_metadata_scopes_active_accounts_before_picker_limit():
    with db_session() as db:
        # These sorted names precede the own doctor. Neither foreign nor
        # inactive accounts may exhaust the bounded picker before scope.
        db.executemany("INSERT INTO users(id,name,role,department,password_hash,is_active) VALUES (?,?,'doctor',?,'not-a-login',?)",
                       [(f"RF-PICKER-{i}", f"A synthetic doctor {i:04}",
                         "ICU" if i % 2 else "General Medicine", i % 2)
                        for i in range(1001)])
        db.execute("UPDATE users SET department='General Medicine' WHERE id=?", (ADMIN["id"],))
    result = finance.metadata(user=ADMIN)
    assert {doctor["id"] for doctor in result["doctors"]} == {DOCTOR["id"]}


def test_patient_export_slices_shared_payout_claims_and_status_without_batch_totals():
    from services import consent_service as consent
    attributed()
    charge, _ = source(patient="RF-P2", gross=200000, discount=0)
    finance.attribute_charge(user=ADMIN, facility_id=1, charge_id=charge["id"], doctor_id=DOCTOR["id"], reason="Explicit synthetic second patient attribution")
    draft = finance.create_statement(user=ADMIN, **PERIOD(), idempotency_key="shared-privacy", deduction_paise=500, deduction_reason="Synthetic batch deduction")
    finance.approve_statement(user=APPROVER, statement_id=draft["id"])
    finance.mark_statement_paid(user=APPROVER, statement_id=draft["id"], payment_reference="SYNTHETIC-SHARED-REFERENCE")
    exported = consent.data_principal_export("RF-P1")
    assert "RF-P2" not in json.dumps(exported)
    claims = exported["sections"]["finance_payout_claims"]["rows"]
    own_lines = [line for line in draft["lines"] if line["patient_id"] == "RF-P1"]
    assert len(claims) == 1 and claims[0]["source_line_id"] == own_lines[0]["source_line_id"]
    context = exported["sections"]["finance_payout_statement_context"]["rows"]
    assert len(context) == 1 and context[0]["statement_id"] == draft["id"]
    assert context[0]["status"] == "paid" and context[0]["approved_by"] == APPROVER["id"]
    assert context[0]["payment_reference"] == "SYNTHETIC-SHARED-REFERENCE"
    assert not {"total_paise", "deduction_paise", "net_paise", "deduction_reason", "preview_hash", "request_hash", "idempotency_key"} & context[0].keys()


def test_receipt_level_refund_without_invoice_allocation_cannot_prove_full_collection():
    _, invoice = attributed(recognition="fully_collected")
    receipt = payments.record_payment(patient_id="RF-P1", amount_paise=90000, method="cash", invoice_id=invoice["id"], received_by=ADMIN["id"])
    assert finance.payout_preview(user=ADMIN, **PERIOD())["can_create"] is True
    refund = payments.request_refund(patient_id="RF-P1", payment_id=receipt["id"], amount_paise=10000, reason="Synthetic receipt-level refund", requested_by=ADMIN["id"])
    payments.decide_refund(refund["id"], approve=True, decided_by=APPROVER["id"])
    payments.pay_refund(refund["id"], method="cash", paid_by=APPROVER["id"])
    preview = finance.payout_preview(user=ADMIN, **PERIOD())
    assert preview["can_create"] is False and preview["lines"] == []
    assert preview["excluded"][0]["code"] == "unallocated_receipt_refund"
    assert finance.journal_preview(user=ADMIN, **PERIOD())["balanced"] is True


def test_manual_deduction_cannot_be_silently_allocated_across_doctors():
    attributed()
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,department,password_hash) VALUES ('RF-OTHER-DOC','Other Synthetic Doctor','doctor','General Medicine','not-a-login')")
    charge, _ = source(patient="RF-P2")
    finance.attribute_charge(user=ADMIN, facility_id=1, charge_id=charge["id"], doctor_id="RF-OTHER-DOC", reason="Other explicit earning doctor")
    finance.create_rule(user=ADMIN, facility_id=1, doctor_id="RF-OTHER-DOC", selector_type="all", selector_value="", mode="percentage", basis="net_ex_tax", recognition="billed", percent_bp=2500, effective_from=hospital_today().isoformat())
    error("ambiguous_deduction", finance.create_statement, user=ADMIN, **PERIOD(), idempotency_key="mixed-deduction", deduction_paise=500, deduction_reason="No per-doctor allocation")
    assert count("finance_payout_statements") == 0
    one = finance.create_statement(user=ADMIN, **PERIOD(), doctor_id=DOCTOR["id"], idempotency_key="one-doctor-deduction", deduction_paise=500, deduction_reason="Explicit deduction for this doctor")
    assert one["net_paise"] == 22000 and {line["doctor_id"] for line in one["lines"]} == {DOCTOR["id"]}
