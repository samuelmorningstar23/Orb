"""The Pilot Scorecard is read by a hospital CIO and a champion, not by the
person who wrote the event log. Its sentences used to carry backticked event
kinds (`alert.fired`, `outcome.recorded`), a table name, a migration number and
"SHADOW_MODE §5.1", a reference to a document the reader does not have."""
import json

from services import shadow_scorecard_service as svc

_JARGON = ("`", "§", "SHADOW_MODE", ".recorded row", "alert.fired", "shadow_events",
           "migration ", "\u2014")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            # Machine fields (ids, keys, states) are not prose.
            if k in ("id", "key", "state", "method", "metric_key"):
                continue
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def test_scorecard_prose_carries_no_event_kinds_or_doc_references():
    card = svc.scorecard(days=14)
    # instrument.source names the database view for machines; the screen
    # prints its own label for it.
    card.get("instrument", {}).pop("source", None)
    prose_keys = ("metrics", "readiness", "instrument")
    found = []
    for key in prose_keys:
        for text in _strings(card.get(key)):
            for token in _JARGON:
                if token in text:
                    found.append((key, token, text[:120]))
    assert not found, json.dumps(found, indent=1)


def test_metric_definitions_name_no_event_kinds():
    for key, meta in svc.METRIC_DEFINITIONS.items() if hasattr(svc, "METRIC_DEFINITIONS") else []:
        for field in ("definition", "caveat"):
            assert "`" not in meta.get(field, ""), (key, field)
