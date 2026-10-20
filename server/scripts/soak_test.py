"""Orb pilot-volume soak test.

Boots an ISOLATED server instance (prod profile, no reload) against a COPY of
the database, drives sustained clinician-shaped load at pilot volume (2 wards /
~60 beds) for N minutes, and reports latency percentiles + SLO verdicts.

This harness is ruling #4 of docs/PILOT_ROADMAP.md made executable: "the
appliance does not fail" is engineered, and this is the instrument that
measures it. The load mix mirrors a real ward: continuous health probes (the
canary), patient-board polling from station displays, chart opens, Relay
traffic, and the in-process vitals simulator + bots writing underneath.

Usage (from server/):
    .venv/bin/python scripts/soak_test.py --minutes 15
    .venv/bin/python scripts/soak_test.py --minutes 12 --pool     # ORB_DB_POOL=1
    .venv/bin/python scripts/soak_test.py --port 8010 --keep-db

SLOs asserted (exit 1 on breach):
    * /api/health: zero failed probes, p99 < 250 ms
    * interactive reads (board, chart, channels): p99 < 2000 ms, error rate < 0.5 %
    * server survives the whole window (no process exit)

Never points at the live DB: the copy lives in a temp dir and is deleted
unless --keep-db.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

SERVER_DIR = Path(__file__).resolve().parent.parent
PYTHON = str(SERVER_DIR / ".venv" / "bin" / "python")

HEALTH_SLO_P99_MS = 250.0
READ_SLO_P99_MS = 2000.0
READ_SLO_ERR_RATE = 0.005


class Series:
    def __init__(self, name: str):
        self.name = name
        self.lat_ms: list[float] = []
        self.errors = 0

    def record(self, ms: float | None):
        if ms is None:
            self.errors += 1
        else:
            self.lat_ms.append(ms)

    def pct(self, p: float) -> float:
        if not self.lat_ms:
            return float("nan")
        return (
            statistics.quantiles(self.lat_ms, n=1000)[max(0, min(998, int(p * 10) - 1))]
            if len(self.lat_ms) >= 20
            else sorted(self.lat_ms)[-1]
        )

    def summary(self) -> dict:
        n = len(self.lat_ms)
        total = n + self.errors
        return {
            "name": self.name,
            "requests": total,
            "errors": self.errors,
            "error_rate": (self.errors / total) if total else 0.0,
            "p50_ms": round(self.pct(50), 1) if n else None,
            "p95_ms": round(self.pct(95), 1) if n else None,
            "p99_ms": round(self.pct(99), 1) if n else None,
            "max_ms": round(max(self.lat_ms), 1) if n else None,
        }


# ── Stall diagnosis instrumentation ──────────────────────────────────────────
# Aggregates alone cannot answer "when did the slow requests happen, and what
# was the server doing at that moment" — the 2026-08-10 soaks produced ~15-20 s
# maxes with pristine p95s and no way to tell one stall window from scattered
# slowness. So: every slow/errored request is recorded with its offset into the
# run, and a request still in flight after STALL_DUMP_AFTER_S fires SIGUSR1 at
# the server (faulthandler is registered in main.py) so the Python stacks land
# in the server log WHILE the stall is happening, not after.
SLOW_SAMPLE_MS = 750.0
STALL_DUMP_AFTER_S = 3.0
STALL_DUMP_MIN_GAP_S = 15.0
STALL_DUMP_MAX = 8

RUN_T0: float | None = None
STALL_PID: int | None = None
SLOW_SAMPLES: list[dict] = []
_dump_state = {"last": 0.0, "count": 0}


def _run_offset_s() -> float:
    return round(time.monotonic() - RUN_T0, 1) if RUN_T0 is not None else -1.0


def _fire_stall_dump(url: str) -> None:
    now = time.monotonic()
    if (
        STALL_PID is None
        or _dump_state["count"] >= STALL_DUMP_MAX
        or now - _dump_state["last"] < STALL_DUMP_MIN_GAP_S
    ):
        return
    _dump_state["last"] = now
    _dump_state["count"] += 1
    print(
        f"[soak] STALL: >{STALL_DUMP_AFTER_S:.0f}s in flight at t+{_run_offset_s()}s"
        f" ({url}) — SIGUSR1 #{_dump_state['count']} → server log"
    )
    try:
        os.kill(STALL_PID, signal.SIGUSR1)
    except OSError:
        pass


async def timed(client: httpx.AsyncClient, series: Series, method: str, url: str, **kw):
    t0 = time.perf_counter()
    watchdog = asyncio.get_running_loop().call_later(STALL_DUMP_AFTER_S, _fire_stall_dump, url)
    try:
        r = await client.request(method, url, **kw)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code >= 500:
            series.record(None)
            SLOW_SAMPLES.append(
                {
                    "series": series.name,
                    "t_s": _run_offset_s(),
                    "ms": round(ms, 1),
                    "url": url,
                    "status": r.status_code,
                }
            )
            return None
        series.record(ms)
        if ms > SLOW_SAMPLE_MS:
            SLOW_SAMPLES.append(
                {
                    "series": series.name,
                    "t_s": _run_offset_s(),
                    "ms": round(ms, 1),
                    "url": url,
                    "status": r.status_code,
                }
            )
        return r
    except Exception as e:
        series.record(None)
        SLOW_SAMPLES.append(
            {
                "series": series.name,
                "t_s": _run_offset_s(),
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "url": url,
                "status": type(e).__name__,
            }
        )
        return None
    finally:
        watchdog.cancel()


async def worker_loop(stop: asyncio.Event, interval: float, fn):
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            await fn()
        except Exception:
            pass
        elapsed = time.monotonic() - t0
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.05, interval - elapsed))
        except TimeoutError:
            pass


def boot_server(db_copy: Path, port: int, pool: bool, log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    # The app's own env file (repo root): the DB copy is SQLCipher-encrypted,
    # so ORB_DB_ENCRYPTION_KEY must come along.
    env_file = SERVER_DIR.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line and line.startswith("ORB_"):
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    env.update(
        {
            "ORB_DB_PATH": str(db_copy),
            # config.py reads ORB_DEPLOYMENT_PROFILE — NOT "ORB_PROFILE". The wrong
            # name here meant every prior soak inherited the operator's dev profile
            # from .env (reloader parent + dev gates) while claiming a prod
            # measurement. With reload off, boot_server's pid is also the actual
            # worker, which is what the stall watchdog must SIGUSR1.
            "ORB_DEPLOYMENT_PROFILE": "prod",
            # …but prod profile also turns on bootstrap credential ENFORCEMENT,
            # which DEACTIVATES every account still holding a shipped default
            # password — including DOC001, which this harness logs in as. Left on,
            # the soak deactivates the accounts on its own DB copy and then fails
            # its own login. The profile is set here for the reloader, not for
            # credential policy, so the check is disabled for this throwaway
            # instance. (Learned the hard way on 2026-08-11, when a benchmark run
            # against the REAL dev DB locked every seeded account out of the app.)
            "ORB_BOOTSTRAP_CHECK_ENABLED": "0",
            "PORT": str(port),
            "ORB_PORT": str(port),
        }
    )
    # Isolated throwaway instance: an ephemeral JWT secret is correct (prod
    # profile rightly refuses to boot without one).
    env.setdefault("ORB_JWT_SECRET", os.urandom(32).hex())
    # Same for the discharge-portal signing key, which prod also requires.
    env.setdefault("ORB_DISCHARGE_JWT_SECRET", os.urandom(32).hex())
    env.pop("ORB_KNOWLEDGE_INPROCESS", None)
    if pool:
        env["ORB_DB_POOL"] = "1"
    else:
        env.pop("ORB_DB_POOL", None)
    log_f = open(log_path, "w")
    return subprocess.Popen(
        [PYTHON, "main.py"],
        cwd=str(SERVER_DIR),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )


async def wait_healthy(base: str, deadline_s: float = 90.0) -> bool:
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as c:
        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline_s:
            try:
                r = await c.get(f"{base}/api/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
    return False


def soak_admission_choice(choices: dict) -> tuple[str | None, str | None]:
    """The department and attending doctor a synthetic admission uses, from
    GET /api/admissions/options: the logged-in doctor's own department first,
    then the first covered one; an attending from that department first, then
    the first listed. (None, None) when the form offers nothing."""
    departments = choices.get("departments") or []
    department = choices.get("default_department") or (departments[0] if departments else None)
    doctors = choices.get("doctors") or []
    attending = next(
        (d["id"] for d in doctors if d.get("department") == department),
        doctors[0]["id"] if doctors else None,
    )
    return department, attending


def soak_admission_payload(idx: int, department: str, attending: str) -> dict:
    """One synthetic admission, in the shape POST /api/admissions/admit takes."""
    return {
        "name": f"Soak Patient {idx:03d}",
        "age": 40 + (idx % 40),
        "sex": "M" if idx % 2 else "F",
        "admission_source": "ED",
        "admission_type": "emergency",
        "department": department,
        "attending_doctor_id": attending,
        "chief_complaint": "Soak load, synthetic admission",
        "initial_diagnosis": "Soak load, synthetic admission",
        "ward": "W1" if idx % 2 else "W2",
        "bed": f"SK-{idx:03d}",
    }


async def run_soak(base: str, minutes: float, target_census: int) -> dict:
    global RUN_T0
    RUN_T0 = time.monotonic()
    stop = asyncio.Event()
    series = {
        k: Series(k)
        for k in (
            "health",
            "board",
            "chart",
            "channels",
            "messages_read",
            "message_post",
            "forecast",
            "ward_brief",
        )
    }
    rss_samples: list[int] = []

    login = httpx.AsyncClient(base_url=base, timeout=30.0, trust_env=False)
    r = await login.post("/api/auth/login", json={"employee_id": "DOC001", "password": "password"})
    r.raise_for_status()
    token = r.json().get("access_token") or r.json().get("token")
    headers = {"Authorization": f"Bearer {token}"}
    client = httpx.AsyncClient(base_url=base, timeout=30.0, headers=headers, trust_env=False)

    # Pilot volume: top up census to the target by admitting synthetic patients.
    # (/api/vigil/patients returns a plain list.) They come in through the
    # admission door, POST /api/admissions/admit, the way a ward admits: the
    # old POST /api/vigil/patients wrote patients with no admission row and is
    # retired (410). The department and attending come from the admission
    # form's own choices, the logged-in doctor's department first, so the
    # synthetic patients land on the board this load polls.
    board = await client.get("/api/vigil/patients?limit=200")
    have = len(board.json()) if board.status_code == 200 else 0
    options = await client.get("/api/admissions/options")
    department, attending = soak_admission_choice(
        options.json() if options.status_code == 200 else {}
    )
    admitted = 0
    idx = 0
    while department and attending and have + admitted < target_census and idx < target_census * 2:
        idx += 1
        resp = await client.post(
            "/api/admissions/admit", json=soak_admission_payload(idx, department, attending)
        )
        if resp.status_code in (200, 201):
            admitted += 1
        else:
            print(f"[soak] admission {idx} refused ({resp.status_code}): {resp.text[:200]}")
            break
    if not (department and attending):
        print(
            "[soak] no covered department or attending doctor on the admission form; "
            "census not topped up"
        )

    pts = await client.get("/api/vigil/patients?limit=200")
    patient_ids = [p["id"] for p in pts.json()] if pts.status_code == 200 else []
    chans = await client.get("/api/relay/channels")
    channel_ids = [c["id"] for c in chans.json()] if chans.status_code == 200 else []

    pid_i = 0
    ch_i = 0

    async def probe_health():
        await timed(client, series["health"], "GET", "/api/health")

    async def poll_board():
        await timed(client, series["board"], "GET", "/api/vigil/patients?limit=100")

    async def open_chart():
        nonlocal pid_i
        if not patient_ids:
            return
        pid = patient_ids[pid_i % len(patient_ids)]
        pid_i += 1
        await timed(client, series["chart"], "GET", f"/api/vigil/patients/{pid}")

    async def poll_channels():
        await timed(client, series["channels"], "GET", "/api/relay/channels")

    async def read_messages():
        nonlocal ch_i
        if not channel_ids:
            return
        ch = channel_ids[ch_i % len(channel_ids)]
        ch_i += 1
        await timed(
            client, series["messages_read"], "GET", f"/api/relay/channels/{ch}/messages?limit=30"
        )

    async def post_message():
        if not channel_ids:
            return
        ch = channel_ids[int(time.time()) % len(channel_ids)]
        await timed(
            client,
            series["message_post"],
            "POST",
            f"/api/relay/channels/{ch}/messages",
            json={"content": f"soak check {datetime.now(UTC).isoformat()}"},
        )

    async def poll_forecast():
        await timed(client, series["forecast"], "GET", "/api/forecast/hospital?days=3")

    async def poll_ward_brief():
        await timed(client, series["ward_brief"], "GET", "/api/sage/ward-brief")

    async def sample_rss():
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(SERVER_PID)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            rss_samples.append(int(out.stdout.strip() or 0))
        except Exception:
            pass

    tasks = [
        asyncio.create_task(worker_loop(stop, 0.5, probe_health)),
        asyncio.create_task(worker_loop(stop, 2.0, poll_board)),
        asyncio.create_task(worker_loop(stop, 2.0, poll_board)),  # 2 station displays
        asyncio.create_task(worker_loop(stop, 3.0, open_chart)),
        asyncio.create_task(worker_loop(stop, 5.0, poll_channels)),
        asyncio.create_task(worker_loop(stop, 4.0, read_messages)),
        asyncio.create_task(worker_loop(stop, 7.0, post_message)),
        asyncio.create_task(worker_loop(stop, 15.0, poll_forecast)),
        asyncio.create_task(worker_loop(stop, 20.0, poll_ward_brief)),
        asyncio.create_task(worker_loop(stop, 30.0, sample_rss)),
    ]

    await asyncio.sleep(minutes * 60)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.aclose()
    await login.aclose()

    return {
        "series": {k: s.summary() for k, s in series.items()},
        "census_start": have,
        "census_admitted": admitted,
        "rss_mb": {
            "min": round(min(rss_samples) / 1024, 1) if rss_samples else None,
            "max": round(max(rss_samples) / 1024, 1) if rss_samples else None,
        },
        "slow_samples": sorted(SLOW_SAMPLES, key=lambda s: s["t_s"]),
        "stall_dumps_fired": _dump_state["count"],
    }


SERVER_PID = 0


def main():
    global SERVER_PID
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--pool", action="store_true", help="run with ORB_DB_POOL=1")
    ap.add_argument("--census", type=int, default=60)
    ap.add_argument("--keep-db", action="store_true")
    ap.add_argument("--db", default=str(SERVER_DIR / "orb.db"))
    args = ap.parse_args()

    src_db = Path(args.db)
    assert src_db.exists(), f"source DB not found: {src_db}"
    tmp = Path(tempfile.mkdtemp(prefix="orb-soak-"))
    db_copy = tmp / "orb-soak.db"
    shutil.copy2(src_db, db_copy)
    # WAL/SHM siblings must come along or recovery discards recent txns.
    for suffix in ("-wal", "-shm"):
        s = Path(str(src_db) + suffix)
        if s.exists():
            shutil.copy2(s, Path(str(db_copy) + suffix))

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = SERVER_DIR.parent / "docs" / "perf"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp / "server.log"
    label = "pool" if args.pool else "percall"

    base = f"http://127.0.0.1:{args.port}"
    proc = boot_server(db_copy, args.port, args.pool, log_path)
    SERVER_PID = proc.pid
    globals()["STALL_PID"] = proc.pid
    print(f"[soak] server pid={proc.pid} port={args.port} mode={label} db={db_copy}")

    try:
        t_boot = time.monotonic()
        healthy = asyncio.run(wait_healthy(base))
        boot_s = round(time.monotonic() - t_boot, 1)
        if not healthy:
            print(f"[soak] FAIL: server never became healthy (see {log_path})")
            sys.exit(1)
        print(f"[soak] healthy in {boot_s}s — running {args.minutes} min at census {args.census}")

        result = asyncio.run(run_soak(base, args.minutes, args.census))
        result["mode"] = label
        result["minutes"] = args.minutes
        result["boot_to_healthy_s"] = boot_s
        result["server_exit_mid_run"] = proc.poll() is not None

        # ── SLO verdicts ──
        verdicts = []
        h = result["series"]["health"]
        verdicts.append(("health zero failures", h["errors"] == 0))
        verdicts.append(
            (
                f"health p99 < {HEALTH_SLO_P99_MS} ms",
                h["p99_ms"] is not None and h["p99_ms"] < HEALTH_SLO_P99_MS,
            )
        )
        for k in ("board", "chart", "channels", "messages_read"):
            s = result["series"][k]
            if s["requests"]:
                verdicts.append(
                    (
                        f"{k} p99 < {READ_SLO_P99_MS} ms",
                        s["p99_ms"] is not None and s["p99_ms"] < READ_SLO_P99_MS,
                    )
                )
                verdicts.append(
                    (f"{k} err rate < {READ_SLO_ERR_RATE:.1%}", s["error_rate"] < READ_SLO_ERR_RATE)
                )
        verdicts.append(("server survived window", not result["server_exit_mid_run"]))
        result["verdicts"] = [{"slo": s, "pass": ok} for s, ok in verdicts]
        passed = all(ok for _, ok in verdicts)

        json_path = out_dir / f"soak-{ts}-{label}.json"
        json_path.write_text(json.dumps(result, indent=2))

        md = [
            f"# Soak {ts}: {label} mode, {args.minutes} min, census {result['census_start']}+{result['census_admitted']}\n"
        ]
        md.append(
            f"Boot→healthy: {boot_s}s · RSS {result['rss_mb']['min']}–{result['rss_mb']['max']} MB\n"
        )
        md.append("| series | reqs | errs | p50 | p95 | p99 | max |")
        md.append("|---|---|---|---|---|---|---|")
        for s in result["series"].values():
            md.append(
                f"| {s['name']} | {s['requests']} | {s['errors']} | "
                f"{s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} | {s['max_ms']} |"
            )
        md.append("\n## SLO verdicts")
        for v in result["verdicts"]:
            md.append(f"- {'✅' if v['pass'] else '❌'} {v['slo']}")
        md.append(f"\nOverall: {'**PASS**' if passed else '**FAIL**'}\n")
        slow = result.get("slow_samples") or []
        if slow:
            md.append(
                f"\n## Slow/errored requests (> {SLOW_SAMPLE_MS:.0f} ms): "
                f"{len(slow)} samples, {result.get('stall_dumps_fired', 0)} stack dumps fired"
            )
            md.append("| t+ (s) | series | ms | status | url |")
            md.append("|---|---|---|---|---|")
            for s in slow[:40]:
                md.append(
                    f"| {s['t_s']} | {s['series']} | {s['ms']} | {s['status']} | {s['url']} |"
                )
            if len(slow) > 40:
                md.append(f"| … | {len(slow) - 40} more | | | |")
        md_path = out_dir / f"soak-{ts}-{label}.md"
        md_path.write_text("\n".join(md))
        print(f"[soak] report: {md_path}")
        print("\n".join(md))
        sys.exit(0 if passed else 1)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if not args.keep_db:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
