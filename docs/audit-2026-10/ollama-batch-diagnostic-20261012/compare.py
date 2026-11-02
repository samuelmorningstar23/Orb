"""Owned, serial, one-token Ollama batch diagnostics; no SLA/quality claim."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import subprocess
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

BASE = Path(__file__).resolve().parent
ASSEMBLED = json.loads((BASE / 'prompts.json').read_text())
ORIGIN = 'http://127.0.0.1:11434'
MODEL = 'medgemma:27b'
DIGEST = '58238ae38f99827496301de22016b2f94157552971f5a38db226f13802c2437e'
OPENER = build_opener(ProxyHandler({}))
RESULT = {'purpose': 'Cold-prefix one-token prefill diagnostics, not clinical answer quality or browser/SLA acceptance',
          'model': MODEL, 'context': 8192, 'source_prompts': {
              key: {k: value[k] for k in ('scope', 'characters', 'sha256')}
              for key, value in ASSEMBLED['prompts'].items()},
          'nonce': 'Fixed-width unique three-digit nonce prepended to the first system message; changes earliest prompt content for equal cold-prefix evaluation',
          'arms': ['AUTO', 512, 1024], 'repeats': 3, 'calls': [], 'restore': None,
          'limitations': ['Model output limited to one token; no answer/quality/ranking conclusions.',
                          'Reloads are ordinary same-tag requests and retained separately from stable-runner calls.',
                          'RSS is sampled every0.5s; process RSS and API size/size_vram are not a guaranteed true GPU allocation peak.']}

def read_json(path: str):
    with OPENER.open(ORIGIN + path, timeout=10) as response:
        return json.load(response)

def runner_stats():
    text = subprocess.check_output(['/bin/ps', '-axo', 'pid=,rss=,args='], text=True)
    rows = []
    for line in text.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        pid, rss, arguments = fields
        try:
            argv = shlex.split(arguments)
        except ValueError:
            continue
        if not argv or argv[0].rsplit('/', 1)[-1] != 'llama-server':
            continue
        flags = {}
        for index, argument in enumerate(argv):
            if argument in {'-b', '-ub', '-c', '-np', '--batch-size', '--ubatch-size', '--ctx-size', '--parallel'} and index + 1 < len(argv):
                flags[argument] = argv[index + 1]
        rows.append({'pid': int(pid), 'rss_kib': int(rss), 'flags': flags})
    return rows

def residency():
    rows = read_json('/api/ps').get('models', [])
    wanted = [row for row in rows if row.get('name') == MODEL]
    assert len(wanted) == 1 and wanted[0].get('digest') == DIGEST, 'Expected existing model missing or changed'
    assert wanted[0].get('context_length') == 8192, 'Expected8192 context changed'
    return [{key: row.get(key) for key in ('name', 'digest', 'context_length', 'size', 'size_vram')}
            for row in rows]

def persist():
    (BASE / 'result.json').write_text(json.dumps(RESULT, indent=2) + '\n')

def one_call(category, arm, repeat, index, *, restoring=False):
    messages = copy.deepcopy(ASSEMBLED['prompts'][category]['messages'])
    messages[0]['content'] = f'{index:03d}\n' + messages[0]['content']
    options = {'num_ctx': 8192, 'num_predict': 1, 'temperature': 0.0,
               'repeat_penalty': 1.15, 'repeat_last_n': 256}
    if arm != 'AUTO':
        options['num_batch'] = arm
    payload = {'model': MODEL, 'messages': messages, 'stream': False,
               'keep_alive': ASSEMBLED['keep_alive'], 'options': options}
    before = runner_stats()
    record = {'index': index, 'repeat': repeat, 'category': category, 'arm': arm,
              'requested_options': options, 'prompt_sha256': hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest(),
              'before': before, 'restoring': restoring}
    samples = []
    stop = threading.Event()
    def sample():
        while not stop.wait(0.5):
            try:
                samples.append(runner_stats())
            except Exception:
                pass
    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    start = time.perf_counter()
    try:
        request = Request(ORIGIN + '/api/chat', data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        with OPENER.open(request, timeout=120) as response:
            record['http_status'] = response.status
            data = json.load(response)
        if not isinstance(data, dict) or data.get('error') is not None or data.get('done') is not True:
            raise RuntimeError('Local diagnostic did not complete')
        for key in ('load_duration', 'total_duration', 'prompt_eval_duration', 'eval_duration'):
            record[key + '_ms'] = data.get(key, 0) / 1_000_000
        for key in ('prompt_eval_count', 'prompt_eval_cached_count', 'eval_count', 'done_reason'):
            record[key] = data.get(key)
    except BaseException as error:
        record['error_type'] = type(error).__name__
        raise
    finally:
        record['wall_ms'] = (time.perf_counter() - start) * 1000
        stop.set()
        monitor.join(timeout=2)
        record['after'] = runner_stats()
        record['runner_pid_changed'] = [r['pid'] for r in before] != [r['pid'] for r in record['after']]
        samples.extend([before, record['after']])
        record['sampled_max_combined_runner_rss_kib'] = max((sum(row['rss_kib'] for row in sample) for sample in samples), default=0)
        try:
            record['residency_after'] = residency()
        except Exception as error:
            record['residency_error_type'] = type(error).__name__
        if restoring:
            RESULT['restore'] = record
        else:
            RESULT['calls'].append(record)
        persist()
        print(json.dumps({k: record.get(k) for k in ('index', 'repeat', 'category', 'arm', 'load_duration_ms', 'prompt_eval_count', 'prompt_eval_cached_count', 'prompt_eval_duration_ms', 'wall_ms', 'runner_pid_changed', 'error_type')}), flush=True)
    return record

RESULT['version'] = read_json('/api/version')
assert RESULT['version'] == {'version': '0.35.1'}
RESULT['residency_before'] = residency()
RESULT['runner_before'] = runner_stats()
persist()
try:
    index = 0
    orders = [['AUTO', 512, 1024], [512, 1024, 'AUTO'], [1024, 'AUTO', 512]]
    for repeat, arms in enumerate(orders, 1):
        for arm in arms:
            for category in ('general', 'patient'):
                index += 1
                one_call(category, arm, repeat, index)
finally:
    try:
        one_call('general', 'AUTO', None, 999, restoring=True)
    except BaseException as error:
        RESULT['restore_error_type'] = type(error).__name__
        persist()

summaries = []
for category in ('general', 'patient'):
    for arm in RESULT['arms']:
        rows = [row for row in RESULT['calls'] if row['category'] == category and row['arm'] == arm]
        summaries.append({'category': category, 'arm': arm, 'count': len(rows),
                          'median_prefill_ms': statistics.median(r['prompt_eval_duration_ms'] for r in rows),
                          'median_load_ms': statistics.median(r['load_duration_ms'] for r in rows),
                          'median_wall_ms': statistics.median(r['wall_ms'] for r in rows),
                          'runner_changes': sum(r['runner_pid_changed'] for r in rows),
                          'actual_batch_flags': sorted({r['flags'].get('-b') or r['flags'].get('--batch-size') for row in rows for r in row['after']})})
RESULT['summaries'] = summaries
persist()
print(json.dumps({'summaries': summaries, 'restore': RESULT['restore']}, indent=2), flush=True)
