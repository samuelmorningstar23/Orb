#!/usr/bin/env python3
"""Tool-assisted key-escrow sheet for the ceremony in docs/KEY_CEREMONY.md.

Two modes:

    # Produce the print-ready sheet (both keys + check digits) for §5B/§5C.
    server/.venv/bin/python scripts/escrow_sheet.py generate
    server/.venv/bin/python scripts/escrow_sheet.py generate --env-file /opt/orb/.env --shred

    # Recompute the check digits from the live keys and compare against what
    # the operator retypes off a printed sheet (§5D step 2, §8 step 4, §10).
    server/.venv/bin/python scripts/escrow_sheet.py verify

The check digit is EXACTLY the ceremony's §5B construction:

    printf 'orb-escrow-check-v1:%s' "$KEY" | shasum -a 256 | cut -c1-8

i.e. the first 8 lowercase hex characters of SHA-256 over the UTF-8 bytes of
the prefixed string — no trailing newline (the printf-not-echo trap in §5B).
The prefix exists so the printed digest can never be the derived encryption
key: Orb derives its 32-byte key as SHA-256 of the *unprefixed* key string
(§1), so a plain SHA-256 written on paper would BE the key. Never "simplify"
the prefix away.

Safety rules this script enforces:
  - Refuses to run unless BOTH keys are present (a sheet escrowing one key
    still loses patient data — §1). Never writes a partial sheet.
  - Key material goes ONLY into the output file (written 0600, refusing to
    overwrite). Nothing key-shaped is ever printed to stdout/stderr.
  - The sheet itself carries the PRINT-THEN-DELETE banner and the sealing /
    verification instructions, so the paper is self-describing on recovery
    night without this script or this repo.

Stdlib only, no server imports: this must run on a bare appliance and on the
standby Mac with nothing but Python.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import os
import socket
import sys
from pathlib import Path

CHECK_PREFIX = "orb-escrow-check-v1:"
CHECK_DIGIT_LEN = 8
KEY_NAMES = ("ORB_DB_ENCRYPTION_KEY", "ORB_UPLOAD_ENCRYPTION_KEY")

# What each key protects / what losing it means — from KEY_CEREMONY.md §1,
# reproduced on the sheet so the paper explains its own stakes.
KEY_STAKES = {
    "ORB_DB_ENCRYPTION_KEY": (
        "orb.db and every backup of it, the entire clinical record. "
        "If lost, every patient record becomes permanently unreadable."
    ),
    "ORB_UPLOAD_ENCRYPTION_KEY": (
        "Uploaded media: Lens images and Scribe audio. If lost, the record "
        "restores with holes: notes referring to an X-ray nobody can open."
    ),
}


def check_digit(key: str) -> str:
    """§5B check digit: first 8 lowercase hex chars of SHA-256 over the
    UTF-8 bytes of 'orb-escrow-check-v1:' + key (no trailing newline)."""
    digest = hashlib.sha256((CHECK_PREFIX + key).encode("utf-8")).hexdigest()
    return digest[:CHECK_DIGIT_LEN]


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal dotenv parser (config.py does not read .env, so we do).

    Understands KEY=VALUE lines, optional `export ` prefix, blank lines and
    full-line # comments, and strips one pair of matching quotes. Deliberately
    does NOT strip trailing `# comment` from unquoted values — truncating a
    value is the dangerous failure mode here, keeping a stray comment is not.
    """
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    return out


def load_keys(env_file: str | None) -> dict[str, str]:
    """Both keys, from --env-file if given else the process environment.

    Exits (no partial output) naming every missing key: escrowing one key and
    losing the other still loses patient data (§1).
    """
    if env_file is not None:
        p = Path(env_file)
        if not p.is_file():
            _die(f"--env-file not found: {p}")
        source: dict[str, str] = parse_env_file(p)
        origin = str(p)
    else:
        source = dict(os.environ)
        origin = "the environment"

    keys = {name: source.get(name, "").strip() for name in KEY_NAMES}
    missing = [name for name, value in keys.items() if not value]
    if missing:
        _die(
            "refusing to continue, missing key(s) in "
            f"{origin}: {', '.join(missing)}. Both keys must be escrowed "
            "together (KEY_CEREMONY.md §1); no sheet was written."
        )
    return keys


