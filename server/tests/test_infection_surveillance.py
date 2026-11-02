"""Manual surveillance boundaries: exact denominators, attribution and privacy."""
import uuid
import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from db_backend import DBIntegrityError
from main import app
from services import infection_surveillance_service as svc, local_operations_common as common
from services.audit_service import AuditWriteError

client = TestClient(app)


def key(): return uuid.uuid4().hex


def user(role='admin', department=None):
    ident = 'INF-U-' + key()
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,department,password_hash,is_active) VALUES(?,'Synthetic surveillance account',?,?,?,1)", (ident, role, department, 'not-a-login-password'))
    return {'id': ident, 'role': role, 'department': department}


def patient(actor, department=None):
    ident = 'INF-P-' + key()
    with db_session() as db:
        db.execute("INSERT INTO patients(id,name,mrn,status) VALUES(?,'Synthetic surveillance patient',?,'admitted')", (ident, ident))
        db.execute("INSERT INTO admissions(id,patient_id,department,status,admitted_by,admitted_at) VALUES(?,?,?,'active',?,?)", ('INF-A-' + key(), ident, department, actor['id'], common.now()))
    return ident


@pytest.fixture
def actor(): return user()


def create(actor, kind, **values): return svc.create(kind, values, actor=actor, key=key())


def window(actor, unit='patient_days', minimum=1, isolate_policy='all_isolates', **extra):
    definition = create(actor, 'definition', code=key(), name='Synthetic surveillance definition', case_definition='Existing hospital definition TEST, manually reviewed', counting_policy='Count each distinct manually included episode and each explicitly observed day.', denominator_unit=unit, device_label='Synthetic device' if unit == 'device_days' else None, evidence_reference='Hospital policy TEST')
    return create(actor, 'window', definition_id=definition['id'], label='Synthetic observation window', population_description='Explicit synthetic population TEST', start_date='2026-09-01', end_date='2026-09-30', source_reference='Population ledger TEST', rate_multiplier=1000, minimum_cell=minimum, isolate_policy=isolate_policy, **extra)


def exposure(actor, win, pid, day='2026-09-01', device=None):
    return create(actor, 'exposure', window_id=win['id'], patient_id=pid, observed_date=day, device_reference=device, evidence_reference='Daily source ledger TEST')


def case(actor, win, pid):
    return create(actor, 'case', window_id=win['id'], patient_id=pid, episode_reference=key(), event_date='2026-09-02', evidence_reference='Clinician source TEST', statement='Authored case evidence TEST')


def susceptibility(actor, win, pid, *, isolate=None, result='S', day='2026-09-02', antibiotic='AB-TEST', standard='Standard TEST edition', method='Method TEST'):
    return create(actor, 'susceptibility', window_id=win['id'], patient_id=pid, isolate_reference=isolate or key(), specimen_date=day, organism_code='ORG-TEST', specimen_code='SPEC-TEST', antibiotic_code=antibiotic, result=result, standard_reference=standard, method=method, evidence_reference='Laboratory report TEST', validator_reference='Validated laboratory reviewer TEST')


def finalize(actor, win):
    return svc.window_action(win['id'], {'action': 'finalize', 'version': win['version'], 'reason': 'Authorized review of completed population, manual case classifications and exact denominator ledger.'}, actor=actor)


def test_explicit_configuration_and_exact_denominator_produce_no_automatic_case(actor):
    win = window(actor)
    pid = patient(actor)
    exposure(actor, win, pid); exposure(actor, win, pid, '2026-09-02')
    row = case(actor, win, pid)
    with pytest.raises(common.OperationsError) as exc: finalize(actor, win)
    assert exc.value.code == 'pending_cases'
    with pytest.raises(common.OperationsError) as exc: svc.metrics(win['id'], actor=actor)
    assert exc.value.code == 'window_not_finalized'
    svc.classify(row['id'], {'version': 1, 'status': 'included', 'reason': 'Clinician applies the stated hospital definition.', 'reviewer_reference': 'Signed review TEST'}, actor=actor)
    final = finalize(actor, win)
    report = svc.metrics(final['id'], actor=actor)
    assert report['rate'] == {'suppressed': False, 'numerator': 1, 'denominator': 2, 'multiplier': 1000, 'value': 500.0}
    assert report['antibiogram'] == []
    with pytest.raises(common.OperationsError) as exc: exposure(actor, final, pid, '2026-09-03')
    assert exc.value.code == 'window_finalized'


