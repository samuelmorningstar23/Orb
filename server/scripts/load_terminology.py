"""Load a diagnosis code release into the terminology table (opd-20, W9-G).

The loader services/terminology_service.py names. It reads a release file the
hospital brings (Orb never downloads one), and loads it into the existing
`terminology` table under code_system ICD10 through
terminology_service.load_release, which holds every rule:

  * NO RELEASE IS LOADED UNTIL COUNSEL CONFIRMS ITS LICENCE (ruling OPD-N11).
    Without --licence-ref (counsel's confirmation reference) every run is a
    DRY RUN: it reads the file and reports what a load would change, and
    writes nothing.
  * Idempotent. The same file twice changes nothing the second time, and the
    row count stays the same. Codes are never deleted. A code the release
    marks inactive is set inactive and no longer validates; with
    --retire-missing, so is every code the table holds that the release
    leaves out (the starter set's ICD-10-CM-only codes, for example).
  * One version row per code system names the release loaded (code_system
    RELEASE, is_active 0, never read as a code). --status prints it.
  * Each load writes one clinical_config audit row in its own transaction.

DIAGNOSES ALREADY WRITTEN. An outpatient diagnosis is free text with an
optional code (ruling OPD-N11); a code that was not in the table when the note
was saved is stored 'unvalidated'. --revalidate-drafts checks the codes on
UNSIGNED notes again against the table as it is now, on request only. A SIGNED
note is never rewritten (a database trigger refuses it as well); the report
only counts signed notes whose codes would now validate.

FILE FORMATS (--format, chosen from the extension when not given):
  csv         a header row, comma separated (.csv)
  tsv         a header row, tab separated (.tsv, .tab)
  code-title  no header; each line is a code, spaces, then its title (the
              plain "codes" text file some publishers ship, for example the
              ICD-10-CM codes file: "E119    Type 2 diabetes mellitus ...")
With a header the columns are found by name, case ignored, extra columns
ignored: the code (code, icd10, icd10_code, icd_code), the wording (display,
description, title, term, long_description, name) and, optionally, whether
the code is current (active, is_active, status: 1/0, true/false, yes/no,
active/inactive, retired, deprecated). A row with no status is active. Codes
are stored dotted after the third character (E119 is stored E11.9); block
ranges such as A00-A09 are skipped and counted.

Usage
-----
    # What would change (no licence reference: always a dry run)
    python scripts/load_terminology.py --file icd10_release.csv \\
        --release "WHO ICD-10" --version 2019

    # Load, once counsel has confirmed the licence
    python scripts/load_terminology.py --file icd10_release.csv \\
        --release "WHO ICD-10" --version 2019 --licence-ref "Counsel letter 2026-11-02"

    # Re-check unsigned outpatient notes against what is loaded now
    python scripts/load_terminology.py --revalidate-drafts

    # What is loaded
    python scripts/load_terminology.py --status

A tiny sample lives at scripts/fixtures/icd10_release_sample.csv to try the
dry run end to end.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

FORMATS = ("csv", "tsv", "code-title")
CODE_COLUMNS = ("code", "icd10", "icd10_code", "icd_code")
DISPLAY_COLUMNS = ("display", "description", "title", "term", "long_description", "name")
ACTIVE_COLUMNS = ("active", "is_active", "status")
_ACTIVE_WORDS = {"1": True, "true": True, "yes": True, "y": True, "active": True,
                 "current": True, "0": False, "false": False, "no": False, "n": False,
                 "inactive": False, "retired": False, "deprecated": False, "deleted": False}
_FORMAT_BY_SUFFIX = {".csv": "csv", ".tsv": "tsv", ".tab": "tsv"}


class ReleaseFileError(ValueError):
    """A release file the loader cannot read. Nothing is loaded."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _column(fieldnames, wanted) -> str | None:
    by_lower = {(name or "").strip().lower(): name for name in fieldnames or []}
    for name in wanted:
        if name in by_lower:
            return by_lower[name]
    return None


def _active(value, line: int) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return True
    if text not in _ACTIVE_WORDS:
        raise ReleaseFileError(
            f"Line {line}: the status '{value}' is not one this loader knows "
            f"({', '.join(sorted(_ACTIVE_WORDS))}). Nothing was loaded.")
    return _ACTIVE_WORDS[text]


