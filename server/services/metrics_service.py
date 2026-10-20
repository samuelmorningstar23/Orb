"""Orb Hospital OS — Prometheus metrics (dependency-free exposition).

This module implements the Prometheus text exposition format by hand so that
scraping works WITHOUT adding `prometheus_client` (or any new pip dependency) to
the install. It provides:

  * A tiny in-process counter/gauge registry (`inc()` / `set_gauge()`) other
    modules can call cheaply (e.g. bump a request counter in middleware).
  * `render_metrics()` which returns a `text/plain; version=0.0.4` metrics body
    combining the registry with a handful of freshly-sampled gauges (process
    uptime, DB size on disk, cheap row COUNTs, LLM queue depth).

Design rules:
  * DEPENDENCY-FREE — only stdlib + Orb's own modules.
  * DEFENSIVE — every sampled metric is wrapped so a single failing query (or an
    absent table on a fresh DB) never breaks the whole /metrics endpoint. A
    failed sample is simply omitted rather than raising.
  * CHEAP — only O(1)/COUNT queries; no scans of PHI content.

NOTE: /metrics exposes coarse operational counts (number of patients, orders,
audit rows, queue depth) but never PHI. It is conventionally scraped
unauthenticated; the router (routes/metrics_routes.py) documents the trade-off.
"""

from __future__ import annotations

import threading
import time

# Process start time, captured at import. Uptime is (now - this).
_PROCESS_START = time.time()

# ── Tiny metric registry ────────────────────────────────────────────────────
# Keyed by (metric_name, sorted label tuple). Values are floats. A lock keeps
# concurrent inc()/set_gauge() calls from racing (FastAPI runs a threadpool).
_LOCK = threading.Lock()
_LabelKey = tuple[tuple[str, str], ...]
_counters: dict[tuple[str, _LabelKey], float] = {}
_gauges: dict[tuple[str, _LabelKey], float] = {}
# Remember declared HELP/TYPE per metric name so the exposition is well-formed.
_meta: dict[str, tuple[str, str]] = {}  # name -> (help_text, metric_type)


def _label_key(labels: dict[str, str] | None) -> _LabelKey:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _declare(name: str, metric_type: str, help_text: str) -> None:
    _meta.setdefault(name, (help_text, metric_type))


def inc(
    name: str, value: float = 1.0, labels: dict[str, str] | None = None, help_text: str = ""
) -> None:
    """Increment a counter (creating it on first use). Safe to call from anywhere."""
    key = (name, _label_key(labels))
    with _LOCK:
        _declare(name, "counter", help_text or f"Orb counter {name}")
        _counters[key] = _counters.get(key, 0.0) + value


def set_gauge(
    name: str, value: float, labels: dict[str, str] | None = None, help_text: str = ""
) -> None:
    """Set a gauge to an absolute value (creating it on first use)."""
    key = (name, _label_key(labels))
    with _LOCK:
        _declare(name, "gauge", help_text or f"Orb gauge {name}")
        _gauges[key] = float(value)


# ── Exposition formatting ───────────────────────────────────────────────────
def _format_labels(label_key: _LabelKey) -> str:
    if not label_key:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in label_key)
    return "{" + inner + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_registry() -> list[str]:
    """Emit all registry counters + gauges grouped by metric name with HELP/TYPE."""
    lines: list[str] = []
    with _LOCK:
        counters = dict(_counters)
        gauges = dict(_gauges)
        meta = dict(_meta)

    # Group samples by metric name so # HELP / # TYPE are printed once each.
    grouped: dict[str, list[tuple[_LabelKey, float]]] = {}
    for (name, lk), val in {**counters, **gauges}.items():
        grouped.setdefault(name, []).append((lk, val))

    for name in sorted(grouped):
        help_text, mtype = meta.get(name, (f"Orb metric {name}", "untyped"))
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for lk, val in grouped[name]:
            lines.append(f"{name}{_format_labels(lk)} {_num(val)}")
    return lines


def _num(v: float) -> str:
    """Render a float without a trailing .0 for integers (nicer Prometheus text)."""
    if v == int(v):
        return str(int(v))
    return repr(v)


def _emit(lines: list[str], name: str, mtype: str, help_text: str, value: float) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {mtype}")
    lines.append(f"{name} {_num(value)}")


