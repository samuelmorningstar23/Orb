# Backend-wide engineering checks

These checks inspect every production Python module under `server/`, including
new modules. Tests, generated environments, credentialed evaluation/data and
external adapters are excluded from production type/format budgets. The separate
critical lint check includes tests too.

Use Python 3.12 and the pinned tools in
`tools/local-inference-quality/requirements.txt`:

```sh
python tools/backend-quality/backend_quality.py check
python -m ruff check --select E9,F63,F7,F82,F811 server
python -m pytest --noconftest tools/backend-quality/test_backend_quality.py
bash tools/local-inference-quality/check.sh
```

The mandatory critical categories have **no allowlist**: syntax errors,
undefined bindings, invalid control flow and duplicate unused definitions fail.
The reviewed production scope now has **zero lint diagnostics, zero mypy
errors and zero unformatted files**. The versioned lint/type/format budgets are
empty, so any new finding fails. FastAPI dependency objects are named at their
original construction points; intentional import-time path/configuration and
cycle barriers retain documented E402 exceptions.

The annotation inventory remains a separate adoption gate: new functions cannot
add unannotated parameters/returns. The 20 October cleanup lowered the starting
commit's inventory from 4,345 to 4,334 missing slots; it did not claim full strict
annotation coverage or complete typing for optional third-party SDKs. This
inventory was reconciled against starting commit `59ee1bc`, which includes the
HIS work committed after the earlier engineering report. No new annotation debt
was added by this cleanup. The checker does not import Orb,
open its database, load models or contact a service.

`record` is a deliberate baseline authoring command, never run by CI. Baseline
changes must be reviewed with the corresponding source/test evidence. Removing
debt is allowed; do not increase budgets to conceal a regression. The six strict
inference/infrastructure utilities retain a separate 100% statement/branch,
format/lint and mypy-strict gate with no legacy diagnostic budget.

Coverage is measured over the complete disposable backend acceptance selection,
with branches enabled. The versioned per-module missing-statement/branch budgets
prevent added holes; a new module needs complete coverage. Missing declared
modules fail. Run:

```sh
python tools/backend-quality/backend_quality.py coverage \
  --report path/to/coverage.json \
  --baseline tools/backend-quality/coverage-baseline.json
```

A coverage report is test/runtime-specific. The recorded native test environment
and explicit skips are part of its evidence. Hosted Linux execution and target
appliance clinical/load acceptance remain separate; synthetic coverage does not
certify those. The guarded backend-wide report and strict utility checks
supplement the full application tests; they never replace them.

For a complete local acceptance run, first build the frontend and use a separate
Python test environment containing `server/requirements-dev.txt` and pinned
coverage 7.16.2. The runner creates private source copies/databases, excludes
`.env`, `site.toml`, uploads, backups, model weights and runtime/evaluation data,
then runs balanced test shards followed by serial owned-process cleanup tests:

```sh
python tools/backend-quality/run_backend.py --python /path/to/test-env/bin/python \
  --output /private/tmp/orb-backend-acceptance --workers 6
```

The current zero-diagnostic baseline describes the reviewed commit source.
The preserved concurrent working copy can contain additional unfinished changes;
its results must be measured separately and must not be substituted for committed
source acceptance. The cleanup evidence records the source hashes and scope.

The output directory must be new and outside the checkout. It contains source
hashes, exact commands, logs, JUnit results and combined coverage. Keyboard interruption
stops only process groups the runner created. It never deploys or starts a live
appliance. Keep virtual-environment interpreter symlinks intact.

The 20 October lint/type cleanup does **not** establish a passing coverage-budget
gate. The starting commit already exceeds older per-module budgets; new defensive
guards also add uncovered branches and type declarations alter statement counts.
The limits were left unchanged. See the cleanup verification and coverage-budget
findings under `docs/audit-2026-10/legacy-lint-types-20261020/` for exact totals
and remaining failures. Coverage follow-up remains open.
