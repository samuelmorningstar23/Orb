#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PYTHONPATH="${PWD}/server${PYTHONPATH:+:${PYTHONPATH}}"
config=tools/local-inference-quality/pyproject.toml
files=(server/offline_policy.py server/services/asr_worker_protocol.py server/services/inference_cache.py server/security_headers.py server/spa_bootstrap.py server/services/warm_policy.py)
python -m ruff check --config "$config" "${files[@]}"
python -m ruff format --check --config "$config" "${files[@]}"
python -m mypy --config-file "$config"
# Pure tests explicitly exclude app fixtures and four integration cases.
python -m coverage run --rcfile="$config" -m pytest -c /dev/null --noconftest -p no:cacheprovider -q \
  server/tests/test_local_inference_boundaries.py server/tests/test_inference_cache.py server/tests/test_infrastructure_modules.py \
  -k 'not speech_worker and not gateway'
python -m coverage report --rcfile="$config"
python -m coverage json --rcfile="$config"
python - <<'PY'
import json
from pathlib import Path
report = json.loads(Path('local-inference-coverage.json').read_text())
assert set(report['files']) == {
    'server/offline_policy.py', 'server/services/asr_worker_protocol.py',
    'server/services/inference_cache.py', 'server/security_headers.py',
    'server/spa_bootstrap.py', 'server/services/warm_policy.py',
}, 'All six declared utilities must be measured'
PY
