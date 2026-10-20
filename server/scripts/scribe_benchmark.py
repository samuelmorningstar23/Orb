#!/usr/bin/env python3
"""Orb Scribe Benchmark Harness

Compares Scribe pipeline stage configurations using text-based fixtures.
Measures latency, entity preservation, note consistency, and section completeness.

Usage:
  python scripts/scribe_benchmark.py
  python scripts/scribe_benchmark.py --fixtures scripts/fixtures/scribe/
  python scripts/scribe_benchmark.py --correction-model gemma3:4b --draft-model orb-sage
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add server root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SCRIBE  # noqa: E402 - application imports require the path/environment bootstrap above.
from services.scribe_providers import StageTimer  # noqa: E402 - application imports require the path/environment bootstrap above.
from services.scribe_service import (  # noqa: E402 - application imports require the path/environment bootstrap above.
    correct_transcript,
    structure_transcript,
    verify_note,
)


def load_fixtures(fixtures_dir: str) -> list[dict]:
    """Load all JSON fixtures from a directory."""
    fixtures = []
    for f in sorted(Path(fixtures_dir).glob("*.json")):
        with open(f) as fh:
            fixtures.append(json.load(fh))
    return fixtures


def compute_entity_preservation(expected: list[str], text: str) -> dict:
    """Check how many expected entities appear in the generated text."""
    text_lower = text.lower()
    found = [e for e in expected if e.lower() in text_lower]
    missing = [e for e in expected if e.lower() not in text_lower]
    score = len(found) / len(expected) if expected else 1.0
    return {
        "score": round(score, 3),
        "found": found,
        "missing": missing,
        "total": len(expected),
    }


def compute_section_completeness(expected_sections: list[str], note: str) -> dict:
    """Check which expected sections are present in the note."""
    note_upper = note.upper()
    found = [
        s
        for s in expected_sections
        if s.upper() + ":" in note_upper or s.upper() + "\n" in note_upper
    ]
    missing = [s for s in expected_sections if s not in found]
    score = len(found) / len(expected_sections) if expected_sections else 1.0
    return {
        "score": round(score, 3),
        "found": found,
        "missing": missing,
    }


async def run_benchmark(fixtures: list[dict], args) -> list[dict]:
    """Run the full pipeline benchmark against each fixture."""
    results = []

    for fixture in fixtures:
        case_id = fixture["case_id"]
        transcript = fixture["transcript"]
        expected_entities = fixture.get("expected_entities", [])
        expected_sections = fixture.get("expected_sections", [])

        print(f"\n{'=' * 60}")
        print(f"  Case: {case_id}")
        print(f"{'=' * 60}")

        timer = StageTimer()
        result = {"case_id": case_id}

        # Stage 2: Correction
        corrected = transcript
        if SCRIBE["correction"]["enabled"]:
            timer.start("correction")
            try:
                corrected = correct_transcript(transcript)
                timer.stop()
                print(f"  ✓ Correction: {timer.timings.get('correction', 0):.2f}s")
            except Exception as e:
                timer.stop()
                print(f"  ✗ Correction failed: {e}")
        else:
            print("  — Correction: disabled")

        # Stage 3: Drafting
        structured = ""
        timer.start("draft")
        try:
            structured = await structure_transcript(corrected, "soap")
            timer.stop()
            print(f"  ✓ Drafting: {timer.timings.get('draft', 0):.2f}s")
        except Exception as e:
            timer.stop()
            print(f"  ✗ Drafting failed: {e}")

        # Stage 4: Verification
        verification = None
        if SCRIBE["verify"]["enabled"] and structured:
            timer.start("verify")
            try:
                verification = await verify_note(transcript, structured)
                timer.stop()
                print(f"  ✓ Verification: {timer.timings.get('verify', 0):.2f}s")
                if verification:
                    print(
                        f"    Verified: {verification.get('verified')}, Confidence: {verification.get('confidence')}"
                    )
                    warnings = verification.get("warnings", [])
                    omissions = verification.get("omissions", [])
                    if warnings:
                        print(f"    Warnings: {len(warnings)}")
                    if omissions:
                        print(f"    Omissions: {len(omissions)}")
            except Exception as e:
                timer.stop()
                print(f"  ✗ Verification failed: {e}")
        else:
            print("  — Verification: disabled")

        # Metrics
        entity_check = compute_entity_preservation(expected_entities, structured)
        section_check = compute_section_completeness(expected_sections, structured)

        e2e_time = sum(timer.timings.values())

        result.update(
            {
                "timings": timer.to_dict(),
                "e2e_seconds": round(e2e_time, 3),
                "entity_preservation": entity_check,
                "section_completeness": section_check,
                "verification": verification,
                "correction_length": len(corrected),
                "note_length": len(structured),
            }
        )

        print(
            f"\n  Entity preservation: {entity_check['score'] * 100:.0f}% ({len(entity_check['found'])}/{entity_check['total']})"
        )
        if entity_check["missing"]:
            print(f"    Missing: {', '.join(entity_check['missing'])}")
        print(f"  Section completeness: {section_check['score'] * 100:.0f}%")
        if section_check["missing"]:
            print(f"    Missing: {', '.join(section_check['missing'])}")
        print(f"  End-to-end: {e2e_time:.2f}s")

        results.append(result)

    return results


def print_summary(results: list[dict]):
    """Print aggregate benchmark summary."""
    print(f"\n{'=' * 60}")
    print("  BENCHMARK SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Cases run: {len(results)}")

    if not results:
        return

    avg_e2e = sum(r["e2e_seconds"] for r in results) / len(results)
    avg_entity = sum(r["entity_preservation"]["score"] for r in results) / len(results)
    avg_section = sum(r["section_completeness"]["score"] for r in results) / len(results)

    print(f"  Avg end-to-end: {avg_e2e:.2f}s")
    print(f"  Avg entity preservation: {avg_entity * 100:.0f}%")
    print(f"  Avg section completeness: {avg_section * 100:.0f}%")

    # Per-stage timing averages
    stages = ["correction", "draft", "verify"]
    for stage in stages:
        times = [r["timings"].get(stage, 0) for r in results if r["timings"].get(stage)]
        if times:
            avg = sum(times) / len(times)
            print(f"  Avg {stage}: {avg:.2f}s")

    # Hallucination count from verification
    verified = [r for r in results if r.get("verification")]
    if verified:
        total_warnings = sum(len(r["verification"].get("warnings", [])) for r in verified)
        total_omissions = sum(len(r["verification"].get("omissions", [])) for r in verified)
        print(f"  Total warnings: {total_warnings}")
        print(f"  Total omissions: {total_omissions}")

    print("\n  Pipeline config:")
    print(f"    ASR model: {SCRIBE['asr']['model']}")
    print(
        f"    Correction model: {SCRIBE['correction']['model']} ({'enabled' if SCRIBE['correction']['enabled'] else 'disabled'})"
    )
    print(f"    Draft model: {SCRIBE['draft']['model']}")
    print(
        f"    Verify model: {SCRIBE['verify']['model']} ({'enabled' if SCRIBE['verify']['enabled'] else 'disabled'})"
    )


def main():
    parser = argparse.ArgumentParser(description="Orb Scribe Benchmark Harness")
    parser.add_argument(
        "--fixtures",
        default="scripts/fixtures/scribe/",
        help="Path to fixtures directory (default: scripts/fixtures/scribe/)",
    )
    parser.add_argument("--correction-model", help="Override correction model for this run")
    parser.add_argument("--draft-model", help="Override draft model for this run")
    parser.add_argument("--verify-model", help="Override verify model for this run")
    parser.add_argument("--enable-verify", action="store_true", help="Enable verification stage")
    parser.add_argument(
        "--disable-correction", action="store_true", help="Disable correction stage"
    )
    parser.add_argument("--output", help="Write JSON results to file")

    args = parser.parse_args()

    # Apply runtime overrides (these only affect this benchmark run)
    if args.correction_model:
        SCRIBE["correction"]["model"] = args.correction_model
    if args.draft_model:
        SCRIBE["draft"]["model"] = args.draft_model
    if args.verify_model:
        SCRIBE["verify"]["model"] = args.verify_model
    if args.enable_verify:
        SCRIBE["verify"]["enabled"] = True
    if args.disable_correction:
        SCRIBE["correction"]["enabled"] = False

    fixtures = load_fixtures(args.fixtures)
    if not fixtures:
        print(f"No fixtures found in {args.fixtures}")
        sys.exit(1)

    print(f"Orb Scribe Benchmark — {len(fixtures)} fixture(s)")
    results = asyncio.run(run_benchmark(fixtures, args))
    print_summary(results)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n  Results written to: {args.output}")


if __name__ == "__main__":
    main()
