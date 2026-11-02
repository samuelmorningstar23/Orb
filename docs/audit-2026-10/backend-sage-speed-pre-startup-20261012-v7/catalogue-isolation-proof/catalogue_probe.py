from pathlib import Path
import hashlib,json
import pytest
STATE={}
OUTPUT=Path('/private/tmp/orb-prose-catalogue-isolation-20261012-iqyvrnwd/restoration-proof.json')
def snapshot():
 import config
 from database import db_session
 with db_session() as db:
  rows=[dict(r) for r in db.execute('SELECT id,name,generic_name,strength FROM helix_medication_master ORDER BY id')]
 return {'db_path':str(config.DB_PATH),'catalogue_count':len(rows),'catalogue_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),'heparin_units_ids':[r['id'] for r in rows if r['name']=='Heparin Stockrule 5000 units/mL']}
def persist(): OUTPUT.write_text(json.dumps(STATE,indent=2)+'\n')
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
 if item.nodeid.startswith('tests/test_patient_specific_screen.py::') and 'before' not in STATE:
  STATE['before']=snapshot();assert STATE['before']['heparin_units_ids'];persist()
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
 outcome=yield
 if item.nodeid.endswith('::test_the_prose_lexicon_contains_no_drug_name'):
  STATE['during']=snapshot();assert STATE['during']['db_path']!=STATE['before']['db_path'];assert STATE['during']['heparin_units_ids']==[];assert outcome.excinfo is None;persist()
