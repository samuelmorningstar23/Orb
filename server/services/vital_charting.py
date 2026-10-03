"""The one rounding rule for a vital that is scored or shown anywhere in Orb.

Ruled 2026-10-02 (see the "SCORE THE VALUE AS CHARTED" block in
vigil_service.py, which is where the rule binds the NEWS2 points): every vital
is rounded HALF-UP to the precision of the RCP NEWS2 chart, whole numbers for
RR, SpO2, blood pressure and heart rate and tenths of a degree for temperature.
NEWS2 is scored on that number, its drivers print it, and every surface that
prints a vital beside a NEWS2 score, band or driver must print the same number,
or the screen shows "RR 20" beside a driver reading "Tachypnea (RR 21)".

Two traps this module exists to avoid, both of which shipped somewhere:

  * Python's round() and format() round half to EVEN, so f"{20.5:.0f}" is "20"
    while the chart (and a person reading a monitor) says 21.
  * Rounding the BINARY value. 38.05 is stored as 38.04999..., so f"{38.05:.1f}",
    Decimal(38.05) and JavaScript's toFixed(1) all give 38.0, though the device
    sent 38.05. The rounding here is done on repr(), the shortest decimal that
    reads back as the same float, which is the value as entered or transmitted.

The frontend twin of this rule is `chartedValue` in src/lib/vitals.ts.
"""
from __future__ import annotations

import math
import numbers
from decimal import Decimal, ROUND_HALF_UP

_QUANTUM = {0: Decimal("1"), 1: Decimal("0.1")}

#: What a vital that was not measured prints as. Never a number: a default such
#: as "SpO2 100%" for an unmeasured saturation is a fabricated reading.
NOT_RECORDED = "--"


def as_charted(value, dp: int = 0):
    """`value` rounded half-up to `dp` decimal places, as the chart records it.

    `dp=0` returns an int, `dp=1` a float with one decimal. None, and anything
    that is not a finite real number, passes through unchanged so its existing
    handling is not altered here.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, numbers.Real):
        return value
    if not math.isfinite(value):
        return value
    charted = Decimal(repr(float(value))).quantize(_QUANTUM[dp], rounding=ROUND_HALF_UP)
    return int(charted) if dp == 0 else float(charted)


def charted_text(value, dp: int = 0, unit: str = "", missing: str = NOT_RECORDED) -> str:
    """The charted value as text with its unit, or `missing` when there is none.

    Accepts a number or a numeric string (a sqlite row can hand back either).
    """
    if value is None or value == "" or isinstance(value, bool):
        return missing
    try:
        number = value if isinstance(value, numbers.Real) else float(value)
    except (TypeError, ValueError):
        return missing
    if not math.isfinite(number):
        return missing
    return f"{as_charted(number, dp):.{dp}f}{unit}"


def vitals_line(*, hr=None, sbp=None, dbp=None, spo2=None, temp=None, rr=None,
                sep: str = " · ") -> str:
    """The vitals snapshot line the Relay alerts print, from charted values.

    "HR 72 · BP 120/80 · SpO2 97% · Temp 38.1°C · RR 21". A missing vital
    prints NOT_RECORDED, never a stand-in number.
    """
    return sep.join([
        f"HR {charted_text(hr)}",
        f"BP {charted_text(sbp)}/{charted_text(dbp)}",
        f"SpO2 {charted_text(spo2, unit='%')}",
        f"Temp {charted_text(temp, 1, unit='°C')}",
        f"RR {charted_text(rr)}",
    ])
