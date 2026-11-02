"""Synthetic-only WER isolation checks; never synthesize speech or load models."""
import copy
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import config
import database
from scripts import benchmark_wer as benchmark


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_launcher_overrides_configured_database_and_cleans_owned_workspace(tmp_path, monkeypatch, outcome):
    configured = tmp_path / "configured.db"
    configured.write_bytes(b"configured sentinel: never open or change")
    monkeypatch.setenv("ORB_DB_PATH", str(configured))
    monkeypatch.setenv("ORB_WHISPER_DAEMON_ENABLED", "true")
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    roots = []
    def run(command, *, env, check):
        root = Path(env["ORB_WER_WORKSPACE"])
        roots.append(root)
        assert root.is_dir() and root.stat().st_mode & 0o777 == 0o700
        assert command[-1] == "--isolated-worker" and check is False
        assert env["ORB_DB_PATH"] == str(root / "benchmark.db")
        assert not (root / "benchmark.db").exists()
        assert (root / ".owned-by-wer").read_text() == env["ORB_WER_TOKEN"]
        assert env["ORB_WHISPER_DAEMON_ENABLED"] == "false"
        assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
        assert env["ORB_DEPLOYMENT_PROFILE"] == "dev" and env["ORB_DB_ENCRYPTION_KEY"] == ""
        (root / "benchmark.db").write_bytes(b"owned synthetic")
        (root / "benchmark.db-wal").write_bytes(b"owned sidecar")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return SimpleNamespace(returncode=0 if outcome == "success" else 1)
    if outcome == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            benchmark.isolated_main(run=run)
    else:
        assert benchmark.isolated_main(run=run) == (0 if outcome == "success" else 1)
    assert roots and not roots[0].exists()
    assert configured.read_bytes() == b"configured sentinel: never open or change"
    assert str(config.DB_PATH) == str(database.DB_PATH)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    (root / ".owned-by-wer").write_text("synthetic-token")
    monkeypatch.setenv("ORB_WER_WORKSPACE", str(root))
    monkeypatch.setenv("ORB_WER_TOKEN", "synthetic-token")
    monkeypatch.setenv("ORB_DB_PATH", str(root / "benchmark.db"))
    monkeypatch.setattr(config, "DB_PATH", root / "benchmark.db")
    monkeypatch.setattr(database, "DB_PATH", root / "benchmark.db")
    monkeypatch.setattr(config, "DB_ENCRYPTION_KEY", "")
    voice = copy.deepcopy(config.VOICE)
    reference = tmp_path / "synthetic-reference.db"
    with sqlite3.connect(reference) as db:
        db.execute("CREATE TABLE synthetic_reference(value TEXT)")
        db.execute("INSERT INTO synthetic_reference VALUES ('owned fixture')")
    voice["corrector"]["rxnorm_db"] = str(reference)
    monkeypatch.setattr(config, "VOICE", voice)
    # The runtime loader exports these references for the short-lived worker.
    for name in ("VOICE", "SCRIBE", "db_session", "transcribe_audio", "build_whisper_hints"):
        monkeypatch.setattr(benchmark, name, None, raising=False)
    return root, reference


def test_worker_creates_fresh_db_and_preserves_reference_registry(worker):
    root, reference = worker
    before = reference.read_bytes()
    benchmark._load_runtime()
    assert reference.read_bytes() == before
    assert Path(benchmark.VOICE["corrector"]["rxnorm_db"]).parent == root
    assert benchmark.VOICE["corrector"]["rxnorm_csv"] == ""
    assert not benchmark.VOICE["daemon"]["enabled"]
    benchmark.seed_test_patient("synthetic-patient", benchmark.CLINICAL_TEST_CASES[0])
    assert "Dolo" in benchmark.build_whisper_hints("synthetic-patient")
    benchmark.cleanup_test_patient("synthetic-patient")
    with database.db_session() as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 0


def test_seed_failure_rolls_back_and_propagates(worker):
    benchmark._load_runtime()
    case = dict(benchmark.CLINICAL_TEST_CASES[0], medications=["Synthetic drug", None])
    # A schema failure after the patient insert must never continue to ASR.
    with database.db_session() as db:
        db.execute("CREATE TRIGGER reject_bad_med BEFORE INSERT ON medications WHEN NEW.drug_name IS NULL BEGIN SELECT RAISE(ABORT,'synthetic seed failure'); END")
    with pytest.raises(Exception, match="synthetic seed failure"):
        benchmark.seed_test_patient("synthetic-patient", case)
    with database.db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 0


def test_existing_or_symlink_database_is_refused_before_seed(worker):
    root, _ = worker
    (root / "benchmark.db").write_bytes(b"existing sentinel")
    with pytest.raises(FileExistsError):
        benchmark._load_runtime()
    assert (root / "benchmark.db").read_bytes() == b"existing sentinel"


def test_missing_ownership_and_wrong_bound_database_refuse_writes(worker, monkeypatch):
    root, _ = worker
    benchmark._load_runtime()
    monkeypatch.setattr(database, "DB_PATH", root.parent / "foreign.db")
    with pytest.raises(RuntimeError, match="Refusing benchmark writes"):
        benchmark.seed_test_patient("synthetic-patient", benchmark.CLINICAL_TEST_CASES[0])
    (root / ".owned-by-wer").unlink()
    with pytest.raises(RuntimeError, match="disposable database worker"):
        benchmark._workspace()


def test_seed_failure_prevents_transcription_and_success_report(monkeypatch, capsys):
    monkeypatch.setattr(benchmark, "VOICE", {"daemon": {"enabled": False}, "corrector": {"rxnorm_db": "synthetic"}}, raising=False)
    monkeypatch.setattr(benchmark, "seed_test_patient", lambda *_: (_ for _ in ()).throw(RuntimeError("seed failed")))
    monkeypatch.setattr(benchmark, "run_transcription", lambda *_a, **_k: pytest.fail("ASR must not run"))
    with pytest.raises(RuntimeError, match="seed failed"):
        benchmark.run_benchmark()
    assert "Benchmark completed successfully" not in capsys.readouterr().out


def test_interruption_while_building_hints_cleans_seeded_context(worker, monkeypatch):
    benchmark._load_runtime()
    monkeypatch.setattr(benchmark, "build_whisper_hints", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        benchmark.run_benchmark()
    with database.db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 0


def test_empty_asr_result_is_an_error_and_restores_configuration(monkeypatch):
    voice = {"corrector": {"enabled": False}}
    scribe = {"correction": {"enabled": True}}
    monkeypatch.setattr(benchmark, "VOICE", voice, raising=False)
    monkeypatch.setattr(benchmark, "SCRIBE", scribe, raising=False)
    monkeypatch.setattr(benchmark, "transcribe_audio", lambda *_a, **_k: "", raising=False)
    with pytest.raises(RuntimeError, match="no transcript"):
        benchmark.run_transcription("synthetic.wav", hints="", apply_corrections=True)
    assert voice["corrector"]["enabled"] is False
    assert scribe["correction"]["enabled"] is True
