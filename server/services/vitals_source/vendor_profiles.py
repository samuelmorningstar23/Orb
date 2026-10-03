"""Orb Hospital OS — bedside-monitor VENDOR PROFILES (Philips / GE).

The hospital's monitors are **Philips or GE** — not yet narrowed further. Both
emit HL7 v2 `ORU^R01`; what differs between them is not the message grammar but

    * which identifier goes in **OBX-3** for "heart rate",
    * how the **units** in OBX-6 are written,
    * how the **gateway handshake** is wired (who dials whom, ACK expectations).

The grammar is already handled once, properly, in `hl7v2.py`. This module is the
thin layer above it: a table per vendor, plus detection, plus the handshake
defaults. Adding the third vendor is a table, not a parser.

╔══════════════════════════════════════════════════════════════════════════════╗
║  READ THIS BEFORE YOU TRUST A ROW IN THIS FILE                               ║
║                                                                              ║
║  NOTHING HERE HAS BEEN CONFIRMED AGAINST A REAL DEVICE OR A VENDOR SPEC.     ║
║  No IntelliVue, no CARESCAPE, no IntelliBridge configuration file and no     ║
║  Unity Network integration manual was available when this was written. Every ║
║  identifier below is a HYPOTHESIS carrying an explicit `Confidence`, and the ║
║  honest reading of the table is:                                             ║
║                                                                              ║
║    STANDARD_MNEMONIC  the string is a published ISO/IEEE 11073-10101 (MDC)   ║
║                       term and means what we say it means. It is NOT proof   ║
║                       that this hospital's device puts it in OBX-3.1.        ║
║    STANDARD_CODE      the numeric MDC code paired with that mnemonic,        ║
║                       reproduced from the nomenclature WITHOUT the document  ║
║                       in front of us. Treat as unverified.                   ║
║    CONVENTION         a short label (`HR`, `NBP-S`) that gateways of this    ║
║                       family are widely configured to send. Field lore.      ║
║    INFERRED           our guess, offered so a bench run has something to     ║
║                       exercise. Assume it is wrong until a message says so.  ║
║                                                                              ║
║  What that means operationally: a bench run proves the PIPELINE (framing,    ║
║  parse, mapping, plausibility, persistence, NEWS2, alerting). It does NOT    ║
║  prove the MAPPING, because the bench sends what this table says a device    ║
║  sends. Only one artefact closes that gap: a captured message from the       ║
║  hospital's actual monitor, or the vendor's point-mapping document.          ║
║  `profile.site_questions` is the list to take to the vendor. `mapping_table` ║
║  prints as a one-page confirmation sheet:                                    ║
║      cd server && .venv/bin/python -m services.vitals_source.bench_simulator \
║          --vendor philips --vendor-table                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

DESIGN RULES

 1. **A profile never overrides a confirmed mapping.** LOINC (and everything
    else in `ingestion_adapters._VITAL_CODE_MAP`) is applied first; a profile is
    consulted ONLY for observations that mapping left unmapped. A gateway
    configured to emit LOINC therefore behaves identically with a vendor profile
    on or off, and a wrong guess in this file cannot corrupt a reading that was
    already understood.

 2. **Unrecognised is recorded, not silently dropped.** Every identifier this
    layer fails to map is counted and named (`unmapped_identifiers` on the
    monitor source). That list IS the bench deliverable: it is exactly what to
    send to the vendor.

 3. **No Orb imports.** Same rule as `hl7v2.py` and `mllp.py`: the bench
    simulator runs on a biomedical engineer's laptop next to a monitor, with no
    database, no key and no `.env`. Standard library only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

# The six vitals Orb scores NEWS2 from. Duplicated as literals rather than
# imported from `base` for the no-Orb-imports rule above; `base.VITAL_KEYS` is
# the same tuple and a test asserts they agree.
VITAL_KEYS = ("HR", "SBP", "DBP", "SpO2", "Temp", "RR")


class Confidence(str, Enum):
    """How much a row in a mapping table is actually worth. See the banner."""

    STANDARD_MNEMONIC = "standard_mnemonic"
    STANDARD_CODE = "standard_code_unverified"
    CONVENTION = "convention"
    INFERRED = "inferred"

    @property
    def is_verified(self) -> bool:
        """No level is 'verified'. Kept explicit so nothing can quietly claim it."""
        return False


#: Confidence levels considered strong enough to use when the operator has asked
#: for strict mapping (`ORB_MONITOR_PROFILE_STRICT=1`). Strict mode means "map
#: only what is anchored in a published standard, and let the rest show up as
#: unmapped so the site visit has a list".
STRICT_CONFIDENCE = frozenset({Confidence.STANDARD_MNEMONIC, Confidence.STANDARD_CODE})


def _norm(token: Optional[str]) -> str:
    """Upper-case, strip, and squash punctuation: `NBP-S` ≡ `NBP_S` ≡ `nbp s`."""
    if not token:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", str(token).strip().upper())


@dataclass(frozen=True)
class ObxMapping:
    """One vendor identifier → one Orb vital key (or deliberately nothing).

    `vital_key` of None means RECOGNISED BUT NOT SCORED — mean arterial
    pressure, ST elevation, PVC counts and the like are real observations that
    Orb's NEWS2 does not take. Listing them keeps them out of the "unknown
    identifier" report, which otherwise fills with noise and stops being read.
    """

    vital_key: Optional[str]
    identifiers: tuple[str, ...] = ()      # matched against OBX-3.1
    texts: tuple[str, ...] = ()            # matched against OBX-3.2
    confidence: Confidence = Confidence.INFERRED
    unit: str = ""                         # what the vendor typically sends
    note: str = ""
    #: Tie-break when one message carries two sources for the same vital — a
    #: Philips panel routinely contains BOTH an ECG heart rate and an oximeter
    #: pulse rate. Lower wins. Without this the "heart rate" Orb scores is
    #: whichever OBX happened to be parsed last, which is a coin toss that
    #: changes NEWS2 on a patient with a poor pleth trace.
    priority: int = 0

    def __post_init__(self) -> None:
        if self.vital_key is not None and self.vital_key not in VITAL_KEYS:
            raise ValueError(f"{self.vital_key!r} is not an Orb vital key {VITAL_KEYS}")


@dataclass(frozen=True)
class GatewayHandshake:
    """How this vendor's gateway is usually wired to a receiver.

    Every field is a DEFAULT to be confirmed at the site visit, not a fact.
    `verified` is False on every profile in this file and there is no code path
    that sets it True — it exists so a future reader can see at a glance that
    nobody has checked yet.
    """

    gateway: str
    orb_mode: str                     # "listen" | "connect" — Orb's socket role
    wiring: str                       # prose: who dials whom
    expects_ack: bool = True
    ack_mode: str = "original"        # original (MSH-15/16 empty) vs enhanced
    hl7_versions: tuple[str, ...] = ("2.3", "2.4", "2.5")
    default_port: int = 2575
    notes: tuple[str, ...] = ()
    verified: bool = False

    def as_dict(self) -> dict:
        return {
            "gateway": self.gateway,
            "orb_mode": self.orb_mode,
            "wiring": self.wiring,
            "expects_ack": self.expects_ack,
            "ack_mode": self.ack_mode,
            "hl7_versions": list(self.hl7_versions),
            "default_port": self.default_port,
            "notes": list(self.notes),
            "verified": self.verified,
        }


@dataclass(frozen=True)
class MappingHit:
    mapping: ObxMapping
    matched_on: str                   # "identifier" | "text"
    profile_key: str


@dataclass
class VendorProfile:
    """A vendor's OBX conventions, unit spellings and gateway defaults."""

    key: str
    display_name: str
    vendor: str
    handshake: GatewayHandshake
    mappings: tuple[ObxMapping, ...] = ()
    #: Vendor unit spelling → the unit string Orb's own normalisation understands.
    #: Philips sends MDC dimension CODES (`262688`) where Orb expects `%`; a
    #: Fahrenheit dimension that is not translated is the difference between
    #: 98.6 °C and 37.0 °C, so this table is a patient-safety surface, not
    #: cosmetics.
    unit_aliases: dict[str, str] = field(default_factory=dict)
    #: Substrings that, in MSH-3/MSH-4, point at this vendor.
    sending_hints: tuple[str, ...] = ()
    #: OBX-3.3 coding-system names this vendor uses.
    coding_systems: tuple[str, ...] = ()
    site_questions: tuple[str, ...] = ()
    summary: str = ""
    #: What this vendor writes in OBX-3.3 when IT sends a message. Used by the
    #: bench simulator to imitate the device; not used for parsing.
    emission_coding_system: str = "LN"
    #: Canonical unit → the exact OBX-6 field this vendor would emit. The bench
    #: sends these so the unit-translation path is exercised, not assumed.
    unit_emission: dict[str, str] = field(default_factory=dict)

    # Built lazily; two dicts of normalised token → mapping.
    _by_identifier: dict[str, ObxMapping] = field(default_factory=dict, repr=False)
    _by_text: dict[str, ObxMapping] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for mapping in self.mappings:
            for ident in mapping.identifiers:
                self._by_identifier.setdefault(_norm(ident), mapping)
            for text in mapping.texts:
                self._by_text.setdefault(_norm(text), mapping)

    # ── mapping ──

    def lookup(self, code: Optional[str], name: Optional[str]) -> Optional[MappingHit]:
        """Identifier first, then display text. Never a substring match.

        Substring matching is how `MDC_PRESS_BLD_NONINV_MEAN` becomes a systolic
        blood pressure. Exact (normalised) tokens only.
        """
        hit = self._by_identifier.get(_norm(code))
        if hit is not None:
            return MappingHit(hit, "identifier", self.key)
        hit = self._by_text.get(_norm(name))
        if hit is not None:
            return MappingHit(hit, "text", self.key)
        return None

    def canonical_unit(self, unit: Optional[str]) -> Optional[str]:
        """Vendor unit spelling → a unit string Orb's normalisation understands.

        Unrecognised units are returned UNCHANGED, never blanked: an unknown
        unit that reaches `normalize_temp_to_celsius` simply fails to match and
        the value is left alone, whereas a blanked unit loses the evidence a
        reviewer needs to spot a bad conversion.
        """
        if unit is None:
            return None
        return self.unit_aliases.get(_norm(unit), unit)

    # ── emission (the bench simulator pretending to BE this device) ──

    def preferred(self, vital_key: str) -> Optional[ObxMapping]:
        """The mapping this vendor would normally send for a vital."""
        candidates = [m for m in self.mappings if m.vital_key == vital_key]
        if not candidates:
            return None
        return sorted(candidates, key=lambda m: (m.priority, m.confidence.value))[0]

    def emission_specs(self) -> list[tuple[str, str, str, str]]:
        """`(vital_key, OBX-3, OBX-6, reference range)` for each scored vital.

        This is what the bench simulator sends. **It is generated from the same
        table Orb parses with**, which means a clean bench run proves the
        pipeline is self-consistent — NOT that the table matches a real device.
        That distinction is the honest limit of any bench without hardware, and
        it is why `site_questions` exists.
        """
        ranges = {"HR": "60-100", "SBP": "90-140", "DBP": "60-90",
                  "SpO2": "95-100", "Temp": "36.1-37.2", "RR": "12-20"}
        out: list[tuple[str, str, str, str]] = []
        for vital in ("HR", "SBP", "DBP", "SpO2", "Temp", "RR"):
            mapping = self.preferred(vital)
            if mapping is None:
                continue
            ident = mapping.identifiers[0] if mapping.identifiers else ""
            text = mapping.texts[0] if mapping.texts else ident
            obx3 = "^".join([ident, text, self.emission_coding_system])
            obx6 = self.unit_emission.get(mapping.unit, mapping.unit)
            out.append((vital, obx3, obx6, ranges.get(vital, "")))
        return out

    def unscored_emission(self) -> list[tuple[str, str, str]]:
        """`(OBX-3, OBX-6, value)` for observations this vendor sends that Orb
        recognises and deliberately does not score — mean arterial pressure and
        friends. The bench sends one so the "recognised, not scored" path is
        exercised: it must neither be scored nor appear as an unknown identifier.
        """
        out = []
        for mapping in self.mappings:
            if mapping.vital_key is not None or not mapping.identifiers:
                continue
            ident = mapping.identifiers[0]
            text = mapping.texts[0] if mapping.texts else ident
            out.append(("^".join([ident, text, self.emission_coding_system]),
                        self.unit_emission.get(mapping.unit, mapping.unit), "88"))
        return out[:1]

    # ── description ──

    def mapping_table(self) -> list[dict]:
        """The one-page confirmation sheet for the vendor / biomed engineer."""
        rows = []
        for m in self.mappings:
            rows.append({
                "vital": m.vital_key or "(not scored)",
                "obx_3_1": ", ".join(m.identifiers) or "—",
                "obx_3_2": ", ".join(m.texts) or "—",
                "unit": m.unit or "—",
                "confidence": m.confidence.value,
                "note": m.note,
            })
        return rows

    def describe(self) -> dict:
        counts: dict[str, int] = {}
        for m in self.mappings:
            counts[m.confidence.value] = counts.get(m.confidence.value, 0) + 1
        return {
            "key": self.key,
            "display_name": self.display_name,
            "vendor": self.vendor,
            "summary": self.summary,
            "mappings": len(self.mappings),
            "scored_vitals": sorted({m.vital_key for m in self.mappings if m.vital_key}),
            "confidence_counts": counts,
            "verified_against_device": False,
            "verification_note": (
                "No mapping in this profile has been confirmed against a real device or a "
                "vendor specification. A bench run proves the pipeline, not the mapping."
            ),
            "handshake": self.handshake.as_dict(),
            "site_questions": list(self.site_questions),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  GENERIC — LOINC, which Orb already understands
# ══════════════════════════════════════════════════════════════════════════════
# Deliberately EMPTY of mappings. `ingestion_adapters._VITAL_CODE_MAP` already
# maps LOINC and the common English names, and this profile exists so that
# "generic" is a real, selectable choice rather than the absence of one.
GENERIC = VendorProfile(
    key="generic",
    display_name="Generic HL7 v2 (LOINC)",
    vendor="—",
    summary=(
        "No vendor translation. Whatever the gateway sends is mapped by Orb's own "
        "LOINC/common-name table. Correct for any gateway configured to emit LOINC, "
        "which most can be, and asking for LOINC at the gateway is the cheapest way "
        "to make this whole file unnecessary."
    ),
    handshake=GatewayHandshake(
        gateway="unspecified",
        orb_mode="listen",
        wiring="Unknown until the site visit. Orb listens by default; it can dial out instead.",
        notes=("Ask for LOINC-coded OBX-3 at the gateway before accepting a proprietary mapping.",),
    ),
    site_questions=(
        "Can the gateway be configured to emit LOINC codes in OBX-3? (If yes, no vendor profile is needed.)",
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PHILIPS IntelliVue (typically via IntelliBridge)
# ══════════════════════════════════════════════════════════════════════════════
# Philips patient monitors carry the ISO/IEEE 11073-10101 (MDC) nomenclature
# internally, and their HL7 output is normally MDC-coded: OBX-3 as
# `<numeric code>^<MDC_ mnemonic>^MDIL`, OBX-6 likewise as a dimension code.
#
# The MNEMONICS below are published nomenclature terms — high confidence in what
# the string means. The NUMERIC codes are reproduced without the standard in
# front of us and are marked accordingly; matching is attempted on BOTH, so a
# wrong numeric costs nothing as long as the mnemonic is present, which is the
# normal Philips output shape.
PHILIPS_INTELLIVUE = VendorProfile(
    key="philips_intellivue",
    display_name="Philips IntelliVue (IntelliBridge)",
    vendor="Philips",
    summary=(
        "IntelliVue bedside monitors (MP/MX series) exporting HL7 v2 ORU^R01, usually "
        "through an IntelliBridge interface or the IntelliVue Information Center. OBX-3 "
        "is normally MDC-coded (`150456^MDC_PULS_OXIM_SAT_O2^MDIL`) rather than LOINC."
    ),
    handshake=GatewayHandshake(
        gateway="Philips IntelliBridge / IntelliVue Information Center",
        orb_mode="listen",
        wiring=(
            "Usually the Philips side is configured with the receiver's IP and port and "
            "opens the TCP connection, so Orb LISTENS. Some Information Center "
            "deployments expect the receiver to dial in instead. Orb supports both "
            "(`ORB_MONITOR_MODE=listen|connect`); this is a site-visit question, not a "
            "code change."
        ),
        expects_ack=True,
        ack_mode="original",
        hl7_versions=("2.3", "2.4", "2.5"),
        notes=(
            "Per-message MLLP ACK expected; a sender that blocks on ACK will stop sending "
            "vitals if the receiver is slow, which is why Orb ACKs on ACCEPT, not on commit.",
            "Interval of periodic export (10 s / 1 min / 5 min) is a gateway setting and "
            "determines the ward's real message rate. Confirm it before sizing the queue.",
            "Numerics vs waveforms: Orb wants NUMERICS only. Waveform export is a different "
            "IntelliBridge feed and must not be pointed at this port.",
        ),
    ),
    sending_hints=("PHILIPS", "INTELLIVUE", "INTELLIBRIDGE", "IBE", "IIC", "PIIC"),
    coding_systems=("MDIL", "MDC", "99MDIL"),
    emission_coding_system="MDIL",
    unit_emission={
        "bpm": "264864^MDC_DIM_BEAT_PER_MIN^MDIL",
        "/min": "264928^MDC_DIM_RESP_PER_MIN^MDIL",
        "%": "262688^MDC_DIM_PERCENT^MDIL",
        "mmHg": "266016^MDC_DIM_MMHG^MDIL",
        "Cel": "268192^MDC_DIM_DEGC^MDIL",
    },
    unit_aliases={
        # Mnemonic dimensions (published terms) …
        "MDCDIMBEATPERMIN": "bpm",
        "MDCDIMRESPPERMIN": "/min",
        "MDCDIMPERCENT": "%",
        "MDCDIMMMHG": "mmHg",
        "MDCDIMDEGC": "Cel",
        "MDCDIMDEGF": "F",
        "MDCDIMKPA": "kPa",
        # … and the numeric dimension codes usually sent alongside them.
        # UNVERIFIED (see the banner); harmless if wrong, because an unrecognised
        # unit is passed through unchanged rather than replaced.
        "264864": "bpm",
        "264928": "/min",
        "262688": "%",
        "266016": "mmHg",
        "268192": "Cel",
        "268160": "F",
    },
    mappings=(
        ObxMapping(
            "HR", ("147842",), ("MDC_ECG_HEART_RATE", "MDC_ECG_CARD_BEAT_RATE", "HR"),
            Confidence.STANDARD_MNEMONIC, "bpm",
            "ECG-derived heart rate. Numeric code unverified; mnemonic is the reliable match.",
        ),
        ObxMapping(
            "HR", ("149530",), ("MDC_PULS_OXIM_PULS_RATE", "PULSE"),
            Confidence.STANDARD_MNEMONIC, "bpm",
            "Pulse rate from the oximeter. Used for HR only when no ECG rate is present in "
            "the same message. See `priority`.",
            priority=10,
        ),
        ObxMapping(
            "SpO2", ("150456",), ("MDC_PULS_OXIM_SAT_O2", "SPO2", "SAO2"),
            Confidence.STANDARD_MNEMONIC, "%",
            "Functional oxygen saturation.",
        ),
        ObxMapping(
            "SBP", ("150017",), ("MDC_PRESS_BLD_NONINV_SYS", "MDC_PRESS_CUFF_SYS", "NBPS"),
            Confidence.STANDARD_MNEMONIC, "mmHg",
            "Non-invasive systolic. NEWS2 scores systolic only.",
        ),
        ObxMapping(
            "DBP", ("150018",), ("MDC_PRESS_BLD_NONINV_DIA", "MDC_PRESS_CUFF_DIA", "NBPD"),
            Confidence.STANDARD_MNEMONIC, "mmHg",
        ),
        ObxMapping(
            None, ("150019",), ("MDC_PRESS_BLD_NONINV_MEAN", "MDC_PRESS_CUFF_MEAN", "NBPM"),
            Confidence.STANDARD_MNEMONIC, "mmHg",
            "Mean arterial pressure, recorded as an observation, NOT scored: NEWS2 takes "
            "systolic. Listed so it does not appear as an unknown identifier.",
        ),
        ObxMapping(
            "RR", ("151562",), ("MDC_RESP_RATE", "MDC_TTHOR_RESP_RATE", "RR"),
            Confidence.STANDARD_MNEMONIC, "/min",
            "Impedance respiration rate.",
        ),
        ObxMapping(
            "RR", ("151594",), ("MDC_AWAY_RESP_RATE", "AWRR"),
            Confidence.STANDARD_MNEMONIC, "/min",
            "Airway (capnography) respiration rate, present on ventilated/EtCO2 beds. "
            "Second choice behind impedance RR.",
            priority=10,
        ),
        ObxMapping(
            "Temp", ("150344",), ("MDC_TEMP", "MDC_TEMP_BLD", "TEMP"),
            Confidence.STANDARD_MNEMONIC, "Cel",
            "UNIT MATTERS: a Fahrenheit dimension must be translated before scoring. "
            "See unit_aliases.",
        ),
        ObxMapping(
            None, (), ("MDC_ECG_V_P_C_CNT", "MDC_ECG_AMPL_ST_I", "MDC_ECG_AMPL_ST_II"),
            Confidence.STANDARD_MNEMONIC, "",
            "Ectopics / ST amplitudes. Real observations, not NEWS2 parameters.",
        ),
    ),
    site_questions=(
        "Which IntelliVue models and software revision are on the pilot wards?",
        "Is the export via IntelliBridge (EC40/EC80) or the IntelliVue Information Center?",
        "Send one captured ORU^R01 from a live bed. One message settles every row in this table.",
        "Is OBX-3 MDC-coded (`^MDIL`) or has the gateway been configured for LOINC?",
        "Does the Philips side connect to us, or does it expect us to connect to it? Which port?",
        "What is the periodic export interval, and does it export on alarm as well?",
        "Are temperatures exported in °C or °F, and is the dimension in OBX-6 a code or a mnemonic?",
        "Does PID-3 carry the hospital MRN, and in which repetition/assigning authority?",
        "Does the DTM in OBX-14 carry a UTC offset? (If not, whose local time is it in?)",
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  GE CARESCAPE (typically via the Unity Network gateway)
# ══════════════════════════════════════════════════════════════════════════════
# GE's monitor family (CARESCAPE B450/B650/B850, and the older Solar/Dash units)
# exports through the Unity Network — historically a Unity Network ID / UNID
# device, more recently a CARESCAPE Gateway. Its HL7 identifiers are commonly
# SHORT LABELS (`HR`, `NBP-S`, `SPO2`) rather than a coded nomenclature.
#
# EVERY ROW HERE IS WEAKER THAN THE PHILIPS TABLE. The short labels are field
# convention, not a published standard, and GE gateways are frequently
# site-configured — two hospitals with the same hardware can send different
# strings. Treat this table as a starting hypothesis for the bench and expect the
# site visit to change it.
GE_CARESCAPE = VendorProfile(
    key="ge_carescape",
    display_name="GE CARESCAPE (Unity Network gateway)",
    vendor="GE HealthCare",
    summary=(
        "CARESCAPE B-series (and legacy Solar / Dash) monitors exporting HL7 v2 ORU^R01 "
        "through the Unity Network / CARESCAPE Gateway. OBX-3 identifiers are typically "
        "short mnemonic labels rather than LOINC or MDC codes, and are often site-configured."
    ),
    handshake=GatewayHandshake(
        gateway="GE Unity Network ID / CARESCAPE Gateway",
        orb_mode="listen",
        wiring=(
            "The Unity gateway is normally given a destination host/port and pushes to it, so "
            "Orb LISTENS. Some installations invert this. Confirm at the site visit; both "
            "directions are already supported."
        ),
        expects_ack=True,
        ack_mode="original",
        hl7_versions=("2.3", "2.4"),
        notes=(
            "GE gateways are commonly licensed PER BED or per concurrent connection. Confirm "
            "the pilot's two wards are inside the licence before bench day.",
            "Unity output is frequently configured per site; the identifier list is a "
            "deployment artefact, not a fixed vendor contract.",
            "Older Solar/Dash beds may route through the same gateway with different labels "
            "than the CARESCAPE beds on the same ward.",
        ),
    ),
    sending_hints=("GE", "GEMS", "CARESCAPE", "UNITY", "MARQUETTE", "SOLAR", "DASH", "DINAMAP", "CIC"),
    coding_systems=("GEMS", "99GE", "GE"),
    emission_coding_system="GEMS",
    unit_emission={
        "bpm": "BPM",
        "/min": "BR/MIN",
        "%": "%",
        "mmHg": "mmHg",
        "Cel": "DEGC",
    },
    unit_aliases={
        "BPM": "bpm",
        "BEATSMIN": "bpm",
        "BRMIN": "/min",
        "BRPM": "/min",
        "PERCENT": "%",
        "PCT": "%",
        "MMHG": "mmHg",
        "DEGC": "Cel",
        "C": "Cel",
        "DEGF": "F",
        "F": "F",
    },
    mappings=(
        ObxMapping(
            "HR", ("HR", "ECGHR", "HRATE"), ("HEART RATE", "ECG HEART RATE"),
            Confidence.CONVENTION, "bpm",
        ),
        ObxMapping(
            "HR", ("PULSE", "PR", "SPO2R", "SPO2PR"), ("PULSE RATE", "SPO2 PULSE RATE"),
            Confidence.INFERRED, "bpm",
            "Oximeter pulse rate. Used for HR only when no ECG rate is in the same message.",
            priority=10,
        ),
        ObxMapping(
            "SpO2", ("SPO2", "SAO2", "SPO2PCT"), ("OXYGEN SATURATION", "SPO2 %", "SAT"),
            Confidence.CONVENTION, "%",
        ),
        ObxMapping(
            "SBP", ("NBPS", "NBPSYS", "SYS", "BPS", "ARTS"), ("NBP SYSTOLIC", "SYSTOLIC"),
            Confidence.CONVENTION, "mmHg",
            "`ARTS` is the invasive arterial systolic, same clinical parameter for NEWS2, "
            "but confirm the site wants it scored.",
        ),
        ObxMapping(
            "DBP", ("NBPD", "NBPDIA", "DIA", "BPD", "ARTD"), ("NBP DIASTOLIC", "DIASTOLIC"),
            Confidence.CONVENTION, "mmHg",
        ),
        ObxMapping(
            None, ("NBPM", "NBPMEAN", "MEAN", "ARTM"), ("NBP MEAN", "MEAN ARTERIAL PRESSURE", "MAP"),
            Confidence.CONVENTION, "mmHg",
            "Mean arterial pressure, recorded, not scored.",
        ),
        ObxMapping(
            "RR", ("RESP", "RR", "RESPRATE"), ("RESPIRATION RATE", "RESPIRATORY RATE"),
            Confidence.CONVENTION, "/min",
        ),
        ObxMapping(
            "RR", ("AWRR",), ("AIRWAY RESPIRATION RATE",),
            Confidence.INFERRED, "/min",
            "Capnography-derived rate; second choice behind impedance RESP.",
            priority=10,
        ),
        ObxMapping(
            "Temp", ("TEMP", "TEMP1", "TMP", "TBLD", "TCORE"), ("TEMPERATURE", "TEMP 1", "CORE TEMP"),
            Confidence.CONVENTION, "Cel",
            "GE units are commonly written `DEGC`/`DEGF`. A Fahrenheit reading that is not "
            "translated scores as a fatal hypothermia or nothing at all.",
        ),
        ObxMapping(
            None, ("ST", "PVC", "STI", "STII"), ("ST LEVEL", "PVC COUNT"),
            Confidence.INFERRED, "",
            "Not NEWS2 parameters. Listed to keep the unknown-identifier report readable.",
        ),
    ),
    site_questions=(
        "Which gateway is in place: Unity Network ID (UNID), CARESCAPE Gateway, or a CIC Pro?",
        "Send one captured ORU^R01 from a live bed. Every row in this table is a guess until then.",
        "Is the OBX-3 identifier list site-configurable here, and has it been customised?",
        "Which bed identifier does the gateway send, and where does the hospital MRN appear?",
        "Is the licence per bed or per connection, and does it cover both pilot wards?",
        "Are legacy Solar/Dash beds on the same gateway, and do they use the same labels?",
        "Temperature units: °C or °F, and how is the unit written in OBX-6?",
        "Does the DTM carry a UTC offset?",
    ),
)


PROFILES: dict[str, VendorProfile] = {
    p.key: p for p in (GENERIC, PHILIPS_INTELLIVUE, GE_CARESCAPE)
}

#: Aliases an operator might plausibly type into `.env`.
_ALIASES = {
    "philips": "philips_intellivue",
    "intellivue": "philips_intellivue",
    "intellibridge": "philips_intellivue",
    "ge": "ge_carescape",
    "carescape": "ge_carescape",
    "unity": "ge_carescape",
    "none": "generic",
    "loinc": "generic",
    "": "generic",
}

#: Every vendor table merged, used ONLY when auto-detection cannot name a vendor.
#: Safe because the two tables share no identifier (a test asserts it); still
#: reported as `unnamed_vendor` so nobody reads a scorecard thinking the device
#: was identified.
_COMBINED_MAPPINGS = PHILIPS_INTELLIVUE.mappings + GE_CARESCAPE.mappings
COMBINED = VendorProfile(
    key="unnamed_vendor",
    display_name="Unnamed vendor (Philips + GE tables tried together)",
    vendor="unknown",
    summary=(
        "Auto-detection could not name the sender, so both vendor tables are consulted. "
        "This is a fallback for a bench, not a configuration to run a pilot on: name the "
        "vendor in ORB_MONITOR_PROFILE once the site visit has happened."
    ),
    handshake=GENERIC.handshake,
    mappings=_COMBINED_MAPPINGS,
    unit_aliases={**GE_CARESCAPE.unit_aliases, **PHILIPS_INTELLIVUE.unit_aliases},
    site_questions=("Who actually made the device sending these messages?",),
)


def profile_for(name: Optional[str]) -> VendorProfile:
    """Resolve a configured name to a profile. Unknown names → GENERIC.

    Unknown is NOT fatal: a typo in `.env` must not stop a ward's monitor feed —
    it degrades to "map only what LOINC maps", which is the pre-profile
    behaviour. The caller logs the fallback.
    """
    key = (name or "").strip().lower()
    key = _ALIASES.get(key, key)
    return PROFILES.get(key, GENERIC)


def is_known_profile(name: Optional[str]) -> bool:
    key = (name or "").strip().lower()
    return _ALIASES.get(key, key) in PROFILES


@dataclass(frozen=True)
class Detection:
    profile: VendorProfile
    reason: str
    named_vendor: bool           # False = we are guessing, and say so


def detect(sending_app: str = "", sending_facility: str = "",
           coding_systems: Iterable[str] = ()) -> Detection:
    """Guess the vendor from MSH-3/MSH-4 and the OBX-3 coding system.

    Cheap, string-only, and deliberately conservative: when nothing matches we
    return the COMBINED fallback and say `named_vendor=False`, so every surface
    that reports it can show "unnamed" rather than asserting a make.
    """
    haystack = f"{sending_app or ''} {sending_facility or ''}".upper()
    # Tokens, because MSH-3/4 are punctuated identifiers (`GE_UNITY_ID`,
    # `CARESCAPE^B650`). A SHORT hint must match a whole token — bare substring
    # matching on "GE" would claim a GE monitor from a facility called SURGERY.
    # A long, distinctive hint may match a substring, because vendors really do
    # run words together (`PhilipsIntelliBridge`).
    tokens = {t for t in re.split(r"[^A-Z0-9]+", haystack) if t}
    for profile in (PHILIPS_INTELLIVUE, GE_CARESCAPE):
        for raw_hint in profile.sending_hints:
            hint = raw_hint.strip()
            if not hint:
                continue
            if hint in tokens or (len(hint) >= 5 and hint in haystack):
                return Detection(profile, f"MSH-3/4 contains {hint!r}", True)

    systems = {_norm(s) for s in coding_systems if s}
    for profile in (PHILIPS_INTELLIVUE, GE_CARESCAPE):
        for system in profile.coding_systems:
            if _norm(system) and _norm(system) in systems:
                # Weaker: MDIL only says "MDC nomenclature", which is Philips'
                # habit but is not exclusive to Philips.
                return Detection(
                    profile,
                    f"OBX-3.3 coding system {system!r} (nomenclature hint, not a vendor claim)",
                    False,
                )

    return Detection(COMBINED, "no vendor hint in MSH-3/4 or OBX-3.3", False)


def detect_from_message(message) -> Detection:
    """`detect` for an already-parsed `hl7v2.Hl7Message`. No re-parsing."""
    systems = []
    try:
        for obx in message.all("OBX")[:8]:
            system = obx.field(3, component=3)
            if system:
                systems.append(system)
    except Exception:
        systems = []
    return detect(getattr(message, "sending_application", "") or "",
                  getattr(message, "sending_facility", "") or "",
                  systems)


def all_profiles() -> list[VendorProfile]:
    return [GENERIC, PHILIPS_INTELLIVUE, GE_CARESCAPE]


def collision_report() -> list[str]:
    """Identifiers/texts that mean DIFFERENT vitals in different vendor tables.

    Must stay empty, or the COMBINED fallback would map by coin-toss. Asserted by
    `tests/test_vendor_profiles.py` rather than trusted.
    """
    seen: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for profile in (PHILIPS_INTELLIVUE, GE_CARESCAPE):
        for mapping in profile.mappings:
            for token in (*mapping.identifiers, *mapping.texts):
                key = _norm(token)
                if not key:
                    continue
                prior = seen.get(key)
                if prior is not None and prior[1] != (mapping.vital_key or ""):
                    problems.append(
                        f"{token!r}: {prior[0]} says {prior[1] or 'not-scored'}, "
                        f"{profile.key} says {mapping.vital_key or 'not-scored'}"
                    )
                seen.setdefault(key, (profile.key, mapping.vital_key or ""))
    return problems
