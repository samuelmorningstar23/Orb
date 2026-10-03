"""Tests for scripts/escrow_sheet.py — the tool-assisted key-escrow sheet.

The check-digit expectations below are pinned to literals produced by the
ceremony doc's OWN shell pipeline (KEY_CEREMONY.md §5B), run by hand:

    printf 'orb-escrow-check-v1:%s' "$KEY" | shasum -a 256 | cut -c1-8

They are deliberately NOT computed with hashlib inside the test — the point is
that the script matches the doc, not that the script matches itself.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SERVER_DIR / "scripts" / "escrow_sheet.py"

# Clearly-fake 64-hex keys used ONLY in this test.
DB_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
UP_KEY = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

# Hand-computed with the §5B pipeline above (shasum on macOS, 2026-08-10):
DB_DIGIT = "165a71a4"
UP_DIGIT = "2e06e181"
# Same keys through `echo` instead of `printf` (the trailing-newline trap §5B
# warns about). The script must NOT produce this:
DB_DIGIT_ECHO_TRAP = "92a9135f"

KEY_ENV = {"ORB_DB_ENCRYPTION_KEY": DB_KEY, "ORB_UPLOAD_ENCRYPTION_KEY": UP_KEY}


def _load_module():
    spec = importlib.util.spec_from_file_location("escrow_sheet", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


escrow = _load_module()


def run_tool(args, env_keys=None, stdin="", cwd=None):
    """Run the script as an operator would, with ONLY the given keys set."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ORB_DB_ENCRYPTION_KEY", "ORB_UPLOAD_ENCRYPTION_KEY")
    }
    if env_keys:
        env.update(env_keys)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
    )


def assert_no_key_material(text: str) -> None:
    """Neither key string, nor either DERIVED key (plain SHA-256 of the key
    string — which §1 says IS the encryption key), may appear anywhere."""
    assert DB_KEY not in text
    assert UP_KEY not in text
    assert hashlib.sha256(DB_KEY.encode()).hexdigest() not in text
    assert hashlib.sha256(UP_KEY.encode()).hexdigest() not in text


# ---------------------------------------------------------------------------
# Check digit — the doc is the spec
# ---------------------------------------------------------------------------

def test_check_digit_matches_doc_pipeline():
    assert escrow.check_digit(DB_KEY) == DB_DIGIT
    assert escrow.check_digit(UP_KEY) == UP_DIGIT


def test_check_digit_is_not_the_echo_trap():
    # printf, not echo: no trailing newline in the hashed string.
    assert escrow.check_digit(DB_KEY) != DB_DIGIT_ECHO_TRAP


def test_check_digit_never_prefixes_the_derived_key():
    # The 'orb-escrow-check-v1:' prefix is what makes the digest printable:
    # without it, the digit would be the first 8 chars of the DERIVED KEY.
    for key in (DB_KEY, UP_KEY):
        derived = hashlib.sha256(key.encode()).hexdigest()
        assert escrow.check_digit(key) != derived[: len(escrow.check_digit(key))]


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def test_generate_writes_0600_sheet_with_keys_and_digits(tmp_path):
    out = tmp_path / "sheet.html"
    res = run_tool(
        ["generate", "--out", str(out), "--date", "20260810",
         "--hostname", "orb-appliance-test"],
        env_keys=KEY_ENV,
    )
    assert res.returncode == 0, res.stderr

    assert out.exists()
    assert (out.stat().st_mode & 0o777) == 0o600

    content = out.read_text()
    # Both keys, correct §5B digits, and self-describing context:
    assert DB_KEY in content
    assert UP_KEY in content
    assert DB_DIGIT in content
    assert UP_DIGIT in content
    assert "orb-appliance-test" in content
    assert "2026-08-10" in content
    assert "PRINT" in content and "DELETE THIS FILE" in content
    assert "Envelope A" in content and "Envelope B" in content
    assert "orb-escrow-check-v1" in content          # §5D one-liner on the sheet
    assert "restore-drill" in content                # §5D drill on the sheet
    # The derived keys (plain SHA-256) must not be on the sheet either.
    assert hashlib.sha256(DB_KEY.encode()).hexdigest() not in content
    assert hashlib.sha256(UP_KEY.encode()).hexdigest() not in content

    # Key material only ever goes to the file, never the terminal.
    assert_no_key_material(res.stdout)
    assert_no_key_material(res.stderr)


def test_generate_default_filename_uses_date(tmp_path):
    res = run_tool(["generate", "--date", "20260810"], env_keys=KEY_ENV,
                   cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "escrow-sheet-20260810.html").exists()
    assert_no_key_material(res.stdout + res.stderr)


