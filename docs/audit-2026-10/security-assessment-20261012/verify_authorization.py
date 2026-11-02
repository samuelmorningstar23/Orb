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

from database import db_session
from auth import create_token, hash_device_secret
from main import app
from fastapi.testclient import TestClient
from services.scribe_service import save_scribe_note, get_scribe_note_by_id
from services.discharge_service import generate_qr_code
from ws import realtime

with db_session() as db:
    db.execute("INSERT INTO users (id,name,role,password_hash,department,is_active) VALUES (?,?,?,?,?,1)",
               ('REVIEW-DOC-B','Synthetic Review Doctor B','doctor','not-a-real-password-hash','Review Oncology'))
    db.execute("INSERT INTO patients (id,name,mrn,status,ward,bed) VALUES (?,?,?,'admitted',?,?)",
               ('REVIEW-PAT-A','Synthetic Review Patient A','REVIEW-MRN-A','Review Shared Ward','REVIEW-A'))
    db.execute("INSERT INTO admissions (patient_id,department,ward,bed,status,admitted_by) VALUES (?,?,?,?,?,?)",
               ('REVIEW-PAT-A','Review Cardiology','Review Shared Ward','REVIEW-A','active','REVIEW-DOC-A'))
    db.execute("UPDATE patients SET ward=?,bed=?,status='admitted' WHERE id=?",
               ('Review Shared Ward','REVIEW-B','REVIEW-PAT-B'))
    db.execute("UPDATE admissions SET ward=? WHERE patient_id=?",('Review Shared Ward','REVIEW-PAT-B'))
    db.execute("INSERT INTO enrolled_devices (id,label,department,secret_hash,enrolled_by,revoked_at) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
               ('REVIEW-REVOKED-DEVICE','Synthetic Revoked Tablet','Review Cardiology',hash_device_secret('synthetic-device-secret'),'REVIEW-DOC-A'))

client=TestClient(app)
headers={'Authorization':'Bearer '+create_token('REVIEW-DOC-A','doctor')}
foreign_note=save_scribe_note('REVIEW-PAT-B','REVIEW-DOC-B','Synthetic foreign transcript',
                              'Synthetic foreign draft','soap')
read=client.get(f'/api/scribe/notes/{foreign_note}',headers=headers)
edit=client.patch(f'/api/scribe/notes/{foreign_note}',headers=headers,
                  json={'structured_note':'Synthetic replacement by a different department'})
stored=get_scribe_note_by_id(foreign_note)
assert read.status_code==404,(read.status_code,read.text)
assert edit.status_code==200,(edit.status_code,edit.text)
assert stored['structured_note']=='Synthetic replacement by a different department',stored

# Existing endpoint permits signed unfiled notes. Seed that existing state as
# a fixture; no clinical signing gate is changed or bypassed by this probe.
private_note=save_scribe_note(None,'REVIEW-DOC-B','Synthetic private unfiled transcript',
                             'Synthetic private unfiled signed text','soap',status='signed')
private_read=client.get(f'/api/scribe/notes/{private_note}',headers=headers)
revision=client.post(f'/api/scribe/notes/{private_note}/revise',headers=headers)
assert private_read.status_code==404,(private_read.status_code,private_read.text)
assert revision.status_code==200,(revision.status_code,revision.text)
new_id=revision.json()['new_note_id']
copied_read=client.get(f'/api/scribe/notes/{new_id}',headers=headers)
assert copied_read.status_code==200,(copied_read.status_code,copied_read.text)
assert 'Synthetic private unfiled transcript' in copied_read.text

pack=client.get('/api/downtime/REVIEW-PAT-A/ward-pack',headers=headers)
foreign_read=client.get('/api/downtime/REVIEW-PAT-B/latest',headers=headers)
assert foreign_read.status_code==404,(foreign_read.status_code,foreign_read.text)
assert pack.status_code==200,(pack.status_code,pack.text[:200])
assert 'Synthetic Review Patient B' in pack.text

device_token=create_token('REVIEW-DOC-A','doctor',device_id='REVIEW-REVOKED-DEVICE',auth_method='pin')
device_http=client.get('/api/auth/me',headers={'Authorization':'Bearer '+device_token})
device_socket_identity=realtime._authenticate({'token':device_token})
assert device_http.status_code==401,(device_http.status_code,device_http.text)
assert device_socket_identity and device_socket_identity['id']=='REVIEW-DOC-A'

previous_umask=os.umask(0o022)
try:
    qr_path=Path(generate_qr_code('SYNTHETIC-QR','http://localhost/discharge/synthetic-public-fixture-only'))
finally:
    os.umask(previous_umask)
assert qr_path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
assert (qr_path.stat().st_mode & 0o777)==0o644

result={
 'schema':'orb.security.authorization-regressions/1',
 'isolation':'Owned source/DB/synthetic data only. In-process ASGI HTTP and actual auth helper. No network/model calls or live data/config reads.',
 'scribe_cross_department_edit':{'scoped_read_status':read.status_code,'edit_status':edit.status_code,
   'foreign_draft_changed':True,'note_id':foreign_note,'author_remains':'REVIEW-DOC-B'},
 'scribe_unfiled_revision_disclosure':{'original_read_status':private_read.status_code,'revise_status':revision.status_code,
   'copied_read_status':copied_read.status_code,'other_author_transcript_disclosed':True,
   'fixture':'Other clinician signed unfiled note, a state currently permitted by signing route.'},
 'downtime_shared_ward_disclosure':{'foreign_patient_direct_read_status':foreign_read.status_code,
   'own_patient_ward_pack_status':pack.status_code,'foreign_department_name_present':True,
   'shared_physical_ward':'Review Shared Ward','caller_department':'Review Cardiology','other_patient_department':'Review Oncology'},
 'revoked_tablet_handshake_parity':{'http_me_status':device_http.status_code,
   'socket_auth_helper_accepted':bool(device_socket_identity),
   'verification_level':'Actual socket authentication helper; complete wire handshake verified separately if available.'},
 'discharge_qr_permissions':{'synthetic_url_only':True,'umask':'022','png_plaintext':True,
   'file_mode':oct(qr_path.stat().st_mode & 0o777),'configured_upload_encryption':True},
 'source_sha256':{str(p):hashlib.sha256((REPO/p).read_bytes()).hexdigest() for p in [
   Path('server/routes/scribe_routes.py'),Path('server/services/scribe_service.py'),
   Path('server/ws/realtime.py'),Path('server/auth.py'),Path('server/routes/downtime_routes.py'),
   Path('server/services/downtime_service.py'),Path('server/services/discharge_service.py')]},
}
(WORK/'authorization-results.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))

