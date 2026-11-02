"""Verify the curated backend in disposable source copies and isolated databases."""
import collections
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path('/Users/samuelmorningstar/Documents/Projects/Orb')
OUT = Path('/private/tmp/orb-ml-backend-shards-20261012-v8')
PYTHON = '/private/tmp/orb-ml-upgrade-20261012/bin/python'
SUFFIXES = {'.py', '.json', '.jsonl', '.md', '.csv', '.toml', '.txt', '.ini',
            '.sh', '.yml', '.yaml', '.ts', '.tsx', '.js', '.cjs', '.mjs', '.css',
            '.html', '.lock', '.sql', '.svg', '.hl7'}
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'uploads',
            'downtime_exports', 'backups', 'ops', 'adapters', 'dev_tools',
            'datasets', '.agents', '.codex', '.aws'}

def prepare():
    OUT.mkdir(exist_ok=False)
    paths = subprocess.check_output(['git', 'ls-files', '--cached', '--others',
                                    '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
    manifest = {}
    source = OUT / 'source'
    for name in sorted(set(paths)):
        if not name:
            continue
        relative = Path(name)
        path = ROOT / relative
        if (any(part in EXCLUDED for part in relative.parts) or path.is_symlink()
                or not path.is_file() or path.suffix not in SUFFIXES
                or relative.name in {'site.toml', '.env', '.env.local'}
                or ('models' in relative.parts and path.suffix != '.py')):
            continue
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    shutil.copytree(ROOT / 'dist', source / 'dist')
    (OUT / 'source-sha256.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return source

def environment(directory):
    spec = importlib.util.spec_from_file_location('owned_e2e_environment', ROOT / 'scripts/e2e_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = module.isolated_environment(directory)
    env.update(ORB_DB_PATH=str(directory / 'orb_test_shard.db'),
               ORB_SKIP_KNOWLEDGE_BASE='1', ORB_BACKGROUND_MODELS_ENABLED='0',
               ORB_KNOWLEDGE_INPROCESS='1', ORB_SCORING_ENABLED='1',
               ORB_SEPSIS_AUTOSTART="1", ORB_INGESTION_API_KEY='orb-ingest-dev-key',
               PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
    for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY'):
        env.pop(key, None)
    return env

def main():
    started = time.monotonic()
    source = prepare()
    collect_dir = OUT / 'collection-state'
    collect_dir.mkdir()
    collection = subprocess.run([PYTHON, '-m', 'pytest', '--collect-only', '-q',
                                 '--ignore=tests/test_e2e_process_cleanup.py'],
                                cwd=source / 'server', env=environment(collect_dir),
                                text=True, capture_output=True)
    (OUT / 'collection.log').write_text(collection.stdout + collection.stderr)
    if collection.returncode:
        raise RuntimeError('Collection failed; see collection.log')
    nodes = [line for line in collection.stdout.splitlines() if line.startswith('tests/') and '::' in line]
    counts = collections.Counter(node.split('::', 1)[0] for node in nodes)
    assert len(nodes) > 14000, 'The complete curated suite must be collected'
    groups = [[] for _ in range(6)]
    weights = [0] * 6
    for name, count in sorted(counts.items(), key=lambda row: (-row[1], row[0])):
        index = min(range(6), key=lambda i: weights[i])
        groups[index].append(name)
        weights[index] += count
    assert len(set(sum(groups, []))) == len(counts)
    report = {'source': str(source), 'runtime': PYTHON, 'collected': len(nodes),
              'exclusion': '7 process-cleanup tests run independently',
              'isolation': '6 independent disposable source copies, synthetic DBs, exports/caches; no live data/models/env copied',
              'shards': []}
    children = []
    for index, files in enumerate(groups):
        directory = OUT / f'shard-{index + 1}'
        shutil.copytree(source, directory)
        state = directory / 'test-state'
        state.mkdir()
        command = [PYTHON, '-m', 'pytest', '-q', '--tb=short',
                   f'--junitxml={OUT / f"shard-{index + 1}.xml"}', *sorted(files)]
        log = (OUT / f'shard-{index + 1}.log').open('w')
        child = subprocess.Popen(command, cwd=directory / 'server', env=environment(state),
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        row = {'index': index + 1, 'expected': weights[index], 'files': sorted(files),
               'pid': child.pid, 'log': str(OUT / f'shard-{index + 1}.log'), 'command': command}
        report['shards'].append(row)
        children.append((child, log, row, time.monotonic()))
    (OUT / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Collected {len(nodes)} tests in {len(counts)} files; 6 isolated shards started: {weights}', flush=True)
    while children:
        running = []
        for child, log, row, shard_started in children:
            code = child.poll()
            if code is None:
                running.append((child, log, row, shard_started))
                continue
            log.close()
            row.update(exit_code=code, elapsed_seconds=round(time.monotonic() - shard_started, 2))
            print(f'Shard {row["index"]} exited {code} after {row["elapsed_seconds"]}s', flush=True)
            (OUT / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
        children = running
        if children:
            time.sleep(20)
    report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    report['passed'] = all(row['exit_code'] == 0 for row in report['shards'])
    (OUT / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('collected', 'elapsed_seconds', 'passed')}), flush=True)
    raise SystemExit(0 if report['passed'] else 1)

if __name__ == '__main__':
    main()
