"""Executed regressions for SEC-01/02/03/04/06/07/08 using synthetic data."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import uuid

from fastapi.testclient import TestClient
import pytest
import socketio

from auth import create_token, hash_device_secret
from database import db_session
from main import app
from routes import auth_routes, lens_routes
from services import crypto_service, discharge_service, mfa_service, scribe_service
from ws import realtime


@pytest.fixture
def people():
    key = uuid.uuid4().hex[:10]
    a, b, pa, pb = [f'SEC-{key}-{x}' for x in ('A', 'B', 'PA', 'PB')]
    with db_session() as db:
        for uid, dept in ((a, 'SEC Cardiology'), (b, 'SEC Oncology')):
            db.execute('INSERT INTO users(id,name,role,password_hash,department) VALUES (?,?,\'doctor\',\'x\',?)', (uid, uid, dept))
        for pid, dept in ((pa, 'SEC Cardiology'), (pb, 'SEC Oncology')):
            db.execute("INSERT INTO patients(id,name,mrn,status,ward,bed) VALUES (?,?,?,'admitted','SEC Shared',?)", (pid, pid, pid, pid))
            db.execute("INSERT INTO admissions(patient_id,department,ward,status,admitted_by) VALUES (?,?,'SEC Shared','active',?)", (pid, dept, a))
    return a, b, pa, pb


def headers(uid):
    return {'Authorization': 'Bearer ' + create_token(uid, 'doctor')}


@pytest.mark.parametrize('private', [False, True])
@pytest.mark.parametrize('operation', ['edit', 'sign', 'reject', 'revise', 'verify'])
def test_scribe_foreign_lifecycle_is_404_without_mutation(people, monkeypatch, private, operation):
    a, b, pa, pb = people
    note = scribe_service.save_scribe_note(None if private else pb, b, 'private words', 'original', 'soap', status='signed' if operation == 'revise' else 'draft')
    from routes import scribe_routes
    monkeypatch.setitem(scribe_routes.SCRIBE['verify'], 'enabled', True)
    with TestClient(app) as client:
        url = f'/api/scribe/notes/{note}'
        before = scribe_service.get_scribe_note_by_id(note)
        if operation == 'edit':
            response = client.patch(url, headers=headers(a), json={'structured_note': 'replacement'})
        else:
            response = client.post(url + '/' + operation, headers=headers(a), json={'reason': 'not mine', 'acknowledge_unverified': True})
        assert response.status_code == 404, response.text
        assert scribe_service.get_scribe_note_by_id(note) == before
        with db_session() as db:
            assert db.execute('SELECT COUNT(*) FROM scribe_notes WHERE supersedes_note_id=?', (note,)).fetchone()[0] == 0


def test_scribe_services_recheck_scope_inside_write(people):
    a, b, pa, pb = people
    nid = scribe_service.save_scribe_note(pb, b, 'private', 'original', 'soap')
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        scribe_service.update_scribe_note_content(nid, 'wrong', a, scope_user={'id': a})
    assert exc.value.status_code == 404
    scribe_service.update_scribe_note_content(nid, 'allowed', b, scope_user={'id': b})
    assert scribe_service.get_scribe_note_by_id(nid)['structured_note'] == 'allowed'


def test_scribe_structure_authorizes_before_generation(people, monkeypatch):
    a, b, pa, pb = people
    from routes import scribe_routes
    async def forbidden(*args, **kwargs):
        raise AssertionError('foreign chart reached model')
        yield ''
    monkeypatch.setattr(scribe_routes, 'structure_transcript_stream', forbidden)
    with TestClient(app) as client:
        response = client.post('/api/scribe/structure', headers=headers(a), json={'patient_id': pb, 'transcript': 'private words'})
    assert response.status_code == 404


def test_ward_pack_scopes_every_patient_and_ignores_shared_cache(people, monkeypatch, tmp_path):
    a, b, pa, pb = people
    monkeypatch.setenv('ORB_FHIR_EXPORT_DIR', str(tmp_path))
    from services import downtime_service as ds
    ds.rebuild_ward_pack('SEC Shared', out_dir=tmp_path)
    with TestClient(app) as client:
        response = client.get(f'/api/downtime/{pa}/ward-pack', headers=headers(a))
        assert response.status_code == 200, response.text
        assert pa in response.text
        assert pb not in response.text
        reverse = client.get(f'/api/downtime/{pb}/ward-pack', headers=headers(b))
        assert pb in reverse.text and pa not in reverse.text


def test_totp_one_concurrent_winner_and_no_unknown_user(people):
    a, *_ = people
    secret = mfa_service.generate_secret()
    code = mfa_service.current_code(secret)
    barrier = threading.Barrier(2)
    def consume():
        barrier.wait()
        return auth_routes.consume_totp(a, secret, code)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == [False, True]
    assert not auth_routes.consume_totp(a, secret, code)
    assert not auth_routes.consume_totp('missing-user', secret, code)


def test_qr_is_encrypted_private_and_decrypts_without_pillow(monkeypatch, tmp_path):
    monkeypatch.setattr(discharge_service, 'UPLOAD_DIR', tmp_path)
    monkeypatch.setattr(crypto_service, 'UPLOAD_ENCRYPTION_KEY', 'synthetic-test-key')
    path = Path(discharge_service.generate_qr_code('synthetic', 'http://localhost/discharge/test-bearer'))
    assert crypto_service.is_encrypted_blob(path.read_bytes())
    assert path.stat().st_mode & 0o777 == 0o600
    assert crypto_service.read_decrypted(str(path)).startswith(b'\x89PNG\r\n\x1a\n')


@pytest.mark.parametrize('mode', ['denied', 'failure', 'success'])
def test_lens_media_ownership(people, monkeypatch, tmp_path, mode):
    a, b, pa, pb = people
    monkeypatch.setattr(lens_routes, 'UPLOAD_DIR', tmp_path)
    import config
    monkeypatch.setattr(config, 'LENS_ENABLED', True)
    monkeypatch.setattr(crypto_service, 'UPLOAD_ENCRYPTION_KEY', 'synthetic-test-key')
    async def generation(*args, **kwargs):
        if mode == 'failure':
            raise RuntimeError('controlled failure')
        yield 'Synthetic image description'
    monkeypatch.setattr(lens_routes, 'analyze_image_stream', generation)
    with TestClient(app) as client:
        response = client.post('/api/lens/analyze', headers=headers(a), data={'patient_id': pb if mode == 'denied' else pa}, files={'image': ('image.png', b'\x89PNG\r\n\x1a\nsynthetic', 'image/png')})
    if mode == 'denied':
        assert response.status_code == 404
    else:
        assert response.status_code == 200
    files = list(tmp_path.iterdir())
    assert len(files) == (1 if mode == 'success' else 0)
    if mode == 'success':
        assert 'analysis_id' in response.text
        with db_session() as db:
            assert db.execute('SELECT COUNT(*) FROM lens_analyses WHERE image_path=?', (str(files[0]),)).fetchone()[0] == 1
    elif mode == 'failure':
        assert 'lens_stream_failed' in response.text and '[DONE]' not in response.text


async def stop_socket_test_server():
    # Engine.IO's service task belongs to the TestClient loop. Reset the
    # test-only lifecycle state after stopping it before opening another loop.
    if realtime.sio.eio.service_task_handle is not None:
        await realtime.sio.shutdown()
    realtime.sio.eio.service_task_event = None
    realtime.sio.eio.start_service_task = True


def open_namespace(client, token):
    origin = {'Origin': 'http://testserver'}
    first = client.get('/socket.io/?EIO=4&transport=polling', headers=origin)
    sid = json.loads(first.text[1:])['sid']
    endpoint = f'/socket.io/?EIO=4&transport=polling&sid={sid}'
    client.post(endpoint, headers=origin, content='40' + json.dumps({'token': token}))
    answer = client.get(endpoint, headers=origin).text
    return sid, endpoint, answer


@pytest.mark.parametrize('change', ['token_version', 'device', 'department', 'role', 'inactive'])
def test_live_socket_cannot_receive_after_revocation_or_scope_change(people, change):
    a, b, pa, pb = people
    device = 'DEVICE-' + uuid.uuid4().hex
    with db_session() as db:
        db.execute('INSERT INTO enrolled_devices(id,label,secret_hash,enrolled_by) VALUES (?,?,?,?)', (device, 'Synthetic', hash_device_secret(device), a))
    token = create_token(a, 'doctor', device_id=device, auth_method='pin')
    with TestClient(socketio.ASGIApp(realtime.sio)) as client:
        eio_sid, endpoint, accepted = open_namespace(client, token)
        assert '40{' in accepted
        sid = realtime.sio.manager.sid_from_eio_sid(eio_sid, '/')
        from ws import scribe_stream
        # Both disconnect cleanups must run; no ghost roster or audio buffer.
        realtime._presence.setdefault(pa, {})[sid] = {'user_id': a, 'name': a, 'role': 'doctor'}
        scribe_stream._create_session(sid, patient_id=pa)
        try:
            client.portal.call(realtime.emit_patient_vitals, 'vitals:update', {'patient_id': pa, 'heart_rate': 80})
            before = client.get(endpoint, headers={'Origin': 'http://testserver'})
            assert 'vitals:update' in before.text
            with db_session() as db:
                if change == 'device':
                    db.execute('UPDATE enrolled_devices SET revoked_at=CURRENT_TIMESTAMP WHERE id=?', (device,))
                else:
                    column, value = {'token_version': ('token_version', 1), 'department': ('department', 'SEC Oncology'), 'role': ('role', 'patient'), 'inactive': ('is_active', 0)}[change]
                    db.execute(f'UPDATE users SET {column}=? WHERE id=?', (value, a))
            client.portal.call(realtime.emit_patient_vitals, 'vitals:update', {'patient_id': pa, 'heart_rate': 81})
            after = client.get(endpoint, headers={'Origin': 'http://testserver'})
            assert 'vitals:update' not in after.text
            assert not realtime.sio.manager.is_connected(sid, '/')
            assert sid not in realtime.sio.session_principals
            assert sid not in realtime._presence.get(pa, {})
            assert sid not in scribe_stream._sessions
            if change in ('device', 'token_version', 'inactive'):
                assert realtime._authenticate({'token': token}) is None
        finally:
            client.post(endpoint, headers={'Origin': 'http://testserver'}, content='1')
            client.portal.call(stop_socket_test_server)


def test_revoked_device_rejected_at_actual_socket_handshake(people):
    a, *_ = people
    device = 'DEVICE-' + uuid.uuid4().hex
    with db_session() as db:
        db.execute('INSERT INTO enrolled_devices(id,label,secret_hash,enrolled_by,revoked_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)', (device, 'Synthetic', hash_device_secret(device), a))
    with TestClient(socketio.ASGIApp(realtime.sio)) as client:
        eio_sid, endpoint, answer = open_namespace(client, create_token(a, 'doctor', device_id=device))
        assert answer.startswith('44'), answer
        client.post(endpoint, headers={'Origin': 'http://testserver'}, content='1')
        client.portal.call(stop_socket_test_server)


@pytest.mark.parametrize('cancel_at', ['stream', 'never_started', 'registration'])
async def test_lens_cancellation_and_registration_failure_cleanup(people, monkeypatch, tmp_path, cancel_at):
    import io
    from starlette.datastructures import UploadFile
    import config
    a, b, pa, pb = people
    with db_session() as db:
        user = dict(db.execute('SELECT * FROM users WHERE id=?', (a,)).fetchone())
    monkeypatch.setattr(config, 'LENS_ENABLED', True)
    monkeypatch.setattr(lens_routes, 'UPLOAD_DIR', tmp_path)
    async def generated(*args, **kwargs):
        yield 'partial description'
        yield ' complete'
    monkeypatch.setattr(lens_routes, 'analyze_image_stream', generated)
    def failed_save(**kwargs):
        raise RuntimeError('controlled registration failure')
    if cancel_at == 'registration':
        monkeypatch.setattr(lens_routes, 'save_lens_analysis', failed_save)
    response = await lens_routes.analyze_image(
        UploadFile(io.BytesIO(b'\x89PNG\r\n\x1a\nsynthetic'), filename='test.png'),
        clinical_context='', patient_id=pa, image_type='other', user=user,
    )
    assert len(list(tmp_path.iterdir())) == 1
    if cancel_at == 'stream':
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
    elif cancel_at == 'registration':
        frames = [frame async for frame in response.body_iterator]
        assert 'lens_stream_failed' in ''.join(frames)
    await response.background()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('legacy', [False, True])
def test_qr_route_serves_decrypted_or_legacy_png_with_no_store(people, monkeypatch, tmp_path, legacy):
    a, b, pa, pb = people
    monkeypatch.setattr(discharge_service, 'UPLOAD_DIR', tmp_path)
    monkeypatch.setattr(crypto_service, 'UPLOAD_ENCRYPTION_KEY', '' if legacy else 'synthetic-test-key')
    path = discharge_service.generate_qr_code(pa, 'http://localhost/discharge/synthetic')
    with db_session() as db:
        db.execute('INSERT INTO discharge_summaries(patient_id,author_id,summary,qr_code_path) VALUES (?,?,?,?)', (pa, a, 'Synthetic summary', path))
    with TestClient(app) as client:
        response = client.get(f'/api/vigil/patients/{pa}/qr', headers=headers(a))
        assert response.status_code == 200, response.text
        assert response.content.startswith(b'\x89PNG\r\n\x1a\n')
        assert response.headers['cache-control'] == 'no-store'
        assert client.get(f'/api/vigil/patients/{pa}/qr', headers=headers(b)).status_code == 404


async def test_lens_disconnect_during_registration_retains_durable_media(people, monkeypatch, tmp_path):
    import io
    import config
    from starlette.datastructures import UploadFile
    a, _, pa, _ = people
    with db_session() as db:
        user = dict(db.execute('SELECT * FROM users WHERE id=?', (a,)).fetchone())
    monkeypatch.setattr(config, 'LENS_ENABLED', True)
    monkeypatch.setattr(lens_routes, 'UPLOAD_DIR', tmp_path)
    started, release = threading.Event(), threading.Event()
    original_save = lens_routes.save_lens_analysis
    def delayed_save(**kwargs):
        started.set()
        assert release.wait(5)
        return original_save(**kwargs)
    async def generated(*args, **kwargs):
        yield 'complete synthetic description'
    monkeypatch.setattr(lens_routes, 'save_lens_analysis', delayed_save)
    monkeypatch.setattr(lens_routes, 'analyze_image_stream', generated)
    response = await lens_routes.analyze_image(
        UploadFile(io.BytesIO(b'\x89PNG\r\n\x1a\nsynthetic'), filename='test.png'),
        clinical_context='', patient_id=pa, image_type='other', user=user,
    )
    await anext(response.body_iterator)
    task = asyncio.create_task(anext(response.body_iterator))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await response.background()
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    with db_session() as db:
        assert db.execute('SELECT COUNT(*) FROM lens_analyses WHERE image_path=?', (str(files[0]),)).fetchone()[0] == 1


@pytest.mark.parametrize('scope_changes', [False, True])
def test_scribe_structure_rechecks_scope_at_initial_save(people, monkeypatch, scope_changes):
    a, _, pa, _ = people
    from routes import scribe_routes
    async def generate(*args, **kwargs):
        yield 'Synthetic authorized note'
        if scope_changes:
            with db_session() as db:
                db.execute('UPDATE users SET department=? WHERE id=?', ('SEC Oncology', a))
    monkeypatch.setattr(scribe_routes, 'structure_transcript_stream', generate)
    with TestClient(app) as client:
        response = client.post('/api/scribe/structure', headers=headers(a), json={'patient_id': pa, 'transcript': 'Synthetic initial note'})
    assert response.status_code == 200
    with db_session() as db:
        count = db.execute('SELECT COUNT(*) FROM scribe_notes WHERE patient_id=?', (pa,)).fetchone()[0]
    assert count == (0 if scope_changes else 1)
    assert ('scribe_stream_failed' in response.text) is scope_changes


async def test_scribe_background_verification_does_not_need_request_user(people, monkeypatch):
    a, _, pa, _ = people
    from routes import scribe_routes
    from services.scribe_providers import StageTimer
    note = scribe_service.save_scribe_note(pa, a, 'Synthetic transcript', 'Synthetic note', 'soap')
    async def verify(*args, **kwargs):
        return {'verified': True, 'warnings': [], 'omissions': []}
    monkeypatch.setattr(scribe_routes, 'verify_note', verify)
    await scribe_routes._run_verification_async(note_id=note, transcript='Synthetic transcript', structured_note='Synthetic note', draft_timer=StageTimer(), patient_id=pa, actor_id=a)
    row = scribe_service.get_scribe_note_by_id(note)
    assert row['verification_json']['status'] == scribe_routes.VERIFY_COMPLETE
