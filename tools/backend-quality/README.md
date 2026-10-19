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
Existing broader lint/type debt is fingerprinted by module, diagnostic and source
expression. It cannot increase or move to a new expression. New functions cannot
add unannotated parameters/returns. An unformatted legacy file is allowed only
with its exact reviewed content hash; editing it requires formatting it. This is
a gradual adoption gate, not a claim that existing legacy diagnostics are fixed
or that third-party libraries are fully typed. The checker does not import Orb,
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

The initial reviewed baseline covers both the selected commit and the concurrent
working-copy snapshot; approved format hashes are explicit lists. Their diagnostic
counts are recorded separately in the engineering report. This does not authorize
future debt increases.

The output directory must be new and outside the checkout. It contains source
hashes, exact commands, logs, JUnit results and combined coverage. Keyboard interruption
stops only process groups the runner created. It never deploys or starts a live
appliance. Keep virtual-environment interpreter symlinks intact.
