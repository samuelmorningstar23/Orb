"""Orb is strictly offline (standing rule, 4 October 2026): no model download.

Hugging Face libraries fetch a model the moment it is missing from the local
cache, and the knowledge embedder and reranker loaders fall back to a plain
model name for exactly that case. config.py and knowledge_worker.py therefore
set HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 before anything imports those
libraries, so a missing model fails fast instead of reaching the internet.
Provisioning, before the appliance joins the hospital network, may still pass
HF_HUB_OFFLINE=0 explicitly.

Each check runs in a fresh interpreter, because the guard acts at import time.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
_HF_VARS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")


def _env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in _HF_VARS}
    env.update(overrides)
    return env


def _read_after_import(module: str, env: dict) -> list[str]:
    code = (f"import os, {module}; "
            "print(os.environ.get('HF_HUB_OFFLINE')); print(os.environ.get('TRANSFORMERS_OFFLINE'))")
    out = subprocess.run([sys.executable, "-c", code], cwd=SERVER, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-2:]


@pytest.mark.parametrize("module", ["config", "services.knowledge_worker"])
def test_importing_orb_turns_model_downloads_off(module):
    assert _read_after_import(module, _env()) == ["1", "1"]


def test_provisioning_can_still_turn_downloads_on_explicitly():
    assert _read_after_import("config", _env(HF_HUB_OFFLINE="0")) == ["0", "1"]


def test_a_missing_model_fails_fast_instead_of_downloading():
    """With the guard on, asking for an uncached model raises at once; it does
    not try the network (which would hang or succeed on a connected machine)."""
    code = (
        "import config, time\n"
        "from huggingface_hub import snapshot_download\n"
        "t = time.monotonic()\n"
        "try:\n"
        "    snapshot_download('orb-test/model-that-is-never-cached-4f1c')\n"
        "    print('DOWNLOADED')\n"
        "except Exception as e:\n"
        "    print('REFUSED', type(e).__name__, round(time.monotonic() - t, 2))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=SERVER, env=_env(),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    verdict = out.stdout.strip().splitlines()[-1].split()
    assert verdict[0] == "REFUSED", out.stdout
    assert float(verdict[2]) < 5.0, "the refusal should be immediate, not a network timeout"


def test_parent_and_worker_share_explicit_local_embedding_model_override(tmp_path):
    code = "import config; print(config.EMBEDDING_MODEL)"
    out = subprocess.run([sys.executable, "-c", code], cwd=SERVER,
                         env=_env(ORB_EMBEDDING_MODEL=str(tmp_path)),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == str(tmp_path)
