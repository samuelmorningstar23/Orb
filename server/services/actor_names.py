"""Names for user ids, resolved at READ time.

The audit chain stores who acted as ``actor_user_id`` and ``actor_role``, and
the hash covers exactly those columns, so a name is never written into a
chained row: a renamed user would otherwise change history, and a name written
later would break the chain. Screens that show the log look the names up here
when they read it. A user who no longer exists reads as None, never as a guess.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def names_for(db: Any, user_ids: Iterable[str | None]) -> dict[str, str]:
    """{user_id: name} for every id that is a row in users. One query."""
    ids = sorted({str(u) for u in user_ids if u})
    if not ids:
        return {}
    out: dict[str, str] = {}
    # SQLite caps bound parameters; 500 at a time stays well inside it.
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = db.execute(
            "SELECT id, name FROM users WHERE id IN ({})".format(",".join("?" * len(chunk))),
            chunk,
        ).fetchall()
        for r in rows:
            out[r["id"]] = r["name"]
    return out


def attach_actor_names(
    db: Any, rows: list[dict], *, id_key: str = "actor_user_id", name_key: str = "actor_name"
) -> list[dict]:
    """Add ``name_key`` to each row from users, None when the user is gone."""
    names = names_for(db, (r.get(id_key) for r in rows))
    for r in rows:
        uid = r.get(id_key)
        r[name_key] = names.get(str(uid)) if uid else None
    return rows
