"""Orb Hospital OS — the per-site configuration file.

ONE documented file holds everything a hospital legitimately varies: what the
site is called, which departments and bed prefixes exist, how the escalation
ladder is staffed and timed, and the handful of operational thresholds that are
safe to move. It is TOML, it is read once at startup, and it is validated hard.

WHY THIS EXISTS AT ALL
──────────────────────
Before it, per-site settings lived as ORB_* variables scattered through a .env
that nobody reviews as a unit. That cost a live safety switch: a key was written
with a BLANK value, every reader treated blank as falsy, the switch was off, and
no log line, no health check and no startup message said so. The setting looked
present because the key was there.

So this loader is built on three rules, and the tests hold them:

  1. PRESENT-BUT-EMPTY IS AN ERROR. Never a default, never a falsy, never a
     shrug. If a value is there it must mean something.
  2. AN UNRECOGNISED KEY IS AN ERROR. A typo that is ignored is a setting the
     operator believes is applied and is not — the same failure as (1) wearing
     a different hat. Errors name the near-miss ("did you mean …") because the
     realistic cause is a fat finger at 2 a.m., not sabotage.
  3. EVERY ERROR NAMES THE FILE AND THE LINE. An operator on a hospital floor
     with no vendor on site can only fix what the message points at.

And one rule pointing the other way: NO FILE IS FINE. An appliance with no
site.toml boots on the shipped defaults. Making the file mandatory would turn a
lever into ceremony, and ceremony gets copy-pasted wrong.

HOW IT REACHES THE REST OF THE SERVER
─────────────────────────────────────
The settings this file owns already have readers — escalation_service reads
ORB_ESCALATION_TIERS, ops_health_service reads ORB_HEARTBEAT_BACKUP_MAX_AGE_H,
and so on. Rather than fork a second source of truth (the exact mistake that
produced the nurse-board scope hole: a surface growing its OWN idea of a value
beside the existing one), this loader TRANSLATES the file into those same
environment variables before anything reads them. One reader per setting, still.

Precedence: THE FILE WINS over an environment variable that sets the same thing,
and the override is reported — logged at WARNING and surfaced on the ops status
page as `env_conflicts`. Silently winning either direction leaves an operator
debugging the layer that is not in charge.

TOML, not YAML or JSON, for three boring reasons: `tomllib` is in the standard
library (no new dependency on an appliance that must install offline), its
parser reports the line number of a syntax error, and comments survive — the
file is meant to be read by the next person, not just parsed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("orb.site_config")

# Default location. `ORB_SITE_CONFIG_FILE` points elsewhere — used by tests, and
# by sites that keep configuration in their own managed repository.
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "site_config" / "site.toml"

# NEWS2 is a 0-20 aggregate. A threshold above it can never fire, which is a
# ward where nothing escalates — configured by accident, discovered by outcome.
_NEWS2_MAX = 20

# A department is a clinical service ("General Medicine"). A ward is a bed
# prefix ("GM-1"). Every PHI scope in this codebase keys on the FORMER, and
# putting a bed prefix in the department field is how a scope stops scoping —
# three leaks this project has already paid for. Refuse the shape outright.
_BED_PREFIX_RE = re.compile(r"^[A-Za-z]{1,4}[-_ ]?\d+[A-Za-z]?$")


class SiteConfigError(Exception):
    """A site configuration Orb will not start on.

    Carries the file and (where it can be found) the line, and renders as the
    multi-line block an operator sees on the console. `str()` is the whole
    message on purpose: the caller prints it and exits, no formatting logic at
    the call site.
    """

    def __init__(
        self,
        path: Path | str,
        problem: str,
        *,
        line: int | None = None,
        source_line: str | None = None,
        hint: str | None = None,
    ):
        self.path = str(path)
        self.problem = problem
        self.line = line
        self.source_line = source_line
        self.hint = hint
        super().__init__(self._render())

    def _render(self) -> str:
        out = [
            "",
            "Orb will not start: the site configuration file is not valid.",
            "",
            f"  File:    {self.path}",
        ]
        if self.line is not None:
            out.append(f"  Line {self.line}:  {(self.source_line or '').strip()}")
        out.append(f"  Problem: {self.problem}")
        if self.hint:
            for i, chunk in enumerate(self.hint.split("\n")):
                out.append(f"  {'Hint:    ' if i == 0 else '           '}{chunk}")
        out += [
            "",
            "  Nothing from this file was applied. Fix the line above and start Orb again.",
            "  Orb also runs with NO site configuration file at all, on shipped defaults.",
            "",
        ]
        return "\n".join(out)


@dataclass
class SiteConfig:
    """The validated file, plus everything the ops surface needs to describe it."""

    path: str = ""
    present: bool = False
    site_name: str | None = None
    timezone: str | None = None
    departments: list[dict] = field(default_factory=list)
    escalation: dict = field(default_factory=dict)
    operations: dict = field(default_factory=dict)
    env_overrides: dict[str, str] = field(default_factory=dict)
    env_conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def wards(self) -> list[str]:
        """Every bed prefix declared by every department, in file order."""
        return [w for d in self.departments for w in d.get("wards", [])]

    def department_for_ward(self, ward: str) -> str | None:
        """Which clinical service owns this bed prefix, per the file.

        Used by the ops status page to report admitted patients sitting in a
        department the file does not declare — a typo there makes a patient
        invisible to their own ward's staff, because scoping keys on the string.
        """
        needle = (ward or "").strip().lower()
        for d in self.departments:
            if any(w.strip().lower() == needle for w in d.get("wards", [])):
                return d["name"]
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Schema
# ═════════════════════════════════════════════════════════════════════════════
# Declared as data so the "unknown key" error can list what WAS allowed, and so
# adding a setting is one line rather than a new branch in a validator.
#
#   type      — "str" | "int" | "number" | "bool" | "str_list"
#   min/max   — inclusive bounds, checked for int/number
#   required  — must be present when the containing table is
_SCHEMA: dict[str, dict[str, dict]] = {
    "site": {
        "name": {"type": "str", "required": True},
        "timezone": {"type": "str"},
    },
    "departments": {
        "name": {"type": "str", "required": True},
        "wards": {"type": "str_list"},
    },
    "escalation": {
        "news2_threshold": {"type": "int", "min": 1, "max": _NEWS2_MAX},
        "escalate_on_single_parameter_3": {"type": "bool"},
        "scan_lookback_minutes": {"type": "int", "min": 5, "max": 1440},
        "relay_notify": {"type": "bool"},
    },
    "escalation.tiers": {
        "role": {"type": "str", "required": True},
        "label": {"type": "str"},
        "timeout_minutes": {"type": "number", "min": 1, "max": 720},
        "entry_min_news2": {"type": "int", "min": 1, "max": _NEWS2_MAX},
    },
    "operations": {
        "backup_max_age_hours": {"type": "number", "min": 1, "max": 720},
        "disk_min_free_percent": {"type": "number", "min": 1, "max": 90},
    },
}

# Tables that are arrays of tables ([[x]]) rather than single tables ([x]).
_ARRAY_TABLES = {"departments", "escalation.tiers"}

_TYPE_NAMES = {
    "str": "some text in quotes",
    "int": "a whole number",
    "number": "a number",
    "bool": "true or false",
    "str_list": "a list of quoted names, e.g. [\"GM-1\", \"GM-2\"]",
}


# ═════════════════════════════════════════════════════════════════════════════
#  Line index — how an error finds its line
# ═════════════════════════════════════════════════════════════════════════════
_TABLE_RE = re.compile(r"^\s*(\[\[?)\s*([A-Za-z0-9_.\-]+)\s*\]\]?")
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=")


class _LineIndex:
    """Where each table header and key sits in the raw text.

    tomllib hands back a plain dict with no positions, so a SEMANTIC complaint
    ("50 is out of range") has no line to point at unless we find it ourselves.
    This is a deliberately simple scanner: it reads table headers and top-level
    `key =` lines. It never has to be perfect — a miss degrades to an error with
    no line number, which is still a correct error. It must never raise.
    """

    def __init__(self, text: str):
        self.lines = text.splitlines()
        self._tables: dict[tuple[str, int], int] = {}
        self._keys: dict[tuple[str, int, str], int] = {}
        current = ""
        occurrence: dict[str, int] = {}
        index = 0
        for n, raw in enumerate(self.lines, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _TABLE_RE.match(raw)
            if m:
                current = m.group(2)
                index = occurrence.get(current, 0)
                occurrence[current] = index + 1
                self._tables.setdefault((current, index), n)
                continue
            k = _KEY_RE.match(raw)
            if k:
                self._keys.setdefault((current, index, k.group(1)), n)

    def table(self, table: str, index: int = 0) -> int | None:
        return self._tables.get((table, index))

    def key(self, table: str, key: str, index: int = 0) -> int | None:
        return self._keys.get((table, index, key)) or self.table(table, index)

    def text(self, line: int | None) -> str | None:
        if line is None or line < 1 or line > len(self.lines):
            return None
        return self.lines[line - 1]


# ═════════════════════════════════════════════════════════════════════════════
#  Validation
# ═════════════════════════════════════════════════════════════════════════════
def _fail(
    path: Path, idx: _LineIndex, table: str, key: str | None, problem: str,
    *, occurrence: int = 0, hint: str | None = None,
) -> "SiteConfigError":
    line = idx.key(table, key, occurrence) if key else idx.table(table, occurrence)
    return SiteConfigError(
        path, problem, line=line, source_line=idx.text(line), hint=hint,
    )


def _suggest(name: str, options: Iterable[str]) -> str:
    """"did you mean" + the full allowed list. Both matter: the near-miss fixes
    the common case in one read, the list fixes the case where the operator was
    guessing at a name that never existed."""
    options = sorted(options)
    close = get_close_matches(name, options, n=1, cutoff=0.6)
    parts = []
    if close:
        parts.append(f"Did you mean '{close[0]}'?")
    parts.append("Settings allowed here: " + (", ".join(options) or "(none)"))
    return "\n".join(parts)


def _check_value(
    path: Path, idx: _LineIndex, table: str, key: str, value: Any,
    spec: dict, occurrence: int = 0,
) -> Any:
    """One value against its spec. Raises SiteConfigError; returns the value."""
    kind = spec["type"]
    want = _TYPE_NAMES[kind]

    # Rule 1. A value that is present but empty is never a default.
    if isinstance(value, str) and not value.strip():
        raise _fail(
            path, idx, table, key,
            f"'{key}' is empty. A setting that is written down but left blank is "
            f"the failure this file exists to prevent. Orb will not guess what "
            f"you meant.",
            occurrence=occurrence,
            hint=f"Give it a value ({want}), or delete the line entirely to use "
                 f"Orb's default.",
        )
    if kind == "str_list" and isinstance(value, list) and not value:
        raise _fail(
            path, idx, table, key,
            f"'{key}' is an empty list. Delete the line to use Orb's default, or "
            f"list at least one entry.",
            occurrence=occurrence,
        )

    if kind == "str":
        if not isinstance(value, str):
            raise _fail(path, idx, table, key,
                        f"'{key}' must be {want}.", occurrence=occurrence)
        return value.strip()

    if kind == "bool":
        if not isinstance(value, bool):
            raise _fail(path, idx, table, key,
                        f"'{key}' must be {want} (no quotes).", occurrence=occurrence)
        return value

    if kind in ("int", "number"):
        # bool is an int subclass in Python — `news2_threshold = true` must not
        # silently become 1.
        ok = isinstance(value, int) and not isinstance(value, bool) if kind == "int" \
            else isinstance(value, (int, float)) and not isinstance(value, bool)
        if not ok:
            raise _fail(path, idx, table, key,
                        f"'{key}' must be {want}, not {value!r}.", occurrence=occurrence)
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and value < lo or hi is not None and value > hi:
            raise _fail(
                path, idx, table, key,
                f"'{key}' is {value}, which is outside the range Orb accepts "
                f"({lo} to {hi}).",
                occurrence=occurrence,
                hint=_range_hint(table, key),
            )
        return value

    if kind == "str_list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _fail(path, idx, table, key,
                        f"'{key}' must be {want}.", occurrence=occurrence)
        cleaned = []
        for v in value:
            if not v.strip():
                raise _fail(path, idx, table, key,
                            f"'{key}' contains an empty entry. Remove it.",
                            occurrence=occurrence)
            cleaned.append(v.strip())
        return cleaned

    raise AssertionError(f"unhandled schema type {kind}")  # pragma: no cover


def _range_hint(table: str, key: str) -> str | None:
    """Why the bound is where it is. An operator who is told 'out of range' and
    not told why will pick the nearest legal number and move on."""
    if key == "news2_threshold" or key == "entry_min_news2":
        return ("NEWS2 is a 0-20 aggregate score. A threshold above 20 can never be "
                "reached, so nothing would ever escalate.")
    if key == "timeout_minutes":
        return "A tier that waits longer than 12 hours is not an escalation."
    if key == "disk_min_free_percent":
        return "This is the free-space level at which Orb starts warning, in percent."
    if key == "backup_max_age_hours":
        return "How old the newest backup may get before Orb calls it stale."
    return None


def _check_table(
    path: Path, idx: _LineIndex, table: str, data: dict, occurrence: int = 0,
) -> dict:
    """Every key in one table, against the schema for that table."""
    spec_table = _SCHEMA[table]
    # A nested array-of-tables shows up as a plain key on its parent; it is
    # validated separately, so allow the name through here.
    nested = {t.split(".", 1)[1] for t in _ARRAY_TABLES
              if t.startswith(table + ".") and t.count(".") == table.count(".") + 1}
    out: dict = {}
    for key, value in data.items():
        if key in nested:
            continue
        spec = spec_table.get(key)
        if spec is None:
            raise _fail(
                path, idx, table, key,
                f"'{key}' is not a setting Orb knows about in [{table}].",
                occurrence=occurrence,
                hint=_suggest(key, set(spec_table) | nested),
            )
        out[key] = _check_value(path, idx, table, key, value, spec, occurrence)
    for key, spec in spec_table.items():
        if spec.get("required") and key not in out:
            raise _fail(
                path, idx, table, None,
                f"[{table}] is missing the required setting '{key}'.",
                occurrence=occurrence,
                hint=f"Add a line: {key} = ...",
            )
    return out


def _validate(path: Path, raw_text: str, doc: dict) -> SiteConfig:
    idx = _LineIndex(raw_text)
    cfg = SiteConfig(path=str(path), present=True)

    # Unknown TOP-LEVEL table. Same reasoning as an unknown key: `[esclation]`
    # parses fine and applies nothing.
    for name in doc:
        if name not in _SCHEMA:
            raise SiteConfigError(
                path,
                f"'[{name}]' is not a section Orb knows about.",
                line=idx.table(name),
                source_line=idx.text(idx.table(name)),
                hint=_suggest(name, [t for t in _SCHEMA if "." not in t]),
            )

    # ── [site] ──
    site = _check_table(path, idx, "site", doc.get("site", {})) if "site" in doc else {}
    if not site:
        raise SiteConfigError(
            path,
            "the file has no [site] section, so Orb cannot tell which hospital "
            "this appliance belongs to.",
            hint='Add:\n  [site]\n  name = "Your Hospital Name"',
        )
    cfg.site_name = site.get("name")
    cfg.timezone = site.get("timezone")

    # ── [[departments]] ──
    seen_departments: dict[str, int] = {}
    seen_wards: dict[str, str] = {}
    for i, entry in enumerate(doc.get("departments", []) or []):
        dept = _check_table(path, idx, "departments", entry, occurrence=i)
        name = dept["name"]
        if _BED_PREFIX_RE.match(name):
            raise _fail(
                path, idx, "departments", "name", occurrence=i,
                problem=f"'{name}' looks like a BED PREFIX, not a department.",
                hint=("In Orb a department is the clinical service, "
                      "\"General Medicine\", \"Cardiology\". A ward is the bed prefix, "
                      "\"GM-1\". Every permission check keys on the department, so "
                      "putting a bed prefix here quietly stops staff seeing their "
                      "own patients.\n"
                      f'Try:\n  name = "General Medicine"\n  wards = ["{name}"]'),
            )
        if name.lower() in seen_departments:
            raise _fail(
                path, idx, "departments", "name", occurrence=i,
                problem=f"department '{name}' is listed twice "
                        f"(first on line {seen_departments[name.lower()]}).",
                hint="Merge the two entries: put all of its wards in one list.",
            )
        seen_departments[name.lower()] = idx.key("departments", "name", i) or 0
        for ward in dept.get("wards", []):
            owner = seen_wards.get(ward.lower())
            if owner:
                raise _fail(
                    path, idx, "departments", "wards", occurrence=i,
                    problem=f"ward '{ward}' is claimed by both '{owner}' and '{name}'.",
                    hint=("A bed prefix belongs to exactly one department. With two "
                          "owners, which staff may see a patient in that bed has no "
                          "answer, and Orb would have to pick one."),
                )
            seen_wards[ward.lower()] = name
        cfg.departments.append(dept)

    # ── [escalation] ──
    esc_raw = doc.get("escalation", {}) or {}
    escalation = _check_table(path, idx, "escalation", esc_raw)
    tiers_raw = esc_raw.get("tiers", []) or []
    tiers: list[dict] = []
    for i, entry in enumerate(tiers_raw):
        tier = _check_table(path, idx, "escalation.tiers", entry, occurrence=i)
        is_last = i == len(tiers_raw) - 1
        if not is_last and tier.get("timeout_minutes") is None:
            raise _fail(
                path, idx, "escalation.tiers", "role", occurrence=i,
                problem=(f"tier {i + 1} ('{tier['role']}') has no timeout_minutes, "
                         f"but it is not the last tier."),
                hint=("Without a timeout this tier never hands on, so an unanswered "
                      "alert stops here and the tiers below it are unreachable.\n"
                      "Add `timeout_minutes = 10`, or move this tier to the end of "
                      "the list if it really is the last resort."),
            )
        if is_last and tier.get("timeout_minutes") is not None:
            # Not an error — escalation_service clears it — but say so, because
            # an operator who set it expects it to do something.
            cfg.errors.append(
                f"the last escalation tier ('{tier['role']}') has a timeout, which "
                f"Orb ignores: there is no tier after it to escalate to. It stays "
                f"open and visible instead."
            )
            tier["timeout_minutes"] = None
        tiers.append({
            "tier": i + 1,
            "role": tier["role"],
            "label": tier.get("label") or tier["role"].title(),
            "timeout_minutes": tier.get("timeout_minutes"),
            "entry_min_news2": tier.get("entry_min_news2"),
        })
    if tiers:
        tiers[-1]["timeout_minutes"] = None
    escalation["tiers"] = tiers
    cfg.escalation = escalation

    # ── [operations] ──
    cfg.operations = _check_table(path, idx, "operations", doc.get("operations", {}) or {})

    cfg.env_overrides = _to_env(cfg)
    return cfg


def _to_env(cfg: SiteConfig) -> dict[str, str]:
    """Translate the file into the ORB_* variables the existing readers use.

    Deliberately a translation and not a second source of truth: escalation_service
    keeps being the only thing that interprets a tier ladder, ops_health_service
    keeps being the only thing that interprets a staleness threshold. This
    function's whole job is to speak their language.
    """
    env: dict[str, str] = {}
    esc = cfg.escalation
    if esc.get("tiers"):
        env["ORB_ESCALATION_TIERS"] = json.dumps(esc["tiers"])
    if "news2_threshold" in esc:
        env["ORB_ESCALATION_NEWS2_THRESHOLD"] = str(esc["news2_threshold"])
    if "escalate_on_single_parameter_3" in esc:
        env["ORB_ESCALATION_ON_SINGLE_PARAM_3"] = \
            "true" if esc["escalate_on_single_parameter_3"] else "false"
    if "scan_lookback_minutes" in esc:
        env["ORB_ESCALATION_SCAN_LOOKBACK_MINUTES"] = str(esc["scan_lookback_minutes"])
    if "relay_notify" in esc:
        env["ORB_ESCALATION_RELAY_NOTIFY"] = "true" if esc["relay_notify"] else "false"

    ops = cfg.operations
    if "backup_max_age_hours" in ops:
        env["ORB_HEARTBEAT_BACKUP_MAX_AGE_H"] = str(ops["backup_max_age_hours"])
    if "disk_min_free_percent" in ops:
        env["ORB_HEARTBEAT_DISK_MIN_PCT"] = str(ops["disk_min_free_percent"])

    if cfg.timezone:
        # An HL7 timestamp with no UTC offset is read in this zone. Getting it
        # wrong stamped every reading 5.5 hours into the future in a UTC+05:30
        # hospital — see config.HL7_DEFAULT_TZ.
        env["ORB_HL7_DEFAULT_TZ"] = cfg.timezone
    return env


# ═════════════════════════════════════════════════════════════════════════════
#  Public entry points
# ═════════════════════════════════════════════════════════════════════════════
_loaded: SiteConfig = SiteConfig()


def load_site_config(path: Path | str | None = None, *, apply_env: bool = True) -> SiteConfig:
    """Read, validate, and (optionally) apply the site configuration file.

    Raises SiteConfigError on anything wrong. NOTHING is applied on the way to
    that exception — validation completes in full before a single environment
    variable is written, so a refused file cannot leave the process half
    configured. A half-applied config is worse than none: it looks applied.

    An absent file is not an error and returns an empty, `present=False` config.
    """
    global _loaded
    target = Path(path) if path is not None else Path(
        os.getenv("ORB_SITE_CONFIG_FILE") or DEFAULT_PATH
    )

    if not target.is_file():
        _loaded = SiteConfig(path=str(target), present=False)
        return _loaded

    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as e:
        raise SiteConfigError(
            target, f"the file could not be read ({e.strerror or e}).",
            hint="Check that it exists and that the Orb user can read it "
                 f"(`ls -l {target}`).",
        ) from e

    try:
        doc = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        # tomllib puts "(at line N, column M)" in the message; lift the number
        # out so the operator gets it on its own line, next to the text.
        m = re.search(r"at line (\d+)", str(e))
        line = int(m.group(1)) if m else None
        raise SiteConfigError(
            target,
            f"the file is not valid TOML: {e}",
            line=line,
            source_line=(raw_text.splitlines()[line - 1] if line and
                         line <= len(raw_text.splitlines()) else None),
            hint=("Common causes: a missing closing bracket on a [section] "
                  "header, a value that needs quotes around it, or a stray "
                  "character pasted from a word processor."),
        ) from e

    cfg = _validate(target, raw_text, doc)

    if apply_env:
        for key, value in cfg.env_overrides.items():
            existing = os.environ.get(key)
            if existing is not None and existing != value:
                cfg.env_conflicts.append(key)
                logger.warning(
                    "Site config %s sets %s=%s, overriding the environment value "
                    "%r. The FILE wins. Remove it from the environment (.env) to "
                    "silence this.", target, key, value, existing,
                )
            os.environ[key] = value

    _loaded = cfg
    for note in cfg.errors:
        logger.warning("Site config %s: %s", target, note)
    logger.info(
        "Site config loaded from %s — site=%r, %d department(s), %d escalation tier(s)",
        target, cfg.site_name, len(cfg.departments), len(cfg.escalation.get("tiers") or []),
    )
    return cfg


def current_site_config() -> SiteConfig:
    """The config this process loaded at startup (empty if none)."""
    return _loaded


def site_config_snapshot() -> dict:
    """Plain, PHI-free description for the ops status surface.

    Counts and names of CONFIGURATION, never of people or patients. Department
    and ward names are hospital structure — they are on the signs in the
    corridor — but nothing derived from a patient row belongs here.
    """
    cfg = _loaded
    return {
        "present": cfg.present,
        "file": cfg.path,
        "site_name": cfg.site_name,
        "timezone": cfg.timezone,
        "department_count": len(cfg.departments),
        "departments": [d["name"] for d in cfg.departments],
        "wards": cfg.wards,
        "escalation_tiers": [
            {"tier": t["tier"], "role": t["role"], "label": t["label"],
             "timeout_minutes": t["timeout_minutes"]}
            for t in (cfg.escalation.get("tiers") or [])
        ],
        "notes": list(cfg.errors),
        "env_conflicts": list(cfg.env_conflicts),
    }
