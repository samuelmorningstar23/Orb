"""Shutdown must never signal a different checkout or a recycled PID.

Run the actual shell script with fake process inspection and a Bash kill
function. No real process is signalled and no network request is sent.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_stop(tmp_path, processes, *, recycle=False):
    checkout = tmp_path / "Orb"
    checkout.mkdir()
    script = checkout / "stop-orb.sh"
    shutil.copyfile(ROOT / "stop-orb.sh", script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "processes.json"
    state.write_text(json.dumps(processes(str(checkout))))
    log = tmp_path / "signals"
    reclaimed = tmp_path / "reclaimed"
    mock = f"""#!{sys.executable}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
rows = json.loads(pathlib.Path(os.environ['PROCESS_STATE']).read_text())
if name == 'pgrep':
    print('\\n'.join(rows))
elif name in ('ps', 'lsof'):
    if '-p' not in sys.argv: sys.exit(0)
    pid = sys.argv[sys.argv.index('-p') + 1]
    row = rows.get(pid)
    if not row: sys.exit(1)
    if pathlib.Path(os.environ['RECLAIMED']).exists() and pid == '111':
        row = {{'command': 'python main.py', 'cwd': '/another-project'}}
    print(row['command'] if name == 'ps' else 'n' + row['cwd'])
elif name == 'curl':
    sys.exit(1)
elif name == 'pkill':
    sys.exit('global pkill is forbidden')
"""
    for name in ("pgrep", "ps", "lsof", "curl", "sleep", "pkill"):
        path = bin_dir / name
        path.write_text(mock)
        path.chmod(0o755)
    bash_env = tmp_path / "bash-env"
    bash_env.write_text('''
kill() {
  printf '%s %s\\n' "$1" "$2" >> "$SIGNAL_LOG"
  if [ "$RECYCLE" = 1 ] && [ "$2" = 111 ]; then : > "$RECLAIMED"; fi
}
''')
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env={
        **os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "BASH_ENV": str(bash_env),
        "PROCESS_STATE": str(state), "SIGNAL_LOG": str(log),
        "RECLAIMED": str(reclaimed), "RECYCLE": str(int(recycle)),
    }, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_shutdown_escalates_only_processes_owned_by_this_checkout(tmp_path):
    signals = run_stop(tmp_path, lambda root: {
        "111": {"command": "python main.py", "cwd": root + "/server"},
        "222": {"command": "python main.py", "cwd": "/other/Orb/server"},
        "333": {"command": "bash /other/Orb/run-orb.sh", "cwd": "/other/Orb"},
        "444": {"command": "node " + root + "/node_modules/.bin/vite", "cwd": root},
        "555": {"command": "bash " + root + "/run-orb.sh --dev", "cwd": "/tmp"},
        "666": {"command": "python /unrelated/tool.py --output " + root + "/server/main.py", "cwd": "/tmp"},
        "777": {"command": "bash /unrelated/tool.sh " + root + "/run-orb.sh", "cwd": "/tmp"},
    })
    assert set(signals) == {f"-{signal} {pid}" for signal in ("INT", "TERM", "KILL")
                            for pid in ("111", "444", "555")}


def test_shutdown_rechecks_ownership_before_escalating_a_reused_pid(tmp_path):
    signals = run_stop(tmp_path, lambda root: {
        "111": {"command": "python main.py", "cwd": root + "/server"},
    }, recycle=True)
    assert signals == ["-INT 111"]


@pytest.mark.parametrize("processes", [{}, {"222": {"command": "python main.py", "cwd": ""}}])
def test_shutdown_without_proven_owned_processes_signals_nobody(tmp_path, processes):
    assert run_stop(tmp_path, lambda _: processes) == []
