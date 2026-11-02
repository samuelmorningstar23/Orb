#!/usr/bin/env python3
"""Run the real app against disposable demo data, without touching an appliance.

Only application source and bundled reference text are copied. Databases, uploads,
site settings, model weights, keys and runtime logs never enter the test checkout.
The real ASGI lifespan, migrations, seeders, authentication and routes still run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".json", ".md", ".csv", ".toml"}
EXCLUDED_DIRS = {"tests", "uploads", "downtime_exports", "ops", "datasets", "adapters", "dev_tools"}
OFFLINE_DEMO_SWITCHES = (
    "FEEDBACK", "MORTUARY", "CSSD", "REPORTING", "FINANCE_TOOLS", "RADIOLOGY",
    "CARE_PLANS", "EMERGENCY", "PERIOPERATIVE", "DIALYSIS", "RECORD_GOVERNANCE",
    "RADIATION_SAFETY", "INFECTION_SURVEILLANCE",
)
CAREFUL_MODEL = "medgemma:27b"


def loopback_inference_url(value: str) -> str:
    """Accept a local HTTP origin only, never an appliance or proxy URL."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise argparse.ArgumentTypeError("inference URL must be a loopback HTTP origin") from error
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or port is None or not 1 <= port <= 65535):
        raise argparse.ArgumentTypeError("inference URL must be a loopback HTTP origin with an explicit port")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


def installed_local_model(url: str) -> dict:
    """Read metadata only. This never pulls, warms, unloads or changes a model."""
    url = loopback_inference_url(url)
    # An inherited HTTP_PROXY must not send the local probe outside the host.
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{url}/api/tags", timeout=5) as response:
        raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("local model metadata exceeded the 1 MiB limit")
    payload = json.loads(raw)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("local model service returned invalid installed-model metadata")
    for model in models:
        if (isinstance(model, dict) and model.get("name") == CAREFUL_MODEL
                and isinstance(model.get("digest"), str) and model["digest"]):
            return {key: model[key] for key in ("name", "digest", "size", "details") if key in model}
    raise ValueError(f"{CAREFUL_MODEL} must already be installed; this launcher does not download models")


def local_snapshot_path(value: str) -> str:
    """Require an explicit installed Transformers directory, never a hub id."""
    path = Path(value)
    if not path.is_absolute() or not path.is_dir() or not (path / "config.json").is_file():
        raise argparse.ArgumentTypeError("model snapshot must be an absolute installed directory containing config.json")
    return str(path)


_INITIALIZE_KNOWLEDGE = """
import json
from services.clinical_knowledge import init_knowledge_base
from database import db_session
import sqlite_vec
init_knowledge_base()
with db_session() as db:
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    sources = db.execute('SELECT COUNT(*) FROM clinical_knowledge').fetchone()[0]
    vectors = db.execute('SELECT COUNT(*) FROM clinical_knowledge_vec').fetchone()[0]
    fingerprint = db.execute("SELECT value FROM clinical_knowledge_meta WHERE key='corpus_fingerprint'").fetchone()[0]
    assert sources > 0 and vectors == sources, 'real knowledge corpus/vector initialization failed'
print('E2E knowledge corpus: ' + json.dumps({'sources': sources, 'vectors': vectors, 'fingerprint': fingerprint}), flush=True)
"""

# Keep the session leader alive until its parent finishes group cleanup. Even
# after the command exits, its PID/PGID cannot be reused for an unrelated job.
# Custom (rather than ignored) handlers reset to defaults when the command is
# exec'd, so TERM still reaches and terminates the application normally.
_CHILD_SUPERVISOR = """
import os, signal, subprocess, sys
signal.signal(signal.SIGTERM, lambda *_: None)
signal.signal(signal.SIGINT, lambda *_: None)
fd = int(sys.argv[1])
os.set_inheritable(fd, False)
try:
    code = subprocess.call(sys.argv[2:])
except OSError:
    code = 127
os.write(fd, (str(code) + '\\n').encode('ascii'))
os.close(fd)
while True:
    signal.pause()
"""


def run_owned_child(
    command: list[str], *, cwd: Path, env: dict[str, str],
    check: bool = False, grace_seconds: float = 15,
) -> int:
    """Run one command in our own session and clean its entire process group.

    Seeding and the server use the same boundary: interruption before startup
    finishes cannot leave a writer behind as the temporary checkout is removed.
    Only the newly created session receives signals. Its supervisor stays
    unreaped until TERM, bounded shutdown, and KILL have finished.
    """
    read_fd, write_fd = os.pipe()
    try:
        status = os.fdopen(read_fd, "rb", buffering=0)
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    try:
        try:
            child = subprocess.Popen(
                [sys.executable, "-c", _CHILD_SUPERVISOR, str(write_fd), *command],
                cwd=cwd, env=env, start_new_session=True, pass_fds=(write_fd,),
            )
        finally:
            os.close(write_fd)
    except BaseException:
        status.close()
        raise

    with status:
        completed = False
        try:
            line = status.readline()
            if not line:
                raise RuntimeError("E2E child supervisor exited without a status")
            returncode = int(line)
            completed = True
        finally:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                if not completed:
                    # The supervisor reports when the command has stopped;
                    # keep the leader alive while descendants get their grace.
                    select.select([status], [], [], max(0, grace_seconds))
            except ProcessLookupError:
                pass
            finally:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, command)
    return returncode


def copy_application(root: Path, destination: Path) -> None:
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "server/"],
        cwd=root,
    ).decode().split("\0")
    for name in sorted(set(paths)):
        if not name:
            continue
        relative = Path(name)
        source = root / relative
        if (source.suffix not in SOURCE_SUFFIXES or source.is_symlink()
                or any(part in EXCLUDED_DIRS for part in relative.parts[1:-1])
                or ("models" in relative.parts[1:-1] and source.suffix != ".py")
                or relative.name == "site.toml" or not source.is_file()):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # Same origin and static assets as the production deployment.
    shutil.copytree(root / "dist", destination / "dist")


