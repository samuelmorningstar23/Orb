"""ops/heartbeat_receiver.py — the receiving end of the appliance heartbeat.

Stdlib unittest ONLY, no server/ imports, no fixtures from the app: the
receiver's whole point is to run on a box that has none of that installed, so
its tests must prove it stands alone.

What is under test is the pager's contract, in both directions:
  * a well-formed beat is accepted and recorded; a malformed, unsigned or
    wrongly-signed one is refused with a fixed reason enum — including the
    beat an attacker would most want to send, a forged "everything is fine";
  * warn/critical alert codes page ONCE per distinct alert set, not once per
    5-minute beat — a pager that cries every beat gets muted, and a muted
    pager is the failure this whole item exists to prevent;
  * silence pages once per outage and recovery notices once per return, and
    both survive a receiver restart via the state file.

Run from the repo root:
    server/.venv/bin/python -m unittest ops/test_heartbeat_receiver.py -v
(any python3 works — the venv is just a convenient known-good interpreter).
"""
import hashlib
import hmac
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import heartbeat_receiver as rx  # noqa: E402


def make_beat(**overrides):
    """A beat shaped like heartbeat_service.build_payload() actually sends it."""
    beat = {
        "schema": "orb.heartbeat/1",
        "sent_utc": "2026-08-10T00:00:00+00:00",
        "site": "pilot-ward-a",
        "appliance_id": "ab12cd34ef56ab78",
        "severity": "ok",
        "alerts": [],
        "errors": [],
        "db": {"encrypted_at_rest": True, "size_bytes": 1024},
        "backup": {"count": 3, "newest_age_s": 3600, "stale": False},
        "loop": {"healthy": True, "status_age_s": 12},
    }
    beat.update(overrides)
    return beat


class RecordingNotify:
    def __init__(self):
        self.calls = []

    def __call__(self, subject, message, urgent=True):
        self.calls.append((subject, message, urgent))

    def subjects(self):
        return [subject for subject, _, _ in self.calls]


class ValidateBeatTests(unittest.TestCase):
    def _validate(self, payload, **kwargs):
        raw = json.dumps(payload).encode("utf-8")
        return rx.validate_beat(raw, kwargs.pop("auth", None), kwargs.pop("sig", None), **kwargs)

    def test_accepts_sender_shaped_beat_and_normalises(self):
        beat = self._validate(make_beat(alerts=["no_admin", "backup_stale", "no_admin"]))
        self.assertEqual(beat["appliance_id"], "ab12cd34ef56ab78")
        self.assertEqual(beat["site"], "pilot-ward-a")
        self.assertEqual(beat["alerts"], ["backup_stale", "no_admin"])  # sorted, deduped

    def test_unknown_extra_keys_do_not_reject(self):
        # A sender one version ahead must still be able to page.
        self._validate(make_beat(standby={"receipt_present": False}))

    def test_missing_alerts_key_defaults_empty(self):
        payload = make_beat()
        del payload["alerts"]
        self.assertEqual(self._validate(payload)["alerts"], [])

    def test_rejects_non_json_and_non_object(self):
        for raw in (b"not json", b"[1,2]", b'"beat"', b"\xff\xfe"):
            with self.assertRaises(rx.BeatRejected) as ctx:
                rx.validate_beat(raw, None, None)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.reason, "bad_json")

    def test_rejects_wrong_schema(self):
        with self.assertRaises(rx.BeatRejected) as ctx:
            self._validate(make_beat(schema="something.else/1"))
        self.assertEqual(ctx.exception.reason, "bad_schema")

    def test_rejects_bad_appliance_id(self):
        for bad in ("", "../../etc/passwd", "id with spaces", "x" * 65, None, 42):
            with self.assertRaises(rx.BeatRejected) as ctx:
                self._validate(make_beat(appliance_id=bad))
            self.assertEqual(ctx.exception.reason, "bad_appliance_id")

    def test_rejects_bad_severity(self):
        for bad in ("fatal", "", None, 1):
            with self.assertRaises(rx.BeatRejected) as ctx:
                self._validate(make_beat(severity=bad))
            self.assertEqual(ctx.exception.reason, "bad_severity")

    def test_rejects_free_text_alert_codes(self):
        # The sender's guard means this cannot happen from a real appliance —
        # but the receiver must not trust that, and must never store or page
        # a string shaped like "no such patient: P001".
        for bad_alerts in (["no such patient: P001"], ["ALERT!"], "backup_stale", [None]):
            with self.assertRaises(rx.BeatRejected) as ctx:
                self._validate(make_beat(alerts=bad_alerts))
            self.assertEqual(ctx.exception.reason, "bad_alerts")

    def test_rejects_bad_site(self):
        with self.assertRaises(rx.BeatRejected) as ctx:
            self._validate(make_beat(site="Bhandari Ward Three"))
        self.assertEqual(ctx.exception.reason, "bad_site")

    def test_token_enforced_when_set(self):
        raw = json.dumps(make_beat()).encode("utf-8")
        for auth in (None, "", "Bearer wrong", "Basic c2VjcmV0"):
            with self.assertRaises(rx.BeatRejected) as ctx:
                rx.validate_beat(raw, auth, None, token="t0ken")
            self.assertEqual((ctx.exception.status, ctx.exception.reason), (401, "bad_token"))
        rx.validate_beat(raw, "Bearer t0ken", None, token="t0ken")

    def test_hmac_signature_enforced_when_set(self):
        raw = json.dumps(make_beat()).encode("utf-8")
        good = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
        forged = "sha256=" + hmac.new(b"wrong-key", raw, hashlib.sha256).hexdigest()
        for sig in (None, "", forged, good[:-2] + "00"):
            with self.assertRaises(rx.BeatRejected) as ctx:
                rx.validate_beat(raw, None, sig, secret="s3cret")
            self.assertEqual((ctx.exception.status, ctx.exception.reason), (401, "bad_signature"))
        rx.validate_beat(raw, None, good, secret="s3cret")

    def test_signature_covers_exact_bytes(self):
        # A signature for one body must not authenticate a different body —
        # that is the forged-"all clear" path.
        raw_bad = json.dumps(make_beat(severity="critical", alerts=["no_admin"])).encode()
        sig_bad = "sha256=" + hmac.new(b"s3cret", raw_bad, hashlib.sha256).hexdigest()
        raw_ok = json.dumps(make_beat()).encode()
        with self.assertRaises(rx.BeatRejected):
            rx.validate_beat(raw_ok, None, sig_bad, secret="s3cret")


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_round_trip(self):
        store = rx.StateStore(self.path)
        store.data["abc"] = {"last_seen_unix": 123, "alerts": ["disk_low"]}
        store.save()
        reloaded = rx.StateStore(self.path)
        self.assertEqual(reloaded.data, {"abc": {"last_seen_unix": 123, "alerts": ["disk_low"]}})

    def test_save_is_atomic_and_leaves_no_tmp(self):
        store = rx.StateStore(self.path)
        store.data["a"] = {"beats": 1}
        store.save()
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_missing_and_corrupt_files_start_fresh(self):
        self.assertEqual(rx.StateStore(self.path).data, {})
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(rx.StateStore(self.path).data, {})

    def test_creates_parent_directory(self):
        deep = Path(self.tmp.name) / "a" / "b" / "state.json"
        store = rx.StateStore(deep)
        store.save()
        self.assertTrue(deep.is_file())


class ReceiverAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.notify = RecordingNotify()
        self.rcv = rx.Receiver(rx.StateStore(self.path), max_silence_s=900, notify=self.notify)

    def beat(self, severity="ok", alerts=(), t=1000.0):
        self.rcv.handle_beat(make_beat(severity=severity, alerts=sorted(alerts)), now=t)

    def test_ok_beats_never_page(self):
        for t in (0, 300, 600):
            self.beat(t=1000.0 + t)
        self.assertEqual(self.notify.calls, [])

    def test_warn_alerts_page_once_not_every_beat(self):
        self.beat("critical", ["backup_stale", "no_admin"], t=1000)
        self.beat("critical", ["backup_stale", "no_admin"], t=1300)
        self.beat("critical", ["no_admin", "backup_stale"], t=1600)  # order must not matter
        self.assertEqual(len(self.notify.calls), 1)
        subject, message, urgent = self.notify.calls[0]
        self.assertIn("CRITICAL", subject)
        self.assertIn("backup_stale", message)
        self.assertTrue(urgent)

    def test_changed_alert_set_pages_again(self):
        self.beat("warn", ["disk_low"], t=1000)
        self.beat("warn", ["disk_low", "models_cold"], t=1300)
        self.assertEqual(len(self.notify.calls), 2)

    def test_severity_escalation_pages_even_with_same_set(self):
        self.beat("warn", ["backup_stale"], t=1000)
        self.beat("critical", ["backup_stale"], t=1300)
        self.assertEqual(len(self.notify.calls), 2)

    def test_cleared_notice_once_then_silence(self):
        self.beat("warn", ["disk_low"], t=1000)
        self.beat("ok", [], t=1300)
        self.beat("ok", [], t=1600)
        self.assertEqual(len(self.notify.calls), 2)
        self.assertIn("CLEARED", self.notify.subjects()[1])
        self.assertFalse(self.notify.calls[1][2])  # a clear is not urgent

    def test_dedup_survives_receiver_restart(self):
        self.beat("critical", ["no_admin"], t=1000)
        restarted = rx.Receiver(rx.StateStore(self.path), max_silence_s=900, notify=self.notify)
        restarted.handle_beat(make_beat(severity="critical", alerts=["no_admin"]), now=1300)
        self.assertEqual(len(self.notify.calls), 1)


class ReceiverSilenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.notify = RecordingNotify()
        self.rcv = rx.Receiver(rx.StateStore(self.path), max_silence_s=900, notify=self.notify)

    def test_silence_fires_once_per_outage_and_recovers_once(self):
        self.rcv.handle_beat(make_beat(), now=1000)
        self.rcv.check_silence(now=1000 + 899)   # inside the window: nothing
        self.assertEqual(self.notify.calls, [])
        self.rcv.check_silence(now=1000 + 901)   # outage: fires
        self.rcv.check_silence(now=1000 + 2000)  # still out: must NOT fire again
        self.rcv.check_silence(now=1000 + 9000)
        self.assertEqual(len(self.notify.calls), 1)
        self.assertIn("SILENT", self.notify.subjects()[0])

        self.rcv.handle_beat(make_beat(), now=1000 + 9100)  # back: one RECOVERED
        self.assertEqual(len(self.notify.calls), 2)
        self.assertIn("RECOVERED", self.notify.subjects()[1])
        self.rcv.check_silence(now=1000 + 9200)  # fresh beat: no alarm
        self.assertEqual(len(self.notify.calls), 2)

    def test_second_outage_fires_again(self):
        self.rcv.handle_beat(make_beat(), now=1000)
        self.rcv.check_silence(now=2000)
        self.rcv.handle_beat(make_beat(), now=2100)
        self.rcv.check_silence(now=2100 + 901)
        silents = [s for s in self.notify.subjects() if s.startswith("SILENT")]
        self.assertEqual(len(silents), 2)

    def test_outage_in_progress_survives_restart_without_repaging(self):
        self.rcv.handle_beat(make_beat(), now=1000)
        self.rcv.check_silence(now=2000)  # fires
        restarted = rx.Receiver(rx.StateStore(self.path), max_silence_s=900, notify=self.notify)
        restarted.check_silence(now=3000)  # same outage: silent
        self.assertEqual(len([s for s in self.notify.subjects() if s.startswith("SILENT")]), 1)

    def test_unseen_appliances_are_not_watched(self):
        self.rcv.check_silence(now=99999)
        self.assertEqual(self.notify.calls, [])


class SimulateTests(unittest.TestCase):
    def test_simulate_walks_all_four_transitions_and_cleans_up(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        notify = RecordingNotify()
        store = rx.StateStore(Path(tmp.name) / "state.json")
        rcv = rx.Receiver(store, max_silence_s=900, notify=notify)
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rx.simulate(rcv), 0)
        prefixes = [s.split(" ")[0] for s in notify.subjects()]
        self.assertEqual(prefixes, ["CRITICAL", "SILENT", "RECOVERED", "CLEARED"])
        self.assertEqual(store.data, {})  # the fake appliance must not linger


class HttpEndToEndTests(unittest.TestCase):
    """The real HTTP surface, real sockets, ephemeral port."""

    TOKEN = "test-token"
    SECRET = "test-secret"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notify = RecordingNotify()
        self.rcv = rx.Receiver(rx.StateStore(Path(self.tmp.name) / "state.json"),
                               max_silence_s=900, notify=self.notify)
        self.server = rx.make_server("127.0.0.1", 0, self.rcv,
                                     token=self.TOKEN, secret=self.SECRET)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _post(self, body: bytes, token=None, sign=True, path="/heartbeat"):
        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {self.TOKEN if token is None else token}"
        if sign:
            mac = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Orb-Signature"] = f"sha256={mac}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_signed_beat_accepted_and_recorded(self):
        body = json.dumps(make_beat(severity="warn", alerts=["disk_low"])).encode()
        status, reply = self._post(body)
        self.assertEqual((status, reply), (200, {"ok": True}))
        summary = self.rcv.status_summary()
        self.assertEqual(summary["ab12cd34ef56ab78"]["alerts"], ["disk_low"])
        self.assertEqual(len(self.notify.calls), 1)

    def test_unsigned_and_miskeyed_beats_rejected(self):
        body = json.dumps(make_beat()).encode()
        status, reply = self._post(body, sign=False)
        self.assertEqual((status, reply["error"]), (401, "bad_signature"))
        status, reply = self._post(body, token="wrong")
        self.assertEqual((status, reply["error"]), (401, "bad_token"))
        self.assertEqual(self.rcv.status_summary(), {})  # nothing recorded

    def test_malformed_body_rejected_with_fixed_enum(self):
        status, reply = self._post(b"{ not json")
        self.assertEqual((status, reply["error"]), (400, "bad_json"))

    def test_handling_failure_is_a_clean_500_not_a_dropped_connection(self):
        # Disk full on the receiver box must reach the sender as http_500
        # (receiver's problem), not as "transport" (network's problem) —
        # they send the operator to different places.
        def broken_save():
            raise OSError("disk full")
        self.rcv.store.save = broken_save
        status, reply = self._post(json.dumps(make_beat()).encode())
        self.assertEqual((status, reply["error"]), (500, "internal"))
        del self.rcv.store.save  # server must still work afterwards
        status, _ = self._post(json.dumps(make_beat()).encode())
        self.assertEqual(status, 200)

    def test_status_requires_token(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/status")
        self.assertEqual(conn.getresponse().status, 401)

    def test_status_reports_appliances(self):
        self._post(json.dumps(make_beat()).encode())
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/status", headers={"Authorization": f"Bearer {self.TOKEN}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read().decode("utf-8"))
        self.assertIn("ab12cd34ef56ab78", body["appliances"])


if __name__ == "__main__":
    unittest.main()
