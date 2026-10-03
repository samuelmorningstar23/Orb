#!/usr/bin/env python3
"""Orb Medical ASR WER Benchmark Suite

Runs high-fidelity end-to-end benchmarking of the Whisper Turbo pipeline on macOS:
1. Uses macOS built-in speech synthesis (say) to generate real acoustic WAV files.
2. Compares raw Whisper, context-biased Whisper, and RxNorm-corrected transcriptions.
3. Computes Word Error Rate (WER) using Levenshtein distance.
4. Generates a clinical accuracy report comparing the three configurations.
"""
import os
import sys
import json
import time
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple, Dict

# Set up paths
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from services.scribe_service import build_whisper_hints, transcribe_audio
from services.rxnorm_corrector import RxNormCorrector
from config import VOICE, SCRIBE
# orb.db may be SQLCipher-encrypted at rest, which the stdlib sqlite3 driver
# cannot open at all ("file is not a database"); the fixture helpers below go
# through the app's codec-aware connection instead. (The RxNorm reference DB used
# by RxNormCorrector is a separate, unencrypted file and keeps stdlib sqlite3.)
from database import _open_raw_connection, _row_factory_for

# Define clinical test cases
CLINICAL_TEST_CASES = [
    {
        "id": "CASE_DOLO",
        "patient_name": "Rajesh Kumar",
        "diagnosis": "Viral Fever",
        "medications": ["Dolo", "Crocin"],
        "spoken_text": "prescribe dolo six fifty for viral fever and body pain",
        "reference": "prescribe dolo six fifty for viral fever and body pain",
        "expected_misspelling": "doloo"
    },
    {
        "id": "CASE_PANTOCID",
        "patient_name": "Anita Sharma",
        "diagnosis": "GERD / Acid Reflux",
        "medications": ["Pantocid", "Omez"],
        "spoken_text": "give pantocid forty milligrams before breakfast for gerd control",
        "reference": "give pantocid forty milligrams before breakfast for gerd control",
        "expected_misspelling": "pantocidd"
    },
    {
        "id": "CASE_MONOCEF",
        "patient_name": "Vikram Singh",
        "diagnosis": "Bacterial Pneumonia",
        "medications": ["Monocef", "Ceftum"],
        "spoken_text": "patient has been started on monocef one gram iv twice daily",
        "reference": "patient has been started on monocef one gram iv twice daily",
        "expected_misspelling": "monoceff"
    },
    {
        "id": "CASE_COMBIFLAM",
        "patient_name": "Sanjay Patel",
        "diagnosis": "Post-operative pain",
        "medications": ["Combiflam", "Calpol"],
        "spoken_text": "take combiflam three times daily for severe pain control",
        "reference": "take combiflam three times daily for severe pain control",
        "expected_misspelling": "combiflamm"
    }
]


def compute_wer(reference: str, hypothesis: str) -> float:
    """Compute Word Error Rate (WER) using Levenshtein distance."""
    # Normalize inputs (lowercase, remove basic punctuation)
    def clean(t: str) -> List[str]:
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
            if ref_words[i-1] == hyp_words[j-1]:
                d[i][j] = d[i-1][j-1]
            else:
                substitution = d[i-1][j-1] + 1
                insertion = d[i][j-1] + 1
                deletion = d[i-1][j] + 1
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
            stderr=subprocess.DEVNULL
        )
        return True
    except Exception as e:
        print(f"Speech synthesis error for '{text}': {e}")
        return False


def seed_test_patient(patient_id: str, case: dict):
    """Seed patient data into orb.db to test dynamic hint building."""
    # Raw (not fully configured) connection on purpose: foreign_keys stays OFF so
    # the fixture can delete and re-insert the test patient without tripping
    # constraints from dependent tables.
    conn = _open_raw_connection()
    conn.row_factory = _row_factory_for(conn)
    cursor = conn.cursor()
    
    try:
        # Clean up existing test data
        cursor.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
        cursor.execute("DELETE FROM medications WHERE patient_id = ?", (patient_id,))
        
        # Insert Patient
        cursor.execute(
            "INSERT INTO patients (id, name, mrn, diagnosis, created_at, status) VALUES (?, ?, ?, ?, ?, 'admitted')",
            (patient_id, case["patient_name"], f"MRN-{case['id']}", case["diagnosis"], "2026-05-20T00:00:00Z")
        )
        
        # Insert Medications
        for med in case["medications"]:
            cursor.execute(
                "INSERT INTO medications (patient_id, drug_name, dosage, frequency, status) VALUES (?, ?, '1 tab', 'once daily', 'active')",
                (patient_id, med)
            )
            
        conn.commit()
    except Exception as e:
        print(f"DB seeding error: {e}")
        conn.rollback()
    finally:
        conn.close()