def isolated_environment(directory: Path) -> dict[str, str]:
    # Inherited appliance flags, credentials and paths must not change this demo.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("ORB_") and key not in {
               "PYTHONPATH", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
               "TRANSFORMERS_CACHE", "PYTORCH_TRANSFORMERS_CACHE", "PYTORCH_PRETRAINED_BERT_CACHE",
               "SENTENCE_TRANSFORMERS_HOME", "TORCH_HOME", "XDG_CACHE_HOME",
               "SAGE_RERANK_MODEL",
           }}
    env.update({
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
    })
    return env


def local_inference_environment(
    directory: Path, url: str, embedding_model: str | None = None, reranker_model: str | None = None,
    *, chart_prewarm: bool = False,
) -> dict[str, str]:
    """An idle, careful-model qualification over disposable synthetic data.

    The only external resource is an already installed model served on loopback.
    Empty offline HF caches deliberately do not copy appliance model assets.
    Disabling background inference controls contention; it does not qualify a
    busy ward or change the interactive clinical safety gates.
    """
    env = isolated_environment(directory)
    env.update({
        "ORB_OLLAMA_BASE_URL": loopback_inference_url(url),
        "ORB_LLM_MODEL": CAREFUL_MODEL,
        "ORB_AMBIENT_LLM_MODEL": CAREFUL_MODEL,
        "ORB_MODEL_FALLBACK_ENABLED": "false",
        "ORB_THINKING_DEFAULT": "on",
        "ORB_FAST_MODE_SAFETY_FLOOR": "1",
        "ORB_BACKGROUND_MODELS_ENABLED": "0",
        "ORB_SAGE_AMBIENT": "false",
        "ORB_SAGE_PREWARM": "true" if chart_prewarm else "false",
        "ORB_SAGE_ASSESSMENT_ENABLED": "false",
        "ORB_SAGE_HISTORY_SUMMARIZE": "0",
        "ORB_VOICE_AGENT_ENABLED": "false",
    })
    if bool(embedding_model) != bool(reranker_model):
        raise ValueError("provide both installed embedding and reranker snapshots")
    if embedding_model and reranker_model:
        env.update({
            "ORB_EMBEDDING_MODEL": local_snapshot_path(embedding_model),
            "SAGE_RERANK_MODEL": local_snapshot_path(reranker_model),
            "ORB_KNOWLEDGE_INPROCESS": "0",
        })
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--offline-modules", action="store_true",
                        help="Enable local workflow modules only inside this owned disposable demo")
    parser.add_argument("--local-inference-url", type=loopback_inference_url,
                        help="Opt in to installed medgemma:27b on a loopback HTTP origin; idle synthetic qualification only")
    parser.add_argument("--embedding-model", type=local_snapshot_path,
                        help="Read existing local embedder snapshot; initialize vectors only in the disposable DB")
    parser.add_argument("--reranker-model", type=local_snapshot_path,
                        help="Read existing local reranker snapshot; requires --embedding-model")
    parser.add_argument("--chart-prewarm", action="store_true",
                        help="Enable existing chart-open prefix prewarm only in the owned local-inference demo")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if not (ROOT / "dist/index.html").is_file():
        parser.error("build the frontend first: npm run build")
    if ((args.embedding_model or args.reranker_model) and not args.local_inference_url
            or bool(args.embedding_model) != bool(args.reranker_model)):
        parser.error("both model snapshots require --local-inference-url and each other")
    if args.chart_prewarm and not args.local_inference_url:
        parser.error("--chart-prewarm requires --local-inference-url")
    if args.local_inference_url:
        try:
            model = installed_local_model(args.local_inference_url)
        except (OSError, ValueError) as error:
            parser.error(f"local inference is unavailable: {error}")
        print("E2E local inference: " + json.dumps({
            "url": args.local_inference_url, "model": model,
            "background_model_jobs": False, "thinking": True, "safety_floor": True,
            "model_fallback": False, "scope": "idle synthetic qualification",
            "chart_prewarm": args.chart_prewarm,
            "embedding_snapshot": args.embedding_model, "reranker_snapshot": args.reranker_model,
            "retrieval": "installed snapshots with a new synthetic corpus" if args.embedding_model else "empty offline cache; retrieval may degrade",
        }), flush=True)

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)

    with tempfile.TemporaryDirectory(prefix="orb_e2e_") as temporary:
        directory = Path(temporary)
        copy_application(ROOT, directory)
        server = directory / "server"
        env = (local_inference_environment(directory, args.local_inference_url, args.embedding_model, args.reranker_model,
                                           chart_prewarm=args.chart_prewarm)
               if args.local_inference_url else isolated_environment(directory))
        if args.offline_modules:
            env.update({f"ORB_{switch}": "1" for switch in OFFLINE_DEMO_SWITCHES})
        env["ORB_HOST"] = args.host
        env["ORB_PORT"] = str(args.port)
        try:
            run_owned_child([sys.executable, "scripts/reset_db_and_seed.py"],
                            cwd=server, env=env, check=True)
            if args.embedding_model:
                run_owned_child([sys.executable, "-c", _INITIALIZE_KNOWLEDGE],
                                cwd=server, env=env, check=True)
            print(f"E2E demo data: {directory}; serving http://{args.host}:{args.port}", flush=True)
            return run_owned_child(
                [sys.executable, "-m", "uvicorn", "main:socket_app", "--host", args.host,
                 "--port", str(args.port)], cwd=server, env=env,
            )
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
