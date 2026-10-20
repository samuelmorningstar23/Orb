"""India's 36 states and union territories, by the two-letter code Orb uses.

One list, used in two places that must agree:

  * site.toml's `[site] state`, which an operator may write as a name
    ("Maharashtra") or a code ("MH"), in any case;
  * the seasonal calendar (site_config/seasonal_calendar.json), whose zones
    must between them name every code here exactly once.

The codes are fixed by the Pulse contract of 6 October 2026. Some differ from
the ones on vehicle plates (Odisha is OR here, not OD; Telangana is TG, not TS),
so a code is only ever read against this table, never guessed.
"""

from __future__ import annotations

import re

#: Code -> official name. Order is alphabetical by name, as an operator would
#: scan a list of states.
STATES: dict[str, str] = {
    "AN": "Andaman and Nicobar Islands",
    "AP": "Andhra Pradesh",
    "AR": "Arunachal Pradesh",
    "AS": "Assam",
    "BR": "Bihar",
    "CH": "Chandigarh",
    "CT": "Chhattisgarh",
    "DH": "Dadra and Nagar Haveli and Daman and Diu",
    "DL": "Delhi",
    "GA": "Goa",
    "GJ": "Gujarat",
    "HR": "Haryana",
    "HP": "Himachal Pradesh",
    "JK": "Jammu and Kashmir",
    "JH": "Jharkhand",
    "KA": "Karnataka",
    "KL": "Kerala",
    "LA": "Ladakh",
    "LD": "Lakshadweep",
    "MP": "Madhya Pradesh",
    "MH": "Maharashtra",
    "MN": "Manipur",
    "ML": "Meghalaya",
    "MZ": "Mizoram",
    "NL": "Nagaland",
    "OR": "Odisha",
    "PY": "Puducherry",
    "PB": "Punjab",
    "RJ": "Rajasthan",
    "SK": "Sikkim",
    "TN": "Tamil Nadu",
    "TG": "Telangana",
    "TR": "Tripura",
    "UP": "Uttar Pradesh",
    "UT": "Uttarakhand",
    "WB": "West Bengal",
}

# Names an operator may reasonably still type: former official names, and the
# two union territories that merged into DH in January 2020. Each maps to the
# one code that covers that place today. Nothing here is a guess at a city.
_FORMER_NAMES: dict[str, str] = {
    "orissa": "OR",
    "pondicherry": "PY",
    "uttaranchal": "UT",
    "nct of delhi": "DL",
    "national capital territory of delhi": "DL",
    "dadra and nagar haveli": "DH",
    "daman and diu": "DH",
}


def _normalise(text: str) -> str:
    """Lower case, '&' read as 'and', runs of spaces collapsed."""
    text = text.replace("&", " and ").lower()
    return re.sub(r"\s+", " ", text).strip(" .")


_BY_NAME: dict[str, str] = {
    **{_normalise(name): code for code, name in STATES.items()},
    **_FORMER_NAMES,
}


def resolve_state(value: object) -> str | None:
    """The canonical code for a state code or name, or None.

    Case does not matter, nor does '&' against 'and': "mh", "MH",
    "maharashtra" and "Maharashtra" all give "MH". Anything that is not one of
    the 36 gives None, never a nearest match: a wrong state is a wrong season
    on a clinical screen.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.upper() in STATES:
        return raw.upper()
    return _BY_NAME.get(_normalise(raw))


def state_name(code: str | None) -> str | None:
    """The official name for a canonical code, or None."""
    return STATES.get(code) if code else None
