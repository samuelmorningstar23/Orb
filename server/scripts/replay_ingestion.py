#!/usr/bin/env python3
"""Orb Hospital OS — Ingestion Replay CLI

Sends sample fixture files to the ingestion API endpoints for development testing.

Usage:
    python scripts/replay_ingestion.py                     # replay all fixtures
    python scripts/replay_ingestion.py --type json         # JSON only
    python scripts/replay_ingestion.py --type hl7          # HL7 only
    python scripts/replay_ingestion.py --type csv          # CSV (via /replay)
    python scripts/replay_ingestion.py --url http://host   # custom target
    python scripts/replay_ingestion.py --key MY_API_KEY    # custom API key
"""

import argparse
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("httpx is required. Install with: pip install httpx")
    sys.exit(1)


FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEFAULT_URL = "http://localhost:8000"
DEFAULT_KEY = "orb-ingest-dev-key"


def replay_json(base_url: str, api_key: str):
    """Send the sample JSON payload."""
    filepath = FIXTURES_DIR / "sample.json"
    if not filepath.exists():
        print(f"  ✗ File not found: {filepath}")
        return

    print("─── JSON Webhook Replay ───")
    payload = filepath.read_text()

    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.post(
            f"{base_url}/api/ingest/json",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "X-Correlation-Id": "replay-json-001",
                "X-Message-Id": "json-demo-001",
                "X-Source-Name": "replay_script",
            },
        )

    print(f"  Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"  Event ID:     {data.get('event_id')}")
        print(f"  Parse Status: {data.get('parse_status')}")
        counts = data.get("counts", {})
        print(f"  Received:     {counts.get('received', 0)}")
        print(f"  Vitals:       {counts.get('inserted_vitals', 0)}")
        print(f"  Observations: {counts.get('inserted_observations', 0)}")
        print(f"  Duplicates:   {counts.get('duplicates', 0)}")
        if data.get("errors"):
            print(f"  Errors:       {data['errors']}")
    except Exception:
        print(f"  Response: {resp.text[:200]}")
    print()


def replay_hl7(base_url: str, api_key: str):
    """Send the sample HL7 ORU message."""
    filepath = FIXTURES_DIR / "sample.hl7"
    if not filepath.exists():
        print(f"  ✗ File not found: {filepath}")
        return

    print("─── HL7 ORU Replay ───")
    payload = filepath.read_text()

    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.post(
            f"{base_url}/api/ingest/hl7",
            content=payload,
            headers={
                "Content-Type": "text/plain",
                "X-API-Key": api_key,
                "X-Correlation-Id": "replay-hl7-001",
                "X-Source-Name": "replay_script",
            },
        )

    print(f"  Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"  Event ID:     {data.get('event_id')}")
        print(f"  Parse Status: {data.get('parse_status')}")
        counts = data.get("counts", {})
        print(f"  Received:     {counts.get('received', 0)}")
        print(f"  Vitals:       {counts.get('inserted_vitals', 0)}")
        print(f"  Observations: {counts.get('inserted_observations', 0)}")
        print(f"  Duplicates:   {counts.get('duplicates', 0)}")
        if data.get("errors"):
            print(f"  Errors:       {data['errors']}")
    except Exception:
        print(f"  Response: {resp.text[:200]}")
    print()


def replay_csv(base_url: str, api_key: str):
    """Send the sample CSV via the auto-detect /replay endpoint."""
    filepath = FIXTURES_DIR / "sample.csv"
    if not filepath.exists():
        print(f"  ✗ File not found: {filepath}")
        return

    print("─── CSV Replay ───")
    payload = filepath.read_text()

    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.post(
            f"{base_url}/api/ingest/replay",
            content=payload,
            headers={
                "Content-Type": "text/csv",
                "X-API-Key": api_key,
            },
        )

    print(f"  Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"  Event ID:     {data.get('event_id')}")
        print(f"  Parse Status: {data.get('parse_status')}")
        counts = data.get("counts", {})
        print(f"  Received:     {counts.get('received', 0)}")
        print(f"  Vitals:       {counts.get('inserted_vitals', 0)}")
        print(f"  Observations: {counts.get('inserted_observations', 0)}")
        print(f"  Duplicates:   {counts.get('duplicates', 0)}")
        if data.get("errors"):
            print(f"  Errors:       {data['errors']}")
    except Exception:
        print(f"  Response: {resp.text[:200]}")
    print()


def check_status(base_url: str):
    """Query the ingestion status endpoint."""
    print("─── Ingestion Status ───")
    with httpx.Client(timeout=10, trust_env=False) as client:
        resp = client.get(f"{base_url}/api/ingest/status?limit=5")

    if resp.status_code == 200:
        data = resp.json()
        print(f"  Mode:   {data.get('ingestion_mode')}")
        print(f"  Events: {data.get('count')}")
        for evt in data.get("events", []):
            print(
                f"    [{evt['id']}] {evt['source_type']:>10} | {evt['parse_status']:<18} | "
                f"vitals={evt.get('vitals_inserted', 0)} obs={evt.get('observations_inserted', 0)} | "
                f"{evt.get('received_at', '')}"
            )
    else:
        print(f"  Error: {resp.status_code} — {resp.text[:200]}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Orb Ingestion Replay Tool")
    parser.add_argument("--url", default=DEFAULT_URL, help="Target server URL")
    parser.add_argument("--key", default=DEFAULT_KEY, help="Ingestion API key")
    parser.add_argument(
        "--type",
        choices=["json", "hl7", "csv", "all"],
        default="all",
        help="Which fixture to replay",
    )
    args = parser.parse_args()

    print("\n═══ Orb Ingestion Replay ═══")
    print(f"  Target: {args.url}")
    print()

    if args.type in ("json", "all"):
        replay_json(args.url, args.key)

    if args.type in ("hl7", "all"):
        replay_hl7(args.url, args.key)

    if args.type in ("csv", "all"):
        replay_csv(args.url, args.key)

    check_status(args.url)

    print("═══ Done ═══\n")


if __name__ == "__main__":
    main()
