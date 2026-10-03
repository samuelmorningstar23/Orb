"""Every command the status page names is one scripts/orb.sh understands.

The status page ends each problem with `./scripts/orb.sh <command>`, and the
footer points at docs/OPERATIONS.md. Until 2026-10-02 neither existed, so the
page's own advice was "command not found" at 3 a.m. This pins the two together:
each command string is parsed out of ops_health_service.py and driven through
orb.sh under ORB_SH_DRY_RUN=1, which prints what would run and runs nothing.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ORB_SH = REPO_ROOT / "scripts" / "orb.sh"
OPS = REPO_ROOT / "server" / "services" / "ops_health_service.py"
OPERATIONS_MD = REPO_ROOT / "docs" / "OPERATIONS.md"


def _named_commands() -> list[list[str]]:
    src = OPS.read_text(encoding="utf-8")
    found = re.findall(r"\./scripts/orb\.sh ([a-z][a-z-]*)((?: --[a-z]+)*)", src)
    commands = {(sub, flags.strip()) for sub, flags in found}
    return sorted([sub, *flags.split()] for sub, flags in commands)


def _orb(*args, env_extra=None, dry=True):
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    if dry:
        env["ORB_SH_DRY_RUN"] = "1"
    env.update(env_extra or {})
    return subprocess.run(["/bin/bash", str(ORB_SH), *args], capture_output=True,
                          text=True, env=env, timeout=30)


def test_the_page_names_commands():
    names = {c[0] for c in _named_commands()}
    # A guard on the parser itself: if the regex stops matching, this test file
    # would otherwise pass while checking nothing.
    assert {"status", "backup", "restore", "install-backup-schedule"} <= names


@pytest.mark.parametrize("command", _named_commands(), ids=lambda c: " ".join(c))
def test_every_command_the_status_page_names_dispatches(command):
    result = _orb(*command)
    assert result.returncode == 0, f"orb.sh {' '.join(command)}: {result.stderr}"
    assert result.stdout.startswith("would "), result.stdout


def test_an_unknown_command_is_refused():
    result = _orb("reticulate-splines")
    assert result.returncode != 0
    assert "unknown command" in result.stderr


def test_the_scripts_each_command_delegates_to_exist():
    for command in _named_commands():
        out = _orb(*command).stdout
        # Absolute for the shell scripts; relative to server/ (where orb.sh
        # cds) for the Python ones.
        for path in re.findall(r"(?:^|\s)(\S+\.(?:sh|py))(?=\s|$)", out):
            assert Path(path).exists() or (REPO_ROOT / "server" / path).exists(), path


def test_operations_doc_exists_and_covers_every_command_and_check():
    text = OPERATIONS_MD.read_text(encoding="utf-8")
    for command in _named_commands():
        assert f"./scripts/orb.sh {command[0]}" in text, command
    for title in ("Orb server", "Patient database", "Backups", "Medical AI",
                  "Bedside data feeds", "Storage space", "Site settings",
                  "Sign-in and administrators"):
        assert f"### {title}" in text, title
    assert "\u2014" not in text, "house style: no em dashes"


def test_restore_list_reads_the_receipts(tmp_path):
    def backup(stamp, ok):
        path = tmp_path / f"orb-backup-{stamp}.db"
        path.write_bytes(b"x" * 2048)
        if ok is not None:
            (tmp_path / f"orb-backup-{stamp}.verified.json").write_text(
                json.dumps({"schema": "orb.backup.verification/1", "ok": ok}, indent=2))
        return path

    backup("20261001-020000", True)
    backup("20261002-020000", False)
    newest = backup("20261003-020000", None)
    later = time.time() + 5
    os.utime(newest, (later, later))
    result = _orb("restore", "--list", env_extra={"ORB_BACKUP_DIR": str(tmp_path)}, dry=False)
    assert result.returncode == 0, result.stderr
    lines = {line.split()[0]: line for line in result.stdout.splitlines()
             if line.strip().startswith("orb-backup-")}
    assert "verified" in lines["orb-backup-20261001-020000.db"]
    assert "FAILED" in lines["orb-backup-20261002-020000.db"]
    assert "unchecked" in lines["orb-backup-20261003-020000.db"]
    first = next(line for line in result.stdout.splitlines() if "orb-backup-" in line)
    assert "20261003" in first, "newest first"