def test_generate_refuses_missing_key_and_writes_nothing(tmp_path):
    # Upload key absent: refuse, name it, write no partial sheet.
    res = run_tool(
        ["generate", "--out", str(tmp_path / "s.html")],
        env_keys={"ORB_DB_ENCRYPTION_KEY": DB_KEY},
    )
    assert res.returncode != 0
    assert "ORB_UPLOAD_ENCRYPTION_KEY" in res.stderr
    assert list(tmp_path.iterdir()) == []
    assert_no_key_material(res.stdout + res.stderr)

    # Both absent: name both.
    res = run_tool(["generate", "--out", str(tmp_path / "s.html")], env_keys={})
    assert res.returncode != 0
    assert "ORB_DB_ENCRYPTION_KEY" in res.stderr
    assert "ORB_UPLOAD_ENCRYPTION_KEY" in res.stderr
    assert list(tmp_path.iterdir()) == []


def test_generate_refuses_to_overwrite(tmp_path):
    out = tmp_path / "sheet.html"
    out.write_text("existing")
    res = run_tool(["generate", "--out", str(out)], env_keys=KEY_ENV)
    assert res.returncode != 0
    assert "refusing to overwrite" in res.stderr
    assert out.read_text() == "existing"


def test_generate_env_file_mode(tmp_path):
    # config.py does not read .env — the tool parses it directly, including
    # `export` prefixes, quotes, comments and blanks.
    env_file = tmp_path / "dotenv"
    env_file.write_text(
        "# Orb appliance keys\n"
        "\n"
        f"export ORB_DB_ENCRYPTION_KEY={DB_KEY}\n"
        f'ORB_UPLOAD_ENCRYPTION_KEY="{UP_KEY}"\n'
    )
    out = tmp_path / "sheet.html"
    res = run_tool(
        ["generate", "--out", str(out), "--env-file", str(env_file)],
        env_keys={},  # nothing in the environment — the file is the source
    )
    assert res.returncode == 0, res.stderr
    content = out.read_text()
    assert DB_KEY in content and UP_KEY in content
    assert DB_DIGIT in content and UP_DIGIT in content
    assert_no_key_material(res.stdout + res.stderr)


def test_generate_shred_flow(tmp_path):
    out = tmp_path / "sheet.html"
    # Confirmed: file is overwritten and removed.
    res = run_tool(["generate", "--out", str(out), "--shred"],
                   env_keys=KEY_ENV, stdin="SHRED\n")
    assert res.returncode == 0, res.stderr
    assert not out.exists()
    assert_no_key_material(res.stdout + res.stderr)

    # Declined: file stays for the operator to print first.
    res = run_tool(["generate", "--out", str(out), "--shred"],
                   env_keys=KEY_ENV, stdin="no\n")
    assert res.returncode == 0, res.stderr
    assert out.exists()
    assert_no_key_material(res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def test_verify_match(tmp_path):
    res = run_tool(["verify"], env_keys=KEY_ENV,
                   stdin=f"{DB_DIGIT}\n{UP_DIGIT}\n")
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("MATCH") >= 2
    assert "MISMATCH" not in res.stdout
    assert_no_key_material(res.stdout + res.stderr)


def test_verify_accepts_case_and_whitespace(tmp_path):
    res = run_tool(["verify"], env_keys=KEY_ENV,
                   stdin=f"  {DB_DIGIT.upper()}  \n{UP_DIGIT}\n")
    assert res.returncode == 0, res.stderr
    assert "MISMATCH" not in res.stdout


def test_verify_mismatch_per_key(tmp_path):
    wrong = "00000000"
    assert wrong != UP_DIGIT
    res = run_tool(["verify"], env_keys=KEY_ENV,
                   stdin=f"{DB_DIGIT}\n{wrong}\n")
    assert res.returncode == 1
    assert "ORB_DB_ENCRYPTION_KEY: MATCH" in res.stdout
    assert "ORB_UPLOAD_ENCRYPTION_KEY: MISMATCH" in res.stdout
    assert_no_key_material(res.stdout + res.stderr)


def test_verify_flags_malformed_input(tmp_path):
    res = run_tool(["verify"], env_keys=KEY_ENV,
                   stdin=f"{DB_DIGIT}\nzzz\n")
    assert res.returncode == 1
    assert "MISMATCH" in res.stdout
    assert "hex" in res.stdout  # tells the operator the input shape was wrong


def test_verify_refuses_missing_key(tmp_path):
    res = run_tool(["verify"], env_keys={"ORB_UPLOAD_ENCRYPTION_KEY": UP_KEY},
                   stdin="\n\n")
    assert res.returncode != 0
    assert "ORB_DB_ENCRYPTION_KEY" in res.stderr
    assert_no_key_material(res.stdout + res.stderr)
