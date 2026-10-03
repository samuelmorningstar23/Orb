"""Nightly FHIR self-export — throttling, isolation, idempotency, retention.

This job was THREAD C in the 2026-08-01 deadlock: it held a shared default-executor
thread for ~11 s while serialising and writing 282 MB, and was caught in
_io_FileIO_write -> _Py_write_impl -> take_gil with the uvloop main thread starved
behind it.

These tests lock in the four properties that keep it from doing that again:
  1. it runs on a PRIVATE single-thread executor, never asyncio's default one;
  2. the throttled writer produces byte-identical output to json.dump but yields
     the GIL between bounded chunks — asserted by measuring event-loop lag;
  3. a restart/duplicate trigger inside the interval is a no-op, not a re-write;
  4. old bundles and orphaned .tmp files are pruned.
"""
import asyncio
import json
import os
import time
import uuid

import pytest

from database import db_session
import services.downtime_service as ds
from services.downtime_service import (
    _write_bundle_throttled,
    prune_fhir_self_exports,
    run_nightly_fhir_self_export,
    run_nightly_fhir_self_export_async,
)


_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW — and this module pays for its own violation.

    `run_nightly_fhir_self_export` selects `patients WHERE status='admitted'`
    and sleeps _PATIENT_SLEEP_S (250 ms, ORB_FHIR_EXPORT_PATIENT_SLEEP_MS)
    BETWEEN EACH PATIENT. 11 of the 21 tests here run a full export, so every
    admitted patient left behind by an EARLIER test in this file adds ~0.275 s
    to every export in every LATER test — the module was inflating itself, on
    top of the ~60 it inherited from upstream modules.

    Discharge on teardown; do not DELETE, because the vitals rows the bundles
    are built from legitimately reference these ids, and
    test_export_run_prunes_discharged_patients depends on a discharged patient
    still existing. The seeding happens in plain helpers rather than a fixture,
    so they register each id here and this autouse teardown discharges the
    batch."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


def _mk_patient(n_vitals: int = 200) -> str:
    """An admitted patient with enough vitals that its bundle spans many chunks."""
    pid = f"PATFX{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status, age, sex)
               VALUES (?, ?, ?, 'B1', 'Ward A', 'Sepsis', 'admitted', 61, 'F')""",
            (pid, "FHIR Export Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        for i in range(n_vitals):
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate)
                   VALUES (?, ?, 92, 118, 74, 96, 37.1, 18)""",
                (pid, f"2026-07-{(i % 28) + 1:02d}T{i % 24:02d}:00:00Z"),
            )
    _SEEDED.append(pid)
    return pid


# ── 1. Isolation: private executor, not the shared default pool ───────────────

def test_export_runs_on_private_single_thread_executor():
    """The whole point of the fix: this job must not be able to occupy a slot in
    asyncio's process-wide default executor (shared with Sage context prep, the
    vigil route helpers and scribe ASR)."""
    assert ds._EXPORT_EXECUTOR._max_workers == 1
    assert ds._EXPORT_EXECUTOR._thread_name_prefix == "orb-fhir-export"


def test_async_entry_point_uses_the_private_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    _mk_patient(n_vitals=20)

    seen = {}

    async def go():
        res = await run_nightly_fhir_self_export_async("test", min_interval_hours=0)
        return res

    # Capture the thread the work actually ran on.
    orig = ds._export_one_patient

    def spy(*a, **kw):
        import threading
        seen["thread"] = threading.current_thread().name
        return orig(*a, **kw)

    monkeypatch.setattr(ds, "_export_one_patient", spy)
    res = asyncio.run(go())
    assert res["count"] >= 1
    assert seen["thread"].startswith("orb-fhir-export"), seen
    # And explicitly NOT the default to_thread pool.
    assert "asyncio" not in seen["thread"]


# ── 2. The throttled writer is byte-for-byte json.dump ────────────────────────

