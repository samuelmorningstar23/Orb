"""Per-site configuration file — loading, validation and refusal.

This file is the one place a hospital changes ward names, escalation timings and
the small set of safely-variable thresholds. Its whole reason to exist is that
the alternative — a dozen ORB_* environment variables spread across a .env
nobody reviews — already cost this project a live safety switch: a key with a
BLANK value read as "off" and nothing said a word.

So the rules under test here are deliberately unforgiving:

  * a value that is present but empty is an ERROR, never a silent default;
  * a key nobody recognises is an ERROR, because a typo that is ignored is a
    setting the operator believes is applied and is not;
  * every error names the FILE and the LINE.

And one rule in the other direction: NO file at all is fine. An appliance with
no site.toml runs on the shipped defaults. Refusing to boot without a config
file would make the file mandatory ceremony rather than a lever.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.site_config_service import (  # noqa: E402
    SiteConfigError,
    load_site_config,
    site_config_snapshot,
)


# Loading applies ORB_* variables to the real process environment and parks the
# result in a module global. Both outlive the test that made them, and the next
# module would measure this one's leftovers — the same isolation rule the DB
# fixtures follow. Snapshot and restore both, around every test.
_TOUCHED = (
    "ORB_ESCALATION_TIERS",
    "ORB_ESCALATION_NEWS2_THRESHOLD",
    "ORB_ESCALATION_ON_SINGLE_PARAM_3",
    "ORB_ESCALATION_SCAN_LOOKBACK_MINUTES",
    "ORB_ESCALATION_RELAY_NOTIFY",
    "ORB_HEARTBEAT_BACKUP_MAX_AGE_H",
    "ORB_HEARTBEAT_DISK_MIN_PCT",
    "ORB_HL7_DEFAULT_TZ",
)


@pytest.fixture(autouse=True)
def _restore_environment(tmp_path):
    before = {k: os.environ.get(k) for k in _TOUCHED}
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    # Reset the module global too, so `site_config_snapshot()` elsewhere in the
    # suite does not describe a hospital invented by this file.
    load_site_config(tmp_path / "no-such-site.toml", apply_env=False)


def _write(tmp_path, body: str):
    path = tmp_path / "site.toml"
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
[site]
name = "Test Hospital"
timezone = "Asia/Kolkata"

[[departments]]
name = "General Medicine"
wards = ["GM-1"]
"""


# ── The happy path ───────────────────────────────────────────────────────────

def test_absent_file_is_not_an_error(tmp_path):
    """No site.toml = shipped defaults. It must not stop the server."""
    cfg = load_site_config(tmp_path / "does-not-exist.toml", apply_env=False)
    assert cfg.present is False
    assert cfg.departments == []
    assert cfg.errors == []


def test_minimal_file_loads(tmp_path):
    cfg = load_site_config(_write(tmp_path, MINIMAL), apply_env=False)
    assert cfg.present is True
    assert cfg.site_name == "Test Hospital"
    assert cfg.timezone == "Asia/Kolkata"
    assert [d["name"] for d in cfg.departments] == ["General Medicine"]
    assert cfg.wards == ["GM-1"]


def test_escalation_tiers_are_exported_in_the_shape_the_service_reads(tmp_path):
    """The ladder in the file must arrive as the JSON escalation_service parses.

    If these two ever disagree the file becomes decorative: the operator edits
    timings, the ward escalates on the old ones, and nothing reports a problem.
    """
    cfg = load_site_config(_write(tmp_path, MINIMAL + """
[escalation]
news2_threshold = 6
escalate_on_single_parameter_3 = false
scan_lookback_minutes = 90

[[escalation.tiers]]
role = "nurse"
label = "Ward nurse"
timeout_minutes = 8
entry_min_news2 = 5

[[escalation.tiers]]
role = "consultant"
label = "Consultant"
"""), apply_env=False)

    env = cfg.env_overrides
    assert env["ORB_ESCALATION_NEWS2_THRESHOLD"] == "6"
    assert env["ORB_ESCALATION_ON_SINGLE_PARAM_3"] == "false"
    assert env["ORB_ESCALATION_SCAN_LOOKBACK_MINUTES"] == "90"

    tiers = json.loads(env["ORB_ESCALATION_TIERS"])
    assert [t["role"] for t in tiers] == ["nurse", "consultant"]
    assert tiers[0]["timeout_minutes"] == 8
    # An omitted timeout on the LAST tier means "waits, never auto-advances" —
    # the shape escalation_service expects is an explicit null, not a missing key.
    assert tiers[1]["timeout_minutes"] is None
    assert tiers[1]["entry_min_news2"] is None

    # And the real parser must accept what we produced.
    from services import escalation_service
    os.environ["ORB_ESCALATION_TIERS"] = env["ORB_ESCALATION_TIERS"]
    try:
        parsed = escalation_service.tier_policy()
    finally:
        os.environ.pop("ORB_ESCALATION_TIERS", None)
    assert [t["role"] for t in parsed] == ["nurse", "consultant"]
    assert parsed[0]["timeout_minutes"] == 8
    assert parsed[-1]["timeout_minutes"] is None


def test_apply_env_sets_process_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("ORB_ESCALATION_NEWS2_THRESHOLD", raising=False)
    load_site_config(_write(tmp_path, MINIMAL + """
[escalation]
news2_threshold = 7
"""), apply_env=True)
    assert os.environ["ORB_ESCALATION_NEWS2_THRESHOLD"] == "7"


