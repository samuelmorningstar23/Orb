#!/usr/bin/env python3
"""Orb heartbeat receiver — the end of the wire that actually pages somebody.

WHAT THIS IS
────────────
server/services/heartbeat_service.py POSTs a small no-PHI JSON document from
each appliance every few minutes. That beat pages nobody until something is
listening. This is that something: a single-file, STDLIB-ONLY server that runs
on any always-on box the operator controls (a $5 VPS, the standby Mac) with
nothing but python3:

    python3 ops/heartbeat_receiver.py --port 8787 --state /var/lib/orb-receiver/state.json \
        --max-silence 15 --ntfy-topic my-private-orb-topic

It alarms on exactly two things, matching what the sender can express:

  1. A beat ARRIVES carrying warn/critical alert codes (backup_stale, no_admin,
     default_credentials, loop_stalled, disk_low, ...). Deduplicated: the same
     alert set does not re-page on every 5-minute beat; a CHANGED set does, and
     a beat with everything clear sends one CLEARED notice.
  2. A beat DOES NOT arrive for --max-silence minutes. This is the dead-man
     half and the reason the receiver exists: a frozen appliance cannot report
     itself. Fires once per outage, plus one RECOVERED notice when the beat
     returns — never once per check.

Alarm channels (all optional, all can be combined; every alarm is also logged):
    --on-alert '<shell command>'   message arrives on the command's stdin
    --ntfy-topic <topic>           POST to https://ntfy.sh/<topic> (or a full URL)
    --smtp host:port:from:to       STARTTLS mail; password ONLY via env
                                   ORB_SMTP_PASSWORD (user defaults to <from>,
                                   override with ORB_SMTP_USER)

Test the whole pager path end to end, without an appliance, before relying on it:

    python3 ops/heartbeat_receiver.py --state /tmp/sim-state.json --simulate \
        --ntfy-topic my-private-orb-topic
    # Expected: exactly 4 notifications — alert, silent, recovered, cleared.

AUTHENTICATION — set the same values on both ends
─────────────────────────────────────────────────
No secrets on the command line and none in this file or the state file. Export
in the RECEIVER's environment the same values the appliance's .env carries:

    ORB_HEARTBEAT_TOKEN    beat must carry `Authorization: Bearer <token>`
    ORB_HEARTBEAT_SECRET   beat must carry X-Orb-Signature: sha256=<HMAC-SHA256
                           over the exact body bytes> — rejects a forged
                           "everything is fine" beat, the one message an
                           attacker would want to send on the appliance's behalf

Unset means unenforced (logged loudly at startup). Set both.

TLS: the sender refuses a plain-http URL by default, and it is right to — the
payload has no PHI but does describe the appliance's weak points. Either front
this with a real certificate (--tls-cert/--tls-key with certbot files, or any
reverse proxy), or run over a private overlay (Tailscale/WireGuard) and set
ORB_HEARTBEAT_ALLOW_INSECURE=1 on the appliance, deliberately, knowing why.
Self-signed will NOT work: the sender verifies certificates.

RUN IT AS A SERVICE
───────────────────
systemd (Linux VPS) — /etc/systemd/system/orb-heartbeat-receiver.service:

    [Unit]
    Description=Orb heartbeat receiver (pages the operator)
    After=network-online.target
    Wants=network-online.target

    [Service]
    # Token/secret live in this root-owned, mode-600 file, never in the unit:
    #     ORB_HEARTBEAT_TOKEN=...
    #     ORB_HEARTBEAT_SECRET=...
    EnvironmentFile=/etc/orb-receiver.env
    ExecStart=/usr/bin/python3 /opt/orb/ops/heartbeat_receiver.py \
        --port 8787 --state /var/lib/orb-receiver/state.json \
        --max-silence 15 --ntfy-topic CHANGE-ME
    DynamicUser=yes
    StateDirectory=orb-receiver
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target

    # systemctl daemon-reload && systemctl enable --now orb-heartbeat-receiver

launchd (the standby Mac) — ~/Library/LaunchAgents/com.orbhospital.heartbeat-receiver.plist.
As with install-backup-schedule.sh, the secrets are NOT written into the plist:
the job sources a mode-600 env file so they live in exactly one place on disk.

    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key><string>com.orbhospital.heartbeat-receiver</string>
      <key>ProgramArguments</key>
      <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>set -a; . "$HOME/.orb-receiver.env"; set +a; exec /usr/bin/python3 /opt/orb/ops/heartbeat_receiver.py --port 8787 --state "$HOME/orb-receiver/state.json" --max-silence 15 --ntfy-topic CHANGE-ME</string>
      </array>
      <key>RunAtLoad</key><true/>
      <key>KeepAlive</key><true/>
      <key>StandardOutPath</key><string>/tmp/orb-heartbeat-receiver.log</string>
      <key>StandardErrorPath</key><string>/tmp/orb-heartbeat-receiver.log</string>
    </dict>
    </plist>

    # launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.orbhospital.heartbeat-receiver.plist

WHAT IT STORES
──────────────
The state file is plain JSON, one entry per appliance_id (a pseudonymous hash —
the sender never puts a hostname or a hospital name on the wire): last-seen
time, last severity, last alert set, and the two dedup flags. No secrets, no
PHI, nothing sensitive; it exists so a receiver restart neither re-pages old
alerts nor forgets an outage in progress. Written atomically (tmp + rename) so
a power cut mid-write cannot destroy it.

GET /status (same bearer token, if set) returns the per-appliance summary so
the operator can check the pager is alive without waiting for an alarm.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("orb.heartbeat.receiver")

SCHEMA_PREFIX = "orb.heartbeat/"

# A real beat is 1–2 KB. Anything bigger is not a beat.
MAX_BODY_BYTES = 128 * 1024

# Shapes mirror the sender's own guarantees (heartbeat_service._SAFE_STR,
# sanitise_site, _ALERT_CODES). Codes are validated by SHAPE, not against a
# copied list: a new alert code added to the sender must page immediately, not
# wait for this file to learn its name.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_SITE = re.compile(r"^[a-z0-9-]{0,32}$")
_SAFE_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_SAFE_UTC = re.compile(r"^[0-9TZ:.+-]{0,40}$")
_SEVERITIES = ("ok", "warn", "critical")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── Beat validation ──────────────────────────────────────────────────────────
class BeatRejected(Exception):
    """A beat we refuse, with the HTTP status and a FIXED reason enum.

    Reasons are fixed strings, never echoes of the input — the response goes
    back onto the network and this side keeps the sender's no-reflection
    discipline.
    """

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def validate_beat(raw: bytes, auth_header: Optional[str], sig_header: Optional[str],
                  token: str = "", secret: str = "") -> dict[str, Any]:
    """Authenticate and shape-check one beat; return the normalised fields.

    Auth is checked BEFORE the body is parsed: an unauthenticated caller learns
    nothing about what this endpoint considers well-formed.
    """
    if token:
        presented = (auth_header or "").strip()
        if not hmac.compare_digest(presented, f"Bearer {token}"):
            raise BeatRejected(401, "bad_token")
    if secret:
        presented = (sig_header or "").strip()
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(presented, expected):
            raise BeatRejected(401, "bad_signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BeatRejected(400, "bad_json")
    if not isinstance(payload, dict):
        raise BeatRejected(400, "bad_json")

    schema = payload.get("schema")
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
        raise BeatRejected(400, "bad_schema")

    appliance_id = payload.get("appliance_id")
    if not isinstance(appliance_id, str) or not _SAFE_ID.match(appliance_id):
        raise BeatRejected(400, "bad_appliance_id")

    severity = payload.get("severity")
    if severity not in _SEVERITIES:
        raise BeatRejected(400, "bad_severity")

    alerts = payload.get("alerts", [])
    if not isinstance(alerts, list) or not all(
            isinstance(a, str) and _SAFE_CODE.match(a) for a in alerts):
        raise BeatRejected(400, "bad_alerts")

    site = payload.get("site", "")
    if not isinstance(site, str) or not _SAFE_SITE.match(site):
        raise BeatRejected(400, "bad_site")

    sent_utc = payload.get("sent_utc", "")
    if not isinstance(sent_utc, str) or not _SAFE_UTC.match(sent_utc):
        sent_utc = ""

    return {
        "appliance_id": appliance_id,
        "site": site,
        "severity": severity,
        "alerts": sorted(set(alerts)),
        "sent_utc": sent_utc,
    }


# ── State (atomic, restart-safe) ─────────────────────────────────────────────
class StateStore:
    """One JSON file: {appliance_id: {last_seen_unix, severity, alerts, ...}}.

    Atomic write (tmp + os.replace + fsync) because this file is the pager's
    memory: lose it and a restart re-pages every standing alert or forgets an
    outage in progress. A corrupt or missing file starts fresh rather than
    refusing to run — a receiver that will not boot pages nobody.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("State file unreadable, starting fresh: %s", self.path)
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


