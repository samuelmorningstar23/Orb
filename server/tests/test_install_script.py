"""Installer failure boundaries, exercised without installing or opening data."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _command(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("failure_stage", ["bootstrap", "backend"])
def test_failed_backend_dependency_install_stops_before_secrets_or_migrations(tmp_path, failure_stage):
    repo = tmp_path / "install fixture"
    server = repo / "server"
    server.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "install.sh", repo / "install.sh")
    (server / "requirements.txt").write_text("unavailable-package==0.0.0\n")
    shutil.copyfile(REPO_ROOT / "server" / "requirements-bootstrap.txt", server / "requirements-bootstrap.txt")

    commands = tmp_path / "commands"
    commands.mkdir()
    # Let the real interpreter answer the version checks; no venv is created.
    _command(commands / "python3", f"exec {shlex.quote(sys.executable)} \"$@\"\n")
    _command(commands / "openssl", 'printf "OpenSSL test-fixture\\n"\n')
    _command(commands / "python", """
if [ "$1 $2 $3 $4" = "-m pip install --quiet" ]; then
    if [ "$INSTALL_FAILURE_STAGE" = "bootstrap" ]; then
        printf 'simulated bootstrap resolution failure\n' >&2
        exit 42
    fi
    exit 0
fi
printf 'simulated dependency resolution failure\n' >&2
exit 42
""")
    activate = server / ".venv" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text(f"export PATH={shlex.quote(str(commands))}:\"$PATH\"\n")

    result = subprocess.run(
        ["bash", str(repo / "install.sh"), "--backend-only"],
        env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "INSTALL_FAILURE_STAGE": failure_stage},
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == (42 if failure_stage == "bootstrap" else 1)
    if failure_stage == "backend":
        assert "Backend dependencies did not install completely" in result.stderr
    else:
        assert "simulated bootstrap resolution failure" in result.stderr
        assert "Installing backend dependencies" not in result.stdout
    assert "Install complete" not in result.stdout
    assert not (repo / ".env").exists()
    assert not (repo / "certs").exists()
    assert not (server / "orb.db").exists()


@pytest.mark.parametrize("package", [
    "bcrypt", "cryptography", "qrcode", "pypng", "sqlcipher3-wheels", "python-socketio",
    "python-multipart", "pyjwt", "fastapi", "torch", "transformers",
])
def test_security_dependencies_have_the_same_explicit_test_and_runtime_pin(package):
    def pins(filename):
        return {
            line.split("==", 1)[0].strip(): line.split("==", 1)[1].split("#", 1)[0].strip()
            for line in (REPO_ROOT / "server" / filename).read_text().splitlines()
            if "==" in line and not line.lstrip().startswith("#")
        }

    runtime = pins("requirements.txt")
    tests = pins("requirements-dev.txt")
    assert package in runtime
    assert tests.get(package) == runtime[package]


@pytest.mark.parametrize("install_exit", [0, 42])
def test_mac_filtered_requirements_preserve_relative_constraints_and_cleanup(tmp_path, install_exit):
    """Run the real bounded install stanza; substitute the package installer."""
    server = tmp_path / "setup fixture with spaces" / "server"
    server.mkdir(parents=True)
    (server / "requirements.txt").write_text(
        "-c constraints-security.txt\ntransformers==5.19.0\nlightning-whisper-mlx==0.0.10\n"
    )
    (server / "constraints-security.txt").write_text("starlette==1.3.1\n")
    capture = tmp_path / "installer-capture.json"
    commands = tmp_path / "commands"
    commands.mkdir()
    fake_uv = commands / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys\n"
        "from pathlib import Path\n"
        "requirements = Path(sys.argv[sys.argv.index('-r') + 1])\n"
        "assert requirements.parent == Path(os.environ['SERVER_DIR'])\n"
        "assert stat.S_IMODE(requirements.stat().st_mode) == 0o600\n"
        "text = requirements.read_text()\n"
        "assert 'lightning-whisper-mlx' not in text\n"
        "constraint = next(line[3:] for line in text.splitlines() if line.startswith('-c '))\n"
        "assert (requirements.parent / constraint).read_text() == 'starlette==1.3.1\\n'\n"
        "Path(os.environ['INSTALL_CAPTURE']).write_text(json.dumps({'requirements':str(requirements)}))\n"
        "sys.exit(int(os.environ['INSTALL_EXIT']))\n"
    )
    fake_uv.chmod(0o755)
    source = (REPO_ROOT / "setup-mac.sh").read_text()
    start = source.index('REQ_CORE="')
    end = source.index('ok "backend dependencies installed"', start)
    stanza = source[start:end + len('ok "backend dependencies installed"')]
    script = 'set -euo pipefail\ninfo() { :; }\nok() { :; }\n' + stanza
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "SERVER_DIR": str(server),
            "VENV": str(server / ".venv"),
            "INSTALL_CAPTURE": str(capture),
            "INSTALL_EXIT": str(install_exit),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == install_exit, result.stderr
    import json
    temporary = Path(json.loads(capture.read_text())["requirements"])
    assert not temporary.exists(), "temporary requirements are removed on success and failure"
    assert list(server.glob(".requirements-core.*")) == []
    assert (server / "requirements.txt").exists()
    assert (server / "constraints-security.txt").exists()
