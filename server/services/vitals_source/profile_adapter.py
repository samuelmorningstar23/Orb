"""Orb Hospital OS — vendor-profile-aware HL7 adapter.

`HL7Adapter` (services/ingestion_adapters.py) is the one parse of an ORU^R01
into canonical observations, and it stays the one parse. This module wraps it:
it runs the SAME parse, then gives a vendor profile a second look at whatever
Orb's LOINC/common-name table could not map.

The ordering is the whole safety argument:

    LOINC first, always.   An observation the shared table already understood is
                           returned untouched. A wrong guess in a vendor profile
                           therefore cannot corrupt a reading Orb already knew
                           how to read — the worst a bad profile row can do is
                           map something that would otherwise have been ignored.

    Profile second.        Only `mapped_vital_key is None` events are offered to
                           the profile. Whatever it maps then goes through the
                           SAME unit normalisation and the SAME plausibility
                           bounds as everything else, because a monitor's
                           Fahrenheit temperature or a disconnected lead's NaN
                           is exactly as dangerous whichever table named it.

    Unmapped is reported.  Every identifier nobody could map is counted and
                           named through `observer`. That list is the artefact a
                           bench run exists to produce.

This module DOES import Orb code (`ingestion_adapters`), so — unlike
`hl7v2.py`, `mllp.py` and `vendor_profiles.py` — it must not be imported by the
bench simulator. The bench runs on a laptop with no database and no `.env`.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from services.ingestion_adapters import (
    HL7Adapter,
    ObservationType,
    normalize_temp_to_celsius,
    validate_vital_value,
)

from . import vendor_profiles
from .vendor_profiles import STRICT_CONFIDENCE, VendorProfile

logger = logging.getLogger("orb.vitals_source.profile")

#: Called as observer(kind, detail) for anything worth counting. Kinds:
#:   "mapped"    a profile mapping was applied     detail = "VITAL<-identifier"
#:   "unmapped"  nobody could map this identifier  detail = the identifier/name
#:   "skipped"   a mapping existed but strict mode refused it
#:   "demoted"   a duplicate source for one vital was not scored
#:   "unit"      a vendor unit spelling was translated (°F→°C is the one that
#:               matters; the others are cosmetic)
Observer = Callable[[str, str], None]


class VendorProfileAdapter(HL7Adapter):
    """`HL7Adapter` + one vendor's OBX identifier and unit conventions."""

    def __init__(
        self,
        profile: Optional[VendorProfile] = None,
        *,
        default_timezone=None,
        observer: Optional[Observer] = None,
        strict: bool = False,
    ):
        super().__init__(default_timezone=default_timezone)
        self.profile = profile or vendor_profiles.GENERIC
        self.observer = observer
        self.strict = bool(strict)

    @property
    def adapter_name(self) -> str:
        # Written into `ingestion_events.adapter_name`, so a clinical reviewer
        # can see WHICH table read a message months later. Unchanged
        # ("hl7_oru") for the generic profile, so nothing that predates vendor
        # profiles gets relabelled.
        if self.profile.key == vendor_profiles.GENERIC.key:
            return "hl7_oru"
        return f"hl7_oru:{self.profile.key}"

    def _note(self, kind: str, detail: str) -> None:
        if self.observer is not None:
            try:
                self.observer(kind, detail)
            except Exception:  # an observability hook may never break ingestion
                logger.debug("profile observer raised", exc_info=True)

    def _parse_obx(self, obx, **kwargs):
        """Base parse, then translate the vendor's UNIT spelling.

        This has to happen per-OBX rather than in `parse()`, because the base
        adapter converts a temperature and REWRITES `unit` to "°C" while it is
        doing it. By the time `parse()` sees the event, the vendor's original
        spelling is gone.

        The case that makes this a patient-safety surface rather than tidiness:
        a GE gateway writing `DEGF`. Orb's own `normalize_temp_to_celsius`
        recognises `F`, `°F`, `DEG F` and `[DEGF]` — not `DEGF` — so 98.6 was
        left as 98.6, labelled °C, and then REJECTED by the plausibility bounds.
        The reading vanished silently and the patient's temperature carried
        forward from hours earlier. Same shape for Philips, which sends the
        dimension as a numeric MDC code that matches nothing at all.
        """
        event = super()._parse_obx(obx, **kwargs)
        if self.profile.key == vendor_profiles.GENERIC.key:
            return event
        raw_unit = obx.field(6, component=1) or obx.field(6)
        canonical = self.profile.canonical_unit(raw_unit)
        if not canonical or canonical == raw_unit:
            return event

        if event.mapped_vital_key == "Temp":
            # Redo the conversion from the ORIGINAL value with the translated
            # unit. Idempotent when the unit was already Celsius.
            number = _numeric(event.value)
            if number is not None:
                event.value_numeric = validate_vital_value(
                    "Temp", normalize_temp_to_celsius(number, canonical))
                event.unit = "°C"
                if canonical.upper() in ("F", "°F"):
                    self._note("unit", f"{_identity(event)} °F→°C via {raw_unit!r}")
        elif event.unit == raw_unit:
            event.unit = canonical
        return event

    def parse(self, raw_payload: str, source_name: Optional[str] = None,
              correlation_id: Optional[str] = None):
        events = super().parse(raw_payload, source_name=source_name,
                               correlation_id=correlation_id)
        if self.profile.key == vendor_profiles.GENERIC.key or not events:
            # Still report what LOINC could not map: on a generic profile that
            # list is the evidence for choosing a vendor profile at all.
            for event in events:
                if event.mapped_vital_key is None and event.observation_code is not None:
                    self._note("unmapped", _identity(event))
            return events

        # Vitals the shared LOINC table already resolved. The profile is not
        # allowed to add a second source for any of these.
        already = {e.mapped_vital_key for e in events if e.mapped_vital_key}

        # vital_key -> (priority, event) currently winning
        claimed: dict[str, tuple[int, object]] = {}

        for event in events:
            if event.mapped_vital_key is not None:
                continue
            hit = self.profile.lookup(event.observation_code, event.observation_name)
            if hit is None:
                self._note("unmapped", _identity(event))
                continue

            mapping = hit.mapping
            if mapping.vital_key is None:
                # Recognised, deliberately not a NEWS2 parameter (MAP, ST, PVC).
                # Left as a LAB-class observation: it is still stored, still
                # visible, simply not scored.
                continue
            if self.strict and mapping.confidence not in STRICT_CONFIDENCE:
                self._note("skipped", f"{_identity(event)} ({mapping.confidence.value})")
                continue
            if mapping.vital_key in already:
                self._note("demoted", f"{_identity(event)} → {mapping.vital_key} (LOINC already mapped it)")
                continue

            winner = claimed.get(mapping.vital_key)
            if winner is not None and winner[0] <= mapping.priority:
                self._note("demoted", f"{_identity(event)} → {mapping.vital_key}")
                continue
            if winner is not None:
                # A better source arrived later in the message; unmap the loser.
                loser = winner[1]
                loser.mapped_vital_key = None
                loser.observation_type = ObservationType.LAB
                loser.value_numeric = _revalidate(loser.value_numeric, None)
                self._note("demoted", f"{_identity(loser)} → {mapping.vital_key}")

            self._apply(event, hit)
            claimed[mapping.vital_key] = (mapping.priority, event)

        return events

    # ── one event ──

    def _apply(self, event, hit) -> None:
        mapping = hit.mapping
        event.mapped_vital_key = mapping.vital_key
        event.observation_type = ObservationType.VITAL

        # Units: translate the vendor's spelling into something Orb's own
        # normalisation recognises BEFORE converting. `268192` means nothing to
        # `normalize_temp_to_celsius`; `Cel` and `F` do. An untranslated
        # Fahrenheit reading is not a cosmetic problem — 98.6 as °C is either
        # rejected as implausible (best case, reading silently lost) or, in the
        # Fahrenheit-hypothermia direction, scores a NEWS2 the patient does not
        # have.
        event.unit = self.profile.canonical_unit(event.unit)

        if mapping.vital_key == "Temp" and event.value_numeric is not None:
            event.value_numeric = normalize_temp_to_celsius(event.value_numeric, event.unit)
            event.unit = "°C"

        # The same plausibility gate every other reading passes. A vendor
        # profile is not a licence to skip it.
        event.value_numeric = _revalidate(event.value_numeric, mapping.vital_key)
        if event.value_numeric is None:
            logger.info(
                "profile %s: %s mapped to %s but the value was implausible or non-numeric; "
                "not scored", self.profile.key, _identity(event), mapping.vital_key,
            )
        self._note("mapped", f"{mapping.vital_key}<-{_identity(event)}")


def _revalidate(value, vital_key):
    return validate_vital_value(vital_key, value)


def _numeric(raw) -> Optional[float]:
    """OBX-5 → float, tolerating the `SN` decorations (`>`, `<`, `=`) the base
    adapter also strips. Returns None for anything else."""
    if raw is None:
        return None
    try:
        return float(re.sub(r"[<>= ]", "", str(raw)))
    except (TypeError, ValueError):
        return None


def _identity(event) -> str:
    """A short, PHI-free label for one observation identifier.

    Code and display name only — never the value, never the patient. This string
    is counted into `status()`, rendered on an admin screen and shipped in the
    heartbeat.
    """
    code = (event.observation_code or "").strip()
    name = (event.observation_name or "").strip()
    if code and name and name != code:
        return f"{code}^{name}"[:80]
    return (code or name or "?")[:80]


def build_adapter(profile_name: Optional[str] = None, *, default_timezone=None,
                  observer: Optional[Observer] = None, strict: bool = False):
    """Convenience for callers holding a configured profile NAME, not an object."""
    return VendorProfileAdapter(
        vendor_profiles.profile_for(profile_name),
        default_timezone=default_timezone, observer=observer, strict=strict,
    )