def cleanup_test_patient(patient_id: str):
    """Clean up test patient data from orb.db."""
    conn = _open_raw_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
        cursor.execute("DELETE FROM medications WHERE patient_id = ?", (patient_id,))
        conn.commit()
    except Exception as e:
        print(f"DB cleanup error: {e}")
    finally:
        conn.close()


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
        return text
    finally:
        VOICE["corrector"]["enabled"] = original_corrector_enabled
        SCRIBE["correction"]["enabled"] = original_scribe_correction_enabled


def main():
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
    
    results = []
    
    # Loop over all cases
    for case in CLINICAL_TEST_CASES:
        print(f"\n[Case {case['id']}] Preparing patient context: {case['patient_name']} ({case['diagnosis']})...")
        
        # 1. Seed the patient context
        patient_id = f"PAT-WER-{case['id']}"
        seed_test_patient(patient_id, case)
        
        # Validate that hints are built properly
        hints = build_whisper_hints(patient_id)
        print(f"  → Generated context hints: \"{hints}\"")
        
        # 2. Synthesize WAV speech file
        temp_wav = tempfile.mktemp(suffix=".wav")
        print(f"  → Synthesizing speech audio (macOS say) into WAV...")
        if not synthesize_speech(case["spoken_text"], temp_wav):
            print("  ❌ Failed to generate audio. Skipping case.")
            cleanup_test_patient(patient_id)
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
        results.append({
            "id": case["id"],
            "reference": case["reference"],
            "baseline": {"text": baseline_text, "wer": baseline_wer, "ms": t_baseline},
            "biased": {"text": biased_text, "wer": biased_wer, "ms": t_biased},
            "full": {"text": full_text, "wer": full_wer, "ms": t_full}
        })
        
        # Cleanup WAV and DB
        if os.path.exists(temp_wav):
            os.remove(temp_wav)
        cleanup_test_patient(patient_id)
        
        # Print case summary
        print(f"  [Baseline] WER: {baseline_wer:.1%}, ASR: \"{baseline_text}\" ({t_baseline}ms)")
        print(f"  [Biased  ] WER: {biased_wer:.1%}, ASR: \"{biased_text}\" ({t_biased}ms)")
        print(f"  [Full    ] WER: {full_wer:.1%}, ASR: \"{full_text}\" ({t_full}ms)")
        
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
    
    print(f"{'Case ID':<18} | {'Baseline WER':<13} | {'Biased WER':<13} | {'Full Pipeline WER':<17} | {'Improvement':<12}")
    print("-" * 80)
    for r in results:
        improvement = r["baseline"]["wer"] - r["full"]["wer"]
        imp_str = f"+{improvement:.1%}" if improvement > 0 else f"{improvement:.1%}"
        if improvement > 0:
            imp_str = f"\033[92m{imp_str}\033[0m"  # Green highlight
        print(f"{r['id']:<18} | {r['baseline']['wer']:<13.1%} | {r['biased']['wer']:<13.1%} | {r['full']['wer']:<17.1%} | {imp_str:<12}")
        
    print("-" * 80)
    overall_imp = avg_baseline_wer - avg_full_wer
    print(f"{'AVERAGE':<18} | {avg_baseline_wer:<13.1%} | {avg_biased_wer:<13.1%} | {avg_full_wer:<17.1%} | \033[92m+{overall_imp:.1%}\033[0m")
    print(f"{'AVG LATENCY':<18} | {avg_baseline_ms:<13.0f}ms | {avg_biased_ms:<13.0f}ms | {avg_full_ms:<17.0f}ms | -")
    print("═" * 80)
    
    # Show detail logs of corrections
    print("\nDetailed Clinical Correction Breakdown:")
    for r in results:
        print(f"\n• Case: {r['id']}")
        print(f"  Reference: \"{r['reference']}\"")
        print(f"  Baseline:  \"{r['baseline']['text']}\"")
        print(f"  Biased:    \"{r['biased']['text']}\"")
        print(f"  Corrected: \"{r['full']['text']}\"")
        
    print("\nBenchmark completed successfully!\n")


if __name__ == "__main__":
    main()