# ── Alarm channels ───────────────────────────────────────────────────────────
def _notify_shell(command: str, subject: str, message: str, urgent: bool) -> None:
    subprocess.run(command, shell=True, check=False, timeout=60,
                   input=f"{subject}\n\n{message}\n".encode("utf-8"))


def _notify_ntfy(topic: str, subject: str, message: str, urgent: bool) -> None:
    import urllib.request

    url = topic if topic.startswith(("https://", "http://")) else f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(
        url, data=message.encode("utf-8"), method="POST",
        headers={"Title": subject, "Priority": "urgent" if urgent else "default"},
    )
    # Empty ProxyHandler for the same reason the sender uses one: the default
    # opener's getproxies() can hang on macOS configd IPC.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15):
        pass


def _notify_smtp(spec: str, subject: str, message: str, urgent: bool) -> None:
    host, port, sender, to = spec.split(":", 3)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(message)
    password = os.environ.get("ORB_SMTP_PASSWORD", "")
    user = os.environ.get("ORB_SMTP_USER", "") or sender
    with smtplib.SMTP(host, int(port), timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if password:
            smtp.login(user, password)
        smtp.send_message(msg)


def build_notifier(args: argparse.Namespace) -> Callable[[str, str, bool], None]:
    """Fan out to every configured channel; a dead channel never stops the rest.

    Every alarm is also logged, so a box with no channels configured still has
    a local record — but that is a log, not a pager, and startup says so.
    """
    channels: list[tuple[str, Callable[[str, str, bool], None]]] = []
    if args.on_alert:
        channels.append(("on-alert", lambda s, m, u: _notify_shell(args.on_alert, s, m, u)))
    if args.ntfy_topic:
        channels.append(("ntfy", lambda s, m, u: _notify_ntfy(args.ntfy_topic, s, m, u)))
    if args.smtp:
        channels.append(("smtp", lambda s, m, u: _notify_smtp(args.smtp, s, m, u)))

    def notify(subject: str, message: str, urgent: bool = True) -> None:
        logger.warning("NOTIFY %s | %s", subject, message)
        for name, send in channels:
            try:
                send(subject, message, urgent)
            except Exception as exc:
                logger.error("Alarm channel %s failed (%s): %s", name, type(exc).__name__, exc)

    notify.channel_count = len(channels)  # type: ignore[attr-defined]
    return notify


# ── The receiver core ────────────────────────────────────────────────────────
class Receiver:
    """Beat bookkeeping plus the two alarms, with dedup that survives restarts.

    Dedup key for arriving alerts is severity + sorted alert set: the same set
    beating every 5 minutes is one page, a changed set or an escalation is a
    new page, and an empty set after a non-empty one is one CLEARED notice.
    Missed-beat state is a single flag per appliance: fires once when silence
    crosses --max-silence, resets (with a RECOVERED notice) when a beat lands.
    """

    def __init__(self, store: StateStore, max_silence_s: float,
                 notify: Callable[[str, str, bool], None]) -> None:
        self.store = store
        self.max_silence_s = max_silence_s
        self.notify = notify
        self.lock = threading.Lock()

    @staticmethod
    def _alarm_sig(severity: str, alerts: list[str]) -> str:
        if severity == "ok" and not alerts:
            return ""
        return f"{severity}|{','.join(sorted(alerts))}"

    @staticmethod
    def _label(appliance_id: str, site: str) -> str:
        return f"{site or 'unlabelled'}/{appliance_id}"

    def handle_beat(self, beat: dict[str, Any], now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        aid = beat["appliance_id"]
        sig = self._alarm_sig(beat["severity"], beat["alerts"])
        with self.lock:
            entry = self.store.data.setdefault(aid, {})
            was_silent = bool(entry.get("silence_alarmed"))
            prev_sig = entry.get("alarm_sig", "")
            entry.update({
                "site": beat["site"],
                "last_seen_unix": int(now),
                "last_sent_utc": beat["sent_utc"],
                "severity": beat["severity"],
                "alerts": beat["alerts"],
                "alarm_sig": sig,
                "silence_alarmed": False,
                "beats": int(entry.get("beats", 0)) + 1,
            })
            self.store.save()

        # Notifications happen OUTSIDE the lock: a slow SMTP handshake must not
        # block the next beat's handling or the silence check.
        label = self._label(aid, beat["site"])
        if was_silent:
            self.notify(f"RECOVERED {label}",
                        f"Appliance {label} is beating again (severity={beat['severity']}).",
                        urgent=False)
        if sig and sig != prev_sig:
            self.notify(
                f"{beat['severity'].upper()} {label}",
                f"Appliance {label} reports: {', '.join(beat['alerts']) or beat['severity']}. "
                f"Sent {beat['sent_utc'] or 'unknown'}. "
                "Codes are defined in server/services/heartbeat_service.py; "
                "triage in docs/INSTALL_RUNBOOK.md Part 2.",
                urgent=True,
            )
        elif not sig and prev_sig:
            self.notify(f"CLEARED {label}",
                        f"Appliance {label} reports all alerts cleared.",
                        urgent=False)

    def check_silence(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        fired: list[tuple[str, dict[str, Any], float]] = []
        with self.lock:
            for aid, entry in self.store.data.items():
                last = entry.get("last_seen_unix")
                if not isinstance(last, (int, float)) or entry.get("silence_alarmed"):
                    continue
                silent_for = now - last
                if silent_for > self.max_silence_s:
                    entry["silence_alarmed"] = True
                    fired.append((aid, dict(entry), silent_for))
            if fired:
                self.store.save()
        for aid, entry, silent_for in fired:
            label = self._label(aid, entry.get("site", ""))
            self.notify(
                f"SILENT {label}",
                f"No heartbeat from {label} for {int(silent_for // 60)} min "
                f"(limit {int(self.max_silence_s // 60)}). Last beat carried "
                f"severity={entry.get('severity', 'unknown')} "
                f"alerts={','.join(entry.get('alerts', [])) or 'none'}. "
                "The appliance may be frozen, powered off, or off the network — "
                "a frozen appliance CANNOT report itself; this alarm is its voice.",
                urgent=True,
            )

    def status_summary(self, now: Optional[float] = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self.lock:
            out: dict[str, Any] = {}
            for aid, entry in self.store.data.items():
                last = entry.get("last_seen_unix")
                out[aid] = {
                    "site": entry.get("site", ""),
                    "last_seen_age_s": int(now - last) if isinstance(last, (int, float)) else None,
                    "severity": entry.get("severity"),
                    "alerts": entry.get("alerts", []),
                    "silence_alarmed": bool(entry.get("silence_alarmed")),
                    "beats": entry.get("beats", 0),
                }
        return out


# ── HTTP surface ─────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    server_version = "orb-heartbeat-receiver/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *fargs: Any) -> None:
        logger.info("%s %s", self.address_string(), fmt % fargs)

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # any path — the URL path is the operator's choice
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"ok": False, "error": "body_too_large"})
            return
        raw = self.rfile.read(length)
        try:
            beat = validate_beat(
                raw,
                self.headers.get("Authorization"),
                self.headers.get("X-Orb-Signature"),
                token=self.server.auth_token,      # type: ignore[attr-defined]
                secret=self.server.auth_secret,    # type: ignore[attr-defined]
            )
        except BeatRejected as exc:
            logger.warning("Beat rejected (%s) from %s", exc.reason, self.address_string())
            self._reply(exc.status, {"ok": False, "error": exc.reason})
            return
        try:
            self.server.receiver.handle_beat(beat)  # type: ignore[attr-defined]
        except Exception:
            # A failing state write (disk full here) must reach the sender as a
            # clean HTTP 500, not a dropped connection: its log then says
            # http_500 (receiver's problem) instead of transport (network's).
            logger.exception("Beat handling failed")
            self._reply(500, {"ok": False, "error": "internal"})
            return
        self._reply(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/status":
            self._reply(404, {"ok": False, "error": "not_found"})
            return
        token = self.server.auth_token             # type: ignore[attr-defined]
        if token and not hmac.compare_digest(
                (self.headers.get("Authorization") or "").strip(), f"Bearer {token}"):
            self._reply(401, {"ok": False, "error": "bad_token"})
            return
        receiver: Receiver = self.server.receiver  # type: ignore[attr-defined]
        self._reply(200, {"ok": True, "utc": _utc_now(),
                          "max_silence_s": receiver.max_silence_s,
                          "appliances": receiver.status_summary()})


def make_server(bind: str, port: int, receiver: Receiver,
                token: str = "", secret: str = "",
                tls_cert: str = "", tls_key: str = "") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind, port), _Handler)
    server.receiver = receiver         # type: ignore[attr-defined]
    server.auth_token = token          # type: ignore[attr-defined]
    server.auth_secret = secret        # type: ignore[attr-defined]
    if tls_cert or tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=tls_cert, keyfile=tls_key or None)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


