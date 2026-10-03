"""Sage guard gaps found 2026-10-01 (F035), pinned.

Four ways a guard looked armed and was not:

  * the history replay scrubbed only the bare "<tag>" form of the output
    contract, while the answer scrub saw "<**escalation**>" and "</__caveats__>";
  * a raw action-intent payload printed as a line of the answer
    ('{"action_type":"answer","medication":null}') reached the wire;
  * SentenceStreamSanitizer re-ran every pattern over the whole uncommitted
    window on each feed, and only ". " committed anything, so a period-free
    answer cost seconds of event-loop CPU (21 s for 6.6 kB);
  * switching a Sage safety setting off said nothing anywhere.

(The underscore-emphasis promise forms live in tests/test_action_lane_gates.py
MUST_STRIP, beside "Dr Ravi can cancel" in MUST_NOT_STRIP.)
"""
import logging
import os
import random
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import routes.sage_routes as sr
from routes.sage_routes import (
    FORBIDDEN_PHRASE_PATTERNS,
    MarkerAnswerStreamer,
    SentenceStreamSanitizer,
    _strip_contract_scaffolding,
    _strip_forbidden_streaming,
    parse_fast_response,
)
from services import sage_service
from services import thinking_mode as tm

SERVER_DIR = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════════════════
#  1. One contract-tag regex for the answer AND the history replay
# ════════════════════════════════════════════════════════════════════════════
def test_history_replay_and_answer_scrub_share_one_regex():
    assert sage_service._HISTORY_TAG_RE is sr._CONTRACT_TAG_RE


@pytest.mark.parametrize("tag", [
    "<escalation>", "<**escalation**>", "<_escalation_>", "</__caveats__>",
    "< caveats>", "<cited_sources >",
])
def test_history_replay_scrubs_every_tag_form(tag):
    text = sage_service._history_turn_text(f"Give fluids now. {tag} None.", 400)
    assert "escalation" not in text and "caveats" not in text and "cited_sources" not in text
    assert "Give fluids now." in text


def test_history_scrub_leaves_a_clinical_less_than_alone():
    assert "K < 3.5" in sage_service._history_turn_text("Replace if K < 3.5 mmol/L.", 400)


# ════════════════════════════════════════════════════════════════════════════
#  2. A raw action-intent line is contract syntax, never an answer line
# ════════════════════════════════════════════════════════════════════════════
ACTION_LINE = '{"action_type":"answer","medication":null}'
LEAKS = [
    f"NOACT| Start IV fluids and recheck lactate.\n{ACTION_LINE}",
    f"NOACT| Start IV fluids.\n  {ACTION_LINE}  \nRecheck lactate in 2 hours.",
    f"NOACT| Start IV fluids.\nNOACT| {ACTION_LINE}",
]


@pytest.mark.parametrize("raw", LEAKS)
def test_buffered_lane_drops_a_standalone_action_line(raw):
    answer = parse_fast_response(raw)["answer"]
    assert "action_type" not in answer
    assert "Start IV fluids" in answer
    assert "action_type" not in _strip_contract_scaffolding(raw)


@pytest.mark.parametrize("raw", LEAKS)
def test_streamed_lane_drops_it_at_every_chunk_size(raw):
    for chunk in range(1, len(raw) + 1):
        st = MarkerAnswerStreamer(SentenceStreamSanitizer(_strip_forbidden_streaming),
                                  mutation_locked=False)
        out = "".join(st.feed(raw[i:i + chunk]) for i in range(0, len(raw), chunk))
        out += st.finalize()
        assert "action_type" not in out, (chunk, out)
        assert "Start IV fluids" in out, (chunk, out)


@pytest.mark.parametrize("prose", [
    'The payload {"action_type": "x"} is internal and never shown.',
    "{not json, but mentions action_type}",
    'Use the {"dose": "1 g"} format when you write the order.',
])
def test_prose_that_only_mentions_braces_is_kept(prose):
    raw = f"NOACT| {prose}"
    assert prose in parse_fast_response(raw)["answer"]
    st = MarkerAnswerStreamer(SentenceStreamSanitizer(_strip_forbidden_streaming),
                              mutation_locked=False)
    out = "".join(st.feed(c) for c in raw) + st.finalize()
    assert prose.strip() in out


