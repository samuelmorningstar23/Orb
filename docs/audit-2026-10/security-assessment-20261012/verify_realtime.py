"""Bounded local regression probes using synthetic records and harmless files."""
from pathlib import Path
import asyncio
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import time

WORK = Path(__file__).resolve().parent
REPO = Path('/Users/samuelmorningstar/Documents/Projects/Orb')
SOURCE = WORK / 'source'
os.chdir(SOURCE / 'server')
sys.path.insert(0, str(SOURCE / 'server'))
for name in list(os.environ):
    if name.startswith('ORB_'):
        del os.environ[name]
os.environ.update({
    'HOME': str(WORK / 'home'),
    'ORB_DEPLOYMENT_PROFILE': 'dev',
    'ORB_DB_PATH': str(WORK / 'orb_test_security.db'),
    'ORB_SITE_CONFIG_FILE': str(WORK / 'no-site.toml'),
    'ORB_JWT_SECRET': secrets.token_urlsafe(48),
    'ORB_UPLOAD_ENCRYPTION_KEY': secrets.token_urlsafe(48),
    'ORB_INGESTION_API_KEY': secrets.token_urlsafe(48),
    'ORB_INGESTION_MODE': 'ingestion',
    'ORB_MODELS_ENABLED': '1',
    'ORB_BACKGROUND_MODELS_ENABLED': '0',
    'ORB_OLLAMA_BASE_URL': 'http://127.0.0.1:9',
    'ORB_SCORING_ENABLED': '0',
    'ORB_SEPSIS_AUTOSTART': '0',
    'ORB_WARD_RECORD': '0',
    'ORB_LENS_ENABLED': '1',
    'ORB_REQUIRE_UPLOAD_ENCRYPTION': '1',
    'ORB_BACKUP_DIR': str(WORK / 'backups'),
    'ORB_WATCHDOG_DIR': str(WORK / 'watchdog'),
    'ORB_FHIR_EXPORT_DIR': str(WORK / 'fhir'),
    'HF_HOME': str(WORK / 'empty-hf'),
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
})

from concurrent.futures import ThreadPoolExecutor
import threading
import socketio
from fastapi.testclient import TestClient
from auth import create_token
from database import db_session
import database
from main import app
from ws import realtime
from routes import auth_routes
from services import mfa_service

def open_namespace(client, token):
    origin={'Origin':'http://testserver'}
    response=client.get('/socket.io/?EIO=4&transport=polling',headers=origin)
    assert response.status_code==200 and response.text.startswith('0'),(response.status_code,response.text)
    engine_sid=json.loads(response.text[1:])['sid']
    path='/socket.io/?EIO=4&transport=polling&sid='+engine_sid
    connected=client.post(path,headers=origin,content='40'+json.dumps({'token':token}))
    assert connected.status_code==200,(connected.status_code,connected.text)
    accepted=client.get(path,headers=origin)
    assert accepted.status_code==200 and '40{' in accepted.text,(accepted.status_code,accepted.text)
    return path,origin,accepted.text

regular=create_token('REVIEW-DOC-A','doctor')
revoked_device=create_token('REVIEW-DOC-A','doctor',device_id='REVIEW-REVOKED-DEVICE',auth_method='pin')
wire=socketio.ASGIApp(realtime.sio)
with TestClient(wire) as client:
    device_path,device_origin,device_connect=open_namespace(client,revoked_device)
    normal_path,normal_origin,normal_connect=open_namespace(client,regular)
    with db_session() as db:
        db.execute("UPDATE users SET token_version=COALESCE(token_version,0)+1 WHERE id=?",('REVIEW-DOC-A',))
    rest=TestClient(app).get('/api/auth/me',headers={'Authorization':'Bearer '+regular})
    assert rest.status_code==401,(rest.status_code,rest.text)
    assert realtime._authenticate({'token':regular}) is None
    client.portal.call(realtime.emit_patient_vitals,'vitals:update',
                       {'patient_id':'REVIEW-PAT-A','heart_rate':72,'risk':{'level':'low'}})
    ordinary_delivery=client.get(normal_path,headers=normal_origin)
    device_delivery=client.get(device_path,headers=device_origin)
    assert ordinary_delivery.status_code==200 and 'vitals:update' in ordinary_delivery.text
    assert 'Synthetic Review Patient A' in ordinary_delivery.text
    assert device_delivery.status_code==200 and 'vitals:update' in device_delivery.text
    for path,origin in [(normal_path,normal_origin),(device_path,device_origin)]:
        client.post(path,headers=origin,content='41')
        client.post(path,headers=origin,content='1')
    client.portal.call(realtime.sio.shutdown)

