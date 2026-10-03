"""Orb Vigil — Download PhysioNet Sepsis Challenge 2019 Dataset

Downloads a lightweight subset (first 2,000 patients) of the
PhysioNet Sepsis Detection Challenge 2019 vital signs telemetry dataset.
Provides realistic hourly ICU patient vitals for continuous risk model training.

PhysioNet serves these as individual .psv files — there is no zip archive.
This script downloads them concurrently using a thread pool.

Source: https://physionet.org/files/challenge-2019/1.0.0/training/training_setA/
"""

import os
import sys
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add server to path
SERVER_DIR = Path(__file__).resolve().parent.parent
DATASET_DIR = SERVER_DIR / "datasets" / "physionet_2019"
BASE_URL = "https://physionet.org/files/challenge-2019/1.0.0/training/training_setA"
MAX_PATIENTS = 2000  # Cap number of patient files to download
MAX_WORKERS = 16     # Concurrent download threads


def download_single_file(patient_num: int) -> tuple[int, bool, str]:
    """Download a single .psv patient file. Returns (num, success, msg)."""
    filename = f"p{patient_num:06d}.psv"
    url = f"{BASE_URL}/{filename}"
    target = DATASET_DIR / filename

    if target.exists() and target.stat().st_size > 0:
        return (patient_num, True, "cached")

    try:
        urllib.request.urlretrieve(url, str(target))
        return (patient_num, True, "downloaded")
    except Exception as e:
        return (patient_num, False, str(e))


def download_dataset():
    """Downloads the first MAX_PATIENTS .psv files concurrently."""
    DATASET_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already complete
    existing = list(DATASET_DIR.glob("*.psv"))
    if len(existing) >= MAX_PATIENTS:
        print(f"[Dataset] Already have {len(existing)} patient files in {DATASET_DIR}. Skipping.")
        return True

    cached = len(existing)
    needed = MAX_PATIENTS - cached
    print(f"[Dataset] Found {cached} cached files. Downloading up to {needed} more...")
    print(f"  Source: {BASE_URL}/")
    print(f"  Target: {DATASET_DIR}")
    print(f"  Workers: {MAX_WORKERS}")

    downloaded = 0
    failed = 0
    skipped = 0

    patient_nums = list(range(1, MAX_PATIENTS + 1))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_single_file, n): n
            for n in patient_nums
        }

        for i, future in enumerate(as_completed(futures), 1):
            num, success, msg = future.result()
            if success:
                if msg == "cached":
                    skipped += 1
                else:
                    downloaded += 1
            else:
                failed += 1

            # Progress reporting every 100 files
            if i % 100 == 0 or i == len(futures):
                total_done = downloaded + skipped
                sys.stdout.write(
                    f"\r  Progress: {i}/{MAX_PATIENTS} "
                    f"(downloaded: {downloaded}, cached: {skipped}, failed: {failed})"
                )
                sys.stdout.flush()

    print(f"\n✓ Dataset acquisition complete.")
    print(f"  Downloaded: {downloaded} new files")
    print(f"  Cached:     {skipped} existing files")
    print(f"  Failed:     {failed} files")

    final_count = len(list(DATASET_DIR.glob("*.psv")))
    print(f"  Total:      {final_count} patient files in {DATASET_DIR}")

    if failed > MAX_PATIENTS * 0.1:
        print(f"⚠ Warning: High failure rate ({failed}/{MAX_PATIENTS}). Check network.")
        return False
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("  ORB VIGIL — PhysioNet Sepsis Dataset Downloader")
    print("=" * 60)

    success = download_dataset()
    if not success:
        sys.exit(1)