# ════════════════════════════════════════════════════════════════════════════
#  3. The sanitizer stays linear on period-free answers, with the same bytes
# ════════════════════════════════════════════════════════════════════════════
def _stream(text, chunk, *, new=True):
    s = SentenceStreamSanitizer(_strip_forbidden_streaming)
    if not new:
        # The pre-2026-10-03 algorithm: sentence units only, no pin.
        s._barrier_cuts = False
        s._is_pinned = lambda: False
    out, widest = [], 0
    for i in range(0, len(text), chunk):
        out.append(s.feed(text[i:i + chunk]))
        widest = max(widest, len(s._win))
    out.append(s.flush())
    return "".join(out), widest


PERIOD_FREE = {
    "bullet_lines": "\n".join(
        f"- Check potassium and repeat ECG, item {i}, review with the registrar"
        for i in range(90))[:6600],
    "one_comma_line": ", ".join(f"check potassium item {i}" for i in range(300))[:6600],
    "after_done": ("Bloods done this morning, "
                   + ", ".join(f"check potassium item {i}" for i in range(300)))[:6600],
}


@pytest.mark.parametrize("name", sorted(PERIOD_FREE))
def test_a_period_free_answer_streams_in_bounded_work(name):
    text = PERIOD_FREE[name]
    t0 = time.perf_counter()
    out, widest = _stream(text, 4)
    elapsed = time.perf_counter() - t0
    assert out == text            # nothing forbidden in it, so nothing removed
    # Generous on a loaded box; the defect was 18-21 s.
    assert elapsed < 5.0, f"{name}: {elapsed:.1f}s of sanitizer CPU for 6.6 kB"
    if name != "after_done":
        # The window is committed at barriers, so it never holds the answer.
        assert widest < 400, f"{name}: window grew to {widest} chars"


# Texts where an earlier cut rule (straight after a newline) stripped a
# "Done." that the whole-text strip keeps, plus joins across a deletion. The
# second one also shows the old sentence-only commit emitting a "?" twice.
REGRESSIONS = [
    "Done.I have added paracetamol(DOC: LOC-1). repeat ECG\r\n this was performed"
    "\tDone.i have ad(DOC: LOC-1)\r\n ",
    "\n\nStop!Ravi can cancel\r\n(DOC: LOC-1)?medication was addedOrder placed- . "
    "!Done.\n  .\n\r\n (DOC: X)- (DOC: LOC-1)?I will cancel the bloodsi have addone"
    "\r\n (DOC: X)repeat ECG\n  \n- i have adRavi can cancel",
    "x, do" + "Order placed." + "ne. Then fluids, 1.5 mg, i have ad, ded.",
    "order\norder placed. placed. Done.\n- Done.",
]


def _no_new_divergence(text, chunk):
    """The barrier cut releases what the sentence-only commit released, or the
    whole-text strip's bytes where that commit got them wrong. It must never
    produce a third answer."""
    new = _stream(text, chunk)[0]
    old = _stream(text, chunk, new=False)[0]
    assert new == old or new == _strip_forbidden_streaming(text), (chunk, text, new, old)


@pytest.mark.parametrize("text", REGRESSIONS)
def test_barrier_cuts_never_change_what_is_stripped(text):
    for chunk in (1, 2, 3, 5, 8, 13):
        _no_new_divergence(text, chunk)


def test_barrier_cuts_never_change_what_is_stripped_on_random_text():
    frags = ["- ", "  ", " ", "\n", "\n  ", "Done.", "done!", "Done", ",", ", ", ";", "1",
             "2.", "-", "Order placed", "order placed.", "I have added paracetamol",
             "I've added", "order", "I will cancel the bloods", "_I will book the CT_",
             "Ravi can cancel", "check potassium", " do", "ne.", "(DOC: LOC-1)", " (DOC: X)",
             ".", ". ", "!", "?", "\t", ",Done.", "\n  Done.", "this was performed",
             "i have ad", "ded", "x", "\r\n", "(DOC:", ")"]
    rng = random.Random(20261003)
    for _ in range(250):
        text = "".join(rng.choice(frags) for _ in range(rng.randint(1, 30)))
        for chunk in (1, 3, 7):
            _no_new_divergence(text, chunk)


