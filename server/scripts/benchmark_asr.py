#!/usr/bin/env python3
"""Orb ASR Benchmark — Measures Whisper pipeline latency & accuracy.

Benchmarks:
    1. Cold subprocess transcription (current baseline)
    2. Warm daemon transcription (new ZMQ path)
    3. RxNorm correction pass time
    4. End-to-end pipeline latency

Usage:
    python benchmark_asr.py                     # Run full benchmark
    python benchmark_asr.py --daemon-only       # Benchmark warm daemon only
    python benchmark_asr.py --audio FILE.wav    # Test with specific audio file
"""
import os
import sys
import json
import time
import tempfile
import subprocess
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))


def generate_test_audio(duration_secs: float = 3.0) -> str:
    """Generate a silent WAV file for latency benchmarking."""
    import struct
    import wave
    
    wav_path = tempfile.mktemp(suffix=".wav")
    sample_rate = 16000
    num_samples = int(sample_rate * duration_secs)
    
    with wave.open(wav_path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        # Write near-silence (tiny noise to avoid Whisper "no speech" skip)
        import random
        samples = [random.randint(-10, 10) for _ in range(num_samples)]
        f.writeframes(struct.pack(f"<{num_samples}h", *samples))
    
    return wav_path


def benchmark_subprocess(audio_path: str, iterations: int = 3) -> dict:
    """Benchmark the existing subprocess worker."""
    print("\n═══ Benchmark: Subprocess Worker (baseline) ═══")
    
    from services.asr_worker_protocol import STDIN_HINTS_FLAG, encode_hints
    worker = str(SERVER_DIR / "scripts" / "transcribe_worker.py")
    python = sys.executable
    
    times = []
    for i in range(iterations):
        t0 = time.time()
        result = subprocess.run(
            [python, worker, audio_path, "large-v3", STDIN_HINTS_FLAG, "12", ""],
            input=encode_hints(""),
            capture_output=True, text=True, timeout=120,
            cwd=str(SERVER_DIR),
        )
        elapsed = time.time() - t0
        times.append(elapsed)
        status = "✅" if result.returncode == 0 else "❌"
        print(f"  Run {i+1}: {elapsed:.2f}s {status}")
    
    avg = sum(times) / len(times)
    print(f"  Average: {avg:.2f}s")
    return {"method": "subprocess", "times": times, "avg": avg}


def benchmark_daemon(audio_path: str, iterations: int = 5) -> dict:
    """Benchmark the warm ZMQ daemon."""
    print("\n═══ Benchmark: Warm Daemon (ZMQ IPC) ═══")
    
    try:
        import zmq
    except ImportError:
        print("  ❌ pyzmq not installed")
        return {"method": "daemon", "times": [], "avg": 0, "error": "pyzmq not installed"}
    
    from config import VOICE
    socket_addr = VOICE["daemon"]["socket"]
    timeout_ms = VOICE["daemon"]["timeout_ms"]
    
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
    socket.setsockopt(zmq.SNDTIMEO, 2000)
    socket.setsockopt(zmq.LINGER, 0)
    
    try:
        socket.connect(socket_addr)
    except zmq.ZMQError as e:
        print(f"  ❌ Cannot connect to daemon: {e}")
        return {"method": "daemon", "times": [], "avg": 0, "error": str(e)}
    
    times = []
    for i in range(iterations):
        t0 = time.time()
        try:
            request = json.dumps({
                "audio_path": audio_path,
                "hints": "",
                "language": "en",
            })
            socket.send_string(request)
            response = socket.recv_string()
            data = json.loads(response)
            elapsed = time.time() - t0
            
            status = "✅" if data.get("status") == "ok" else "❌"
            server_ms = data.get("duration_ms", "?")
            print(f"  Run {i+1}: {elapsed:.3f}s (server: {server_ms}ms) {status}")
            times.append(elapsed)
        except zmq.Again:
            print(f"  Run {i+1}: TIMEOUT ❌")
        except Exception as e:
            print(f"  Run {i+1}: ERROR {e} ❌")
    
    socket.close()
    context.term()
    
    avg = sum(times) / len(times) if times else 0
    print(f"  Average: {avg:.3f}s")
    return {"method": "daemon", "times": times, "avg": avg}


def benchmark_rxnorm(iterations: int = 100) -> dict:
    """Benchmark RxNorm phonetic correction speed."""
    print("\n═══ Benchmark: RxNorm Phonetic Correction ═══")
    
    from config import VOICE
    from services.rxnorm_corrector import RxNormCorrector
    
    corrector = RxNormCorrector(
        db_path=VOICE["corrector"]["rxnorm_db"],
        csv_path=VOICE["corrector"]["rxnorm_csv"],
    )
    
    test_texts = [
        "Patient prescribed amoxcilin and metoprolol for hypertension management",
        "Started on vancomicin IV drip with trough levels ordered",
        "Give augmentin 625 mg oral three times daily with food",
        "Insulin glargine 20 units subcutaneous at bedtime",
        "Paracetamol 500 mg PRN every six hours for fever",
    ]
    
    times = []
    for _ in range(iterations):
        for text in test_texts:
            t0 = time.time()
            corrector.correct(text)
            times.append(time.time() - t0)
    
    avg_ms = (sum(times) / len(times)) * 1000
    p95_ms = sorted(times)[int(len(times) * 0.95)] * 1000
    p99_ms = sorted(times)[int(len(times) * 0.99)] * 1000
    
    print(f"  Avg:  {avg_ms:.2f}ms")
    print(f"  P95:  {p95_ms:.2f}ms")
    print(f"  P99:  {p99_ms:.2f}ms")
    print(f"  Runs: {len(times)}")
    
    return {"method": "rxnorm", "avg_ms": avg_ms, "p95_ms": p95_ms, "p99_ms": p99_ms}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Orb ASR Benchmark")
    parser.add_argument("--audio", type=str, help="Path to audio file to test")
    parser.add_argument("--daemon-only", action="store_true", help="Only benchmark daemon")
    parser.add_argument("--rxnorm-only", action="store_true", help="Only benchmark RxNorm corrector")
    args = parser.parse_args()
    
    print("╔══════════════════════════════════════╗")
    print("║   Orb ASR Pipeline Benchmark Suite   ║")
    print("╚══════════════════════════════════════╝")
    
    results = {}
    
    # RxNorm benchmark (always fast)
    if args.rxnorm_only or not args.daemon_only:
        results["rxnorm"] = benchmark_rxnorm()
    
    if args.rxnorm_only:
        return
    
    # Generate or use provided test audio
    if args.audio:
        audio_path = args.audio
        print(f"\nUsing provided audio: {audio_path}")
    else:
        print("\nGenerating 3s test audio...")
        audio_path = generate_test_audio(3.0)
        print(f"  Created: {audio_path}")
    
    # Subprocess benchmark (baseline)
    if not args.daemon_only:
        results["subprocess"] = benchmark_subprocess(audio_path)
    
    # Daemon benchmark
    results["daemon"] = benchmark_daemon(audio_path)
    
    # Summary
    print("\n" + "═" * 50)
    print("SUMMARY")
    print("═" * 50)
    
    if "subprocess" in results and results["subprocess"]["times"]:
        print(f"  Subprocess avg: {results['subprocess']['avg']:.2f}s")
    if "daemon" in results and results["daemon"]["times"]:
        print(f"  Daemon avg:     {results['daemon']['avg']:.3f}s")
    if "subprocess" in results and "daemon" in results:
        if results["subprocess"]["times"] and results["daemon"]["times"]:
            speedup = results["subprocess"]["avg"] / results["daemon"]["avg"]
            print(f"  Speedup:        {speedup:.1f}x faster with daemon")
    if "rxnorm" in results:
        print(f"  RxNorm avg:     {results['rxnorm']['avg_ms']:.2f}ms")
    
    # Cleanup
    if not args.audio and os.path.exists(audio_path):
        os.remove(audio_path)
    
    print("\nDone.")


if __name__ == "__main__":
    main()
