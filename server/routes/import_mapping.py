"""Shared, side-effect-free parsing for CSV import mapping form fields."""

import json

from fastapi import HTTPException


def parse_mapping(mapping: str) -> dict[str, str]:
    """Parse {orb_field: csv_header}, preserving both import routes' contract."""
    try:
        parsed = json.loads(mapping or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"mapping must be valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422, detail="mapping must be a JSON object of {orb_field: csv_column}"
        )
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if value is None or value == "":
            continue
        if not isinstance(key, str) or not isinstance(value, str):
            raise HTTPException(
                status_code=422, detail="mapping keys and values must both be strings"
            )
        out[key] = value
    return out
