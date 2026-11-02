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

from database import db_session, init_db
init_db()
from auth import create_token
from config import UPLOAD_DIR
from main import app
from fastapi.testclient import TestClient
from routes import lens_routes

assert Path(UPLOAD_DIR).resolve().is_relative_to(SOURCE), UPLOAD_DIR
with db_session() as db:
    db.execute("INSERT INTO users (id,name,role,password_hash,department,is_active) VALUES (?,?,?,?,?,1)",
               ('REVIEW-DOC-A','Synthetic Review Doctor A','doctor','not-a-real-password-hash','Review Cardiology'))
    db.execute("INSERT INTO patients (id,name,mrn) VALUES (?,?,?)",
               ('REVIEW-PAT-B','Synthetic Review Patient B','REVIEW-MRN-B'))
    db.execute("INSERT INTO admissions (patient_id,department,ward,bed,status,admitted_by) VALUES (?,?,?,?,?,?)",
               ('REVIEW-PAT-B','Review Oncology','Review Oncology','REVIEW-1','active','REVIEW-DOC-A'))

client = TestClient(app)
headers = {'Authorization': 'Bearer ' + create_token('REVIEW-DOC-A','doctor')}
image = b'\x89PNG\r\n\x1a\n' + b'synthetic-only' * 10
before = set(Path(UPLOAD_DIR).iterdir())
response = client.post('/api/lens/analyze',headers=headers,
                       data={'patient_id':'REVIEW-PAT-B'},
                       files={'image':('synthetic.png',image,'image/png')})
added = set(Path(UPLOAD_DIR).iterdir()) - before
with db_session() as db:
    saved = db.execute("SELECT COUNT(*) FROM lens_analyses WHERE author_id='REVIEW-DOC-A'").fetchone()[0]
assert response.status_code == 404, (response.status_code,response.text)
assert len(added) == 1 and saved == 0, (added,saved)
unauth_before = set(Path(UPLOAD_DIR).iterdir())
unauth = client.post('/api/lens/analyze',files={'image':('synthetic.png',image,'image/png')})
assert unauth.status_code == 401 and set(Path(UPLOAD_DIR).iterdir()) == unauth_before

# Exercise the existing failed-stream cleanup branch with a controlled local
# model seam. This does not contact an inference process or load model weights.
async def controlled_failed_stream(*args, **kwargs):
    raise RuntimeError('Controlled synthetic inference failure')
    yield ''
lens_routes.analyze_image_stream = controlled_failed_stream
failed_before = set(Path(UPLOAD_DIR).iterdir())
failed = client.post('/api/lens/analyze',headers=headers,
                     files={'image':('synthetic.png',image,'image/png')})
failed_added = set(Path(UPLOAD_DIR).iterdir()) - failed_before
assert failed.status_code == 200 and 'lens_stream_failed' in failed.text
assert len(failed_added) == 1, failed_added
with db_session() as db:
    failed_saved = db.execute("SELECT COUNT(*) FROM lens_analyses WHERE author_id='REVIEW-DOC-A'").fetchone()[0]
assert failed_saved == 0

# Use the restore script's exact extraction command in a wholly owned fixture.
# A normal top-level member, without traversal/symlinks, is sufficient to prove
# that the extraction boundary is the parent of uploads rather than uploads.
fixture = WORK / 'restore-fixture'
uploads = fixture / 'uploads'
uploads.mkdir(parents=True)
sentinel = fixture / 'review-sentinel.txt'
sentinel.write_text('original harmless fixture\n')
archive = WORK / 'synthetic-uploads.tar'
with tarfile.open(archive,'w') as tar:
    payload = b'replaced harmless fixture\n'
    info = tarfile.TarInfo('review-sentinel.txt')
    info.size = len(payload)
    tar.addfile(info,io.BytesIO(payload))
command = ['tar','-xf',str(archive),'-C',str(uploads.parent)]
completed = subprocess.run(command,check=False,capture_output=True,text=True,timeout=5)
assert completed.returncode == 0 and sentinel.read_text() == 'replaced harmless fixture\n'

result = {
    'schema':'orb.security.boundary-regressions/1',
    'isolation': 'Owned source/DB/media/tar fixture only; no network/model calls or live data/config reads.',
    'lens_scope': {
        'endpoint':'POST /api/lens/analyze',
        'caller':'Synthetic Cardiology doctor', 'patient':'Synthetic Oncology patient',
        'response_status':response.status_code,'response':response.json(),
        'new_orphan_files':len(added),'registered_analyses':saved,
        'files_encrypted':all(p.read_bytes().startswith(b'ORBENC1\n') for p in added),
        'file_modes':[oct(p.stat().st_mode & 0o777) for p in sorted(added)],
        'unauthenticated_control_status':unauth.status_code,
        'unauthenticated_control_created_files':0,
    },
    'lens_failure_cleanup':{
        'controlled_model_seam':True,'response_status':failed.status_code,
        'terminal_error_present':'lens_stream_failed' in failed.text,
        'new_orphan_files':len(failed_added),'registered_analyses':failed_saved,
    },
    'restore_archive_containment':{
        'command':command,'exit_code':completed.returncode,
        'member':'review-sentinel.txt','traversal_or_symlink_used':False,
        'file_outside_uploads_replaced':True,
        'restoration_script_invoked':False,
        'scope':'Exact extraction command only, owned harmless fixture; full restore/gating not exercised.',
    },
    'source_sha256':{
        str(p):hashlib.sha256((REPO/p).read_bytes()).hexdigest()
        for p in [Path('server/routes/lens_routes.py'),Path('scripts/standby-restore.sh'),Path('server/services/crypto_service.py')]
    },
}
(WORK/'boundary-results.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
