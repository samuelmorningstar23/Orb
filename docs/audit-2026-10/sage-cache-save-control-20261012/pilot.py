"""Serial full-answer SWA cache controls; same offline model and prompts."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
MODEL_ROOT = Path('/Users/samuelmorningstar/.ollama/models')
ORIGIN = 'http://127.0.0.1:11436'
MODEL = 'medgemma:27b'
DIGEST = '58238ae38f99827496301de22016b2f94157552971f5a38db226f13802c2437e'
PROMPTS = json.loads((ROOT / 'control-prompts.json').read_text())
OPENER = build_opener(ProxyHandler({}))
RESULT = {'schema': 1, 'purpose': 'Full-answer G/P/D/G one-slot cache-save diagnostic, not browser SLA acceptance',
          'endpoint': ORIGIN, 'model': MODEL, 'digest': DIGEST, 'arms': [],
          'prompts_sha256': hashlib.sha256((ROOT / 'control-prompts.json').read_bytes()).hexdigest()}

def api(path, payload=None, timeout=10):
    req = Request(ORIGIN + path, data=json.dumps(payload).encode() if payload is not None else None,
                  headers={'Content-Type': 'application/json'})
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)

def model_files():
    return {str(p.relative_to(MODEL_ROOT)): [p.stat().st_size, p.stat().st_mtime_ns]
            for p in MODEL_ROOT.rglob('*') if p.is_file()}

def save():
    (ROOT / 'results.json').write_text(json.dumps(RESULT, indent=2) + '\n')

def run_turn(category, index):
    record = {'category': category, 'index': index}
    options = {'num_ctx': 8192, 'num_predict': 2048 if category == 'patient' else 1024 if category == 'department' else 512,
               'temperature': 0.2 if category == 'department' else 0.4, 'repeat_penalty': 1.15, 'repeat_last_n': 256}
    request = Request(ORIGIN + '/api/chat', data=json.dumps({
        'model': MODEL, 'messages': PROMPTS['prompts'][category]['messages'],
        'stream': True, 'keep_alive': '1h', 'options': options}).encode(),
        headers={'Content-Type': 'application/json'})
    start = time.perf_counter()
    content = []
    final = None
    with OPENER.open(request, timeout=120) as response:
        record['http_status'] = response.status
        for raw in response:
            if not raw.strip():
                continue
            chunk = json.loads(raw)
            assert isinstance(chunk, dict) and chunk.get('error') is None, 'Invalid model stream'
            text = (chunk.get('message') or {}).get('content', '')
            if text:
                if not content:
                    record['first_content_ms'] = (time.perf_counter() - start) * 1000
                content.append(text)
            if chunk.get('done') is True:
                final = chunk
                break
    record['wall_ms'] = (time.perf_counter() - start) * 1000
    answer = ''.join(content)
    assert final and final.get('done_reason') != 'length' and answer.strip(), 'Unfinished answer'
    assert category == 'department' or answer.startswith('NOACT|'), 'Model marker changed'
    for key in ('load_duration', 'total_duration', 'prompt_eval_duration', 'eval_duration'):
        record[key + '_ms'] = final.get(key, 0) / 1e6
    for key in ('prompt_eval_count', 'prompt_eval_cached_count', 'eval_count', 'done_reason'):
        record[key] = final.get(key)
    record['evaluated_prompt_tokens'] = record['prompt_eval_count'] - record['prompt_eval_cached_count']
    record['answer'] = answer
    record['residency'] = [{key: row.get(key) for key in ('name', 'digest', 'context_length', 'size', 'size_vram')}
                            for row in api('/api/ps').get('models', [])]
    assert len(record['residency']) == 1 and record['residency'][0]['digest'] == DIGEST
    print(json.dumps({k: v for k, v in record.items() if k not in ('answer', 'residency')}), flush=True)
    return record

before = model_files()
try:
    for enabled in (True,):
        arm = {'full_swa': enabled, 'turns': [], 'log': f"ollama-{'full' if enabled else 'default'}.log"}
        RESULT['arms'].append(arm)
        env = {'PATH': os.environ['PATH'], 'HOME': str(ROOT / 'home'), 'TMPDIR': str(ROOT),
               'OLLAMA_HOST': ORIGIN, 'OLLAMA_MODELS': str(MODEL_ROOT),
               'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NOPRUNE': '1',
               'OLLAMA_NUM_PARALLEL': '1', 'OLLAMA_MAX_LOADED_MODELS': '1',
               'XDG_CONFIG_HOME': str(ROOT / 'config'),
               'LLAMA_ARG_SWA_FULL': '1' if enabled else '0'}
        with (ROOT / arm['log']).open('w') as log:
            process = subprocess.Popen(['/usr/local/bin/ollama', 'serve'], env=env,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            arm['owned_pid'] = process.pid
            save()
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError('Owned Ollama stopped before readiness')
                    try:
                        assert api('/api/version') == {'version': '0.35.1'}
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError('Owned Ollama did not become ready')
                assert not api('/api/ps').get('models'), 'Owned process unexpectedly loaded a model'
                tags = api('/api/tags').get('models', [])
                assert any(r.get('name') == MODEL and r.get('digest') == DIGEST for r in tags)
                show = api('/api/show', {'model': MODEL})
                arm['model_info'] = {key: val for key, val in show.get('model_info', {}).items()
                                     if 'architecture' in key or 'sliding_window' in key or key.endswith('block_count')}
                for index, category in enumerate(('general', 'patient', 'department', 'general'), 1):
                    arm['turns'].append(run_turn(category, index))
                    save()
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                arm['owned_process_exit_code'] = process.returncode
                save()
finally:
    after = model_files()
    RESULT['model_files_unchanged'] = before == after
    RESULT['model_files_count'] = len(before)
    save()
    assert before == after, 'Model cache files changed'
