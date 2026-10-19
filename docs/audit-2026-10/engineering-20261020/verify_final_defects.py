from pathlib import Path
import importlib.util,json,shutil,subprocess
root=Path('/Users/samuelmorningstar/Documents/Projects/Orb');sel=Path('/private/tmp/orb-engineering-selected');work=Path('/private/tmp/orb-engineering-final-known-defects');shutil.copytree('/private/tmp/orb-engineering-selected-full-20261020-v7/source',work)
for n in json.loads((sel/'manifest.json').read_text()):
 p=work/n;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(sel/n,p)
spec=importlib.util.spec_from_file_location('owned_runner',root/'tools/backend-quality/run_backend.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r);state=work/'test-state';state.mkdir();env=r.environment(state)
files=['test_adt_relay_transfer', 'test_narrator_timeline_ambient_tier', 'test_relay_ambient_gate', 'test_relay_channel_scope', 'test_relay_dm_membership', 'test_relay_earlier_messages', 'test_relay_idempotency', 'test_relay_mixed_timestamps', 'test_relay_role_guard', 'test_relay_room_fit', 'test_relay_unread_exact_reader', 'test_sage_ambient_critical_fastpath', 'test_sage_ambient_retraction', 'test_sage_relay_context', 'test_shadow_clinical_lanes', 'test_shadow_patient_specific_keys', 'test_shadow_producers', 'test_shadow_routes', 'test_shadow_scorecard', 'test_wedge_end_to_end', 'test_wedge_loop']
raise SystemExit(subprocess.run(['/private/tmp/orb-engineering-runtime-20261020/bin/python','-m','pytest','-q','--tb=short',*[f'tests/{n}.py' for n in files]],cwd=work/'server',env=env).returncode)
