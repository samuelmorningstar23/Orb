"""Orb Hospital OS — vitals sources.

Simulator, manual entry and bedside monitor are PEERS behind one interface. The
hospital's monitor make/model is still unknown (site-visit deliverable), so this
package is shaped so that naming one costs a single `VitalsSource` subclass, not
an architecture change.

    base.py               the contract: SourceKind, VitalsReading, VitalsSource,
                          provenance labels + `classify_source_label`
    hl7v2.py              a real HL7 v2 parser (encoding characters, repeats,
                          components, sub-components, escapes) + ACK builder
    vendor_profiles.py    Philips / GE OBX identifier + unit tables, vendor
                          detection and gateway handshake defaults (pure data)
    profile_adapter.py    HL7Adapter + a vendor profile, applied only to what
                          LOINC could not map
    mllp.py               bounded, resynchronising MLLP framing
    sink.py               the one persistence path — hands off to ingestion_service
    monitor_source.py     HL7 v2 ORU^R01 over MLLP (listen or connect)
    manual_source.py      clinician-typed vitals
    simulator_source.py   the existing synthetic bot, behind the same lifecycle
    registry.py           process-wide registry + supervision
    bench_simulator.py    a fake monitor that speaks the protocol, with faults

`configure_from_env` is what main.py calls; it is the only place that decides
which sources a deployment runs.

WHY THE LAZY IMPORTS BELOW: `hl7v2`, `mllp` and `bench_simulator` are pure
protocol code with no Orb dependencies, and they have to STAY that way. The
bench simulator is run on a biomedical engineer's laptop next to a monitor, over
a ward network, with no database, no key and no `.env` — if importing it dragged
in `ingestion_service → database → config`, it would refuse to start with a
missing-JWT-secret error and the Week-6 bench test would need a full Orb install
to run. Deferring the source/sink/registry imports to first attribute access
also keeps `ingestion_adapters`' use of the parser free of an import cycle.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional

# Contract only — stdlib dependencies, safe to import anywhere.
from .base import (
    LABEL_ADMISSION_VITALS,
    LABEL_MANUAL_ENTRY,
    LABEL_MONITOR_HL7,
    LABEL_NURSE_OBSERVATION,
    LABEL_SIMULATED,
    ReadingHint,
    SourceKind,
    SourceStats,
    VitalsReading,
    VitalsSource,
    VITAL_KEYS,
    classify_source_label,
    is_machine_measured,
)

logger = logging.getLogger("orb.vitals_source")

# name → (module, attribute). Resolved on first access; see the docstring.
_LAZY: dict[str, tuple[str, str]] = {
    "VendorProfileAdapter": (".profile_adapter", "VendorProfileAdapter"),
    "ManualEntrySource": (".manual_source", "ManualEntrySource"),
    "HL7MonitorSource": (".monitor_source", "HL7MonitorSource"),
    "DEFAULT_MLLP_PORT": (".monitor_source", "DEFAULT_MLLP_PORT"),
    "SimulatorSource": (".simulator_source", "SimulatorSource"),
    "VitalsSink": (".sink", "VitalsSink"),
    "IngestionSink": (".sink", "IngestionSink"),
    "RecordingSink": (".sink", "RecordingSink"),
    "VitalsSourceRegistry": (".registry", "VitalsSourceRegistry"),
    "registry": (".registry", "registry"),
}


def __getattr__(name: str) -> Any:  # PEP 562
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module(target[0], __name__), target[1])
    globals()[name] = value  # cache, so this costs one lookup per process
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def configure_from_env(
    *,
    sio=None,
    simulator_enabled: bool = False,
    simulator_interval: float = 5.0,
    simulator_companion_tick: Optional[Callable[[], Awaitable[None]]] = None,
    target: Optional["VitalsSourceRegistry"] = None,
) -> "VitalsSourceRegistry":
    """Build the deployment's vitals sources. Does NOT start them.

    Manual entry is always registered: it is the redundancy layer and it has no
    external dependency that could fail. The monitor source is registered only
    when explicitly configured, because until the site visit names a make and
    model we do not know where to point it — and a listener nobody is sending to
    is a port open on a hospital network for no reason.
    """
    from .manual_source import ManualEntrySource
    from .monitor_source import DEFAULT_MLLP_PORT, HL7MonitorSource
    from .registry import registry as default_registry
    from .simulator_source import SimulatorSource
    from .sink import DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS, IngestionSink

    reg = target if target is not None else default_registry

    # ORB_HL7_DEFAULT_TZ is read inside hl7v2 (which must stay Orb-free so the
    # bench simulator runs on a laptop). Resolving it once here means the
    # assumption is stated in the startup log rather than inferred at 03:00 from
    # a reading that is five and a half hours out.
    from . import hl7v2
    logger.info(
        "Vitals timestamps with no UTC offset will be read as %s "
        "(set %s to change). This hospital is UTC+05:30; reading an offset-less "
        "device timestamp as UTC would stamp every reading 5.5 h in the future.",
        hl7v2.appliance_timezone(), hl7v2.TZ_ENV_VAR,
    )

    try:
        tolerance = int(os.getenv("ORB_MONITOR_CLOCK_SKEW_TOLERANCE_SECONDS",
                                  str(DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS)))
    except ValueError:
        logger.error(
            "ORB_MONITOR_CLOCK_SKEW_TOLERANCE_SECONDS is not a number; using %ds.",
            DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS)
        tolerance = DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS
    sink = IngestionSink(sio=sio, skew_tolerance_seconds=tolerance)

    def _register(source):
        """Register, replacing any same-id source left behind.

        The registry is process-global, and a lifespan that ran twice in one
        process (the test suite does this; so does a reload) would otherwise
        hit "already registered", which the caller swallows — silently leaving
        the ward with NO monitor feed and no manual source. A leftover that is
        still RUNNING is a real leak, so it is stopped-by-warning rather than
        quietly dropped on the floor.
        """
        existing = reg.get(source.source_id)
        if existing is not None:
            if existing.running:
                logger.warning(
                    "Vitals source %r was still running when reconfigured — the "
                    "previous lifespan did not stop it.", source.source_id)
            reg.unregister(source.source_id)
        return reg.register(source)

    _register(ManualEntrySource(sink))

    if simulator_enabled:
        _register(SimulatorSource(
            sio=sio,
            interval_seconds=simulator_interval,
            companion_tick=simulator_companion_tick,
        ))

    if _env_flag("ORB_MONITOR_ENABLED"):
        protocol = os.getenv("ORB_MONITOR_PROTOCOL", "hl7v2-mllp").strip().lower()
        if protocol not in ("hl7v2-mllp", "hl7", "mllp"):
            # Loud, not fatal: an unknown protocol name is a configuration
            # mistake, and the ward still needs manual entry to come up.
            logger.error(
                "ORB_MONITOR_PROTOCOL=%r is not supported. Supported: hl7v2-mllp. "
                "Monitor feed NOT started; manual entry remains available.", protocol,
            )
        else:
            _register(HL7MonitorSource(
                sink,
                source_id=os.getenv("ORB_MONITOR_SOURCE_ID", "monitor_hl7"),
                host=os.getenv("ORB_MONITOR_HOST", "127.0.0.1"),
                port=int(os.getenv("ORB_MONITOR_PORT", str(DEFAULT_MLLP_PORT))),
                mode=os.getenv("ORB_MONITOR_MODE", "listen").strip().lower(),
                # Per-device override for the zone this monitor's offset-less
                # timestamps are in. Unset = the appliance's zone, which is
                # right when they share a ward. Set it when they do not: two
                # devices on one ward configured differently is how a timezone
                # assumption bites a second time.
                device_timezone=os.getenv("ORB_MONITOR_TZ") or None,
                # Which vendor's OBX identifier/unit table to translate with:
                # philips_intellivue | ge_carescape | generic | auto.
                # "auto" detects per message and SAYS it is guessing; name the
                # vendor here once the site visit has settled it.
                profile=os.getenv("ORB_MONITOR_PROFILE", "auto"),
                # Strict = use only mappings anchored in a published standard,
                # and let everything else show up as unmapped. Useful on the
                # bench when the point of the run is to produce the list of
                # identifiers to take to the vendor.
                strict_profile=_env_flag("ORB_MONITOR_PROFILE_STRICT"),
            ))
    return reg


def manual_entry_source(target: Optional["VitalsSourceRegistry"] = None):
    """The registered manual-entry source, for paths that record typed vitals.

    NOT named `manual_source`: the import system sets the `.manual_source`
    SUBMODULE as a package attribute the moment anything imports it, silently
    clobbering any function of the same name — the previous helper by that name
    returned a module, not a source, in any process that had loaded the class.
    """
    from .manual_source import ManualEntrySource
    from .registry import registry as default_registry

    reg = target if target is not None else default_registry
    source = reg.get("manual")
    return source if isinstance(source, ManualEntrySource) else None


# Stand-in for a process that never ran `configure_from_env` (unit tests,
# scripts, a management command). Recording vitals must not fail because the
# observability plumbing is absent, so an unregistered instance carries the
# counters instead of nobody carrying them.
_standin_manual_source: Optional["ManualEntrySource"] = None


def manual_entry_source_or_standin(target: Optional["VitalsSourceRegistry"] = None):
    """`manual_entry_source()`, but never None — for callers that MUST write.

    `configure_from_env` always registers manual entry, so in a live appliance
    this is the registry's instance and the write lands in the stats the admin
    surface actually shows. The stand-in exists only so that a process without
    a configured registry still records the reading; sink=None is safe because
    the paths that use this (nurse charting, admission) write transactionally
    and never touch the sink.
    """
    from .manual_source import ManualEntrySource

    global _standin_manual_source
    source = manual_entry_source(target)
    if source is not None:
        return source
    if _standin_manual_source is None:
        _standin_manual_source = ManualEntrySource(None)
    return _standin_manual_source


__all__ = [
    # contract
    "VitalsSource", "VitalsReading", "SourceKind", "SourceStats", "ReadingHint",
    "VITAL_KEYS", "classify_source_label", "is_machine_measured",
    "LABEL_MONITOR_HL7", "LABEL_MANUAL_ENTRY", "LABEL_NURSE_OBSERVATION",
    "LABEL_SIMULATED", "LABEL_ADMISSION_VITALS",
    # sources (lazy)
    "HL7MonitorSource", "ManualEntrySource", "SimulatorSource",
    "DEFAULT_MLLP_PORT", "VendorProfileAdapter",
    # plumbing (lazy)
    "VitalsSink", "IngestionSink", "RecordingSink",
    "VitalsSourceRegistry", "registry", "configure_from_env", "manual_entry_source",
    "manual_entry_source_or_standin",
]
