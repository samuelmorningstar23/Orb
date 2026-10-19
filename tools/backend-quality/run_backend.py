"""Verify the curated backend in disposable source copies and isolated databases."""

import argparse
import collections
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = None
PYTHON = sys.executable
SUFFIXES = {
    ".py",
    ".json",
    ".jsonl",
    ".md",
    ".csv",
    ".toml",
    ".txt",
    ".ini",
    ".sh",
    ".yml",
    ".yaml",
    ".ts",
    ".mts",
    ".cts",
    ".tsx",
    ".js",
    ".cjs",
    ".mjs",
    ".css",
    ".html",
    ".lock",
    ".sql",
    ".svg",
    ".hl7",
}
EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "__pycache__",
    "uploads",
    "downtime_exports",
    "backups",
    "ops",
    "adapters",
    "dev_tools",
    "datasets",
    ".agents",
    ".codex",
    ".aws",
    ".claude",
    ".venv-dev",
    "tmp",
    "release-candidate",
}


def prepare():
    OUT.mkdir(mode=0o700, exist_ok=False)
    (OUT / "coverage").mkdir()
    (OUT / "coverage.ini").write_text(
        "[run]\nbranch = True\nrelative_files = True\nsource = .\nomit = tests/*, scripts/*, eval/*, dev_tools/*\n[report]\ninclude_namespace_packages = True\n"
    )
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    manifest = {}
    source = OUT / "source"
    for name in sorted(set(paths)):
        if not name:
            continue
        relative = Path(name)
        path = ROOT / relative
        if (
            any(part in EXCLUDED for part in relative.parts)
            or path.is_symlink()
            or not path.is_file()
            or path.suffix not in SUFFIXES
            or (relative.name == "site.toml" or relative.name.startswith(".env"))
            or ("models" in relative.parts and path.suffix != ".py")
        ):
            continue
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    shutil.copytree(ROOT / "dist", source / "dist")
    (OUT / "source-sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return source


def isolated_environment(directory: Path) -> dict[str, str]:
    # Inherited appliance flags, credentials and paths must not change this demo.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ORB_")
        and key
        not in {
            "PYTHONPATH",
            "HF_HOME",
            "HF_HUB_CACHE",
            "HUGGINGFACE_HUB_CACHE",
            "TRANSFORMERS_CACHE",
            "PYTORCH_TRANSFORMERS_CACHE",
            "PYTORCH_PRETRAINED_BERT_CACHE",
            "SENTENCE_TRANSFORMERS_HOME",
            "TORCH_HOME",
            "XDG_CACHE_HOME",
            "SAGE_RERANK_MODEL",
        }
    }
    env.update(
        {
            "ORB_DEPLOYMENT_PROFILE": "dev",
            "ORB_DB_PATH": str(directory / "demo.db"),
            "ORB_BACKUP_DIR": str(directory / "backups"),
            "ORB_WATCHDOG_DIR": str(directory / "watchdog"),
            "ORB_FHIR_EXPORT_DIR": str(directory / "fhir"),
            "ORB_SITE_CONFIG_FILE": str(directory / "no-site-settings.toml"),
            "ORB_JWT_SECRET": secrets.token_urlsafe(48),
            "ORB_INGESTION_API_KEY": secrets.token_urlsafe(48),
            "ORB_INGESTION_MODE": "ingestion",
            "ORB_SCORING_ENABLED": "0",
            "ORB_SEPSIS_AUTOSTART": "0",
            # Exercise the model-feature UI, with no local model assets or service.
            # The journeys never request inference or microphone capture.
            "ORB_MODELS_ENABLED": "1",
            "ORB_OLLAMA_BASE_URL": "http://127.0.0.1:9",
            "ORB_WHISPER_DAEMON_ENABLED": "0",
            "ORB_WHISPER_SOCKET": f"ipc://{directory / 'whisper.sock'}",
            "ORB_KNOWLEDGE_INPROCESS": "1",
            "HF_HOME": str(directory / "hf-cache"),
            "HF_HUB_CACHE": str(directory / "hf-cache"),
            "HUGGINGFACE_HUB_CACHE": str(directory / "hf-cache"),
            "TRANSFORMERS_CACHE": str(directory / "hf-cache"),
            "PYTORCH_TRANSFORMERS_CACHE": str(directory / "hf-cache"),
            "PYTORCH_PRETRAINED_BERT_CACHE": str(directory / "hf-cache"),
            "SENTENCE_TRANSFORMERS_HOME": str(directory / "hf-cache"),
            "TORCH_HOME": str(directory / "torch-cache"),
            "XDG_CACHE_HOME": str(directory / "cache"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def environment(directory):
    env = isolated_environment(directory)
    env.update(
        ORB_DB_PATH=str(directory / "orb_test_shard.db"),
        ORB_SKIP_KNOWLEDGE_BASE="1",
        ORB_BACKGROUND_MODELS_ENABLED="0",
        ORB_KNOWLEDGE_INPROCESS="1",
        ORB_SCORING_ENABLED="1",
        ORB_SEPSIS_AUTOSTART="1",
        ORB_INGESTION_API_KEY="orb-ingest-dev-key",
        PYTHONDONTWRITEBYTECODE="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        env.pop(key, None)
    return env


def _monitor_children(children, report):
    pending = list(children)
    while pending:
        running = []
        for child, log, row, shard_started in pending:
            code = child.poll()
            if code is None:
                running.append((child, log, row, shard_started))
                continue
            log.close()
            row.update(exit_code=code, elapsed_seconds=round(time.monotonic() - shard_started, 2))
            print(f"Shard {row['index']} exited {code} after {row['elapsed_seconds']}s", flush=True)
            (OUT / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        pending = running
        if pending:
            time.sleep(10)


def _stop_owned_children(children):
    for child, log, row, started in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        log.close()


def junit_counts(paths: list[Path]) -> dict[str, int]:
    counts: collections.Counter[str] = collections.Counter()
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            skipped = case.find("skipped")
            if case.find("failure") is not None:
                key = "failed"
            elif case.find("error") is not None:
                key = "error"
            elif skipped is not None:
                key = "expected_failure" if skipped.get("type") == "pytest.xfail" else "skipped"
            else:
                key = "passed"
            counts[key] += 1
    return dict(counts)


def main():
    global OUT, PYTHON
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--workers", type=int, default=6, choices=range(1, 9))
    args = parser.parse_args()
    OUT = args.output.resolve()
    PYTHON = str(Path(args.python).absolute())
    if OUT == ROOT or ROOT in OUT.parents:
        parser.error("Output must be a new directory outside the source checkout")
    started = time.monotonic()
    source = prepare()
    collect_dir = OUT / "collection-state"
    collect_dir.mkdir()
    collection = subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--ignore=tests/test_e2e_process_cleanup.py",
        ],
        check=False,
        cwd=source / "server",
        env=environment(collect_dir),
        text=True,
        capture_output=True,
    )
    (OUT / "collection.log").write_text(collection.stdout + collection.stderr)
    if collection.returncode:
        raise RuntimeError("Collection failed; see collection.log")
    nodes = [
        line
        for line in collection.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    ]
    counts = collections.Counter(node.split("::", 1)[0] for node in nodes)
    assert len(nodes) > 14000, "The complete curated suite must be collected"
    groups = [[] for _ in range(args.workers)]
    weights = [0] * args.workers
    for name, count in sorted(counts.items(), key=lambda row: (-row[1], row[0])):
        index = min(range(args.workers), key=lambda i: weights[i])
        groups[index].append(name)
        weights[index] += count
    assert len({name for group in groups for name in group}) == len(counts)
    report = {
        "source": str(source),
        "runtime": PYTHON,
        "collected": len(nodes),
        "exclusion": "Process-cleanup tests run serially after the shards",
        "isolation": f"{args.workers} independent disposable source copies, synthetic DBs, exports/caches; no live data/models/env copied",
        "shards": [],
    }
    children = []
    try:
        for index, files in enumerate(groups):
            directory = OUT / f"shard-{index + 1}"
            shutil.copytree(source, directory)
            state = directory / "test-state"
            state.mkdir()
            command = [
                PYTHON,
                "-m",
                "coverage",
                "run",
                "--rcfile=" + str(OUT / "coverage.ini"),
                "--data-file=" + str(OUT / "coverage" / f".coverage.{index + 1}"),
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                f"--junitxml={OUT / f'shard-{index + 1}.xml'}",
                *sorted(files),
            ]
            log = (OUT / f"shard-{index + 1}.log").open("w")
            child = subprocess.Popen(
                command,
                cwd=directory / "server",
                env=environment(state),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            row = {
                "index": index + 1,
                "expected": weights[index],
                "files": sorted(files),
                "pid": child.pid,
                "log": str(OUT / f"shard-{index + 1}.log"),
                "command": command,
            }
            report["shards"].append(row)
            children.append((child, log, row, time.monotonic()))
        (OUT / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            f"Collected {len(nodes)} tests in {len(counts)} files; {args.workers} isolated shards started: {weights}",
            flush=True,
        )
        _monitor_children(children, report)
    finally:
        _stop_owned_children(children)
    children = []
    if (source / "server/tests/test_e2e_process_cleanup.py").exists():
        cleanup = OUT / "process-cleanup"
        shutil.copytree(source, cleanup)
        state = cleanup / "test-state"
        state.mkdir()
        with (OUT / "process-cleanup.log").open("w") as log:
            check = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "pytest",
                    "-q",
                    "--tb=short",
                    f"--junitxml={OUT / 'process-cleanup.xml'}",
                    "tests/test_e2e_process_cleanup.py",
                ],
                check=False,
                cwd=cleanup / "server",
                env=environment(state),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        report["process_cleanup_exit_code"] = check.returncode
    else:
        report["process_cleanup_exit_code"] = None
        report["process_cleanup_note"] = (
            "This source snapshot has no separate process-cleanup test module"
        )
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    report["passed"] = report["process_cleanup_exit_code"] in (None, 0) and all(
        row["exit_code"] == 0 for row in report["shards"]
    )
    if report["passed"]:
        for command in (
            [
                PYTHON,
                "-m",
                "coverage",
                "combine",
                "--keep",
                "--rcfile=" + str(OUT / "coverage.ini"),
                "--data-file=" + str(OUT / ".coverage"),
                str(OUT / "coverage"),
            ],
            [
                PYTHON,
                "-m",
                "coverage",
                "json",
                "--rcfile=" + str(OUT / "coverage.ini"),
                "--data-file=" + str(OUT / ".coverage"),
                "-o",
                str(OUT / "coverage.json"),
            ],
        ):
            subprocess.run(command, cwd=source / "server", check=True)
        report["counts"] = junit_counts(sorted(OUT.glob("*.xml")))
    (OUT / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps({key: report[key] for key in ("collected", "elapsed_seconds", "passed")}),
        flush=True,
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
