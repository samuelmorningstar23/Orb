# ops/ — things that run OFF the appliance

Everything in `server/` runs inside the hospital. This directory holds the
operator-side pieces that live outside it.

| File | What it is |
|---|---|
| `heartbeat_receiver.py` | The receiving end of the appliance heartbeat — the part that actually pages Samuel. Single file, stdlib only. |
| `test_heartbeat_receiver.py` | Its tests. Stdlib `unittest`, no server dependencies. |
| `grafana-dashboard.json` | Dashboard for the appliance's `/metrics` endpoint (LAN-side Prometheus). |

## The heartbeat receiver

`server/services/heartbeat_service.py` POSTs a small no-PHI JSON beat from each
appliance every 5 minutes. The appliance is behind hospital NAT with no inbound
path, so **a missed beat is the only external signal that the box is frozen,
off, or off the network** — but only if something is listening. This is that
something.

Copy the one file to any always-on box you control (a small VPS, the standby
Mac) and run it with any python3 — no pip, no venv:

```bash
python3 heartbeat_receiver.py --port 8787 --state /var/lib/orb-receiver/state.json \
    --max-silence 15 --ntfy-topic <your-private-topic>
```

It pages on exactly two things:

1. **A beat arrives carrying warn/critical alert codes** (`backup_stale`,
   `no_admin`, `default_credentials`, `loop_stalled`, `disk_low`, ... — the
   vocabulary is defined by the sender). Deduplicated: the same alert set does
   not re-page every 5 minutes; a changed set or an escalation does, and one
   CLEARED notice is sent when the set empties.
2. **No beat for `--max-silence` minutes** (default 15 = three missed beats at
   the sender's default interval). Fires once per outage, one RECOVERED notice
   when the beat returns.

### Alarm channels

All optional, all combinable; every alarm is also written to the local log.

| Flag | Behaviour |
|---|---|
| `--on-alert '<cmd>'` | Runs the shell command per notification, message on stdin. Wire it to anything — Twilio CLI, a Telegram bot, `say`. |
| `--ntfy-topic <topic>` | POST to `https://ntfy.sh/<topic>` (or a full URL of a self-hosted ntfy). The ntfy phone app makes this a pager in about two minutes; pick an unguessable topic name. |
| `--smtp host:port:from:to` | STARTTLS mail. Password comes ONLY from `ORB_SMTP_PASSWORD` in the environment (user defaults to `from`, override `ORB_SMTP_USER`). |

### Authentication — set the same values on both ends

Export in the receiver's environment the same `ORB_HEARTBEAT_TOKEN` and
`ORB_HEARTBEAT_SECRET` the appliance's `.env` carries. Token gates the POST;
the secret verifies an HMAC-SHA256 over the exact body bytes, which is what
stops a forged "everything is fine" beat. Unset means unenforced, and the
receiver says so loudly at startup. No secrets go on the command line, in this
file, or in the state file.

TLS: the sender refuses plain http by default. Either give the receiver a real
certificate (`--tls-cert`/`--tls-key`, e.g. certbot files — self-signed will
NOT work, the sender verifies), put it behind any TLS reverse proxy, or run
over a private overlay (Tailscale/WireGuard) and set
`ORB_HEARTBEAT_ALLOW_INSECURE=1` on the appliance deliberately.

### Prove the pager pages — before trusting it

```bash
python3 heartbeat_receiver.py --state /tmp/sim-state.json --simulate \
    --ntfy-topic <your-private-topic>
```

Drives one fake appliance through every transition. **Exactly 4 notifications
must arrive on your phone: CRITICAL, SILENT, RECOVERED, CLEARED.** If fewer do,
the pager path is broken and you just found out now instead of during an
outage. Re-run this after any change to the receiver box, and calendar it
monthly next to the restore drill.

### Checking it is alive

`GET /status` (same bearer token, if set) returns last-seen age, severity and
standing alerts per appliance. `curl -H "Authorization: Bearer $ORB_HEARTBEAT_TOKEN" https://<receiver>/status`

### Running it as a service

A ready-to-paste systemd unit (Linux VPS) and launchd plist (standby Mac) are
in the header of `heartbeat_receiver.py` itself, secrets externalised to a
mode-600 env file in both. Full install walk-through:
`docs/INSTALL_RUNBOOK.md`, section "Stand up the pager".

### Tests

```bash
# from the repo root; any python3 ≥ 3.9 works, the venv is just a known-good one
server/.venv/bin/python -m unittest ops/test_heartbeat_receiver.py -v
```
