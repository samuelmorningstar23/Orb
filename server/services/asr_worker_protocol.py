"""Private, bounded stdin framing for speech hints (never process arguments)."""

import json
from typing import TextIO

MAX_HINT_CHARS = 8192
MAX_HINT_WIRE_CHARS = MAX_HINT_CHARS * 12 + 32
STDIN_HINTS_FLAG = "--stdin-hints"


def encode_hints(hints: str) -> str:
    if not isinstance(hints, str) or len(hints) > MAX_HINT_CHARS:
        raise ValueError("Invalid speech hint payload")
    return json.dumps({"hints": hints}, ensure_ascii=True)


def read_hints(stream: TextIO) -> str:
    wire = stream.read(MAX_HINT_WIRE_CHARS + 1)
    if len(wire) > MAX_HINT_WIRE_CHARS:
        raise ValueError("Invalid speech hint payload")
    try:
        payload = json.loads(wire)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid speech hint payload") from exc
    if not isinstance(payload, dict) or set(payload) != {"hints"}:
        raise ValueError("Invalid speech hint payload")
    hints = payload["hints"]
    if not isinstance(hints, str):
        raise ValueError("Invalid speech hint payload")
    encode_hints(hints)
    return hints