def test_throttled_writer_is_byte_identical_to_json_dump(tmp_path):
    """Throttling must not change a single byte of the exported record. If this
    ever fails, the escrow copy is no longer the record."""
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "meta": {"source": "https://orb.health"},
        "timestamp": "2026-08-01T00:00:00+00:00",
        "entry": [
            {"fullUrl": f"https://orb.health/fhir/Observation/{i}",
             "resource": {"resourceType": "Observation", "id": str(i),
                          "valueQuantity": {"value": i * 1.5, "unit": "mmHg"},
                          "note": "unicode ✓ é 漢字"}}
            for i in range(500)
        ],
    }
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(bundle, separators=(",", ":")))

    actual = str(tmp_path / "actual.json")
    n = _write_bundle_throttled(bundle, actual, chunk_bytes=1024, chunk_sleep_s=0.0)

    assert open(actual, "rb").read() == expected.read_bytes()
    assert n == len(expected.read_bytes())


@pytest.mark.parametrize("bundle", [
    {"resourceType": "Bundle", "entry": []},
    {"resourceType": "Bundle"},
    {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": {"id": "x"}}]},
])
def test_throttled_writer_edge_cases_round_trip(tmp_path, bundle):
    p = str(tmp_path / "b.json")
    _write_bundle_throttled(bundle, p, chunk_bytes=16, chunk_sleep_s=0.0)
    back = json.loads(open(p).read())
    assert back["resourceType"] == "Bundle"
    assert back["entry"] == bundle.get("entry", [])


def test_throttled_writer_leaves_no_tmp_and_never_a_partial_file(tmp_path, monkeypatch):
    """A crash mid-write must not leave a truncated bundle where a valid one was."""
    good = {"resourceType": "Bundle", "entry": [{"resource": {"id": str(i)}} for i in range(50)]}
    final = str(tmp_path / "b.fhir.json")
    _write_bundle_throttled(good, final, 64, 0.0)
    original = open(final, "rb").read()

    import json as _json
    real_dumps = _json.dumps
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 10:
            raise RuntimeError("disk went away")
        return real_dumps(*a, **kw)

    monkeypatch.setattr("services.downtime_service.json.dumps", boom)
    with pytest.raises(RuntimeError):
        _write_bundle_throttled(good, final, 64, 0.0)

    assert open(final, "rb").read() == original, "previous good bundle was clobbered"
    assert not list(tmp_path.glob("*.tmp")), "orphaned temp file left behind"


def test_bundle_files_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    _mk_patient(n_vitals=5)
    run_nightly_fhir_self_export("test", min_interval_hours=0)
    files = list(tmp_path.glob("*.fhir.json"))
    assert files
    for f in files:
        assert oct(f.stat().st_mode)[-3:] == "600", f


# ── 3. It cannot starve the event loop ───────────────────────────────────────

def test_export_does_not_stall_the_event_loop(tmp_path, monkeypatch):
    """ACCEPTANCE: a 10 ms ticker keeps ticking through a full export.

    This is the regression test for the deadlock's Thread C. The unthrottled
    version measured p95 24 ms / max 58 ms of loop lag on an idle loop; the
    throttled one measures p95 ~1.5 ms / max ~20 ms. The bar here is deliberately
    loose (a CI box is noisy) but still far below the unthrottled behaviour.
    """
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    for _ in range(3):
        _mk_patient(n_vitals=400)

    lags: list[float] = []
    stop = False

    async def ticker():
        period = 0.010
        nxt = time.perf_counter() + period
        while not stop:
            await asyncio.sleep(max(0, nxt - time.perf_counter()))
            now = time.perf_counter()
            lags.append((now - nxt) * 1000.0)
            nxt = now + period

    async def go():
        nonlocal stop
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.2)
        lags.clear()
        res = await run_nightly_fhir_self_export_async("test", min_interval_hours=0)
        stop = True
        await asyncio.sleep(0.05)
        t.cancel()
        return res

    res = asyncio.run(go())
    assert res["count"] >= 3
    assert len(lags) > 50, "ticker did not get enough samples to judge"

    s = sorted(lags)
    p95 = s[int(len(s) * 0.95)]
    worst = s[-1]
    # The unthrottled implementation blew past these on a 30-patient run.
    assert p95 < 50, f"p95 loop lag {p95:.1f} ms — export is stalling the loop"
    assert worst < 400, f"max loop lag {worst:.1f} ms — export is stalling the loop"