@pytest.mark.parametrize('count', [0, 2, 1.0, True])
def test_does_not_round_or_coerce_exposure_days(actor, count):
    win, pid = window(actor), patient(actor)
    with pytest.raises(common.OperationsError):
        create(actor, 'exposure', window_id=win['id'], patient_id=pid, observed_date='2026-09-01', day_count=count, evidence_reference='TEST')


def test_device_days_require_distinct_device_reference_and_deduplicate(actor):
    win, pid = window(actor, 'device_days'), patient(actor)
    with pytest.raises(common.OperationsError, match='device reference'): exposure(actor, win, pid)
    first = exposure(actor, win, pid, device='DEVICE-1')
    with pytest.raises(common.OperationsError) as exc: exposure(actor, win, pid, device='DEVICE-1')
    assert exc.value.code == 'duplicate_record'
    exposure(actor, win, pid, device='DEVICE-2')
    svc.void('exposure', first['id'], {'version': 1, 'reason': 'Duplicate physical entry identified against source.'}, actor=actor)
    assert svc.detail('window', win['id'], actor=actor)['exposures'][0]['day_count'] == 1
    finalize(actor, win)
    assert svc.metrics(win['id'], actor=actor)['rate']['denominator'] == 1


def test_patient_day_rejects_device_and_dates_outside_window(actor):
    win, pid = window(actor), patient(actor)
    for day, device in [('2026-09-01', 'DEVICE-1'), ('2026-08-31', None), ('2026-10-01', None), ('2026-9-1', None)]:
        with pytest.raises(common.OperationsError): exposure(actor, win, pid, day, device)


def test_finalization_requires_ended_window_and_explicit_denominator(actor):
    win = window(actor)
    with pytest.raises(common.OperationsError) as exc: finalize(actor, win)
    assert exc.value.code == 'missing_denominator'
    with db_session() as db:
        # A second independently defined future window; policies are never patched in-place.
        definition_id = win['definition_id']
    future = create(actor, 'window', definition_id=definition_id, label='Future observation TEST', population_description='Explicit future population', start_date='2999-01-01', end_date='2999-01-02', source_reference='Source TEST', rate_multiplier=1, minimum_cell=1, isolate_policy='all_isolates')
    with pytest.raises(common.OperationsError) as exc: finalize(actor, future)
    assert exc.value.code == 'window_not_ended'


def test_antibiogram_preserves_s_i_r_denominators_and_interpretation_cohorts(actor):
    win, pid = window(actor), patient(actor)
    exposure(actor, win, pid)
    for result in ('S', 'I', 'R'): susceptibility(actor, win, pid, result=result)
    susceptibility(actor, win, pid, antibiotic='AB-OTHER')
    susceptibility(actor, win, pid, standard='Different standard TEST')
    finalize(actor, win)
    rows = svc.metrics(win['id'], actor=actor)['antibiogram']
    primary = next(row for row in rows if row['antibiotic'] == 'AB-TEST' and row['standard_reference'] == 'Standard TEST edition')
    assert primary['tested'] == 3 and primary['counts'] == {'S': 1, 'I': 1, 'R': 1}
    assert primary['susceptible_percent'] == 33.33
    assert len(rows) == 3


def test_small_tested_or_outcome_cells_are_not_reconstructable(actor):
    win, pid = window(actor, minimum=3), patient(actor)
    for day in ('2026-09-01', '2026-09-02', '2026-09-03'): exposure(actor, win, pid, day)
    for result in ('S', 'S', 'S', 'R'): susceptibility(actor, win, pid, result=result)
    susceptibility(actor, win, pid, antibiotic='SMALL-N')
    finalize(actor, win)
    rows = svc.metrics(win['id'], actor=actor)['antibiogram']
    assert len(rows) == 2
    assert all(row['suppressed'] and row['tested'] is None and row['counts'] is None and row['susceptible_percent'] is None for row in rows)


