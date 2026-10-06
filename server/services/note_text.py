"""A signed Scribe note as plain text.

A structured note is stored as JSON (one string per SOAP section). Anything
that quotes a note to a person (the pharmacist's Verify Queue, signed-note
extraction finding the sentence that names a drug) wants its words, in order,
not the JSON around them. Pure, no database.
"""
from __future__ import annotations

import json


def note_plain_text(structured_text) -> str:
    """A structured (JSON) note's string values, one per line, in order;
    anything that is not JSON, as it is."""
    if not structured_text:
        return ""
    try:
        parsed = json.loads(structured_text)
    except (TypeError, ValueError):
        return str(structured_text)
    parts: list[str] = []

    def walk(value):
        if isinstance(value, str):
            if value.strip():
                parts.append(value.strip())
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(parsed)
    return "\n".join(parts) if parts else str(structured_text)
