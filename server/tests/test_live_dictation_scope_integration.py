"""Actual authenticated Socket.IO frames cannot read an out-of-scope chart."""
import json

from fastapi.testclient import TestClient
import pytest
import socketio

from auth import create_token
from database import db_session
from tests.test_security_remediation import people as people, open_namespace, stop_socket_test_server
from ws import realtime


@pytest.mark.parametrize('refusal', ['other_department', 'missing_patient', 'role'])
def test_socket_dictation_authorizes_before_hint_lookup(people, monkeypatch, refusal):
    doctor, _, own, other = people
    monkeypatch.setattr('config.MODELS_ENABLED', True)
    monkeypatch.setattr('config.WARD_RECORD', False)
    monkeypatch.setattr('services.scribe_service.build_whisper_hints', lambda *_: pytest.fail('unauthorized hint lookup'))
    if refusal == 'role':
        with db_session() as db:
            db.execute("UPDATE users SET role='patient' WHERE id=?", (doctor,))
    token = create_token(doctor, 'patient' if refusal == 'role' else 'doctor')
    with TestClient(socketio.ASGIApp(realtime.sio)) as client:
        _, endpoint, accepted = open_namespace(client, token)
        assert '40{' in accepted
        try:
            patient = {'other_department': other, 'missing_patient': 'ABSENT-SYNTHETIC', 'role': ''}[refusal]
            client.post(endpoint, headers={'Origin': 'http://testserver'}, content='42'+json.dumps(['scribe:start_session', {'patient_id': patient, 'stream_id': 'synthetic-stream'}]))
            answer = client.get(endpoint, headers={'Origin': 'http://testserver'}).text
            assert 'scribe_forbidden' in answer and 'synthetic-stream' in answer
        finally:
            client.post(endpoint, headers={'Origin': 'http://testserver'}, content='1')
            client.portal.call(stop_socket_test_server)
