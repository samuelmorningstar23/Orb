"""Orb Hospital OS — bench monitor simulator (speaks HL7 v2 ORU^R01 over MLLP).

This is the DEVICE side of the wire. It exists so the monitor integration can be
built, exercised and regression-tested with no hardware in the room — which is
the only way the Week-6 bench milestone survives a site visit that has not
happened yet. When a real unit does arrive, this is also the reference we diff
its traffic against.

It is not a "happy path demo". A bench that only sends well-formed messages
proves nothing about an appliance that has to survive a ward. The fault modes
below are the ones bedside monitors and serial-to-IP gateways actually produce:

    garbage       bytes with no MLLP start block (a serial converter's line noise)
    truncate      a message cut off mid-segment (device rebooted mid-send)
    duplicate     the same message twice (retransmit after a missed ACK)
    out_of_order  a timestamped reading older than one already sent (buffer replay)
    flood         a burst far above the ward's real rate (a device stuck in retry)
    drop          the connection torn down without a FIN (cable pulled)
    unsupported   a non-ORU message on the same feed (ADT from a shared gateway)
    no_ack_wait   fire-and-forget without reading ACKs (a sender that ignores them)
    unknown_obx   an observation identifier nobody has mapped (a device option
                  the integration was never told about) — must be counted and
                  NAMED by the receiver, not silently dropped

VENDORS. The hospital's monitors are Philips or GE, and which one is not yet
known. `--vendor philips|ge|generic` makes this simulator speak that family's
OBX conventions, so the Week-6 bench can be run for BOTH before any hardware
arrives and the answer to "will it work" does not have to wait on procurement.

    cd server && .venv/bin/python -m services.vitals_source.bench_simulator \\
        --host 127.0.0.1 --port 2575 --patients P001,P002 --interval 5 \\
        --vendor philips --faults duplicate,out_of_order

    # the one-page sheet to send the vendor / biomedical engineer
    .venv/bin/python -m services.vitals_source.bench_simulator --vendor ge --vendor-table

BE CLEAR ABOUT WHAT A GREEN BENCH RUN PROVES. The vendor messages are built from
`vendor_profiles.py`, which is the same table Orb parses with, and NOTHING in
that table has been confirmed against a real device or a vendor specification.
So a clean run proves the pipeline — framing, parsing, timestamps, mapping,
plausibility, persistence, NEWS2, alerting, backpressure. It does NOT prove the
identifiers are the ones the hospital's monitors actually emit. Only a captured
message or the vendor's point-mapping document does that.

Or drive it from a test in-process via `BenchMonitor` / `build_oru`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from . import hl7v2
from . import vendor_profiles
from .mllp import MllpFramer, frame

logger = logging.getLogger("orb.vitals_source.bench")

# LOINC codes Orb's vital mapping understands (ingestion_adapters._VITAL_CODE_MAP).
# Kept here as literal wire values, not imported, because the bench simulator is
# pretending to be a THIRD-PARTY device: if someone edits Orb's map, the bench
# must keep sending what a real monitor sends and the test must fail.
#
# NOTE the asymmetry with the vendor profiles, and that it is deliberate: these
# LOINC literals are an INDEPENDENT check of Orb's mapping, because they were
# written from the standard rather than copied from the code under test. The
# vendor tables cannot be independent in the same way — nobody has a Philips or
# a GE message to write them from — so `--vendor` messages ARE generated from
# the table Orb parses with, and prove correspondingly less. See the docstring.
OBSERVATION_SPECS = [
    ("8867-4", "Heart Rate", "bpm", "60-100"),
    ("8480-6", "Systolic Blood Pressure", "mmHg", "90-140"),
    ("8462-4", "Diastolic Blood Pressure", "mmHg", "60-90"),
    ("2708-6", "Oxygen Saturation", "%", "95-100"),
    ("8310-5", "Body Temperature", "Cel", "36.1-37.2"),
    ("9279-1", "Respiratory Rate", "/min", "12-20"),
]

# Bench order of the six vitals — must match `BedState.values()`.
VITAL_ORDER = ("HR", "SBP", "DBP", "SpO2", "Temp", "RR")

# How each vendor's gateway names itself in MSH-3 / MSH-4. Chosen so that Orb's
# own auto-detection has to work off the same strings a real gateway would send,
# rather than the bench telling the receiver what it is out of band.
VENDOR_SENDERS = {
    "philips_intellivue": ("PhilipsIntelliBridge", "IntelliVue^MX450"),
    "ge_carescape": ("GE_UNITY_ID", "CARESCAPE^B650"),
}

FAULTS = (
    "garbage", "truncate", "duplicate", "out_of_order",
    "flood", "drop", "unsupported", "no_ack_wait", "unknown_obx",
)

# An observation identifier nobody has mapped, per vendor family. Shaped like the
# vendor's own vocabulary so the receiver's "unknown identifier" list looks
# exactly as it would in the hospital.
UNKNOWN_OBX = {
    "philips_intellivue": ("150022^MDC_PRESS_BLD_ART_ABP_MEAN^MDIL", "266016^MDC_DIM_MMHG^MDIL", "76"),
    "ge_carescape": ("CO^CARDIAC OUTPUT^GEMS", "L/MIN", "4.8"),
    "generic": ("99999-9^Some Unmapped Parameter^LN", "widgets", "42"),
}


# ════════════════════════════════════════════
#  Message construction
# ════════════════════════════════════════════

@dataclass
class BedState:
    """A simulated bed's slowly-drifting vitals."""
    patient_id: str
    bed: str = "BED-1"
    hr: float = 82.0
    sbp: float = 118.0
    dbp: float = 74.0
    spo2: float = 97.0
    temp: float = 36.9
    rr: float = 16.0
    sequence: int = 0

    def step(self, rng: random.Random) -> None:
        """Random walk with mild mean reversion — enough motion for the wire to
        carry distinct values, not a clinical model. Realistic PHYSIOLOGY is
        `bot_service`'s job; this class's job is realistic PROTOCOL."""
        self.hr = _clamp(self.hr + rng.gauss(0, 2.5) + 0.05 * (82 - self.hr), 40, 170)
        self.sbp = _clamp(self.sbp + rng.gauss(0, 3.0) + 0.05 * (118 - self.sbp), 70, 190)
        self.dbp = _clamp(self.dbp + rng.gauss(0, 2.0) + 0.05 * (74 - self.dbp), 40, 110)
        self.spo2 = _clamp(self.spo2 + rng.gauss(0, 0.5) + 0.10 * (97 - self.spo2), 80, 100)
        self.temp = _clamp(self.temp + rng.gauss(0, 0.08) + 0.10 * (36.9 - self.temp), 34.5, 41.0)
        self.rr = _clamp(self.rr + rng.gauss(0, 0.7) + 0.08 * (16 - self.rr), 6, 40)
        self.sequence += 1

    def values(self) -> list[float]:
        return [round(self.hr), round(self.sbp), round(self.dbp),
                round(self.spo2), round(self.temp, 1), round(self.rr)]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_oru(
    state: BedState,
    *,
    observed_at: Optional[datetime] = None,
    control_id: Optional[str] = None,
    sending_app: Optional[str] = None,
    sending_facility: Optional[str] = None,
    patient_name: str = "Doe^Test",
    use_escapes: bool = False,
    device_timezone=None,
    emit_offset: bool = False,
    clock_offset_seconds: float = 0.0,
    vendor=None,
    unknown_obx: bool = False,
) -> str:
    """Build one ORU^R01 vitals message for a bed.

    Segment separator is `\\r`, per the standard — not `\\n`. A parser that only
    works when a fixture file happens to use newlines will fail on the wire, and
    that is exactly the class of bug this simulator exists to catch.

    `vendor` is a profile name (`philips`, `ge`, `generic`) or a `VendorProfile`.
    It changes the OBX-3 identifiers, the OBX-6 units and MSH-3/MSH-4 — i.e.
    everything that actually differs between a Philips and a GE feed — and
    nothing else, because nothing else does differ.

    THE CLOCK KNOBS ARE PART OF THE FAULT MODEL, not conveniences:

      `device_timezone`      the zone this device believes it is in. Defaults to
                             the machine's own zone, because that is what a
                             bedside monitor on a ward is set to.
      `emit_offset`          False (the default) writes a DTM with NO UTC offset
                             — legal HL7, and what most monitors actually send.
                             A receiver that assumes UTC misreads it by the
                             device's whole offset; in IST that is 5.5 hours.
      `clock_offset_seconds` the device's clock is WRONG by this much. Negative
                             = slow. A monitor with a wrong clock is the single
                             most common integration fault in a hospital, and a
                             receiver that treats it as "old data" goes silent
                             on a crashing patient.
    """
    profile = _profile(vendor)
    default_app, default_facility = VENDOR_SENDERS.get(profile.key, ("ICU_MONITOR", "HOSPITAL_A"))
    sending_app = sending_app or default_app
    sending_facility = sending_facility or default_facility

    instant = observed_at or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    if clock_offset_seconds:
        instant = instant + timedelta(seconds=clock_offset_seconds)
    zone = hl7v2.resolve_timezone(device_timezone) or datetime.now().astimezone().tzinfo
    ts = hl7v2.format_timestamp(instant, tz=zone, with_offset=emit_offset)
    ctrl = control_id or f"MON{state.sequence:07d}"
    # `\S\` is the HL7 escape for the component separator. Devices really do
    # send escaped separators inside display names; exercising it here keeps the
    # unescaping path honest.
    panel = "85354-9^Vital Signs\\S\\Bedside^LN" if use_escapes else "85354-9^Vital Signs^LN"

    # MSH-4 carries the model as a component (`IntelliVue^MX450`), which is how
    # gateways really identify themselves — and it exercises the receiver's
    # component parsing rather than assuming a flat field.
    facility_field = sending_facility

    segments = [
        f"MSH|^~\\&|{sending_app}|{facility_field}|ORB|ORB_OS|{ts}||ORU^R01^ORU_R01|{ctrl}|P|2.5",
        f"PID|1||{state.patient_id}^^^{sending_facility.split('^')[0]}^MR~{state.patient_id}-ALT^^^MON^PI||{patient_name}||19700101|U",
        f"PV1|1|I|ICU^{state.bed}^{sending_facility.split('^')[0]}",
        f"OBR|1|{ctrl}|{ctrl}|{panel}|||{ts}",
    ]

    values = dict(zip(VITAL_ORDER, state.values()))
    index = 0
    specs = profile.emission_specs()
    if specs:
        for vital, obx3, obx6, ref in specs:
            index += 1
            segments.append(
                f"OBX|{index}|NM|{obx3}||{values[vital]}|{obx6}|{ref}|N|||F|||{ts}")
        # One observation the vendor sends that Orb recognises and deliberately
        # does not score (mean arterial pressure). It must be stored, not
        # scored, and must NOT show up as an unknown identifier.
        for obx3, obx6, value in profile.unscored_emission():
            index += 1
            segments.append(f"OBX|{index}|NM|{obx3}||{value}|{obx6}||N|||F|||{ts}")
    else:
        for (code, name, unit, ref), value in zip(OBSERVATION_SPECS, state.values()):
            index += 1
            segments.append(
                f"OBX|{index}|NM|{code}^{name}^LN||{value}|{unit}|{ref}|N|||F|||{ts}")

    if unknown_obx:
        obx3, obx6, value = UNKNOWN_OBX.get(profile.key, UNKNOWN_OBX["generic"])
        index += 1
        segments.append(f"OBX|{index}|NM|{obx3}||{value}|{obx6}||N|||F|||{ts}")

    return "\r".join(segments) + "\r"