def _die(message: str, code: int = 2) -> "None":
    print(f"escrow_sheet: {message}", file=sys.stderr)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def render_sheet(keys: dict[str, str], date_str: str, hostname: str) -> str:
    """The print-ready HTML sheet. This string is the ONLY place key material
    is allowed to end up."""
    esc_host = html.escape(hostname)
    esc_date = html.escape(date_str)

    key_blocks = []
    for name in KEY_NAMES:
        key = keys[name]
        key_blocks.append(f"""
    <section class="key">
      <h2>{name}</h2>
      <p class="stakes">{html.escape(KEY_STAKES[name])}</p>
      <div class="value" aria-label="{name}">{html.escape(key)}</div>
      <div class="digit">check digit&nbsp;=&nbsp;<strong>{check_digit(key)}</strong></div>
    </section>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ORB ENCRYPTION KEYS: escrow sheet</title>
<style>
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
         color: #000; background: #fff; margin: 2rem auto; max-width: 46rem;
         line-height: 1.45; }}
  .banner {{ border: 4px solid #000; padding: 0.9rem 1.1rem; font-weight: 700;
             font-size: 1.05rem; margin-bottom: 1.6rem; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 0.2rem; }}
  h2 {{ font-size: 1rem; font-family: ui-monospace, Menlo, monospace;
        margin: 0 0 0.3rem; }}
  .meta {{ margin: 0.8rem 0 1.4rem; }}
  .meta td {{ padding: 0.1rem 1rem 0.1rem 0; vertical-align: top; }}
  .key {{ border: 1px solid #000; padding: 0.9rem 1.1rem; margin: 0 0 1.1rem; }}
  .stakes {{ margin: 0 0 0.6rem; font-size: 0.85rem; }}
  .value {{ font-family: ui-monospace, Menlo, monospace; font-size: 1.05rem;
            letter-spacing: 0.08em; word-break: break-all; padding: 0.5rem 0; }}
  .digit {{ font-family: ui-monospace, Menlo, monospace; margin-top: 0.3rem; }}
  ol li {{ margin-bottom: 0.35rem; }}
  code, pre {{ font-family: ui-monospace, Menlo, monospace; font-size: 0.82rem; }}
  pre {{ border: 1px solid #000; padding: 0.6rem 0.8rem; overflow-x: auto; }}
  .foot {{ font-size: 0.8rem; margin-top: 1.4rem; }}
</style>
</head>
<body>
  <div class="banner">
    PRINT THIS SHEET IMMEDIATELY: TWO COPIES, ON A DIRECTLY ATTACHED PRINTER,
    THEN DELETE THIS FILE. It contains the only keys to this hospital's
    clinical record. It must never be emailed, synced, screenshotted, or
    printed over the network. (KEY_CEREMONY.md §4, §5C)
  </div>

  <h1>ORB ENCRYPTION KEYS: escrow sheet</h1>
  <table class="meta">
    <tr><td>Appliance hostname</td><td><code>{esc_host}</code></td></tr>
    <tr><td>Generated on</td><td>{esc_date}</td></tr>
    <tr><td>Ceremony reference</td><td>__________________________ (from the §12 record sheet)</td></tr>
  </table>
{''.join(key_blocks)}
  <h2>Seal (KEY_CEREMONY.md §5E–§5F)</h2>
  <ol>
    <li>Print <strong>two identical copies</strong> of this sheet. Proof-read
        both against the screen, character by character, out loud, with a
        second person. Then clear the print queue and power-cycle the printer.</li>
    <li>Fold each copy so no key is readable through the envelope; check
        against the light.</li>
    <li><strong>Envelope A</strong> → tamper-evident envelope → the hospital
        safe, held by the hospital custodian (separate compartment from any
        backup media).</li>
    <li><strong>Envelope B</strong> → tamper-evident envelope → off site, held
        by the off-site custodian (Samuel, the Orb operator, for this pilot).</li>
    <li>Everyone present signs across both seals. Do not sign the §12 record
        sheet until the §5D drill below has PASSED.</li>
  </ol>

  <h2>Verify the escrow works (KEY_CEREMONY.md §5D)</h2>
  <p>On the standby machine, typing the key <strong>from this printed sheet</strong>
     (never from <code>.env</code>):</p>
  <pre>read -s -p "ORB_DB_ENCRYPTION_KEY from the sheet: " ORB_DB_ENCRYPTION_KEY; echo
export ORB_DB_ENCRYPTION_KEY
printf 'orb-escrow-check-v1:%s' "$ORB_DB_ENCRYPTION_KEY" | shasum -a 256 | cut -c1-8   # must equal the check digit above
./scripts/restore-drill.sh --backup /path/to/orb-backup-YYYYMMDD-HHMMSS.db            # must end RESULT: PASS</pre>
  <p>Or tool-assisted: <code>server/.venv/bin/python scripts/escrow_sheet.py verify</code>
     and retype the check digits from this sheet.</p>

  <p class="foot">
    Check digits are the first {CHECK_DIGIT_LEN} lowercase hex characters of
    SHA-256 over the UTF-8 bytes of <code>{CHECK_PREFIX}</code> + key, with no
    trailing newline (<code>printf</code>, not <code>echo</code>), the §5B
    construction. The prefix is what makes this digest safe to print: a plain
    SHA-256 of the key string <em>is</em> the derived encryption key (§1).
    Never write a plain SHA-256 of either key anywhere.<br><br>
    Recovery: KEY_CEREMONY.md §8. These keys are the ONLY way to read this
    hospital's clinical record. There is no vendor copy and no override.
  </p>
</body>
</html>
"""


def write_sheet(path: Path, content: str) -> None:
    """Create the sheet 0600, refusing to overwrite anything (O_EXCL also
    refuses to write through an existing symlink)."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _die(
            f"refusing to overwrite existing file: {path}. If it is a stale "
            "sheet, shred it first (or pick another --out path)."
        )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


def shred(path: Path) -> None:
    """Overwrite with random bytes, fsync, then unlink. Best effort on
    APFS/SSD (copy-on-write may keep old blocks) — FileVault plus printing
    promptly is the real control; this just avoids leaving plaintext lying
    around at the path."""
    size = path.stat().st_size
    with path.open("r+b") as fh:
        fh.write(os.urandom(size))
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()


def cmd_generate(args: argparse.Namespace) -> int:
    keys = load_keys(args.env_file)

    if args.date is not None:
        try:
            date = _dt.datetime.strptime(args.date, "%Y%m%d")
        except ValueError:
            _die(f"--date must be YYYYMMDD, got: {args.date!r}")
    else:
        date = _dt.datetime.now()
    date_compact = date.strftime("%Y%m%d")
    date_human = date.strftime("%Y-%m-%d")

    hostname = args.hostname or socket.gethostname()
    out = Path(args.out) if args.out else Path(f"./escrow-sheet-{date_compact}.html")

    write_sheet(out, render_sheet(keys, date_human, hostname))

    # Path and instructions only — never key material — on stdout.
    print(f"escrow sheet written: {out}  (mode 0600)")
    print("PRINT IT NOW — two copies, directly attached printer — then delete")
    print("this file. See docs/KEY_CEREMONY.md §5C–§5F.")

    if args.shred:
        answer = input(
            f"Type SHRED to overwrite and delete {out} "
            "(anything else leaves it in place): "
        )
        if answer.strip().upper() == "SHRED":
            shred(out)
            print(f"shredded and removed: {out} (best effort on SSD/APFS)")
        else:
            print(f"left in place: {out} — delete it yourself after printing.")
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    keys = load_keys(args.env_file)
    print("Retype the check digits from the printed sheet. (Digits only —")
    print("this tool never asks for, or prints, the keys themselves.)")
    ok = True
    for name in KEY_NAMES:
        typed = input(f"  {name} check digit ({CHECK_DIGIT_LEN} hex chars): ")
        typed = typed.strip().lower()
        expected = check_digit(keys[name])
        if typed == expected:
            print(f"  {name}: MATCH")
        else:
            ok = False
            if len(typed) != CHECK_DIGIT_LEN or any(
                c not in "0123456789abcdef" for c in typed
            ):
                print(
                    f"  {name}: MISMATCH — input was not "
                    f"{CHECK_DIGIT_LEN} hex characters"
                )
            else:
                print(f"  {name}: MISMATCH — sheet and live key disagree")
    if ok:
        print("RESULT: MATCH — the printed digits are consistent with the live keys.")
        return 0
    print("RESULT: MISMATCH — re-read the sheet character by character with a")
    print("second person (KEY_CEREMONY.md §8). Zero vs letter O, one vs letter l.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="escrow_sheet.py",
        description="Key-escrow sheet tool for docs/KEY_CEREMONY.md.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    gen = sub.add_parser("generate", help="write the print-ready escrow sheet")
    gen.add_argument("--out", help="output path (default ./escrow-sheet-YYYYMMDD.html)")
    gen.add_argument("--date", help="sheet date as YYYYMMDD (default: today)")
    gen.add_argument("--hostname", help="appliance hostname (default: this machine's)")
    gen.add_argument(
        "--env-file",
        help="read the keys from this dotenv file instead of the environment",
    )
    gen.add_argument(
        "--shred",
        action="store_true",
        help="after a confirmation prompt, overwrite and delete the sheet file",
    )
    gen.set_defaults(func=cmd_generate)

    ver = sub.add_parser(
        "verify", help="retype printed check digits; compare against live keys"
    )
    ver.add_argument(
        "--env-file",
        help="read the keys from this dotenv file instead of the environment",
    )
    ver.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
