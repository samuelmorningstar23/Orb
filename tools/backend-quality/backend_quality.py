"""Measured backend-wide diagnostics and coverage budgets; never imports Orb."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED = {
    ".venv",
    ".venv-dev",
    "__pycache__",
    "tests",
    "eval",
    "dev_tools",
    "datasets",
    "adapters",
}
CRITICAL = {"E9", "F63", "F7", "F82", "F811"}


def backend_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in (root / "server").rglob("*.py")
        if not any(
            part in EXCLUDED or part.startswith(".") for part in path.relative_to(root).parts
        )
        and not path.is_symlink()
    )


def run_tool(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MYPYPATH": str(root / "server"), "PYTHONHASHSEED": "0"}
    result = subprocess.run(
        [sys.executable, "-m", *args],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode not in (0, 1) or (result.returncode == 1 and not result.stdout.strip()):
        raise RuntimeError(f"{args[0]} did not complete: {result.stderr or result.stdout}")
    return result


def canonical_path(root: Path, name: str) -> str:
    path = Path(name)
    return (path.relative_to(root) if path.is_absolute() else path).as_posix()


def diagnostic_key(root: Path, name: str, code: str, message: str, line: int) -> str:
    relative = canonical_path(root, name)
    source = (root / relative).read_text().splitlines()
    content = source[line - 1].strip() if 0 < line <= len(source) else ""
    # Locations can move when a neighbouring function is extracted. The source
    # expression, category and diagnostic still have to match the reviewed debt.
    return json.dumps([relative, code, message, content], ensure_ascii=True)


def annotation_debt(root: Path, paths: list[Path]) -> dict[str, int]:
    debt: dict[str, int] = {}

    def visit(nodes: list[ast.stmt], prefix: str, relative: str) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg)
                missing = int(node.returns is None) + sum(
                    arg.annotation is None for arg in arguments if arg.arg not in {"self", "cls"}
                )
                qualified = prefix + node.name
                if missing:
                    debt[relative + ":" + qualified] = missing
                visit(node.body, qualified + ".", relative)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".", relative)
            else:
                # Include definitions under feature/platform guards as well.
                for field in ("body", "orelse", "finalbody"):
                    body = getattr(node, field, None)
                    if isinstance(body, list):
                        visit(body, prefix, relative)
                if isinstance(node, (ast.Try, ast.TryStar)):
                    for handler in node.handlers:
                        visit(handler.body, prefix, relative)

    for path in paths:
        visit(
            ast.parse(path.read_text(), filename=str(path)).body,
            "",
            path.relative_to(root).as_posix(),
        )
    return debt


def snapshot(root: Path) -> dict[str, Any]:
    paths = backend_files(root)
    names = [path.relative_to(root).as_posix() for path in paths]
    if not names:
        raise ValueError("No backend sources were found")
    ruff = run_tool(
        root,
        [
            "ruff",
            "check",
            "--isolated",
            "--target-version",
            "py312",
            "--line-length",
            "100",
            "--select",
            "E4,E7,E9,F,I,UP,B",
            "--output-format",
            "json",
            *names,
        ],
    )
    lint: Counter[str] = Counter()
    critical = []
    for item in json.loads(ruff.stdout):
        code = item["code"]
        if any(code.startswith(prefix) for prefix in CRITICAL):
            critical.append(
                f"{canonical_path(root, item['filename'])}:{item['location']['row']} {code} {item['message']}"
            )
        lint[
            diagnostic_key(root, item["filename"], code, item["message"], item["location"]["row"])
        ] += 1
    formatter = run_tool(
        root,
        [
            "ruff",
            "format",
            "--isolated",
            "--target-version",
            "py312",
            "--line-length",
            "100",
            "--check",
            "--output-format",
            "json",
            *names,
        ],
    )
    format_debt = {
        canonical_path(root, item["filename"]): hashlib.sha256(
            Path(item["filename"]).read_bytes()
        ).hexdigest()
        for item in json.loads(formatter.stdout)
    }
    typing = run_tool(
        root,
        [
            "mypy",
            "--no-incremental",
            "--follow-imports=skip",
            "--ignore-missing-imports",
            "--no-site-packages",
            "--explicit-package-bases",
            "--namespace-packages",
            "--check-untyped-defs",
            "--platform",
            "linux",
            "--python-version",
            "3.12",
            "--output",
            "json",
            *names,
        ],
    )
    types: Counter[str] = Counter()
    for line in typing.stdout.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["severity"] == "error":
            types[
                diagnostic_key(root, item["file"], item["code"], item["message"], item["line"])
            ] += 1
    return {
        "schema": 1,
        "files": len(names),
        "critical": critical,
        "lint": dict(lint),
        "typing": dict(types),
        "annotations": annotation_debt(root, paths),
        "format": format_debt,
    }


def regressions(actual: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    errors = list(actual["critical"])
    for category in ("lint", "typing", "annotations"):
        for key, count in actual[category].items():
            allowed = baseline[category].get(key, 0)
            if count > allowed:
                errors.append(f"New {category} debt ({count} > {allowed}): {key}")
    for path, digest in actual["format"].items():
        if digest not in baseline["format"].get(path, []):
            errors.append(f"Format the new or changed legacy file: {path}")
    return errors


def coverage_budget(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        (name if name.startswith("server/") else "server/" + name): {
            "missing_statements": item["summary"]["missing_lines"],
            "missing_branches": item["summary"].get("missing_branches", 0),
        }
        for name, item in report["files"].items()
    }


def coverage_regressions(
    actual: dict[str, dict[str, int]],
    baseline: dict[str, dict[str, int]],
    existing: set[str] | None = None,
) -> list[str]:
    errors = []
    for path, values in actual.items():
        for metric, count in values.items():
            allowed = baseline.get(path, {}).get(metric, 0)
            if count > allowed:
                errors.append(f"Coverage regressed for {path}: {metric} {count} > {allowed}")
    for path in (baseline.keys() if existing is None else existing) - actual.keys():
        errors.append(f"Coverage omitted a declared backend module: {path}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "record", "coverage", "record-coverage"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--baseline", type=Path, default=ROOT / "tools/backend-quality/baseline.json"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.mode in {"coverage", "record-coverage"}:
        if args.report is None:
            parser.error("coverage modes require --report")
        actual = coverage_budget(json.loads(args.report.read_text()))
        if args.mode == "record-coverage":
            args.baseline.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
            return 0
        existing = {
            path.relative_to(args.root).as_posix()
            for path in backend_files(args.root)
            if path.relative_to(args.root / "server").parts[0] != "scripts"
        }
        errors = coverage_regressions(actual, json.loads(args.baseline.read_text()), existing)
    else:
        actual = snapshot(args.root)
        if args.report:
            args.report.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        if args.mode == "record":
            if actual["critical"]:
                raise RuntimeError("Critical lint findings cannot be grandfathered")
            args.baseline.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
            errors = []
        else:
            errors = regressions(actual, json.loads(args.baseline.read_text()))
        print(
            f"Scanned {actual['files']} backend files; critical={len(actual['critical'])}; "
            f"legacy lint={sum(actual['lint'].values())}; typing={sum(actual['typing'].values())}; "
            f"unannotated slots={sum(actual['annotations'].values())}; unformatted files={len(actual['format'])}"
        )
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