def test_first_isolate_policy_deduplicates_patients_without_mixing_test_panels(actor):
    win, pid = window(actor, isolate_policy='first_per_patient'), patient(actor)
    exposure(actor, win, pid)
    susceptibility(actor, win, pid, isolate='FIRST', result='S', day='2026-09-01')
    susceptibility(actor, win, pid, isolate='FIRST', result='I', day='2026-09-01', antibiotic='AB-OTHER')
    susceptibility(actor, win, pid, isolate='LATER', result='R', day='2026-09-02')
    finalize(actor, win)
    rows = svc.metrics(win['id'], actor=actor)['antibiogram']
    assert all(row['tested'] == 1 for row in rows)
    assert next(row for row in rows if row['antibiotic'] == 'AB-TEST')['counts'] == {'S': 1, 'I': 0, 'R': 0}
    assert next(row for row in rows if row['antibiotic'] == 'AB-OTHER')['counts'] == {'S': 0, 'I': 1, 'R': 0}


def test_isolate_identity_cannot_be_reused_for_another_patient(actor):
    win, one, two = window(actor), patient(actor), patient(actor)
    susceptibility(actor, win, one, isolate='FIXED')
    with pytest.raises(common.OperationsError) as exc: susceptibility(actor, win, two, isolate='FIXED', antibiotic='OTHER')
    assert exc.value.code == 'isolate_conflict'


def test_isolation_start_end_is_authored_versioned_and_does_not_change_care_flags(actor):
    pid = patient(actor)
    with db_session() as db: before = dict(db.execute('SELECT * FROM patients WHERE id=?', (pid,)).fetchone())
    row = create(actor, 'isolation', patient_id=pid, precautions='Clinician-authored precautions TEST', started_at='2026-09-01T12:00:00+05:30', start_reason='Existing signed decision TEST', authority_reference='Decision TEST')
    with pytest.raises(common.OperationsError): svc.end_isolation(row['id'], {'version': 1, 'ended_at': '2026-09-01T06:00:00Z', 'reason': 'Earlier time', 'authority_reference': 'TEST'}, actor=actor)
    ended = svc.end_isolation(row['id'], {'version': 1, 'ended_at': '2026-09-02T12:00:00+05:30', 'reason': 'Authored clinical end decision', 'authority_reference': 'End decision TEST'}, actor=actor)
    assert ended['status'] == 'ended'
    with pytest.raises(common.OperationsError): svc.end_isolation(row['id'], {'version': 1, 'ended_at': '2026-09-03T12:00:00Z', 'reason': 'Stale', 'authority_reference': 'TEST'}, actor=actor)
    detail = svc.detail('isolation', row['id'], actor=actor)
    assert len(detail['events']) == 2 and all(item['patient_id'] == pid for item in detail['events'])
    with db_session() as db: assert dict(db.execute('SELECT * FROM patients WHERE id=?', (pid,)).fetchone()) == before


def test_isolation_link_cannot_name_another_patient(actor):
    win, one, two = window(actor), patient(actor), patient(actor)
    linked = case(actor, win, one)
    with pytest.raises(common.OperationsError) as exc:
        create(actor, 'isolation', patient_id=two, case_id=linked['id'], precautions='TEST', started_at='2026-09-01T00:00:00Z', start_reason='TEST', authority_reference='TEST')
    assert exc.value.status == 404


def test_transfer_refuses_entire_aggregate_instead_of_silently_narrowing_denominator():
    dept = 'INF ward ' + key()
    actor = user(department=dept)
    win, pid = window(actor), patient(actor, dept)
    exposure(actor, win, pid); finalize(actor, win)
    with db_session() as db: db.execute('UPDATE admissions SET department=? WHERE patient_id=?', ('Other ward ' + key(), pid))
    for fn in (lambda: svc.detail('window', win['id'], actor=actor), lambda: svc.metrics(win['id'], actor=actor)):
        with pytest.raises(common.OperationsError) as exc: fn()
        assert exc.value.status == 404