def test_file_wins_over_env_and_says_so(tmp_path, monkeypatch):
    """The documented file is authoritative — but the override is REPORTED.

    Silently winning is how an operator ends up debugging the wrong layer.
    """
    monkeypatch.setenv("ORB_ESCALATION_NEWS2_THRESHOLD", "4")
    cfg = load_site_config(_write(tmp_path, MINIMAL + """
[escalation]
news2_threshold = 7
"""), apply_env=True)
    assert os.environ["ORB_ESCALATION_NEWS2_THRESHOLD"] == "7"
    assert "ORB_ESCALATION_NEWS2_THRESHOLD" in cfg.env_conflicts


# ── Refusals. Each must name the file and the line. ──────────────────────────

def _refuses(tmp_path, body: str) -> SiteConfigError:
    path = _write(tmp_path, body)
    with pytest.raises(SiteConfigError) as excinfo:
        load_site_config(path, apply_env=False)
    err = excinfo.value
    assert str(path) in str(err), f"error does not name the file: {err}"
    return err


def test_blank_value_is_refused_not_defaulted(tmp_path):
    """THE regression this file exists for: a present-but-empty value."""
    err = _refuses(tmp_path, """
[site]
name = ""
timezone = "Asia/Kolkata"

[[departments]]
name = "General Medicine"
wards = ["GM-1"]
""")
    assert err.line == 3, f"expected line 3, got {err.line}: {err}"
    assert "empty" in str(err).lower()


def test_unknown_key_is_refused_with_its_line(tmp_path):
    err = _refuses(tmp_path, MINIMAL + """
[escalation]
news2_treshold = 6
""")
    assert "news2_treshold" in str(err)
    assert err.line == 11, f"expected line 11, got {err.line}: {err}"
    # A typo must be met with the list of things it could have meant.
    assert "news2_threshold" in str(err)


def test_unknown_table_is_refused(tmp_path):
    err = _refuses(tmp_path, MINIMAL + """
[esclation]
news2_threshold = 6
""")
    assert "esclation" in str(err)
    assert err.line == 10, f"expected line 10, got {err.line}: {err}"


def test_malformed_toml_reports_the_line(tmp_path):
    err = _refuses(tmp_path, """
[site
name = "Test Hospital"
""")
    assert err.line is not None


def test_wrong_type_is_refused(tmp_path):
    err = _refuses(tmp_path, MINIMAL + """
[escalation]
news2_threshold = "six"
""")
    assert err.line == 11, f"expected line 11, got {err.line}: {err}"
    assert "whole number" in str(err).lower() or "integer" in str(err).lower()


def test_out_of_range_threshold_is_refused(tmp_path):
    """NEWS2 tops out at 20. A threshold of 50 never fires — that is a ward
    where nothing escalates, configured by accident."""
    err = _refuses(tmp_path, MINIMAL + """
[escalation]
news2_threshold = 50
""")
    assert "50" in str(err)


def test_non_final_tier_without_timeout_is_refused(tmp_path):
    """A middle tier with no timeout is a chain that stops climbing there."""
    err = _refuses(tmp_path, MINIMAL + """
[[escalation.tiers]]
role = "nurse"

[[escalation.tiers]]
role = "consultant"
""")
    assert "timeout_minutes" in str(err)


def test_duplicate_department_is_refused(tmp_path):
    err = _refuses(tmp_path, """
[site]
name = "Test Hospital"

[[departments]]
name = "General Medicine"
wards = ["GM-1"]

[[departments]]
name = "General Medicine"
wards = ["GM-2"]
""")
    assert "General Medicine" in str(err)


def test_ward_shared_between_departments_is_refused(tmp_path):
    """A bed prefix in two departments makes 'which ward is this patient on'
    ambiguous — and department is what every PHI scope keys on."""
    err = _refuses(tmp_path, """
[site]
name = "Test Hospital"

[[departments]]
name = "General Medicine"
wards = ["GM-1"]

[[departments]]
name = "Cardiology"
wards = ["GM-1"]
""")
    assert "GM-1" in str(err)


def test_department_named_like_a_bed_prefix_is_refused(tmp_path):
    """SCOPING LAW: department is 'General Medicine', ward is 'GM-1'. A site
    that puts the bed prefix in the department field silently un-scopes PHI."""
    err = _refuses(tmp_path, """
[site]
name = "Test Hospital"

[[departments]]
name = "GM-1"
wards = ["GM-1"]
""")
    assert "GM-1" in str(err)


def test_a_bad_file_never_half_applies(tmp_path, monkeypatch):
    """A refusal must leave the environment untouched — a partially applied
    config is worse than none, because it looks applied."""
    monkeypatch.delenv("ORB_ESCALATION_NEWS2_THRESHOLD", raising=False)
    path = _write(tmp_path, MINIMAL + """
[escalation]
news2_threshold = 6
nonsense = 1
""")
    with pytest.raises(SiteConfigError):
        load_site_config(path, apply_env=True)
    assert "ORB_ESCALATION_NEWS2_THRESHOLD" not in os.environ


# ── The snapshot the ops surface reads ───────────────────────────────────────

def test_snapshot_has_no_secrets(tmp_path):
    load_site_config(_write(tmp_path, MINIMAL), apply_env=False)
    snap = site_config_snapshot()
    assert snap["present"] is True
    assert snap["site_name"] == "Test Hospital"
    assert snap["department_count"] == 1
    # The file PATH is excluded from the sweep: it is chosen by the test harness
    # (pytest tmp dirs are named after the test, so "no_secrets" would trip it),
    # and it is a path, not content.
    body = json.dumps({k: v for k, v in snap.items() if k != "file"}).lower()
    for forbidden in ("key", "secret", "password", "token"):
        assert forbidden not in body, f"snapshot leaked a {forbidden}: {snap}"