def _lookbehind_widths(pattern: str):
    """Width of every lookbehind in `pattern`, from the regex parser itself."""
    try:
        import re._parser as sre_parse
        from re._constants import ASSERT, ASSERT_NOT
    except ImportError:  # Python < 3.11
        import sre_parse
        from sre_constants import ASSERT, ASSERT_NOT
    widths = []

    def walk(sub):
        for op, av in sub:
            if op in (ASSERT, ASSERT_NOT):
                direction, inner = av
                if direction == -1:
                    widths.append(inner.getwidth()[1])
                walk(inner)
            elif isinstance(av, (list, tuple)):
                for item in av:
                    if isinstance(item, sre_parse.SubPattern):
                        walk(item)
                    elif isinstance(item, (list, tuple)):
                        for x in item:
                            if isinstance(x, sre_parse.SubPattern):
                                walk(x)
            elif isinstance(av, sre_parse.SubPattern):
                walk(av)

    walk(sre_parse.parse(pattern))
    return widths


def test_the_barrier_cut_keeps_enough_context_for_every_lookbehind():
    widest = max(w for p in FORBIDDEN_PHRASE_PATTERNS for w in _lookbehind_widths(p))
    assert widest <= sr._MAX_LOOKBEHIND, (
        f"a forbidden pattern now looks back {widest} characters; raise "
        f"_MAX_LOOKBEHIND or the barrier cut can change what is stripped")


def test_no_barrier_character_can_be_part_of_a_trigger():
    s = SentenceStreamSanitizer(_strip_forbidden_streaming)
    assert s._barrier_cuts
    assert "\n" in s._barriers and "," in s._barriers
    for c in s._barriers:
        assert not any(c in t for t in s._triggers), c


# ════════════════════════════════════════════════════════════════════════════
#  4. A safety setting switched off is said at boot
# ════════════════════════════════════════════════════════════════════════════
def test_no_warning_with_the_defaults(monkeypatch, caplog):
    monkeypatch.setattr(tm, "FAST_MODE_SAFETY_FLOOR", True)
    monkeypatch.delenv("ORB_SAGE_HISTORY_MUTATION_MARKER", raising=False)
    monkeypatch.delenv("ORB_SAGE_HISTORY_MARKER", raising=False)
    with caplog.at_level(logging.WARNING, logger="orb.thinking_mode"):
        assert tm.log_safety_setting_warnings() == []
    assert not [r for r in caplog.records if "SAGE SAFETY SETTING" in r.getMessage()]


def test_each_non_default_setting_is_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(tm, "FAST_MODE_SAFETY_FLOOR", False)
    monkeypatch.setenv("ORB_SAGE_HISTORY_MUTATION_MARKER", "noact")
    monkeypatch.setenv("ORB_SAGE_HISTORY_MARKER", "off")
    with caplog.at_level(logging.WARNING, logger="orb.thinking_mode"):
        lines = tm.log_safety_setting_warnings()
    msgs = [r.getMessage() for r in caplog.records if "SAGE SAFETY SETTING" in r.getMessage()]
    assert len(lines) == 3 and len(msgs) == 3
    assert any("ORB_FAST_MODE_SAFETY_FLOOR" in m for m in msgs)
    assert any("ORB_SAGE_HISTORY_MUTATION_MARKER" in m and "noact" in m for m in msgs)
    assert any("ORB_SAGE_HISTORY_MARKER" in m for m in msgs)
    assert all(r.levelno == logging.WARNING for r in caplog.records
               if "SAGE SAFETY SETTING" in r.getMessage())


def test_the_warning_is_logged_when_the_app_imports_sage():
    """Through a fresh interpreter, so the env var is read the way boot reads
    it: a typo'd name passes an in-process test and fails this one."""
    script = textwrap.dedent("""
        import logging, sys
        sys.path.insert(0, ".")
        logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
        import routes.sage_routes
    """)
    env = {**os.environ, "ORB_DEPLOYMENT_PROFILE": "dev",
           "ORB_FAST_MODE_SAFETY_FLOOR": "0"}
    env.pop("ORB_SAGE_HISTORY_MUTATION_MARKER", None)
    env.pop("ORB_SAGE_HISTORY_MARKER", None)
    out = subprocess.run([sys.executable, "-c", script], cwd=SERVER_DIR, env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    warned = [ln for ln in out.stdout.splitlines() if "SAGE SAFETY SETTING NOT DEFAULT" in ln]
    assert len(warned) == 1 and "ORB_FAST_MODE_SAFETY_FLOOR" in warned[0], out.stdout