# ── Simulation — prove the pager pages, before trusting it ───────────────────
def simulate(receiver: Receiver) -> int:
    """Drive one fake appliance through every alarm transition.

    Four notifications should land on the configured channels, in order:
    CRITICAL, SILENT, RECOVERED, CLEARED. If fewer arrive, the pager path is
    broken and the time to learn that is now, not during an outage.
    """
    aid = "simulated-appliance"

    def beat(severity: str, alerts: list[str]) -> dict[str, Any]:
        return {"appliance_id": aid, "site": "simulated", "severity": severity,
                "alerts": sorted(alerts), "sent_utc": _utc_now()}

    print("[1/5] Beat with critical alerts -> CRITICAL should fire NOW")
    receiver.handle_beat(beat("critical", ["backup_stale", "no_admin"]))
    print("[2/5] Identical beat -> dedup: nothing should fire")
    receiver.handle_beat(beat("critical", ["backup_stale", "no_admin"]))
    print("[3/5] Silence past --max-silence -> SILENT should fire NOW")
    with receiver.lock:
        receiver.store.data[aid]["last_seen_unix"] -= int(receiver.max_silence_s) + 60
        receiver.store.save()
    receiver.check_silence()
    print("[4/5] Beat returns -> RECOVERED should fire NOW (alerts unchanged: no re-page)")
    receiver.handle_beat(beat("critical", ["backup_stale", "no_admin"]))
    print("[5/5] Clean beat -> CLEARED should fire NOW")
    receiver.handle_beat(beat("ok", []))
    with receiver.lock:
        receiver.store.data.pop(aid, None)
        receiver.store.save()
    print("Simulation complete. Expected exactly 4 notifications on every "
          "configured channel: CRITICAL, SILENT, RECOVERED, CLEARED.")
    return 0