# Deterministic two-reader scheduling of the real single-use function. Only
# scheduling is controlled: code verification and DB reads/writes are real.
with db_session() as db:
    db.execute("UPDATE users SET totp_last_step=NULL WHERE id=?",('REVIEW-DOC-A',))
secret=mfa_service.generate_secret()
code=mfa_service.current_code(secret)
barrier=threading.Barrier(2,timeout=5)
original_get_db=database.get_db
class DelayedReadCursor:
    def __init__(self,cursor): self.cursor=cursor
    def fetchone(self):
        value=self.cursor.fetchone()
        barrier.wait()
        return value
class ScheduledConnection:
    def __init__(self,connection): self.connection=connection
    def execute(self,sql,args=()):
        cursor=self.connection.execute(sql,args)
        return DelayedReadCursor(cursor) if sql.startswith('SELECT totp_last_step') else cursor
    def commit(self): return self.connection.commit()
    def close(self): return self.connection.close()
database.get_db=lambda:ScheduledConnection(original_get_db())
try:
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(auth_routes.consume_totp,'REVIEW-DOC-A',secret,code) for _ in range(2)]
        consumed=[future.result(timeout=8) for future in futures]
finally:
    database.get_db=original_get_db
assert consumed==[True,True],consumed
sequential_replay=auth_routes.consume_totp('REVIEW-DOC-A',secret,code)
assert sequential_replay is False

result={
 'schema':'orb.security.realtime-mfa-regressions/1',
 'isolation':'In-process actual Engine.IO/Socket.IO polling; owned DB and synthetic records. No native network/model calls.',
 'revoked_device_wire_handshake':{'accepted':True,'wire_packet_type':'Socket.IO namespace connect 40',
   'token':'Fixture token deliberately omitted','device_fixture':'Existing revoked enrolled_devices row'},
 'live_socket_revocation':{'rest_after_user_revocation':rest.status_code,'fresh_socket_auth_after_revocation':False,
   'previously_connected_socket_received_event':'vitals:update','patient_name_received':True,
   'ordinary_socket_wire':ordinary_delivery.text,'revoked_device_socket_wire':device_delivery.text,
   'scope':'Actual wire deliveries after token_version bump; expiry/department-change variants source-reviewed separately.'},
 'totp_concurrent_single_use':{'same_step_results':consumed,'sequential_replay_result':sequential_replay,
   'controlled_scheduling':'Barrier after each actual SELECT fetchone; no change to verification/UPDATE/commit logic.',
   'verification_level':'Actual consume_totp function and SQLite connections; complete login HTTP race not run.'},
 'socket_disconnect_handler':{'module':realtime.sio.handlers['/']['disconnect'].__module__,
   'name':realtime.sio.handlers['/']['disconnect'].__name__},
 'source_sha256':{str(p):hashlib.sha256((REPO/p).read_bytes()).hexdigest() for p in [
   Path('server/ws/realtime.py'),Path('server/ws/scribe_stream.py'),Path('server/routes/auth_routes.py'),Path('server/database.py')]},
 'cleanup':'Both owned Socket.IO namespaces and Engine.IO sessions explicitly closed; server service tasks stopped.',
}
(WORK/'realtime-results.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))