def read_release(path: Path, fmt: str | None = None, *,
                 encoding: str = "utf-8-sig") -> list[tuple[str, str, bool]]:
    """(code, display, active) for every row of the file, as the file states
    them. terminology_service.load_release normalises and checks them."""
    fmt = fmt or _FORMAT_BY_SUFFIX.get(path.suffix.lower())
    if fmt not in FORMATS:
        raise ReleaseFileError(
            f"Say which format {path.name} is in with --format ({', '.join(FORMATS)}).")
    try:
        with path.open(newline="", encoding=encoding) as fh:
            if fmt == "code-title":
                rows = []
                for line_no, line in enumerate(fh, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    parts = text.split(None, 1)
                    rows.append((parts[0], parts[1] if len(parts) > 1 else "", True))
                return rows
            reader = csv.DictReader(fh, delimiter="," if fmt == "csv" else "\t")
            code_col = _column(reader.fieldnames, CODE_COLUMNS)
            display_col = _column(reader.fieldnames, DISPLAY_COLUMNS)
            active_col = _column(reader.fieldnames, ACTIVE_COLUMNS)
            if code_col is None or display_col is None:
                raise ReleaseFileError(
                    f"{path.name} needs a code column ({', '.join(CODE_COLUMNS)}) and a wording "
                    f"column ({', '.join(DISPLAY_COLUMNS)}) in its header row. Nothing was loaded.")
            return [(row.get(code_col) or "", row.get(display_col) or "",
                     _active(row.get(active_col), line_no + 1) if active_col else True)
                    for line_no, row in enumerate(reader, start=1)]
    except UnicodeDecodeError as exc:
        raise ReleaseFileError(
            f"{path.name} is not {encoding} text ({exc.reason}). Give its encoding with "
            "--encoding. Nothing was loaded.") from exc


def _operator() -> str | None:
    try:
        return getpass.getuser()
    except Exception:
        return None


def _print_report(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return
    print(f"Release: {report['release']} {report['version']} ({report['code_system']})")
    print(f"  file: {report.get('source_name') or 'unnamed'}, sha256 {report['source_sha256']}")
    print(f"  rows read {report['rows_read']}, codes {report['codes_in_release']} "
          f"({report['active_in_release']} current), "
          f"skipped as not a code {report['skipped_not_a_code']}")
    if report["skipped_samples"]:
        print(f"  skipped, for example: {', '.join(report['skipped_samples'])}")
    print(f"  new {report['inserted']} (+{report['inserted_inactive']} inactive), "
          f"reworded {report['display_updated']}, made current again {report['reactivated']}, "
          f"no longer valid {report['deactivated']}, "
          f"left out and retired {report['retired_missing']}, unchanged {report['unchanged']}")
    print(f"  current codes: {report['active_before']} before, {report['active_after']} after")
    if report["already_loaded"]:
        print("This release is already loaded and the table matches it. Nothing was written.")
    elif report["applied"]:
        print(f"Loaded at {report['loaded_at']} under licence reference {report['licence_ref']}.")
    else:
        print("Dry run: nothing was written.")


def _print_revalidation(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"revalidation": report}, indent=2, sort_keys=True, default=str))
        return
    if report.get("table_absent"):
        print("No outpatient notes on this computer; nothing to check.")
        return
    print(f"Unsigned outpatient notes checked: {report['drafts_checked']}, "
          f"changed {report['drafts_changed']} "
          f"({report['diagnoses_validated']} code(s) now valid, "
          f"{report['diagnoses_unvalidated']} no longer valid)")
    if report["unreadable_drafts"]:
        print(f"  {report['unreadable_drafts']} draft(s) could not be read and were left alone")
    print(f"Signed notes are never changed; {report['signed_with_codes_now_valid']} hold a code "
          "that would now validate. A doctor adds an addendum if one matters.")
    print("Applied." if report["applied"] else "Nothing was written.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Load a diagnosis code release into the terminology table (opd-20).")
    ap.add_argument("--file", help="The release file the hospital holds.")
    ap.add_argument("--format", choices=FORMATS, help="The file's format (default: from its extension).")
    ap.add_argument("--encoding", default="utf-8-sig", help="The file's text encoding.")
    ap.add_argument("--code-system", default="ICD10", help="The code system (ICD10 only).")
    ap.add_argument("--release", help="The release's name as the publisher prints it.")
    ap.add_argument("--version", help="The release's version.")
    ap.add_argument("--licence-ref",
                    help="Counsel's confirmation of the licence (ruling OPD-N11). Without it, a dry run.")
    ap.add_argument("--retire-missing", action="store_true",
                    help="Also make codes the release leaves out no longer valid.")
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    ap.add_argument("--revalidate-drafts", action="store_true",
                    help="Check the codes on unsigned outpatient notes again (signed notes never change).")
    ap.add_argument("--status", action="store_true", help="Print the release loaded for each code system.")
    ap.add_argument("--json", action="store_true", help="Print reports as JSON.")
    args = ap.parse_args(argv)

    from services import terminology_service as ts

    if args.status:
        out = {system: ts.release_info(system) for system in ts.LOADABLE_CODE_SYSTEMS}
        if args.json:
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            for system, info in out.items():
                if info is None:
                    print(f"{system}: no release loaded (the starter set only)")
                else:
                    print(f"{system}: {info.get('release')} {info.get('version')}, loaded "
                          f"{info.get('loaded_at')} by {info.get('loaded_by') or 'unknown'}, "
                          f"licence reference {info.get('licence_ref')}")
        return 0

    if not args.file and not args.revalidate_drafts:
        ap.error("give --file to load a release, --revalidate-drafts, or --status")

    operator = _operator()
    dry_run = bool(args.dry_run)
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            ap.error(f"file not found: {path}")
        if not args.release or not args.version:
            ap.error("name the release with --release and --version")
        if not args.licence_ref and not dry_run:
            print("No licence reference given: no release is loaded until counsel confirms its "
                  "licence (ruling OPD-N11). Running as a dry run.")
            dry_run = True
        try:
            entries = read_release(path, args.format, encoding=args.encoding)
            report = ts.load_release(
                entries, code_system=args.code_system, release=args.release,
                version=args.version, source_sha256=file_sha256(path), source_name=path.name,
                licence_ref=args.licence_ref, operator=operator,
                retire_missing=args.retire_missing, dry_run=dry_run)
        except (ReleaseFileError, ts.TerminologyLoadError) as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 1
        _print_report(report, args.json)

    if args.revalidate_drafts:
        result = ts.revalidate_draft_diagnoses(operator=operator, dry_run=dry_run)
        _print_revalidation(result, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