def _count(query: str) -> int | None:
    """Run a cheap COUNT query defensively; return None if it cannot be sampled."""
    try:
        from database import get_db  # local import: avoid import cost at module load

        conn = get_db()
        try:
            row = conn.execute(query).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except Exception:
        return None


def _db_size_bytes() -> int | None:
    """Total on-disk size of the SQLite DB plus its -wal/-shm sidecars."""
    try:
        import os

        from config import DB_PATH

        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = f"{DB_PATH}{suffix}"
            if os.path.exists(path):
                total += os.path.getsize(path)
        return total
    except Exception:
        return None


def _llm_queue_depth() -> tuple[int | None, int | None]:
    """(queued_items, active_workers) for the LLM queue, without touching the loop.

    The queue is lazily created; if it has never been used `_queue` is None and
    the depth is simply 0. qsize() is a safe, synchronous read on asyncio queues.
    """
    try:
        from services.llm_queue_service import llm_queue

        depth = 0
        q = getattr(llm_queue, "_queue", None)
        if q is not None:
            depth = q.qsize()
        active = int(getattr(llm_queue, "active_workers", 0))
        return depth, active
    except Exception:
        return None, None


def _llm_long_holds() -> int | None:
    """Holds that outran the lane-hold budget since boot.

    The background lane is one slot wide, so this counter — not queue depth —
    is what says "nothing could run". A waiter that never got in records a
    queue timeout whether the queue was fifty deep or one caller sat on an idle
    lane for five minutes; only this tells the two apart.
    """
    try:
        from services.llm_queue_service import llm_queue

        return int(getattr(llm_queue, "long_holds", 0))
    except Exception:
        return None


def render_metrics() -> str:
    """Return the full Prometheus exposition body as a string (text/plain).

    Combines the live in-process registry with freshly-sampled operational
    gauges. Each sampled metric is guarded independently so one failure never
    blanks the endpoint.
    """
    lines: list[str] = []

    # Process uptime — always available.
    try:
        uptime = max(0.0, time.time() - _PROCESS_START)
        _emit(
            lines,
            "orb_process_uptime_seconds",
            "gauge",
            "Seconds since this Orb process started.",
            uptime,
        )
    except Exception:
        pass

    # DB size on disk (main + WAL + SHM).
    size = _db_size_bytes()
    if size is not None:
        _emit(
            lines,
            "orb_db_size_bytes",
            "gauge",
            "Total size in bytes of the SQLite database and its WAL/SHM sidecars.",
            size,
        )

    # Cheap row counts. Each independently guarded.
    row_counts = (
        ("orb_patients_total", "Number of patient records.", "SELECT COUNT(*) FROM patients"),
        ("orb_orders_total", "Number of CPOE orders.", "SELECT COUNT(*) FROM orders"),
        (
            "orb_audit_rows_total",
            "Number of audit_log rows (append-only).",
            "SELECT COUNT(*) FROM audit_logs",
        ),
        (
            "orb_active_admissions",
            "Number of currently admitted patients.",
            "SELECT COUNT(*) FROM patients WHERE status = 'admitted'",
        ),
    )
    for name, help_text, query in row_counts:
        val = _count(query)
        if val is not None:
            _emit(lines, name, "gauge", help_text, val)

    # LLM queue depth + active workers.
    depth, active = _llm_queue_depth()
    if depth is not None:
        _emit(
            lines,
            "orb_llm_queue_depth",
            "gauge",
            "Number of tasks waiting in the LLM priority queue.",
            depth,
        )
    if active is not None:
        _emit(lines, "orb_llm_active_workers", "gauge", "Number of in-flight LLM workers.", active)
    long_holds = _llm_long_holds()
    if long_holds is not None:
        _emit(
            lines,
            "orb_llm_lane_long_holds_total",
            "counter",
            "Lane acquisitions held past the lane-hold budget since boot. "
            "The background lane is one slot wide, so this is the measure of "
            "'nothing else could run'. Queue depth is not.",
            long_holds,
        )

    # Everything registered by the app at runtime (request counters, etc.).
    lines.extend(_render_registry())

    return "\n".join(lines) + "\n"


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