# ── Entry point ──────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="heartbeat_receiver",
        description="Receive Orb appliance heartbeats and page the operator.",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--state", required=True,
                        help="JSON state file (created if absent; no secrets in it).")
    parser.add_argument("--max-silence", type=float, default=15.0,
                        help="Minutes without a beat before the SILENT alarm (default 15 "
                             "= 3 missed beats at the sender's default 300 s interval).")
    parser.add_argument("--check-interval", type=float, default=60.0,
                        help="Seconds between silence checks (default 60).")
    parser.add_argument("--on-alert", default="",
                        help="Shell command run per notification; message on stdin.")
    parser.add_argument("--ntfy-topic", default="",
                        help="ntfy topic name, or a full ntfy server URL.")
    parser.add_argument("--smtp", default="",
                        help="host:port:from:to — STARTTLS; password via ORB_SMTP_PASSWORD only.")
    parser.add_argument("--tls-cert", default="", help="TLS certificate chain (PEM).")
    parser.add_argument("--tls-key", default="", help="TLS private key (PEM).")
    parser.add_argument("--simulate", action="store_true",
                        help="Fake one appliance through alert/silence/recovery and exit.")
    args = parser.parse_args(argv)

    if args.smtp and args.smtp.count(":") < 3:
        parser.error("--smtp must be host:port:from:to")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    notify = build_notifier(args)
    store = StateStore(Path(args.state))
    receiver = Receiver(store, max_silence_s=args.max_silence * 60.0, notify=notify)

    if getattr(notify, "channel_count", 0) == 0:
        logger.warning("No alarm channel configured (--on-alert/--ntfy-topic/--smtp): "
                       "alarms will only appear in this log, and a log is not a pager.")

    if args.simulate:
        return simulate(receiver)

    token = os.environ.get("ORB_HEARTBEAT_TOKEN", "").strip()
    secret = os.environ.get("ORB_HEARTBEAT_SECRET", "").strip()
    if not token and not secret:
        logger.warning("Neither ORB_HEARTBEAT_TOKEN nor ORB_HEARTBEAT_SECRET is set in "
                       "this environment: accepting UNAUTHENTICATED beats. Anyone who "
                       "can reach this port can silence the dead-man's switch.")

    server = make_server(args.bind, args.port, receiver, token=token, secret=secret,
                         tls_cert=args.tls_cert, tls_key=args.tls_key)

    stop = threading.Event()

    def _watchdog() -> None:
        while not stop.is_set():
            try:
                receiver.check_silence()
            except Exception:  # the dead-man half must never die of its own bug
                logger.exception("Silence check failed")
            stop.wait(max(5.0, args.check_interval))

    threading.Thread(target=_watchdog, name="silence-watchdog", daemon=True).start()

    logger.info(
        "Listening on %s:%d (%s) — silence alarm after %.0f min, auth: token=%s hmac=%s. "
        "Run with --simulate to prove the pager path before trusting it.",
        args.bind, args.port, "https" if args.tls_cert else "http",
        args.max_silence, "on" if token else "OFF", "on" if secret else "OFF",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
