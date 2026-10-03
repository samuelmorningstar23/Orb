"""How a NEWS2 result is written in words, on every server surface.

The server twin of `news2Description` in src/lib/news2.ts. Same sentence, same
order, same separator: "NEWS2 9 (critical) · a single parameter scores 3 ·
SpO2 Scale 2". Two deliberate differences:

  * "SpO2", not "SpO₂". Every server string (Relay alerts, prompts, the
    downtime pack's text summary) spells it with a plain 2, and a prompt is
    better off without the subscript glyph.
  * " · incomplete set, minimum" when `complete` is False. An unmeasured
    parameter scores 0, so a partial observation set can only UNDER-score; the
    aggregate is a floor and the reader has to be told (compute_news2's
    `coverage_note` has the full explanation and the missing parameters).

WHY THIS EXISTS (2026-10-03). The 0-1 `score` on a NEWS2 result is a normalised
index of the ordinal aggregate, explicitly NOT a calibrated probability, and it
was still reaching five LLM prompts in a slot labelled "risk" ("RISK SCORE:
0.55", "CRITICAL risk 0.55", "Risk: Score 0.55"). A model reads 0.55 as a 55%
chance of something. Those slots now state the NEWS2 integer and band, and the
dozen hand-rolled "NEWS2 n (level)" labels across the Relay alert lanes, the
escalation chain, I-PASS, the downtime pack and the Sage assessment now take
their facts from here, so a single-parameter-3 or an incomplete set cannot be
said on one surface and dropped on the next.

Where a surface has only a stored `risk_scores` row there is no NEWS2 integer
to print (the row stores the index and the band). Such a surface prints the
band, never the index.
"""

from __future__ import annotations

from collections.abc import Mapping

NOT_RECORDED = "NEWS2 not recorded"
SINGLE_PARAM_3 = "a single parameter scores 3"
SCALE_2 = "SpO2 Scale 2"
INCOMPLETE = "incomplete set, minimum"


def news2_total(news2: Mapping | None):
    """The NEWS2 aggregate (the integer), or None when there is none."""
    if not news2:
        return None
    return news2.get("raw_news2", news2.get("raw_mews"))


def news2_qualifiers(news2: Mapping | None, *, scale: bool = True) -> list[str]:
    """The clauses that follow "NEWS2 n (level)", in news2Description's order.

    `scale=False` is for a surface that already states the SpO2 scale in its own
    words (I-PASS prints the 88-92% target, the downtime pack its own marker).

    `complete` must be exactly False to add the incomplete clause. A dict
    without the key (a hand-built result, a stored chain row) makes no claim
    about coverage either way, and saying "minimum" there would be inventing it.
    """
    if not news2:
        return []
    out = []
    if news2.get("single_param_3"):
        out.append(SINGLE_PARAM_3)
    if scale and news2.get("spo2_scale") == 2:
        out.append(SCALE_2)
    if news2.get("complete") is False:
        out.append(INCOMPLETE)
    return out


def news2_description(news2: Mapping | None, *, scale: bool = True) -> str:
    """The one sentence a NEWS2 result means: "NEWS2 9 (critical) · ...".

    Takes a compute_news2 / score_vitals_row result, or any dict with the same
    keys (`raw_news2`, `level`, `single_param_3`, `spo2_scale`, `complete`).
    `level` is printed as given: a caller holding a CfC-escalated level is
    responsible for saying so.
    """
    total = news2_total(news2)
    if total is None:
        return NOT_RECORDED
    level = news2.get("level")
    head = f"NEWS2 {total} ({level})" if level else f"NEWS2 {total}"
    return head + "".join(f" · {q}" for q in news2_qualifiers(news2, scale=scale))
