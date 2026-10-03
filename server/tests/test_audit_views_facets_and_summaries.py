"""What the Audit Logs and Security screens are given to work with.

  * /api/audit/facets lists the categories and actions the trail really holds.
    The screen used to hard-code them: it offered 'auth', which matched no row
    (sign-ins are 'authentication'), and never offered newer actions.
  * /api/audit/logs takes a comma-separated outcome, so the Security tab can
    open on "failure,denied" instead of burying two refusals in every sign-in.
  * the mutation safety net's summary reads as words ("Submitted to
    administration: rejected"), not "POST /api/... -> 410"; method, path and
    status stay in their own columns and metadata, so tracing loses nothing.
"""
import json
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.audit_middleware import _readable_summary
from services.audit_service import log_audit_event

client = TestClient(app)


def _user(role: str) -> str:
    uid = f"AVF{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'Audit Views Fixture', ?, 'x')", (uid, role))
    return uid


def _h(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _seed(category: str, action: str, outcome: str) -> None:
    log_audit_event(correlation_id=str(uuid.uuid4()), event_category=category,
                    action_type=action, outcome=outcome,
                    summary_message=f"audit views fixture {action}")


def test_facets_list_what_the_trail_holds_and_never_the_dead_auth_value():
    _seed("authentication", "avf_login_failure", "failure")
    _seed("phi_access", "avf_chart_viewed", "success")
    admin = _user("admin")

    resp = client.get("/api/audit/facets", headers=_h(admin, "admin"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    with db_session() as db:
        held = sorted(r[0] for r in db.execute(
            "SELECT DISTINCT event_category FROM audit_logs").fetchall())
    assert body["categories"] == held
    assert "authentication" in body["categories"] and "auth" not in body["categories"]
    assert {"avf_login_failure", "avf_chart_viewed"} <= set(body["actions"])
    assert body["outcomes"] == ["success", "failure", "partial_success", "denied"]

    narrowed = client.get("/api/audit/facets?event_category=authentication",
                          headers=_h(admin, "admin")).json()
    assert "avf_login_failure" in narrowed["actions"]
    assert "avf_chart_viewed" not in narrowed["actions"], \
        "the action list did not narrow to the chosen category"


def test_facets_need_audit_read():
    nurse = _user("nurse")
    assert client.get("/api/audit/facets", headers=_h(nurse, "nurse")).status_code == 403


def test_logs_filter_on_several_outcomes_at_once():
    tag = uuid.uuid4().hex[:6]
    for outcome in ("success", "failure", "denied"):
        _seed("security", f"avf_outcome_{tag}", outcome)
    admin = _user("admin")

    resp = client.get(f"/api/audit/logs?action_type=avf_outcome_{tag}&outcome=failure,denied",
                      headers=_h(admin, "admin"))
    assert resp.status_code == 200, resp.text
    assert sorted(r["outcome"] for r in resp.json()["logs"]) == ["denied", "failure"]
    assert resp.json()["total"] == 2


def test_summaries_name_the_area_and_result_in_words():
    assert _readable_summary("POST", "/api/emar/P001/administer", 200) == \
        "Submitted to medication administration: completed"
    assert _readable_summary("DELETE", "/api/relay/messages/4", 403) == \
        "Deleted from Relay: refused, not permitted"
    assert _readable_summary("PATCH", "/api/some-new-area/x", 404) == \
        "Updated in some new area: not found"
    assert _readable_summary("PUT", "/api/orders/7", 500) == \
        "Updated in orders: failed with a server error"


def test_a_mutation_row_keeps_method_path_and_status_beside_the_readable_summary():
    admin = _user("admin")
    path = f"/api/admin/patients/AVF{uuid.uuid4().hex[:8].upper()}/erase"

    resp = client.post(path, headers=_h(admin, "admin"))
    assert resp.status_code == 410, resp.text

    with db_session() as db:
        row = db.execute(
            "SELECT summary_message, http_method, request_path, metadata_json "
            "FROM audit_logs WHERE event_category = 'api_mutation' AND request_path = ? "
            "ORDER BY id DESC LIMIT 1", (path,)).fetchone()
    assert row is not None, "the mutation safety net wrote nothing"
    assert row["summary_message"] == "Submitted to administration: rejected"
    assert "->" not in row["summary_message"]
    assert (row["http_method"], row["request_path"]) == ("POST", path)
    meta = json.loads(row["metadata_json"])
    assert (meta["status_code"], meta["method"], meta["path"]) == (410, "POST", path)