def _profile(vendor) -> "vendor_profiles.VendorProfile":
    """Accept a profile object, a name, an alias, or None."""
    if isinstance(vendor, vendor_profiles.VendorProfile):
        return vendor
    return vendor_profiles.profile_for(vendor)


def build_adt(state: BedState) -> str:
    """A non-ORU message, for the 'shared gateway sends us everything' case."""
    ts = hl7v2.format_timestamp()
    return "\r".join([
        f"MSH|^~\\&|HIS|HOSPITAL_A|ORB|ORB_OS|{ts}||ADT^A08|ADT{state.sequence:05d}|P|2.5",
        f"PID|1||{state.patient_id}^^^HOSPITAL_A||Doe^Test||19700101|U",
    ]) + "\r"


# ════════════════════════════════════════════
#  The bench monitor
# ════════════════════════════════════════════

@dataclass
class BenchStats:
    messages_sent: int = 0
    faults_injected: dict = field(default_factory=dict)
    acks: dict = field(default_factory=dict)
    connects: int = 0
    errors: int = 0

    def note_fault(self, name: str) -> None:
        self.faults_injected[name] = self.faults_injected.get(name, 0) + 1

    def note_ack(self, code: str) -> None:
        self.acks[code] = self.acks.get(code, 0) + 1