@pytest.mark.parametrize("kind", ["case", "exposure", "susceptibility", "isolation"])
def test_delegated_patient_creation_denies_foreign_scope_before_persistence(kind):
    ward = "INF creation ward " + key()
    actor = user(department=ward)
    win, own, foreign = window(actor), patient(actor, ward), patient(actor, ward + " other")
    values = {
        "case": {"window_id": win["id"], "episode_reference": key(), "event_date": "2026-09-02", "evidence_reference": "Test", "statement": "Authored test"},
        "exposure": {"window_id": win["id"], "observed_date": "2026-09-02", "evidence_reference": "Test"},
        "susceptibility": {"window_id": win["id"], "isolate_reference": key(), "specimen_date": "2026-09-02", "organism_code": "TEST", "specimen_code": "TEST", "antibiotic_code": "TEST", "result": "S", "standard_reference": "Test", "method": "Test", "evidence_reference": "Test", "validator_reference": "Test"},
        "isolation": {"precautions": "Authored test", "start_reason": "Authored test", "authority_reference": "Test", "started_at": "2026-09-02T00:00:00Z"},
    }[kind]
    with pytest.raises(common.OperationsError) as denied:
        create(actor, kind, patient_id=foreign, **values)
    assert denied.value.status == 404
    request_key = key()
    row = svc.create(kind, {"patient_id": own, **values}, actor=actor, key=request_key)
    with db_session() as db:
        assert db.execute(f"SELECT COUNT(*) FROM {svc.TABLES[kind]} WHERE patient_id=?", (foreign,)).fetchone()[0] == 0
        db.execute("UPDATE admissions SET department=? WHERE patient_id=?", (ward + " transferred", own))
    with pytest.raises(common.OperationsError) as replay:
        svc.create(kind, {"patient_id": own, **values}, actor=actor, key=request_key)
    assert replay.value.status == 404
    with db_session() as db:
        assert db.execute(f"SELECT COUNT(*) FROM {svc.TABLES[kind]} WHERE id=?", (row["id"],)).fetchone()[0] == 1


def test_erasure_invalidates_finalized_population_and_requires_new_attestation(actor):
    win, pid = window(actor), patient(actor)
    source = exposure(actor, win, pid)
    finalize(actor, win)
    with db_session() as db: db.execute('DELETE FROM infection_exposure_days WHERE id=?', (source['id'],))
    with pytest.raises(common.OperationsError) as exc: svc.metrics(win['id'], actor=actor)
    assert exc.value.code == 'population_changed'


def test_reopen_invalidates_old_results_and_corrections_preserve_history(actor):
    win, pid = window(actor), patient(actor)
    source = exposure(actor, win, pid)
    final = finalize(actor, win)
    reopened = svc.window_action(win['id'], {'action': 'reopen', 'version': final['version'], 'reason': 'Source ledger correction identified'}, actor=actor)
    with pytest.raises(common.OperationsError): svc.metrics(win['id'], actor=actor)
    svc.void('exposure', source['id'], {'version': 1, 'reason': 'Incorrect source entry'}, actor=actor)
    exposure(actor, win, pid, '2026-09-02')
    finalize(actor, reopened)
    assert len(svc.detail('window', win['id'], actor=actor)['events']) == 4


def test_idempotent_creation_replays_without_duplicate_isolation(actor):
    pid, request_key = patient(actor), key()
    data = {'patient_id': pid, 'precautions': 'TEST', 'started_at': '2026-09-01T00:00:00Z', 'start_reason': 'TEST', 'authority_reference': 'TEST'}
    one = svc.create('isolation', data, actor=actor, key=request_key)
    assert svc.create('isolation', data, actor=actor, key=request_key)['id'] == one['id']
    with pytest.raises(common.OperationsError) as exc: svc.create('isolation', {**data, 'start_reason': 'Different'}, actor=actor, key=request_key)
    assert exc.value.code == 'idempotency_conflict'


def test_fresh_roles_and_inactive_users_cannot_forge_authority(actor):
    nurse = user('nurse')
    with pytest.raises(common.OperationsError) as exc: svc.register(actor={**nurse, 'role': 'admin'})
    assert exc.value.status == 403
    with db_session() as db: db.execute('UPDATE users SET is_active=0 WHERE id=?', (actor['id'],))
    with pytest.raises(common.OperationsError) as exc: svc.register(actor=actor)
    assert exc.value.status == 401