def test_yield_points_actually_fire(tmp_path):
    """The throttling must come from real sleeps, not from luck. Writing with a
    tiny chunk size and a measurable sleep must take proportionally longer."""
    bundle = {"resourceType": "Bundle",
              "entry": [{"resource": {"id": str(i), "pad": "x" * 200}} for i in range(200)]}
    p = str(tmp_path / "b.json")

    t0 = time.perf_counter()
    _write_bundle_throttled(bundle, p, chunk_bytes=1024, chunk_sleep_s=0.0)
    fast = time.perf_counter() - t0

    t0 = time.perf_counter()
    _write_bundle_throttled(bundle, p, chunk_bytes=1024, chunk_sleep_s=0.005)
    slow = time.perf_counter() - t0

    assert slow > fast + 0.05, f"sleeps did not fire (fast={fast:.3f}s slow={slow:.3f}s)"


# ── 4. Idempotency: restarts and duplicate triggers are no-ops ───────────────

def test_second_run_within_interval_is_skipped(tmp_path, monkeypatch):
    """The bug this closes: main.py fired the export on `hours % 24 == 0` with
    hours starting at 0, so EVERY process restart re-wrote the whole 282 MB
    export 30 s into boot, on top of model loading."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    _mk_patient(n_vitals=10)

    first = run_nightly_fhir_self_export("test", min_interval_hours=0)
    assert first["count"] >= 1
    assert (tmp_path / ".last_fhir_export").exists()

    second = run_nightly_fhir_self_export("test", min_interval_hours=24)
    assert second.get("skipped") == "too_soon"
    assert second["count"] == 0


def test_first_ever_run_is_not_skipped(tmp_path, monkeypatch):
    """An appliance that was powered off overnight must still export on boot."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    _mk_patient(n_vitals=10)
    res = run_nightly_fhir_self_export("test", min_interval_hours=24)
    assert res.get("skipped") is None
    assert res["count"] >= 1


def test_stale_marker_allows_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    _mk_patient(n_vitals=10)
    run_nightly_fhir_self_export("test", min_interval_hours=0)
    marker = tmp_path / ".last_fhir_export"
    old = time.time() - 25 * 3600
    os.utime(marker, (old, old))
    res = run_nightly_fhir_self_export("test", min_interval_hours=24)
    assert res.get("skipped") is None
    assert res["count"] >= 1


def test_concurrent_trigger_is_refused_not_queued(tmp_path, monkeypatch):
    """A manual/admin trigger landing on top of the scheduled run must be a
    no-op rather than a second full 282 MB write."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    assert ds._EXPORT_LOCK.acquire(blocking=False)
    try:
        res = run_nightly_fhir_self_export("test", min_interval_hours=0)
        assert res.get("skipped") == "already_running"
        assert res["count"] == 0
    finally:
        ds._EXPORT_LOCK.release()


# ── 5. Bounded disk ──────────────────────────────────────────────────────────

def test_prune_removes_stale_bundles_and_orphaned_tmp(tmp_path):
    fresh = tmp_path / "PAT-FRESH.aaaaaaaa.fhir.json"
    stale = tmp_path / "PAT-STALE.bbbbbbbb.fhir.json"
    orphan = tmp_path / "PAT-CRASH.cccccccc.fhir.json.tmp"
    young_tmp = tmp_path / "PAT-LIVE.dddddddd.fhir.json.tmp"
    for p in (fresh, stale, orphan, young_tmp):
        p.write_text("{}")

    old = time.time() - 20 * 86400
    os.utime(stale, (old, old))
    os.utime(orphan, (old, old))

    res = prune_fhir_self_exports(tmp_path, retention_days=14)
    assert res["removed"] == 1
    assert res["tmp_removed"] == 1
    assert fresh.exists(), "a fresh bundle must never be pruned"
    assert not stale.exists()
    assert not orphan.exists()
    assert young_tmp.exists(), "a .tmp from a live run must not be reaped"


def test_export_run_prunes_discharged_patients(tmp_path, monkeypatch):
    """A discharged patient's bundle stops being refreshed, so it ages out —
    which is the disk fix AND the PHI-minimisation fix."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    pid = _mk_patient(n_vitals=10)
    run_nightly_fhir_self_export("test", min_interval_hours=0)
    files = sorted(tmp_path.glob("*.fhir.json"))
    assert files

    with db_session() as db:
        db.execute("UPDATE patients SET status='discharged' WHERE id = ?", (pid,))

    # Age every existing bundle past retention, then run again.
    old = time.time() - 30 * 86400
    for f in files:
        os.utime(f, (old, old))
    res = run_nightly_fhir_self_export("test", min_interval_hours=0)
    assert res["pruned"]["removed"] >= 1
    remaining = {f.name for f in tmp_path.glob("*.fhir.json")}
    gone = [f.name for f in files if pid in f.name]
    for name in gone:
        assert name not in remaining, f"{name} should have been pruned after discharge"