class BenchMonitor:
    """A fake bedside monitor that connects to Orb and pushes ORU messages."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2575,
        *,
        patients: Sequence[str] = ("P001",),
        interval_seconds: float = 5.0,
        faults: Iterable[str] = (),
        seed: Optional[int] = None,
        flood_burst: int = 200,
        read_acks: bool = True,
        device_timezone=None,
        emit_offset: bool = False,
        clock_offset_seconds: float = 0.0,
        vendor=None,
    ):
        self.host = host
        self.port = port
        self.interval_seconds = float(interval_seconds)
        self.vendor = _profile(vendor)
        self.device_timezone = device_timezone
        self.emit_offset = bool(emit_offset)
        self.clock_offset_seconds = float(clock_offset_seconds)
        self.faults = {f for f in faults if f in FAULTS}
        unknown = {f for f in faults} - set(FAULTS)
        if unknown:
            raise ValueError(f"unknown fault(s): {sorted(unknown)}; choose from {FAULTS}")
        self.flood_burst = int(flood_burst)
        self.read_acks = read_acks and "no_ack_wait" not in self.faults
        self.rng = random.Random(seed)
        self.beds = [BedState(patient_id=p, bed=f"BED-{i + 1}")
                     for i, p in enumerate(patients)]
        self.stats = BenchStats()
        self._framer = MllpFramer()

    # ── one cycle over every bed ──

    def _oru(self, bed: BedState, **kwargs) -> str:
        """`build_oru` with this monitor's clock/timezone personality applied."""
        kwargs.setdefault("device_timezone", self.device_timezone)
        kwargs.setdefault("emit_offset", self.emit_offset)
        kwargs.setdefault("clock_offset_seconds", self.clock_offset_seconds)
        kwargs.setdefault("vendor", self.vendor)
        return build_oru(bed, **kwargs)

    async def send_cycle(self, writer: asyncio.StreamWriter) -> None:
        for bed in self.beds:
            bed.step(self.rng)
            unknown = "unknown_obx" in self.faults and self.rng.random() < 0.5
            if unknown:
                self.stats.note_fault("unknown_obx")
            message = self._oru(bed, use_escapes=True, unknown_obx=unknown)

            if "garbage" in self.faults and self.rng.random() < 0.25:
                # Raw noise with no start block: must be discarded by the
                # framer without disturbing the message that follows it.
                writer.write(b"\x00\x1f garbage from a serial converter \xff")
                self.stats.note_fault("garbage")

            if "unsupported" in self.faults and self.rng.random() < 0.2:
                writer.write(frame(build_adt(bed)))
                self.stats.note_fault("unsupported")

            if "truncate" in self.faults and self.rng.random() < 0.2:
                # Start block, half a message, then a fresh start block. The
                # framer must abandon the partial and pick up the next message.
                writer.write(b"\x0b" + message[: len(message) // 2].encode())
                self.stats.note_fault("truncate")

            writer.write(frame(message))
            self.stats.messages_sent += 1

            if "duplicate" in self.faults and self.rng.random() < 0.3:
                writer.write(frame(message))
                self.stats.messages_sent += 1
                self.stats.note_fault("duplicate")

            if "out_of_order" in self.faults and self.rng.random() < 0.3:
                stale = self._oru(
                    bed,
                    observed_at=datetime.now(timezone.utc) - timedelta(minutes=45),
                    control_id=f"STALE{bed.sequence:06d}",
                )
                writer.write(frame(stale))
                self.stats.messages_sent += 1
                self.stats.note_fault("out_of_order")

            if "flood" in self.faults and self.rng.random() < 0.1:
                for n in range(self.flood_burst):
                    burst = self._oru(bed, control_id=f"FLOOD{bed.sequence:05d}{n:04d}")
                    writer.write(frame(burst))
                    self.stats.messages_sent += 1
                self.stats.note_fault("flood")

            await writer.drain()

    async def _read_acks(self, reader: asyncio.StreamReader, timeout: float = 2.0) -> None:
        """Drain whatever ACKs have arrived. A real sender blocks on these; the
        bench records the codes so a run reports how Orb answered."""
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
        except asyncio.TimeoutError:
            return
        if not chunk:
            return
        for raw in self._framer.feed(chunk):
            try:
                ack = hl7v2.parse(raw.decode("utf-8", errors="replace"))
            except hl7v2.Hl7ParseError:
                self.stats.note_ack("unparseable")
                continue
            msa = ack.first("MSA")
            self.stats.note_ack(msa.field(1) if msa else "missing-MSA")

    # ── run modes ──

    async def run_once(self) -> BenchStats:
        """Connect, send one cycle for every bed, read ACKs, disconnect."""
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.stats.connects += 1
        try:
            await self.send_cycle(writer)
            if self.read_acks:
                await self._read_acks(reader)
        finally:
            await self._close(writer)
        return self.stats

    async def run_forever(self, cycles: Optional[int] = None) -> BenchStats:
        """Keep a connection up and push on the interval, reconnecting on loss."""
        sent_cycles = 0
        while cycles is None or sent_cycles < cycles:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                self.stats.connects += 1
                logger.info("bench monitor connected to %s:%s", self.host, self.port)
                try:
                    while cycles is None or sent_cycles < cycles:
                        await self.send_cycle(writer)
                        sent_cycles += 1
                        if self.read_acks:
                            await self._read_acks(reader, timeout=1.0)
                        if "drop" in self.faults and self.rng.random() < 0.15:
                            # Yank the connection without a graceful close.
                            self.stats.note_fault("drop")
                            logger.info("bench monitor: injecting connection drop")
                            break
                        await asyncio.sleep(self.interval_seconds)
                finally:
                    await self._close(writer)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError) as e:
                self.stats.errors += 1
                logger.warning("bench monitor connection failed: %s", e)
                await asyncio.sleep(2.0)
        return self.stats

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bench_simulator",
        description="Fake bedside monitor: HL7 v2 ORU^R01 over MLLP, with fault injection.",
    )
    p.add_argument("--host", default="127.0.0.1", help="Orb MLLP listener host")
    p.add_argument("--port", type=int, default=2575, help="Orb MLLP listener port")
    p.add_argument("--patients", default="P001",
                   help="comma-separated patient identifiers (one simulated bed each)")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between cycles")
    p.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: forever)")
    p.add_argument("--faults", default="",
                   help=f"comma-separated fault modes: {','.join(FAULTS)}")
    p.add_argument("--flood-burst", type=int, default=200,
                   help="messages per flood burst when the flood fault fires")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="print one sample message and exit (no network)")
    p.add_argument("--device-tz", default=None,
                   help="zone this fake monitor believes it is in, e.g. Asia/Kolkata "
                        "(default: this machine's zone, like a real bedside unit)")
    p.add_argument("--emit-offset", action="store_true",
                   help="write the UTC offset into the DTM. OFF by default, because "
                        "most bedside monitors send an offset-less local timestamp")
    p.add_argument("--clock-offset", type=float, default=0.0, metavar="SECONDS",
                   help="pretend this device's clock is wrong by N seconds "
                        "(negative = slow). The commonest real integration fault")
    p.add_argument("--vendor", default="generic",
                   help="monitor family to imitate: philips | ge | generic "
                        "(aliases: intellivue, intellibridge, carescape, unity, loinc)")
    p.add_argument("--vendor-table", action="store_true",
                   help="print this vendor's OBX mapping table, gateway defaults and the "
                        "questions to ask the vendor, then exit. This is the sheet to send "
                        "the biomedical engineer before bench day.")
    return p


