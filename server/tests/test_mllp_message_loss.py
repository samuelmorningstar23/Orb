"""MLLP framing: a message that came off the wire is either delivered or counted.

The failure this file exists for is SILENT loss. A bedside monitor blasts a burst
of ORUs and hangs up without draining our ACKs — ordinary device behaviour, not a
fault. The consumer's ACK write fails, it abandons the rest of the chunk, and the
observations that were already decoded out of that chunk disappear: no exception,
no error counter, no log line. A ward loses readings and nothing anywhere says so.

The framer cannot force a consumer to finish a batch. What it can do — and what
is asserted here — is refuse to let the loss be silent: every message is counted
as delivered when the caller takes it, and anything left over is counted as
abandoned and logged at ERROR.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.vitals_source import mllp  # noqa: E402


def _burst(n: int) -> bytes:
    return b"".join(mllp.frame(f"MSH|^~\\&|MON|H|ORB|ORB|20260801143025||ORU^R01|C{i}|P|2.5".encode())
                    for i in range(n))


class TestBatchAccounting:
    def test_a_fully_consumed_batch_reports_no_loss(self):
        f = mllp.MllpFramer()
        for _ in f.feed(_burst(5)):
            pass
        f.reset()
        assert f.stats.messages == 5
        assert f.stats.messages_delivered == 5
        assert f.stats.messages_abandoned == 0
        assert f.stats.lost_messages == 0

    def test_a_consumer_that_stops_mid_batch_is_counted_not_forgiven(self):
        """THE regression. The consumer took 2 of 10 and walked away; the other
        8 were real observations, already off the wire and unrecoverable."""
        f = mllp.MllpFramer()
        taken = 0
        for _ in f.feed(_burst(10)):
            taken += 1
            if taken == 2:
                break
        f.reset()  # what a connection handler does in its finally block
        assert f.stats.messages == 10
        assert f.stats.messages_delivered == 2
        assert f.stats.messages_abandoned == 8
        assert f.stats.lost_messages == 8

    def test_abandonment_is_logged_at_error_with_no_payload(self, caplog):
        """A counter nobody reads is not visibility. It must reach the log — and
        it must not carry the message bytes, which are PHI."""
        f = mllp.MllpFramer(name="10.0.0.7:2575")
        with caplog.at_level(logging.ERROR, logger="orb.vitals_source.mllp"):
            for _ in f.feed(_burst(4)):
                break
            f.reset()
        records = [r for r in caplog.records if r.name == "orb.vitals_source.mllp"]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        message = records[0].getMessage()
        assert "10.0.0.7:2575" in message and "3" in message
        assert "MSH" not in message and "ORU" not in message

    def test_a_never_consumed_batch_is_entirely_loss(self):
        f = mllp.MllpFramer()
        f.feed(_burst(3))
        f.reset()
        assert f.stats.messages_delivered == 0
        assert f.stats.messages_abandoned == 3

    def test_the_next_feed_settles_the_previous_batch(self):
        """A long-lived connection settles as it goes, so the counter is right
        while the connection is still open — not only once it closes."""
        f = mllp.MllpFramer()
        f.feed(_burst(2))
        for _ in f.feed(_burst(2)):
            pass
        assert f.stats.messages_abandoned == 2
        assert f.stats.messages_delivered == 2

    def test_settling_is_idempotent(self):
        f = mllp.MllpFramer()
        f.feed(_burst(2))
        f.reset()
        f.reset()
        f.feed(b"")
        assert f.stats.messages_abandoned == 2


class TestBatchIsStillAList:
    """Every existing consumer treats feed()'s result as a list. It must stay
    one — a behaviour change here would break the monitor source's read loop."""

    def test_equality_indexing_and_length(self):
        f = mllp.MllpFramer()
        batch = f.feed(mllp.frame(b"one") + mllp.frame(b"two"))
        assert batch == [b"one", b"two"]
        assert len(batch) == 2
        assert batch[0] == b"one"
        assert b"two" in batch
        assert list(reversed(batch)) == [b"two", b"one"]

    def test_reading_the_whole_batch_counts_as_taking_it(self):
        """`assert framer.feed(x) == [...]` and `out.extend(framer.feed(x))` both
        consume every message. Neither may be reported as loss."""
        f = mllp.MllpFramer()
        assert f.feed(mllp.frame(b"hello")) == [b"hello"]
        collected: list[bytes] = []
        collected.extend(f.feed(mllp.frame(b"world")))
        f.reset()
        assert collected == [b"world"]
        assert f.stats.messages_abandoned == 0
        assert f.stats.messages_delivered == 2

    def test_stats_dict_carries_the_new_counters(self):
        f = mllp.MllpFramer()
        data = f.stats.as_dict()
        assert data["messages_delivered"] == 0
        assert data["messages_abandoned"] == 0

    def test_empty_feed_returns_an_empty_batch(self):
        f = mllp.MllpFramer()
        assert f.feed(b"") == []

    def test_still_never_raises(self):
        f = mllp.MllpFramer()
        f.feed(bytes(range(256)) * 8)
        f.feed(b"")
        f.reset()
