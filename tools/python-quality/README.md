# Incremental Python quality gate

The adoption scope is exactly two pure utilities:

- `server/routes/import_mapping.py`: shared importer mapping validation.
- `scripts/release_candidate.py`: offline candidate identity and frontend packaging.

CI checks Ruff lint and formatting, mypy strict typing, and 100% statement and
branch coverage for these files. The measured selection covers 90 statements and
34 branches with 35 tests. Only the conventional `if __name__ == "__main__"`
launcher is excluded; the actual CLI `main()` paths are tested. Coverage counts
do not measure the rest of the backend or clinical acceptance.

The versioned tool requirements include all tool dependencies and belong in a
Python 3.12 developer environment with `server/requirements-dev.txt` already
installed, never the appliance runtime. From the repository root:

```sh
python -m pip install -r server/requirements-quality.txt
python -m ruff check --config tools/python-quality/pyproject.toml server/routes/import_mapping.py scripts/release_candidate.py
python -m ruff format --check --config tools/python-quality/pyproject.toml server/routes/import_mapping.py scripts/release_candidate.py
python -m mypy --config-file tools/python-quality/pyproject.toml
python -m coverage run --rcfile=tools/python-quality/pyproject.toml -m pytest -q \
  server/tests/test_import_and_time_compatibility.py::test_both_importers_preserve_mapping_form_values \
  server/tests/test_import_and_time_compatibility.py::test_both_importers_reject_ambiguous_mapping_types_with_422 \
  server/tests/test_release_candidate.py
python -m coverage report --rcfile=tools/python-quality/pyproject.toml
python -m coverage json --rcfile=tools/python-quality/pyproject.toml
```

The full backend suite remains a separate required CI step. Expand this adoption
scope only after measuring and resolving the next module's actual debt; these
checks do not imply whole-backend lint, formatting, strict typing, or coverage.

The security workflow retains direct production, test, and quality-tool pin
audits and separately installs the slim CPU-only Linux runtime, freezes its
resolved tree, and audits every installed dependency from a separate scanner
environment. Existing advisories fail the workflow. This resolves visibility
for slim CI transitives; a fully locked Apple/ML production environment and its
hardware acceptance remain separate work.

Both runtime requirements apply `server/constraints-security.txt` to pin the
patched Starlette version and constrain optional HTTP transitives without
installing unused clients. The coordinated FastAPI upgrade is validated by
synthetic transport/authentication/upload and offline workflow selections;
known ML advisories continue to block security and candidate workflows.
