#!/usr/bin/env python3
"""Orb Medical ASR WER Benchmark Suite

Runs a synthetic-speech comparison of the installed Whisper pipeline on macOS
inside an invocation-owned disposable database:
1. Uses macOS built-in speech synthesis (say) to generate real acoustic WAV files.
2. Compares raw Whisper, context-biased Whisper, and RxNorm-corrected transcriptions.
3. Computes Word Error Rate (WER) using Levenshtein distance.
4. Reports synthetic-case WER for the three configurations. This does not
   establish clinical accuracy or patient-speech performance.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict

# Set up paths
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

# Import the application only inside a fresh worker whose database is owned by
# this invocation. Importing this developer tool must never bind the live DB.
import secrets
import sqlite3


def _workspace() -> Path:
    root = Path(os.environ.get("ORB_WER_WORKSPACE", "")).resolve()
    token = os.environ.get("ORB_WER_TOKEN", "")
    marker = root / ".owned-by-wer"
    if (
        not token
        or not marker.is_file()
        or marker.is_symlink()
        or marker.read_text() != token
        or Path(os.environ.get("ORB_DB_PATH", "")).resolve() != root / "benchmark.db"
    ):
        raise RuntimeError("WER benchmark requires its own disposable database worker")
    return root


def _load_runtime():
    root = _workspace()
    path = root / "benchmark.db"
    # Refuse reuse, including an empty preexisting file or a symlink. This is
    # never a snapshot/copy of the configured application database.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE patients(id TEXT PRIMARY KEY, name TEXT, mrn TEXT UNIQUE,
                diagnosis TEXT, created_at TEXT, status TEXT,
                attending_doctor_id TEXT REFERENCES users(id));
            CREATE TABLE medications(id INTEGER PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id),
                drug_name TEXT, dosage TEXT, frequency TEXT, status TEXT);
        """)
    global build_whisper_hints, transcribe_audio, VOICE, SCRIBE, db_session
    from config import DB_PATH, SCRIBE, VOICE
    from database import db_session
    from services.scribe_service import build_whisper_hints, transcribe_audio

    if Path(DB_PATH).resolve() != path:
        raise RuntimeError("Application database was imported before benchmark isolation")
    # Use installed reference data, never rebuild or mutate its registry. The
    # existing corrector may create a missing DB from CSV; only a private copy
    # is therefore handed to it. Missing provisioning fails before inference.
    reference = Path(VOICE["corrector"]["rxnorm_db"])
    if not reference.is_file():
        raise RuntimeError("The local RxNorm reference must be provisioned before benchmarking")
    local_reference = root / "rxnorm-reference.db"
    with sqlite3.connect(reference.resolve().as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(local_reference) as dest:
            src.backup(dest)
    os.chmod(local_reference, 0o600)
    VOICE["corrector"]["rxnorm_db"] = str(local_reference)
    VOICE["corrector"]["rxnorm_csv"] = ""
    VOICE["daemon"]["enabled"] = False


def _require_benchmark_connection():
    root = _workspace()
    import config
    import database

    if (
        Path(config.DB_PATH).resolve() != root / "benchmark.db"
        or Path(database.DB_PATH).resolve() != root / "benchmark.db"
    ):
        raise RuntimeError("Refusing benchmark writes outside the owned disposable database")


def isolated_main(run=subprocess.run) -> int:
    """Launch a fresh process before config/database imports, then remove its DB."""
    with tempfile.TemporaryDirectory(prefix="orb_wer_benchmark_") as directory:
        root = Path(directory).resolve()
        token = secrets.token_urlsafe(32)
        marker = root / ".owned-by-wer"
        marker.write_text(token)
        marker.chmod(0o600)
        env = dict(os.environ)
        env.update(
            ORB_WER_WORKSPACE=str(root),
            ORB_WER_TOKEN=token,
            ORB_DB_PATH=str(root / "benchmark.db"),
            ORB_DEPLOYMENT_PROFILE="dev",
            ORB_DB_ENCRYPTION_KEY="",
            ORB_UPLOAD_ENCRYPTION_KEY="",
            ORB_DB_POOL="0",
            ORB_JWT_SECRET=secrets.token_urlsafe(48),
            ORB_WHISPER_DAEMON_ENABLED="false",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            ORB_BACKUP_DIR=str(root / "backups"),
            ORB_FHIR_EXPORT_DIR=str(root / "exports"),
            ORB_WATCHDOG_DIR=str(root / "watchdog"),
        )
        result = run(
            [sys.executable, str(Path(__file__).resolve()), "--isolated-worker"],
            env=env,
            check=False,
        )
        return result.returncode if result.returncode >= 0 else 1


# Define clinical test cases
class _ClinicalCase(TypedDict):
    id: str
    patient_name: str
    diagnosis: str
    medications: list[str]
    spoken_text: str
    reference: str
    expected_misspelling: str


class _AsrMeasurement(TypedDict):
    text: str
    wer: float
    ms: int


class _CaseResult(TypedDict):
    id: str
    reference: str
    baseline: _AsrMeasurement
    biased: _AsrMeasurement
    full: _AsrMeasurement


CLINICAL_TEST_CASES: list[_ClinicalCase] = [
    {
        "id": "CASE_DOLO",
        "patient_name": "Rajesh Kumar",
        "diagnosis": "Viral Fever",
        "medications": ["Dolo", "Crocin"],
        "spoken_text": "prescribe dolo six fifty for viral fever and body pain",
        "reference": "prescribe dolo six fifty for viral fever and body pain",
        "expected_misspelling": "doloo",
    },
    {
        "id": "CASE_PANTOCID",
        "patient_name": "Anita Sharma",
        "diagnosis": "GERD / Acid Reflux",
        "medications": ["Pantocid", "Omez"],
        "spoken_text": "give pantocid forty milligrams before breakfast for gerd control",
        "reference": "give pantocid forty milligrams before breakfast for gerd control",
        "expected_misspelling": "pantocidd",
    },
    {
        "id": "CASE_MONOCEF",
        "patient_name": "Vikram Singh",
        "diagnosis": "Bacterial Pneumonia",
        "medications": ["Monocef", "Ceftum"],
        "spoken_text": "patient has been started on monocef one gram iv twice daily",
        "reference": "patient has been started on monocef one gram iv twice daily",
        "expected_misspelling": "monoceff",
    },
    {
        "id": "CASE_COMBIFLAM",
        "patient_name": "Sanjay Patel",
        "diagnosis": "Post-operative pain",
        "medications": ["Combiflam", "Calpol"],
        "spoken_text": "take combiflam three times daily for severe pain control",
        "reference": "take combiflam three times daily for severe pain control",
        "expected_misspelling": "combiflamm",
    },
]


def compute_wer(reference: str, hypothesis: str) -> float:
    """Compute Word Error Rate (WER) using Levenshtein distance."""

    # Normalize inputs (lowercase, remove basic punctuation)
    def clean(t: str) -> list[str]:
        punc = '.,;:!?()[]{}""\''
        cleaned = "".join(c for c in t.lower() if c not in punc)
        return cleaned.split()

    ref_words = clean(reference)
    hyp_words = clean(hypothesis)

    # Standard DP Levenshtein table
    d = [[0] * (len(hyp_words) + 1) for _ in range(len(ref_words) + 1)]
    for i in range(len(ref_words) + 1):
        d[i][0] = i
    for j in range(len(hyp_words) + 1):
        d[0][j] = j

    for i in range(1, len(ref_words) + 1):
        for j in range(1, len(hyp_words) + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                substitution = d[i - 1][j - 1] + 1
                insertion = d[i][j - 1] + 1
                deletion = d[i - 1][j] + 1
                d[i][j] = min(substitution, insertion, deletion)

    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    return d[len(ref_words)][len(hyp_words)] / len(ref_words)


def synthesize_speech(text: str, output_wav: str) -> bool:
    """Synthesize text using macOS built-in speech synthesis (say)."""
    try:
        # Use macOS 'say' command to export WAV directly
        subprocess.run(
            ["say", "-o", output_wav, "--file-format=WAVE", "--data-format=I16@16000", text],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        print(f"Speech synthesis error for '{text}': {e}")
        return False


def seed_test_patient(patient_id: str, case: _ClinicalCase):
    """Seed only this invocation's synthetic context, with enforced references."""
    _require_benchmark_connection()
    with db_session(immediate=True) as db:
        db.execute(
            "INSERT INTO patients(id,name,mrn,diagnosis,created_at,status) VALUES (?,?,?,?,?,'admitted')",
            (
                patient_id,
                case["patient_name"],
                "MRN-" + patient_id,
                case["diagnosis"],
                "2026-05-20T00:00:00Z",
            ),
        )
        for med in case["medications"]:
            db.execute(
                "INSERT INTO medications(patient_id,drug_name,dosage,frequency,status) VALUES (?,?,'1 tab','once daily','active')",
                (patient_id, med),
            )


def cleanup_test_patient(patient_id: str):
    _require_benchmark_connection()
    with db_session(immediate=True) as db:
        db.execute("DELETE FROM medications WHERE patient_id=?", (patient_id,))
        db.execute("DELETE FROM patients WHERE id=?", (patient_id,))


def run_transcription(audio_path: str, hints: str, apply_corrections: bool) -> str:
    """Run transcription through Scribe pipeline with specific configurations."""
    # Temporarily override configuration for testing
    original_corrector_enabled = VOICE["corrector"]["enabled"]
    original_scribe_correction_enabled = SCRIBE["correction"]["enabled"]

    try:
        VOICE["corrector"]["enabled"] = apply_corrections
        SCRIBE["correction"]["enabled"] = False  # Disable general LLM correction for raw WER

        # Use transcribe_audio pipeline
        text = transcribe_audio(audio_path, apply_correction=False, hints=hints)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("ASR returned no transcript; benchmark result is unavailable")
        return text
    finally:
        VOICE["corrector"]["enabled"] = original_corrector_enabled
        SCRIBE["correction"]["enabled"] = original_scribe_correction_enabled


def run_benchmark():
    print("=========================================================")
    print("      Orb Medical ASR Word Error Rate (WER) Benchmark    ")
    print("=========================================================")
    print(f"Server Directory: {SERVER_DIR}")
    print(f"Local time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=========================================================\n")

    # Check if Whisper warm daemon is enabled
    daemon_enabled = VOICE["daemon"]["enabled"]
    print(f"Warm Daemon Configuration: {'ENABLED' if daemon_enabled else 'DISABLED'}")
    print(f"RxNorm SQLite DB: {VOICE['corrector']['rxnorm_db']}")

    results: list[_CaseResult] = []

    # Loop over all cases
    for case in CLINICAL_TEST_CASES:
        print(
            f"\n[Case {case['id']}] Preparing patient context: {case['patient_name']} ({case['diagnosis']})..."
        )

        fd = None
        temp_wav = None
        seeded = False
        try:
            # 1. Seed the patient context
            patient_id = f"PAT-WER-{secrets.token_hex(8)}-{case['id']}"
            seed_test_patient(patient_id, case)
            seeded = True

            # Validate that hints are built properly
            hints = build_whisper_hints(patient_id)
            if not hints:
                raise RuntimeError("Benchmark patient hints could not be built")
            print(f'  → Generated context hints: "{hints}"')

            # 2. Synthesize WAV speech file
            fd, temp_wav = tempfile.mkstemp(prefix="orb_wer_benchmark_", suffix=".wav")
            os.close(fd)
            fd = None
            print("  → Synthesizing speech audio (macOS say) into WAV...")
            if not synthesize_speech(case["spoken_text"], temp_wav):
                print("  ❌ Failed to generate audio. Skipping case.")
                continue

            print("  → Speech successfully synthesized. Running ASR pipeline...")

            # 3. Baseline ASR (No hints, no phonetic corrector)
            t_start = time.perf_counter()
            baseline_text = run_transcription(temp_wav, hints="", apply_corrections=False)
            t_baseline = round((time.perf_counter() - t_start) * 1000)
            baseline_wer = compute_wer(case["reference"], baseline_text)

            # 4. Context-Biased ASR (With patient context hints, no phonetic corrector)
            t_start = time.perf_counter()
            biased_text = run_transcription(temp_wav, hints=hints, apply_corrections=False)
            t_biased = round((time.perf_counter() - t_start) * 1000)
            biased_wer = compute_wer(case["reference"], biased_text)

            # 5. Full Pipeline (With patient context hints AND RxNorm phonetic correction)
            t_start = time.perf_counter()
            full_text = run_transcription(temp_wav, hints=hints, apply_corrections=True)
            t_full = round((time.perf_counter() - t_start) * 1000)
            full_wer = compute_wer(case["reference"], full_text)

            # Log case details
            results.append(
                {
                    "id": case["id"],
                    "reference": case["reference"],
                    "baseline": {"text": baseline_text, "wer": baseline_wer, "ms": t_baseline},
                    "biased": {"text": biased_text, "wer": biased_wer, "ms": t_biased},
                    "full": {"text": full_text, "wer": full_wer, "ms": t_full},
                }
            )

        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_wav is not None:
                try:
                    os.remove(temp_wav)
                except OSError:
                    pass

            if seeded:
                cleanup_test_patient(patient_id)

        # Print case summary
        print(f'  [Baseline] WER: {baseline_wer:.1%}, ASR: "{baseline_text}" ({t_baseline}ms)')
        print(f'  [Biased  ] WER: {biased_wer:.1%}, ASR: "{biased_text}" ({t_biased}ms)')
        print(f'  [Full    ] WER: {full_wer:.1%}, ASR: "{full_text}" ({t_full}ms)')

    if not results:
        print(
            "No benchmark cases completed; check speech synthesis before retrying.", file=sys.stderr
        )
        raise SystemExit(1)

    # --- Generate Beautiful Final Summary Report ---
    print("\n\n" + "═" * 80)
    print("                      ASR ACCURACY & LATENCY BENCHMARK REPORT                    ")
    print("═" * 80)

    # Calculate global statistics
    cases_run = len(results)
    avg_baseline_wer = sum(r["baseline"]["wer"] for r in results) / cases_run
    avg_biased_wer = sum(r["biased"]["wer"] for r in results) / cases_run
    avg_full_wer = sum(r["full"]["wer"] for r in results) / cases_run

    avg_baseline_ms = sum(r["baseline"]["ms"] for r in results) / cases_run
    avg_biased_ms = sum(r["biased"]["ms"] for r in results) / cases_run
    avg_full_ms = sum(r["full"]["ms"] for r in results) / cases_run

    print(
        f"{'Case ID':<18} | {'Baseline WER':<13} | {'Biased WER':<13} | {'Full Pipeline WER':<17} | {'Improvement':<12}"
    )
    print("-" * 80)
    for r in results:
        improvement = r["baseline"]["wer"] - r["full"]["wer"]
        imp_str = f"+{improvement:.1%}" if improvement > 0 else f"{improvement:.1%}"
        if improvement > 0:
            imp_str = f"\033[92m{imp_str}\033[0m"  # Green highlight
        print(
            f"{r['id']:<18} | {r['baseline']['wer']:<13.1%} | {r['biased']['wer']:<13.1%} | {r['full']['wer']:<17.1%} | {imp_str:<12}"
        )

    print("-" * 80)
    overall_imp = avg_baseline_wer - avg_full_wer
    print(
        f"{'AVERAGE':<18} | {avg_baseline_wer:<13.1%} | {avg_biased_wer:<13.1%} | {avg_full_wer:<17.1%} | \033[92m+{overall_imp:.1%}\033[0m"
    )
    print(
        f"{'AVG LATENCY':<18} | {avg_baseline_ms:<13.0f}ms | {avg_biased_ms:<13.0f}ms | {avg_full_ms:<17.0f}ms | -"
    )
    print("═" * 80)

    # Show detail logs of corrections
    print("\nDetailed Clinical Correction Breakdown:")
    for r in results:
        print(f"\n• Case: {r['id']}")
        print(f'  Reference: "{r["reference"]}"')
        print(f'  Baseline:  "{r["baseline"]["text"]}"')
        print(f'  Biased:    "{r["biased"]["text"]}"')
        print(f'  Corrected: "{r["full"]["text"]}"')

    print("\nBenchmark completed successfully!\n")


def main() -> int:
    if sys.argv[1:] == ["--isolated-worker"]:
        try:
            _load_runtime()
            run_benchmark()
            return 0
        except Exception:
            print("WER benchmark failed; no application database was modified.", file=sys.stderr)
            return 1
    if sys.argv[1:]:
        print("Usage: benchmark_wer.py (always uses a disposable database)", file=sys.stderr)
        return 2
    return isolated_main()


if __name__ == "__main__":
    raise SystemExit(main())
