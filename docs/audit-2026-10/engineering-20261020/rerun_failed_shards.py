"""Retain passed shards; rerun failed shards after test-only fixture fixes."""
from pathlib import Path
import hashlib,importlib.util,json,os,shutil,subprocess,time
root=Path('/Users/samuelmorningstar/Documents/Projects/Orb');sel=Path('/private/tmp/orb-engineering-selected');out=Path('/private/tmp/orb-engineering-selected-full-20261020-v7');python='/private/tmp/orb-engineering-runtime-20261020/bin/python'
spec=importlib.util.spec_from_file_location('owned_retry_runner',root/'tools/backend-quality/run_backend.py');runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
allowed={'server/tests/test_aggregate_reasoning.py','server/tests/test_sage_department_scope.py','server/tests/test_sage_sticky_anchor.py'}
for name in json.loads((sel/'manifest.json').read_text()):
 if not name.startswith(('server/','scripts/')) or name.endswith(('.pdf','.md')) or name == 'scripts/build_work_summary_pdf.py':continue
 old=out/'source'/name
 if old.exists() and old.read_bytes()!=(sel/name).read_bytes():assert name in allowed | {'server/services/sage_ambient_service.py','server/routes/relay_routes.py','server/services/shadow_scorecard_service.py','server/tests/test_sage_ambient_retraction.py','server/tests/test_wedge_end_to_end.py','server/tests/test_shadow_scorecard.py'}, 'Unexpected source change: '+name
children=[];done={};launched=set();started=time.monotonic()
try:
 while True:
  report=json.loads((out/'manifest.json').read_text())
  for row in report['shards']:
   index=row['index']
   if row.get('exit_code',0)==0 or index in launched:continue
   directory=out/f'shard-{index}-final2';shutil.copytree(out/'source',directory)
   for name in allowed:
    shutil.copyfile(sel/name,directory/name)
   state=directory/'final-state';state.mkdir()
   command=[python,'-m','coverage','run','--rcfile='+str(out/'coverage.ini'),'--data-file='+str(out/'coverage'/f'.coverage.retry.{index}'),'-m','pytest','-q','--tb=short','--junitxml='+str(out/f'shard-{index}-retry.xml'),*row['files']]
   log=(out/f'shard-{index}-final2.log').open('w');child=subprocess.Popen(command,cwd=directory/'server',env=runner.environment(state),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
   attempt={'index':index,'command':command,'log':str(out/f'shard-{index}-final2.log'),'source':str(directory),'fixture_sha256':{n:hashlib.sha256((directory/n).read_bytes()).hexdigest() for n in allowed}}
   children.append((child,log,attempt,time.monotonic()));launched.add(index);print(f'Rerunning complete shard {index}; production source unchanged.',flush=True)
  for child,log,attempt,t0 in children:
   if attempt['index'] not in done and child.poll() is not None:
    done[attempt['index']]={**attempt,'exit_code':child.returncode,'elapsed_seconds':round(time.monotonic()-t0,2)};log.close();print('Final shard result: '+json.dumps(done[attempt['index']]),flush=True)
  if 'passed' in report and len(done)==len(launched):break
  time.sleep(5)
finally:runner._stop_owned_children(children)
assert launched, 'No failing shards were rerun'
assert all(a['exit_code']==0 for a in done.values()), 'A final shard still failed'
archive=out/'initial-results';archive.mkdir(exist_ok=False)
shutil.copyfile(out/'manifest.json',archive/'manifest.json');shutil.copyfile(out/'source-sha256.json',archive/'source-sha256.json')
report=json.loads((out/'manifest.json').read_text())
for row in report['shards']:
 index=row['index']
 if index in done:
  old={k:row[k] for k in ['exit_code','command','log','elapsed_seconds'] if k in row};shutil.move(out/f'shard-{index}.xml',archive/f'shard-{index}.xml');shutil.move(out/'coverage'/f'.coverage.{index}',archive/f'.coverage.{index}');shutil.move(out/f'shard-{index}-retry.xml',out/f'shard-{index}.xml');shutil.move(out/'coverage'/f'.coverage.retry.{index}',out/'coverage'/f'.coverage.{index}')
  row.update(done[index]);row['initial_attempt']=old
for name in allowed:shutil.copyfile(sel/name,out/'source'/name)
manifest=json.loads((out/'source-sha256.json').read_text())
for name in allowed:manifest[name]=hashlib.sha256((out/'source'/name).read_bytes()).hexdigest()
(out/'source-sha256.json').write_text(json.dumps(manifest,indent=2)+'\n')
report['fixture_reruns']=sorted(launched | set(report.get('prior_fixture_reruns', [])));report['verification_boundary']='Failed shards were rerun in full after test-only census-isolation fixes; other passing shards retained. Production source identical across every accepted shard.'
report['passed']=all(row['exit_code']==0 for row in report['shards']) and report['process_cleanup_exit_code'] in (None,0)
for command in [[python,'-m','coverage','combine','--keep','--rcfile='+str(out/'coverage.ini'),'--data-file='+str(out/'.coverage'),str(out/'coverage')],[python,'-m','coverage','json','--rcfile='+str(out/'coverage.ini'),'--data-file='+str(out/'.coverage'),'-o',str(out/'coverage.json')]]:subprocess.run(command,cwd=out/'source/server',check=True)
report['counts']=runner.junit_counts(sorted(out.glob('*.xml')));report['final_retry_elapsed_seconds']=round(time.monotonic()-started,2)
(out/'manifest.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'counts':report['counts'],'fixture_reruns':report['fixture_reruns']}),flush=True)
