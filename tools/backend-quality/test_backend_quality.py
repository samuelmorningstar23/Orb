"""Regression budgets must fail closed; no application or external tools needed."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "backend_quality", Path(__file__).with_name("backend_quality.py")
)
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def clean():
    return {"critical": [], "lint": {}, "typing": {}, "annotations": {}, "format": {}}


def test_critical_findings_can_never_be_grandfathered():
    actual = clean()
    actual["critical"] = ["undefined name"]
    assert quality.regressions(actual, actual) == ["undefined name"]


@pytest.mark.parametrize("category", ["lint", "typing", "annotations"])
def test_existing_diagnostics_cannot_increase_or_move_to_a_new_expression(category):
    old = clean()
    old[category] = {"old": 2}
    actual = clean()
    actual[category] = {"old": 2}
    assert not quality.regressions(actual, old)
    actual[category]["old"] = 3
    assert len(quality.regressions(actual, old)) == 1
    actual[category] = {"new": 1}
    assert len(quality.regressions(actual, old)) == 1
    actual[category] = {}
    assert not quality.regressions(actual, old)


def test_changed_legacy_files_must_be_formatted():
    old = clean()
    old["format"] = {"old.py": "approved"}
    actual = clean()
    actual["format"] = {"old.py": "approved"}
    assert not quality.regressions(actual, old)
    actual["format"]["old.py"] = "modified"
    assert quality.regressions(actual, old) == ["Format the new or changed legacy file: old.py"]
    actual["format"] = {"new.py": "modified"}
    assert quality.regressions(actual, old) == ["Format the new or changed legacy file: new.py"]
    actual["format"] = {}
    assert not quality.regressions(actual, old)


def test_scope_excludes_tests_generated_environments_and_symlinks(tmp_path):
    for name in (
        "server/a.py",
        "server/sub/b.py",
        "server/tests/t.py",
        "server/.venv/no.py",
        "server/eval/no.py",
    ):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("pass\n")
    (tmp_path / "server/link.py").symlink_to(tmp_path / "server/a.py")
    assert [str(p.relative_to(tmp_path)) for p in quality.backend_files(tmp_path)] == [
        "server/a.py",
        "server/sub/b.py",
    ]


def test_annotation_inventory_includes_nested_guards_and_parameters(tmp_path):
    source = tmp_path / "server/a.py"
    source.parent.mkdir()
    source.write_text("""class C:
    def f(self, a, /, b: int, *args, c, **kwargs):
        if True:
            def nested(x): pass
try:
    async def guarded(v: int) -> None: pass
except Exception:
    def fallback(): pass
finally:
    def done(): pass
""")
    assert quality.annotation_debt(tmp_path, [source]) == {
        "server/a.py:C.f": 5,
        "server/a.py:C.f.nested": 2,
        "server/a.py:fallback": 1,
        "server/a.py:done": 1,
    }


def test_diagnostics_are_line_stable_but_expression_specific(tmp_path):
    source = tmp_path / "server/a.py"
    source.parent.mkdir()
    source.write_text("\nvalue = bad()\n")
    key = quality.diagnostic_key(tmp_path, str(source), "F821", "Undefined name", 2)
    source.write_text("\n\nvalue = bad()\n")
    assert quality.diagnostic_key(tmp_path, "server/a.py", "F821", "Undefined name", 3) == key
    source.write_text("value = other()\n")
    assert quality.diagnostic_key(tmp_path, "server/a.py", "F821", "Undefined name", 1) != key
    assert json.loads(quality.diagnostic_key(tmp_path, "server/a.py", "code", "msg", 999))[-1] == ""


def test_tool_crashes_do_not_become_clean_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quality.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 2, "", "tool failed"),
    )
    with pytest.raises(RuntimeError, match="did not complete"):
        quality.run_tool(tmp_path, ["mypy"])


def test_coverage_budgets_reject_missing_modules_new_holes_and_branch_regressions():
    baseline = {"a.py": {"missing_statements": 2, "missing_branches": 1}}
    assert quality.coverage_regressions(baseline, baseline) == []
    assert quality.coverage_regressions({}, baseline) == [
        "Coverage omitted a declared backend module: a.py"
    ]
    assert (
        len(
            quality.coverage_regressions(
                {"a.py": {"missing_statements": 3, "missing_branches": 2}}, baseline
            )
        )
        == 2
    )
    assert quality.coverage_regressions(
        {"new.py": {"missing_statements": 1, "missing_branches": 0}}, {}
    )
    assert (
        quality.coverage_regressions(
            {"new.py": {"missing_statements": 0, "missing_branches": 0}}, {}
        )
        == []
    )
    assert quality.coverage_budget({"files": {"a.py": {"summary": {"missing_lines": 2}}}}) == {
        "server/a.py": {"missing_statements": 2, "missing_branches": 0}
    }


def test_snapshot_rejects_empty_scope(tmp_path):
    with pytest.raises(ValueError, match="No backend sources"):
        quality.snapshot(tmp_path)


def test_snapshot_measures_actual_tool_output_and_signature_debt(tmp_path, monkeypatch):
    source = tmp_path / "server/a.py"
    source.parent.mkdir()
    source.write_text("def f(x): return wrong\n")
    responses = iter(
        [
            [
                {
                    "filename": str(source),
                    "code": "F821",
                    "message": "Undefined name",
                    "location": {"row": 1},
                }
            ],
            [{"filename": str(source)}],
            {
                "file": "server/a.py",
                "line": 1,
                "message": "Bad type",
                "code": "arg-type",
                "severity": "error",
            },
        ]
    )

    def tool(root, args):
        value = next(responses)
        return subprocess.CompletedProcess(args, 1, json.dumps(value), "")

    monkeypatch.setattr(quality, "run_tool", tool)
    result = quality.snapshot(tmp_path)
    assert result["files"] == 1 and len(result["critical"]) == 1
    assert sum(result["lint"].values()) == sum(result["typing"].values()) == 1
    assert result["annotations"] == {"server/a.py:f": 2}
    assert set(result["format"]) == {"server/a.py"}


@pytest.mark.parametrize("mode", ["record", "check", "record-coverage", "coverage"])
def test_cli_persists_or_checks_the_declared_budgets(tmp_path, monkeypatch, mode):
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "report.json"
    snapshot = clean()
    snapshot["files"] = 1
    monkeypatch.setattr(quality, "snapshot", lambda root: snapshot)
    if "coverage" in mode:
        report.write_text(json.dumps({"files": {"a.py": {"summary": {"missing_lines": 0}}}}))
        baseline.write_text(json.dumps({"a.py": {"missing_statements": 0, "missing_branches": 0}}))
    else:
        baseline.write_text(json.dumps(snapshot))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quality",
            mode,
            "--root",
            str(tmp_path),
            "--baseline",
            str(baseline),
            "--report",
            str(report),
        ],
    )
    assert quality.main() == 0


def test_cli_cannot_record_critical_errors(tmp_path, monkeypatch):
    snapshot = clean()
    snapshot["critical"] = ["bad"]
    monkeypatch.setattr(quality, "snapshot", lambda root: snapshot)
    monkeypatch.setattr(
        sys, "argv", ["quality", "record", "--baseline", str(tmp_path / "baseline.json")]
    )
    with pytest.raises(RuntimeError, match="cannot be grandfathered"):
        quality.main()


def test_cli_requires_a_real_coverage_report(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quality", "coverage"])
    with pytest.raises(SystemExit) as error:
        quality.main()
    assert error.value.code == 2
