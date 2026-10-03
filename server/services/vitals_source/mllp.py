"""Orb Hospital OS — MLLP (Minimal Lower Layer Protocol) framing.

MLLP is how HL7 v2 travels over TCP, and it is the wire every bedside-monitor
gateway we are likely to meet speaks:

    <VT> message-bytes <FS><CR>          VT = 0x0B, FS = 0x1C, CR = 0x0D

That is the entire protocol. Everything hard about it is failure handling, and
on an appliance watching real patients the failures are the design:

  * TCP gives us a byte stream, not messages — one read may hold half a message,
    three messages, or a message split across ten reads.
  * A monitor that reboots mid-message leaves a truncated block in our buffer.
  * A misconfigured device can point a non-HL7 stream at the port and never send
    a start block at all.
  * A device stuck in a retry loop can send faster than we can persist.

`MllpFramer` is therefore a bounded, resynchronising, exception-free incremental
decoder: it NEVER raises, it never grows without limit, and it reports what it
discarded so a silent feed loss shows up as a counter instead of a mystery. It
is pure bytes-in/messages-out with no I/O, so it is trivially testable and can be
driven from a socket, a file, or a test.

CONSUMER CONTRACT — finish the batch.
`feed()` hands back every message that chunk completed. Those messages have been
taken off the wire and cannot be asked for again: TCP has no rewind, and a sender
that has already moved on will not resend them. A consumer that stops iterating
part-way — `break`, `return`, an early exit because an ACK could not be written —
therefore DESTROYS clinical readings it has already received.

That is not hypothetical. It is exactly what happens when a monitor blasts a
burst of ORUs and hangs up without draining our ACKs (ordinary device behaviour:
the gateway got what it wanted and closed). The write of ACK #2 fails, the reader
gives up on the whole chunk, and every message behind it in that same chunk
vanishes with no error and no counter.

The framer cannot stop a caller doing that, so it does the next best thing: it
counts it and shouts. Every message is counted as *delivered* at the instant the
caller receives it; anything left over when the batch is settled (the next
`feed()`, or `reset()` on connection close) is counted as ``messages_abandoned``
and logged at ERROR. Silent loss becomes loud loss.

The correct consumer shape, for anyone writing one:

    for message in framer.feed(chunk):
        persist(message)                 # ALWAYS, first
        if can_ack:
            try:
                send_ack(message)
            except OSError:
                can_ack = False          # stop ACKing, keep persisting
    # ...and only leave the loop when the socket is actually done.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("orb.vitals_source.mllp")

# MLLP framing bytes.
SB = 0x0B  # start block  <VT>
EB = 0x1C  # end block    <FS>
CR = 0x0D  # carriage return

START_BLOCK = bytes([SB])
END_BLOCK = bytes([EB, CR])

# A vitals ORU is a few hundred bytes; a fat lab panel a few kilobytes. 1 MiB is
# ~1000× headroom and still small enough that a stuck sender cannot exhaust the
# appliance's memory through this path.
DEFAULT_MAX_MESSAGE_BYTES = 1024 * 1024


@dataclass
class FramerStats:
    """Everything the framer threw away, so it is observable rather than silent."""
    messages: int = 0
    bytes_discarded_before_start: int = 0
    oversize_messages: int = 0
    resyncs: int = 0
    # Decoded and handed to the caller. `messages` counts what came off the wire;
    # `messages_delivered` counts what the caller actually took. The gap is loss.
    messages_delivered: int = 0
    # Decoded off the wire, never taken by the caller, gone forever. A non-zero
    # value here is a dropped clinical reading, not a framing curiosity.
    messages_abandoned: int = 0

    def as_dict(self) -> dict:
        return {
            "messages": self.messages,
            "bytes_discarded_before_start": self.bytes_discarded_before_start,
            "oversize_messages": self.oversize_messages,
            "resyncs": self.resyncs,
            "messages_delivered": self.messages_delivered,
            "messages_abandoned": self.messages_abandoned,
        }

    @property
    def lost_messages(self) -> int:
        """One number an operator can alarm on: readings this framer received
        and could not hand on. Abandoned batches plus oversize blocks."""
        return self.messages_abandoned + self.oversize_messages


class MessageBatch(list):
    """The messages one `feed()` call completed.

    It IS a list — equality, indexing, iteration and length are unchanged, so
    every existing caller keeps working — with one addition: it remembers how
    many of its messages the caller actually took. Reading a message counts as
    taking it (iterating, indexing, or comparing the whole batch), and the
    delivery counter moves at that moment rather than at the end of the batch,
    so the numbers are honest on a connection that is still open. Whatever is
    left when the framer settles the batch is loss, and gets counted and logged
    instead of disappearing.
    """

    __slots__ = ("_stats", "taken")

    def __init__(self, messages=(), stats: FramerStats | None = None):
        super().__init__(messages)
        self._stats = stats
        self.taken = 0

    def _mark(self, count: int) -> None:
        """Record that the caller now holds the first `count` messages."""
        if count <= self.taken:
            return
        if self._stats is not None:
            self._stats.messages_delivered += count - self.taken
        self.taken = count

    def _take_all(self) -> None:
        self._mark(len(self))

    def __iter__(self):
        # Counted at the moment the caller receives each message, so a caller
        # that breaks out mid-loop is credited with what it got and charged for
        # what it left behind.
        for index, message in enumerate(super().__iter__()):
            self._mark(index + 1)
            yield message

    def __getitem__(self, index):
        item = list.__getitem__(self, index)
        if isinstance(index, slice):
            self._take_all()  # cannot attribute a slice; assume nothing is lost
        else:
            position = index if index >= 0 else len(self) + index
            self._mark(position + 1)
        return item

    def __eq__(self, other):
        self._take_all()  # comparing the batch reads every message in it
        return list.__eq__(self, other)

    def __ne__(self, other):
        self._take_all()
        return list.__ne__(self, other)

    def __contains__(self, item):
        self._take_all()
        return list.__contains__(self, item)

    def __reversed__(self):
        self._take_all()
        return list.__reversed__(self)

    __hash__ = None  # a list is unhashable; defining __eq__ must not change that


class MllpFramer:
    """Incremental MLLP decoder. Feed it bytes, get whole messages back.

    Usage:
        framer = MllpFramer()
        for message in framer.feed(chunk):
            ...

    Guarantees:
      - never raises
      - buffered bytes never exceed ``max_message_bytes`` (plus one chunk)
      - garbage before a start block is dropped and counted, not parsed
      - an oversize block is abandoned and the stream resynchronises to the next
        start block instead of wedging forever
      - a message it decoded and handed over is either taken by the caller or
        counted as lost — never silently dropped
    """

    __slots__ = ("_batch", "_buf", "_in_message", "max_message_bytes", "name", "stats")

    def __init__(self, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 *, name: str = "mllp"):
        self.max_message_bytes = max(512, int(max_message_bytes))
        # Identifies the connection in the loss log. Never PHI — a peer address
        # or a source id, supplied by the caller.
        self.name = str(name)
        self._buf = bytearray()
        self._in_message = False
        self._batch: MessageBatch | None = None
        self.stats = FramerStats()

    def reset(self) -> None:
        """Drop any partial message (used when a connection closes).

        Also settles the last batch, which is why a connection that dies holding
        undelivered messages reports them here rather than taking them to the
        grave.
        """
        self._settle()
        self._buf.clear()
        self._in_message = False

    def _settle(self) -> None:
        """Account for the previous batch: what the caller took, and what it did
        not. Called before each new batch and on reset()."""
        batch = self._batch
        self._batch = None
        if not batch:
            return
        # Deliveries were counted as they happened; only the shortfall is left.
        taken = min(batch.taken, len(batch))
        lost = len(batch) - taken
        if lost <= 0:
            return
        self.stats.messages_abandoned += lost
        # ERROR, not warning: these are observations that were successfully
        # received from a bedside monitor and then thrown away by the consumer.
        # No payload is logged — the bytes are PHI.
        logger.error(
            "[%s] %d HL7 message(s) were decoded off the wire and never consumed "
            "— clinical readings LOST (delivered %d of %d in this batch; %d "
            "abandoned on this connection so far). The consumer stopped "
            "iterating a feed() batch early; it must finish the batch even when "
            "it can no longer ACK.",
            self.name, lost, taken, len(batch), self.stats.messages_abandoned,
        )

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> MessageBatch:
        """Consume a chunk; return every complete message it completed.

        The result is a list (`MessageBatch`) that also records how much of it
        the caller consumed — see the CONSUMER CONTRACT at the top of this file.
        Every message in it is already off the wire and unrecoverable, so a
        caller that leaves any of them unread has lost a clinical reading.
        """
        # Whatever the caller did with the previous batch, the answer is now
        # final: settle it before handing out another one.
        self._settle()
        if not data:
            return MessageBatch(stats=self.stats)
        messages = MessageBatch(stats=self.stats)
        self._batch = messages
        self._buf.extend(data)

        while True:
            if not self._in_message:
                start = self._buf.find(SB)
                if start == -1:
                    # No start block in flight. Everything buffered is noise —
                    # keep nothing (a partial start block is a single byte and
                    # would be re-sent as part of the next chunk anyway).
                    if self._buf:
                        self.stats.bytes_discarded_before_start += len(self._buf)
                        self._buf.clear()
                    return messages
                if start > 0:
                    self.stats.bytes_discarded_before_start += start
                    self.stats.resyncs += 1
                del self._buf[: start + 1]  # drop noise AND the start byte
                self._in_message = True

            end = self._buf.find(EB)

            # A start block INSIDE an open message means the sender was cut off
            # mid-message and has begun a new one (device reboot, cable pull).
            # MLLP has no way to say "ignore what I just sent", so the framer
            # has to infer it: abandon the partial and start at the new block.
            # Without this the truncated bytes and the NEXT message fuse into
            # one unparseable blob — losing a good reading to a bad one.
            restart = self._buf.find(SB)
            if restart != -1 and (end == -1 or restart < end):
                self.stats.bytes_discarded_before_start += restart
                self.stats.resyncs += 1
                del self._buf[: restart + 1]
                continue

            if end == -1:
                # Message still arriving. Bound it: a sender that opens a block
                # and never closes it must not grow our buffer without limit.
                if len(self._buf) > self.max_message_bytes:
                    self.stats.oversize_messages += 1
                    self.stats.resyncs += 1
                    self._buf.clear()
                    self._in_message = False
                    continue  # resynchronise on the next start block
                return messages

            payload = bytes(self._buf[:end])
            # Consume the end block, plus the trailing CR when present. Some
            # gateways omit the CR; accepting both keeps a conformant-enough
            # device working instead of stalling on byte boundaries.
            consume = end + 1
            if len(self._buf) > consume and self._buf[consume] == CR:
                consume += 1
            del self._buf[:consume]
            self._in_message = False

            if len(payload) > self.max_message_bytes:
                self.stats.oversize_messages += 1
                continue
            self.stats.messages += 1
            messages.append(payload)


def frame(message: bytes | str, encoding: str = "utf-8") -> bytes:
    """Wrap a message in MLLP framing for transmission."""
    if isinstance(message, str):
        message = message.encode(encoding, errors="replace")
    return START_BLOCK + message + END_BLOCK