def print_vendor_table(profile) -> None:
    """The one-page confirmation sheet. Deliberately blunt about confidence."""
    info = profile.describe()
    print(f"\n{info['display_name']}  ({info['key']})")
    print("=" * 78)
    print(info["summary"])
    print()
    print("!! NOT VERIFIED AGAINST A DEVICE OR A VENDOR SPEC.")
    print("   " + info["verification_note"])
    print()
    rows = profile.mapping_table()
    if rows:
        width = max(len(r["obx_3_1"]) for r in rows)
        print(f"{'VITAL':<6} {'OBX-3.1':<{width}}  {'OBX-3.2 (identifier text)':<46} "
              f"{'UNIT':<8} CONFIDENCE")
        print("-" * 78)
        for r in rows:
            print(f"{r['vital']:<6} {r['obx_3_1']:<{width}}  {r['obx_3_2'][:44]:<46} "
                  f"{r['unit']:<8} {r['confidence']}")
    else:
        print("(no vendor translation — Orb's own LOINC table is used as-is)")
    print()
    hs = info["handshake"]
    print("GATEWAY")
    print("-" * 78)
    print(f"  gateway        {hs['gateway']}")
    print(f"  Orb socket     {hs['orb_mode']}   (ORB_MONITOR_MODE)")
    print(f"  wiring         {hs['wiring']}")
    print(f"  ACK            expects={hs['expects_ack']} mode={hs['ack_mode']}")
    print(f"  HL7 versions   {', '.join(hs['hl7_versions'])}")
    for note in hs["notes"]:
        print(f"  · {note}")
    print()
    print("ASK THE VENDOR / BIOMED")
    print("-" * 78)
    for i, q in enumerate(info["site_questions"], start=1):
        print(f"  {i}. {q}")
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not vendor_profiles.is_known_profile(args.vendor):
        print(f"unknown vendor {args.vendor!r}; choose from: "
              f"{', '.join(sorted(vendor_profiles.PROFILES))} (aliases: philips, ge, loinc)")
        return 2
    profile = vendor_profiles.profile_for(args.vendor)

    if args.vendor_table:
        print_vendor_table(profile)
        return 0

    patients = [p.strip() for p in args.patients.split(",") if p.strip()]
    if not patients:
        print("no patients given")
        return 2

    if args.print_only:
        bed = BedState(patient_id=patients[0])
        bed.step(random.Random(args.seed))
        print(build_oru(bed, use_escapes=True, device_timezone=args.device_tz,
                        emit_offset=args.emit_offset, vendor=profile,
                        unknown_obx="unknown_obx" in args.faults,
                        clock_offset_seconds=args.clock_offset).replace("\r", "\n"))
        return 0

    faults = [f.strip() for f in args.faults.split(",") if f.strip()]
    monitor = BenchMonitor(
        args.host, args.port,
        patients=patients, interval_seconds=args.interval,
        faults=faults, seed=args.seed, flood_burst=args.flood_burst,
        device_timezone=args.device_tz, emit_offset=args.emit_offset,
        clock_offset_seconds=args.clock_offset, vendor=profile,
    )
    logger.info("bench monitor imitating %s", profile.display_name)
    try:
        stats = asyncio.run(monitor.run_forever(cycles=args.cycles))
    except KeyboardInterrupt:
        stats = monitor.stats
    print(f"vendor={profile.key} sent={stats.messages_sent} connects={stats.connects} "
          f"errors={stats.errors} acks={stats.acks} faults={stats.faults_injected}")
    if profile.key != "generic":
        print("NOTE: vendor messages are generated from Orb's own (unverified) profile table. "
              "A clean run proves the pipeline, not the mapping — confirm with a captured "
              "message from the hospital's monitor.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