def test_audit_failure_rolls_back_source_and_idempotency_records(actor, monkeypatch):
    pid = patient(actor)
    def fail(**kwargs): raise AuditWriteError('Synthetic failure')
    monkeypatch.setattr(common, 'log_audit_event', fail)
    with pytest.raises(AuditWriteError): create(actor, 'isolation', patient_id=pid, precautions='TEST', started_at='2026-09-01T00:00:00Z', start_reason='TEST', authority_reference='TEST')
    with db_session() as db:
        assert db.execute('SELECT COUNT(*) FROM infection_isolation_records WHERE patient_id=?', (pid,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM infection_requests WHERE patient_id=?', (pid,)).fetchone()[0] == 0
    with pytest.raises(AuditWriteError): svc.register(actor=actor)


def test_route_gates_strict_payload_and_live_role(actor, monkeypatch):
    path = '/api/infection-surveillance'
    assert client.get(path).status_code == 401
    headers = {'Authorization': 'Bearer ' + create_token(actor['id'], 'admin')}
    monkeypatch.setattr(config, 'INFECTION_SURVEILLANCE_ENABLED', False)
    assert client.get(path, headers=headers).status_code == 404
    monkeypatch.setattr(config, 'INFECTION_SURVEILLANCE_ENABLED', True)
    assert client.get(path, headers=headers).status_code == 200
    assert client.post(path + '/records/exposure', headers={**headers, 'Idempotency-Key': key()}, json={'window_id': 'X', 'patient_id': 'X', 'observed_date': '2026-09-01', 'evidence_reference': 'TEST', 'department': 'forged'}).status_code == 422
    nurse = user('nurse')
    assert client.get(path, headers={'Authorization': 'Bearer ' + create_token(nurse['id'], 'admin')}).status_code == 403


def test_definition_policy_and_history_are_immutable(actor):
    win = window(actor)
    with db_session() as db:
        with pytest.raises(DBIntegrityError): db.execute("UPDATE infection_definitions SET counting_policy='Changed' WHERE id=?", (win['definition_id'],))
        with pytest.raises(DBIntegrityError): db.execute('UPDATE infection_windows SET minimum_cell=1 WHERE id=?', (win['id'],))
        with pytest.raises(DBIntegrityError): db.execute("UPDATE infection_events SET note='Changed' WHERE record_id=?", (win['id'],))


def test_private_population_seal_is_never_serialized_by_list_detail_or_replay(actor):
    initial = window(actor)
    values = {'definition_id': initial['definition_id'], 'label': 'Replay window TEST', 'population_description': 'Explicit replay population', 'start_date': '2026-09-01', 'end_date': '2026-09-30', 'source_reference': 'Source TEST', 'rate_multiplier': 1000, 'minimum_cell': 3, 'isolate_policy': 'all_isolates'}
    request_key = key()
    win = svc.create('window', values, actor=actor, key=request_key)
    exposure(actor, win, patient(actor)); finalize(actor, win)
    assert 'finalized_counts_json' not in svc.detail('window', win['id'], actor=actor)
    assert all('finalized_counts_json' not in row for row in svc.register(actor=actor)['windows'])
    assert 'finalized_counts_json' not in svc.create('window', values, actor=actor, key=request_key)


def test_finalized_window_replay_cannot_bypass_whole_population_scope():
    dept = 'Replay ward ' + key()
    actor = user(department=dept)
    initial = window(actor)
    values = {'definition_id': initial['definition_id'], 'label': 'Replay TEST', 'population_description': 'Explicit source population', 'start_date': '2026-09-01', 'end_date': '2026-09-30', 'source_reference': 'Source TEST', 'rate_multiplier': 1, 'minimum_cell': 1, 'isolate_policy': 'all_isolates'}
    request_key = key()
    win = svc.create('window', values, actor=actor, key=request_key)
    pid = patient(actor, dept); exposure(actor, win, pid); finalize(actor, win)
    with db_session() as db: db.execute('UPDATE admissions SET department=? WHERE patient_id=?', ('Transferred ' + key(), pid))
    with pytest.raises(common.OperationsError) as exc: svc.create('window', values, actor=actor, key=request_key)
    assert exc.value.status == 404