def test_since_days_bounds_bundle_size_when_explicitly_enabled(tmp_path, monkeypatch):
    """ORB_FHIR_EXPORT_SINCE_DAYS is the escape hatch for unbounded bundle growth
    (vitals accrue ~2,000 rows/patient/day, so a bundle grew 1 MB -> 9.4 MB in
    three days). It is OFF by default on purpose — truncating a clinical escrow
    copy is a safety decision, not a disk tweak — so this proves the opt-in path
    actually works rather than silently exporting everything."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    pid = f"PATSINCE{uuid.uuid4().hex[:4].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status, age, sex)
               VALUES (?, ?, ?, 'B2', 'Ward A', 'CAP', 'admitted', 55, 'M')""",
            (pid, "Since Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        _SEEDED.append(pid)
        for ts in ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z",
                   "2099-01-01T00:00:00Z", "2099-01-02T00:00:00Z"):
            db.execute(
                "INSERT INTO vitals (patient_id, timestamp, heart_rate) VALUES (?, ?, 88)",
                (pid, ts),
            )

    def count_obs():
        f = [x for x in tmp_path.glob("*.fhir.json") if pid in x.name][0]
        b = json.loads(f.read_text())
        return sum(1 for e in b["entry"]
                   if e["resource"]["resourceType"] == "Observation")

    monkeypatch.setattr(ds, "_SINCE_DAYS", None)
    run_nightly_fhir_self_export("test", min_interval_hours=0)
    full = count_obs()

    # A cutoff between the 2020 and 2099 rows must drop the ancient ones.
    monkeypatch.setattr(ds, "_SINCE_DAYS", "365")
    run_nightly_fhir_self_export("test", min_interval_hours=0)
    bounded = count_obs()

    assert full == 4, full
    assert bounded == 2, f"since-cutoff did not bound the bundle ({bounded} of {full})"


def test_bad_since_days_falls_back_to_full_history(tmp_path, monkeypatch):
    """A typo in the env var must not silently truncate the record."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    _mk_patient(n_vitals=10)
    monkeypatch.setattr(ds, "_SINCE_DAYS", "not-a-number")
    res = run_nightly_fhir_self_export("test", min_interval_hours=0)
    assert res["count"] >= 1
    assert res["bytes_written"] > 0


def test_prune_disabled_when_retention_zero(tmp_path):
    stale = tmp_path / "PAT-OLD.eeeeeeee.fhir.json"
    stale.write_text("{}")
    old = time.time() - 999 * 86400
    os.utime(stale, (old, old))
    res = prune_fhir_self_exports(tmp_path, retention_days=0)
    assert res["removed"] == 0
    assert stale.exists()


# ── 6. Still does the job ────────────────────────────────────────────────────

def test_export_writes_valid_bundles_and_reports_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    pid = _mk_patient(n_vitals=50)
    res = run_nightly_fhir_self_export("test", min_interval_hours=0)

    assert res["count"] >= 1
    assert res["bytes_written"] > 0
    assert res["duration_s"] >= 0
    files = [f for f in tmp_path.glob("*.fhir.json") if pid.replace("-", "-") in f.name]
    assert files, f"no bundle written for {pid}"
    bundle = json.loads(files[0].read_text())
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    obs = [e for e in bundle["entry"]
           if e["resource"]["resourceType"] == "Observation"]
    assert len(obs) >= 50, "vitals did not make it into the bundle"


def test_one_bad_patient_does_not_abort_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    good = _mk_patient(n_vitals=5)
    bad = _mk_patient(n_vitals=5)

    real = ds._export_one_patient

    def flaky(pid, *a, **kw):
        if pid == bad:
            raise RuntimeError("simulated bundle failure")
        return real(pid, *a, **kw)

    monkeypatch.setattr(ds, "_export_one_patient", flaky)
    res = run_nightly_fhir_self_export("test", min_interval_hours=0)

    assert any(f["patient_id"] == bad for f in res["failed"])
    assert res["count"] >= 1
    assert any(good in f.name for f in tmp_path.glob("*.fhir.json"))
