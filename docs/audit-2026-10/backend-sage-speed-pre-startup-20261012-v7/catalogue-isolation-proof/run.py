from pathlib import Path
import importlib.util,json,subprocess,sys
base=Path(__file__).resolve().parent;source=base/'source';phase=sys.argv[1]
spec=importlib.util.spec_from_file_location('proof_launcher',source/'scripts/e2e_server.py');launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
state=base/(phase+'-state');state.mkdir()
env=launcher.isolated_environment(state)
env.update(ORB_DB_PATH=str(state/'orb_test_prose_proof.db'),ORB_SKIP_KNOWLEDGE_BASE='1',ORB_BACKGROUND_MODELS_ENABLED='0',ORB_KNOWLEDGE_INPROCESS='1',ORB_SCORING_ENABLED='1',ORB_SEPSIS_AUTOSTART='1',ORB_INGESTION_API_KEY='orb-ingest-dev-key',PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
command=[sys.executable,'-m','pytest','-q','--tb=short',*sys.argv[2:]]
with (base/(phase+'.log')).open('w') as log:
 result=subprocess.run(command,cwd=source/'server',env=env,stdout=log,stderr=subprocess.STDOUT)
record={'command':command,'cwd':str(source/'server'),'state':str(state),'log':str(base/(phase+'.log')),'exit_code':result.returncode,'isolation':'Owned source/DB/uploads/FHIR/watchdog/cache, closed Ollama port9 and offline HF; no live data/models.'}
(base/(phase+'-verification.json')).write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record));print((base/(phase+'.log')).read_text()[-4500:])
