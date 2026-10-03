"""The 0-1 NEWS2 index never reaches a prompt, and every surface says NEWS2 alike.

THE DEFECT (verified 2026-10-02). `compute_news2`'s `score` is a 0-1 NORMALISED
INDEX of the ordinal aggregate, explicitly not a calibrated probability, and the
risk_scores row stores that index. Five LLM prompts still printed it in a slot
labelled "risk":

    sage_service channel A    RISK SCORE: 0.55 (critical): [...]
    sage_service census       CRITICAL(0.55) ...
    sage_narrator             CRITICAL risk 0.55        (shown in the ward brief)
    surgery_service           CURRENT RISK LEVEL: critical (score: 0.55)
    voice_agent_service       Risk: Score 0.55: Level: critical

A model reads 0.55 as a 55% chance of something. Those slots now state the NEWS2
integer and band, or only the band where all a surface holds is a risk_scores
row. Every risk row below is planted with the index INDEX, whose digits must
appear in no prompt.

THE LABEL. "NEWS2 n (level)" was hand-rolled in a dozen places and each copy
said a different subset of the facts. services/news2_text.news2_description is
now the one sentence (the twin of src/lib/news2.ts news2Description) and adds
"incomplete set, minimum" when the observation set was partial: an unmeasured
parameter scores 0, so the aggregate is a floor.

THE HIDDEN HALF. Sage's three context builders, the downtime snapshot and the
escalation sweep filled an unassessed ACVPU with "alert" and an unassessed O2
with False before scoring. Same points, but compute_news2 then reported the set
COMPLETE, so no surface could ever say "minimum" for them. They now pass None
through, as score_vitals_row always has; the tests below prove both halves (the
clause appears, and the aggregate does not move).
"""
import asyncio
import json
import re
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session                                        # noqa: E402
from services.news2_text import (INCOMPLETE, NOT_RECORDED, SCALE_2,    # noqa: E402
                                 SINGLE_PARAM_3, news2_description,
                                 news2_qualifiers)
from services.vigil_service import compute_news2                       # noqa: E402

SERVER = Path(__file__).resolve().parent.parent
REPO = SERVER.parent

# The index planted on every stored risk row. Distinctive on purpose: no vital,
# timestamp or NEWS2 integer can contain these digits by accident.
INDEX = 0.4321
INDEX_TEXT = "0.4321"

DEPT = f"News2 Label Medicine {uuid.uuid4().hex[:6]}"

# NEWS2 20, every parameter maxed, ACVPU and O2 assessed: a COMPLETE set.
PERI_ARREST = dict(heart_rate=140, systolic_bp=80, respiratory_rate=32,
                   temperature=34.5, spo2=80, consciousness="voice", on_oxygen=1)
# NEWS2 0, complete.
QUIET = dict(heart_rate=70, systolic_bp=120, respiratory_rate=16,
             temperature=37.0, spo2=98, consciousness="alert", on_oxygen=0)
# A monitor feed: five numbers, NO ACVPU and NO O2 (both human assessments).
# RR 23 = 2, SpO2 93 = 2, HR 95 = 1, Temp 38.5 = 1, SBP 120 = 0: NEWS2 6.
MONITOR = dict(heart_rate=95, systolic_bp=120, respiratory_rate=23,
               temperature=38.5, spo2=93, consciousness=None, on_oxygen=None)


# ═════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═════════════════════════════════════════════════════════════════════════════
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: discharge what this module admits, and delete the
    escalation chains it raises, so no later module measures our leftovers."""
    yield
    if not _CREATED:
        return
    marks = ",".join("?" * len(_CREATED))
    with db_session() as db:
        db.execute(
            f"DELETE FROM escalation_events WHERE escalation_id IN "
            f"(SELECT id FROM clinical_escalations WHERE patient_id IN ({marks}))",
            tuple(_CREATED))
        db.execute(f"DELETE FROM clinical_escalations WHERE patient_id IN ({marks})",
                   tuple(_CREATED))
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   tuple(_CREATED))
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})",
                   tuple(_CREATED))
    _CREATED.clear()


def _mk_patient(*, scale2=False, case_room=False, name=None) -> str:
    pid = f"N2L{uuid.uuid4().hex[:10].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('N2LADM', 'News2 Label Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, "
            "status, admission_date, on_scale_2) "
            "VALUES (?, ?, ?, ?, ?, 68, 'F', 'Community-acquired pneumonia', "
            "'admitted', ?, ?)",
            (pid, name or f"Labelcase {pid[-5:]}", f"MRN-{uuid.uuid4().hex[:10]}",
             f"NL-{pid[-3:]}", DEPT, now, int(scale2)))
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', 'N2LADM')",
            (pid, DEPT, DEPT, f"NL-{pid[-3:]}"))
        if case_room:
            db.execute("INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
                       "VALUES (?, ?, 'case_room', ?)",
                       (f"n2l-room-{pid}", f"N2L Case Room {pid}", pid))
    return pid


def _chart(pid: str, v: dict, *, when=None):
    ts = (when or datetime.now(timezone.utc)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, consciousness, on_oxygen, timestamp) "
            "VALUES (?, ?, ?, 70, ?, ?, ?, ?, ?, ?)",
            (pid, v["heart_rate"], v["systolic_bp"], v["spo2"], v["temperature"],
             v["respiratory_rate"], v["consciousness"], v["on_oxygen"], ts))


def _risk_row(pid: str, level: str, drivers=("Tachypnea (RR 23)",), hours_ago=6):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, INDEX, level, json.dumps(list(drivers)), ts))


def _doctor() -> dict:
    uid = f"N2LDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'News2 Label Doctor', 'doctor', 'x', ?)", (uid, DEPT))
    return {"id": uid, "role": "doctor", "department": DEPT, "name": "News2 Label Doctor"}


def _channel_a(pid: str) -> list[str]:
    from services.sage_service import _assemble_patient_context, format_patient_channel_a
    with db_session() as db:
        facts = _assemble_patient_context(db, pid, None)
    return format_patient_channel_a(facts, pid)


def _line(lines: list[str], prefix: str) -> str:
    found = [ln for ln in lines if ln.startswith(prefix)]
    assert len(found) == 1, f"expected one {prefix!r} line, got {found} in {lines}"
    return found[0]


# ═════════════════════════════════════════════════════════════════════════════
#  1. The helper, and its twin in the front end
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("news2,expected", [
    (None, NOT_RECORDED),
    ({}, NOT_RECORDED),
    ({"raw_news2": None, "level": "stable"}, NOT_RECORDED),
    ({"raw_news2": 0, "level": "stable"}, "NEWS2 0 (stable)"),
    ({"raw_mews": 4, "level": "stable"}, "NEWS2 4 (stable)"),
    ({"raw_news2": 9, "level": "critical", "complete": True}, "NEWS2 9 (critical)"),
    ({"raw_news2": 3, "level": "elevated", "single_param_3": True},
     "NEWS2 3 (elevated) · a single parameter scores 3"),
    ({"raw_news2": 0, "level": "stable", "spo2_scale": 2},
     "NEWS2 0 (stable) · SpO2 Scale 2"),
    ({"raw_news2": 6, "level": "elevated", "complete": False},
     "NEWS2 6 (elevated) · incomplete set, minimum"),
    ({"raw_news2": 8, "level": "critical", "single_param_3": True, "spo2_scale": 2,
      "complete": False},
     "NEWS2 8 (critical) · a single parameter scores 3 · SpO2 Scale 2 · "
     "incomplete set, minimum"),
    ({"raw_news2": 5}, "NEWS2 5"),
])
def test_the_label_states_each_fact_once_in_the_frontend_order(news2, expected):
    assert news2_description(news2) == expected


def test_a_dict_without_complete_makes_no_coverage_claim():
    """A chain row or a hand-built dict never said whether ACVPU and O2 were
    assessed. Saying "minimum" there would be inventing a finding."""
    assert INCOMPLETE not in news2_description({"raw_news2": 6, "level": "elevated"})
    assert news2_qualifiers({"raw_news2": 6, "complete": None}) == []


def test_scale_false_leaves_the_scale_to_the_surface_that_words_it_itself():
    n = {"raw_news2": 5, "level": "elevated", "spo2_scale": 2, "complete": False}
    assert news2_qualifiers(n, scale=False) == [INCOMPLETE]
    assert news2_description(n, scale=False) == "NEWS2 5 (elevated) · incomplete set, minimum"


def test_the_server_label_is_the_twin_of_news2Description():
    """Read the TypeScript, not a copy of it: if the front end rewords a clause,
    this goes red until the server says the same thing. The one deliberate
    difference is the plain "SpO2" in server strings."""
    ts = (REPO / "src" / "lib" / "news2.ts").read_text(encoding="utf-8")
    assert "`NEWS2 ${news2.raw_news2} (${news2.level})`" in ts
    assert f"' · {SINGLE_PARAM_3}'" in ts
    assert f"' · {SCALE_2.replace('SpO2', 'SpO₂')}'" in ts
    # The minimum clause reached the badge on 2026-10-03, same words, last.
    assert f"(news2.complete === false ? ' · {INCOMPLETE}' : '')" in ts
    assert "'NEWS2 not recorded'" in ts and NOT_RECORDED == "NEWS2 not recorded"


def test_compute_news2_results_feed_the_label_directly():
    full = compute_news2(140, 80, 32, 34.5, 80, "voice", True)
    assert news2_description(full) == "NEWS2 20 (critical) · a single parameter scores 3"
    partial = compute_news2(95, 120, 23, 38.5, 93)          # monitor feed
    assert news2_description(partial) == "NEWS2 6 (elevated) · incomplete set, minimum"


# ═════════════════════════════════════════════════════════════════════════════
#  2. Sage's chart block (Channel A), the broad lane, and the census
# ═════════════════════════════════════════════════════════════════════════════
def test_channel_a_states_news2_and_its_band_never_the_index():
    pid = _mk_patient()
    _risk_row(pid, "stable")
    _chart(pid, PERI_ARREST)

    lines = _channel_a(pid)
    text = "\n".join(lines)
    assert INDEX_TEXT not in text, f"the 0-1 index reached the prompt:\n{text}"
    assert not any(ln.startswith("RISK SCORE") for ln in lines), lines
    risk = _line(lines, "RISK:")
    assert risk.startswith("RISK: NEWS2 20 (critical) · a single parameter scores 3. "
                           "Drivers: "), risk
    # Drivers as text, not a Python list repr.
    assert "['" not in risk and "(RR 32)" in risk, risk
    # The fresh index is not there either.
    assert f"{compute_news2(140, 80, 32, 34.5, 80, 'voice', True)['score']}" not in risk


def test_an_unassessed_acvpu_and_o2_make_the_score_a_stated_minimum():
    """Both halves of the pass-through fix: the aggregate is exactly what the
    old "alert"/False defaults produced, and the prompt now says it is a floor."""
    pid = _mk_patient()
    _risk_row(pid, "elevated")
    _chart(pid, MONITOR)

    lines = _channel_a(pid)
    risk = _line(lines, "RISK:")
    assert "NEWS2 6 (elevated) · incomplete set, minimum" in risk, risk
    authoritative = _line(lines, "NEWS2 (computed")
    assert authoritative.endswith(
        ": 6 (incomplete set, minimum; not recorded: Consciousness (ACVPU), "
        "Supplemental O2)"), authoritative

    old_default = compute_news2(95, 120, 23, 38.5, 93, "alert", False)
    assert old_default["raw_news2"] == 6 and old_default["complete"] is True
    from services.sage_service import _fresh_risk_block
    with db_session() as db:
        block = _fresh_risk_block(db, pid)
    assert block["news2"] == old_default["raw_news2"], "the pass-through moved the score"
    assert block["news2_complete"] is False
    assert block["news2_missing"] == ["Consciousness (ACVPU)", "Supplemental O2"]


def test_a_complete_set_carries_no_minimum_clause():
    pid = _mk_patient()
    _risk_row(pid, "stable")
    _chart(pid, QUIET)
    lines = _channel_a(pid)
    assert INCOMPLETE not in "\n".join(lines)
    assert _line(lines, "NEWS2 (computed").endswith(": 0")


def test_a_band_raised_by_the_stored_row_is_not_credited_to_news2():
    """_reconciled_band lets a stored row RAISE the band (the CfC overlay). The
    line must not read "NEWS2 0 (critical)": NEWS2 never said critical."""
    pid = _mk_patient()
    _risk_row(pid, "critical")
    _chart(pid, QUIET)

    risk = _line(_channel_a(pid), "RISK:")
    assert risk.startswith(
        "RISK: NEWS2 0 (stable), band raised to critical by the stored risk row."), risk
    assert "NEWS2 0 (critical)" not in risk


def test_with_no_observation_only_the_stored_band_is_stated():
    pid = _mk_patient()
    _risk_row(pid, "elevated", drivers=["Tachypnea (RR 23)", "Fever (38.5°C)"])

    risk = _line(_channel_a(pid), "RISK:")
    assert risk == ("RISK: NEWS2 not recorded; stored risk band elevated. "
                    "Drivers: Tachypnea (RR 23); Fever (38.5°C)"), risk


def test_the_broad_lane_pin_also_says_minimum():
    from services.sage_service import _authoritative_news2_line
    partial, full = _mk_patient(), _mk_patient()
    _chart(partial, MONITOR)
    _chart(full, QUIET)
    with db_session() as db:
        p_line = _authoritative_news2_line(db, partial, "Partial Patient")
        f_line = _authoritative_news2_line(db, full, "Full Patient")
    assert p_line.splitlines()[0].endswith(
        ": 6 (incomplete set, minimum; not recorded: Consciousness (ACVPU), "
        "Supplemental O2)"), p_line
    assert f_line.splitlines()[0].endswith(": 0"), f_line


def test_the_census_row_states_the_band_never_the_index():
    from services.sage_service import get_relevant_context
    doctor = _doctor()
    pid = _mk_patient(name="Censusrow Bandonly")
    _chart(pid, MONITOR)
    _risk_row(pid, "critical", drivers=["Tachypnea (RR 23)", "Hypoxia (SpO2 93%)"],
              hours_ago=0)

    context, *_ = get_relevant_context("give me an overview of all my patients", doctor)
    row = next((ln for ln in context.splitlines() if "Censusrow Bandonly" in ln), "")
    assert row, context[:2000]
    assert "risk band CRITICAL: Tachypnea (RR 23); Hypoxia (SpO2 93%)" in row, row
    assert INDEX_TEXT not in row and "CRITICAL(" not in row, row


# ═════════════════════════════════════════════════════════════════════════════
#  3. The ward brief, the pre-op assessment and voice
# ═════════════════════════════════════════════════════════════════════════════
class _Captured:
    def __init__(self):
        self.bodies: list[dict] = []


def _fake_model(monkeypatch, captured: _Captured, *, stream_line: str = ""):
    """Stand in for the model and the queue, keeping the prompt that was sent."""
    import httpx
    import services.model_router as model_router
    from services.llm_queue_service import llm_queue

    async def _choice(*_a, **_k):
        return types.SimpleNamespace(model="fake-model")

    async def _not_evicted(*_a, **_k):
        return False

    async def _acquire(*_a, **_k):
        return True

    class _Resp:
        status_code = 200

        def json(self):
            return {"response": "brief"}

        async def aiter_lines(self):
            yield stream_line

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *_a):
            return False

    class _Client:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, _url, json=None, **_k):
            captured.bodies.append(json)
            return _Resp()

        def stream(self, _method, _url, json=None, **_k):
            captured.bodies.append(json)
            return _Stream()

    monkeypatch.setattr(model_router, "select_chat_model", _choice)
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)
    monkeypatch.setattr(llm_queue, "acquire", _acquire)
    monkeypatch.setattr(llm_queue, "release", lambda *a, **k: None)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_the_ward_brief_prompt_states_the_band_never_the_index(monkeypatch):
    from services.sage_narrator_service import WardNarrator
    captured = _Captured()
    _fake_model(monkeypatch, captured)
    snapshot = {
        "census": 12,
        "risk_distribution": {"critical": 1, "elevated": 1, "stable": 10},
        "active_alerts": 2,
        "watchlist": [
            {"name": "Anita Rao", "bed": "GM-04", "ward": "GM", "level": "critical",
             "score": INDEX},
            {"name": "Ravi Kumar", "bed": "GM-07", "ward": "GM", "level": "elevated",
             "score": 0.31},
        ],
    }
    assert asyncio.run(WardNarrator()._narrate(snapshot)) == "brief"
    prompt = captured.bodies[0]["prompt"]
    assert "- Anita Rao (bed GM-04, GM): CRITICAL risk band" in prompt, prompt
    assert "- Ravi Kumar (bed GM-07, GM): ELEVATED risk band" in prompt, prompt
    assert INDEX_TEXT not in prompt and "0.31" not in prompt, prompt


def test_the_ward_brief_prompt_states_the_board_news2_when_it_has_one(monkeypatch):
    """Since the brief is scored from the board's latest vitals (2026-10-03)
    each watchlist entry carries the NEWS2 integer, so the prompt says the
    badge's sentence, minimum clause included."""
    from services.sage_narrator_service import WardNarrator
    captured = _Captured()
    _fake_model(monkeypatch, captured)
    snapshot = {
        "census": 12, "risk_distribution": {"critical": 1, "elevated": 1, "stable": 10},
        "active_alerts": 2,
        "watchlist": [
            {"name": "Anita Rao", "bed": "GM-04", "ward": "GM", "level": "critical",
             "score": INDEX, "news2": 9, "single_param_3": True, "spo2_scale": 1,
             "complete": True},
            {"name": "Ravi Kumar", "bed": "GM-07", "ward": "GM", "level": "elevated",
             "score": 0.31, "news2": 6, "single_param_3": False, "spo2_scale": 1,
             "complete": False},
        ],
    }
    assert asyncio.run(WardNarrator()._narrate(snapshot)) == "brief"
    prompt = captured.bodies[0]["prompt"]
    assert ("- Anita Rao (bed GM-04, GM): NEWS2 9 (critical) · a single parameter "
            "scores 3") in prompt, prompt
    assert ("- Ravi Kumar (bed GM-07, GM): NEWS2 6 (elevated) · incomplete set, "
            "minimum") in prompt, prompt
    assert INDEX_TEXT not in prompt and "0.31" not in prompt, prompt


def test_the_ward_brief_snapshot_carries_the_label_fields(monkeypatch):
    """The fields the prompt needs come off the same score the board shows."""
    from services import sage_narrator_service as sns
    from services import vigil_service
    patient = {"id": "N2LWB1", "name": "Wardbrief Labelcase", "bed": "W-1", "ward": DEPT,
               "admission_department": DEPT}
    vitals = {"timestamp": "2026-10-03T01:00:00+00:00"}
    scored = compute_news2(95, 120, 23, 38.5, 93)            # monitor feed, NEWS2 6
    monkeypatch.setattr(vigil_service, "scored_admitted_patients",
                        lambda _dept: [(patient, vitals, scored)])
    entry = next(w for w in sns.WardNarrator._build_ward_snapshot()["watchlist"]
                 if w["patient_id"] == "N2LWB1")
    assert (entry["news2"], entry["complete"], entry["single_param_3"],
            entry["spo2_scale"]) == (6, False, False, 1), entry


def _surgery_detail(**over) -> dict:
    detail = {
        "patient_name": "X", "age": 50, "sex": "M", "mrn": "M1",
        "patient_diagnosis": "Sepsis", "procedure_name": "Laparotomy",
        "diagnosis": "Perforation", "co_morbidities": ["DM", "HTN"],
        "risk_level": "critical", "risk_score": INDEX, "estimated_duration_min": 120,
        "possible_complications": ["bleeding"], "success_probability": 72.5,
    }
    detail.update(over)
    return detail


def _surgery_prompt(monkeypatch, detail: dict) -> str:
    from services.surgery_service import generate_risk_assessment
    captured = _Captured()
    _fake_model(monkeypatch, captured,
                stream_line=json.dumps({"message": {"content": "ok"}, "done": True}))

    async def drain():
        return [c async for c in generate_risk_assessment("S-N2L", detail=detail)]

    asyncio.run(drain())
    return captured.bodies[0]["messages"][0]["content"]


def test_the_preop_prompt_states_the_band_never_the_index(monkeypatch):
    prompt = _surgery_prompt(monkeypatch, _surgery_detail())
    assert "CURRENT RISK LEVEL: critical\n" in prompt, prompt
    assert INDEX_TEXT not in prompt and "score:" not in prompt, prompt


def test_the_preop_prompt_does_not_invent_stable_for_a_patient_with_no_risk_row(monkeypatch):
    """get_surgery_detail defaults risk_level to "stable" for the UI when the
    LEFT JOIN finds no row. That default is not a finding."""
    prompt = _surgery_prompt(monkeypatch, _surgery_detail(risk_level="stable", risk_score=None))
    assert "CURRENT RISK LEVEL: not recorded (no Vigil risk band on file)" in prompt, prompt
    assert "CURRENT RISK LEVEL: stable" not in prompt


def test_voice_states_news2_like_sage_and_prints_charted_vitals():
    from services.sage_service import _fresh_risk_block, _risk_summary_line
    from services.voice_agent_service import build_patient_context
    pid = _mk_patient()
    _risk_row(pid, "elevated")
    _chart(pid, dict(MONITOR, heart_rate=95.4, temperature=38.46))

    ctx = build_patient_context(pid, {"id": "N2LADM", "role": "doctor"})
    assert INDEX_TEXT not in ctx and "Score" not in ctx, ctx
    with db_session() as db:
        sage_line = _risk_summary_line(_fresh_risk_block(db, pid))
    assert sage_line in ctx.splitlines(), f"voice and Sage disagree:\n{ctx}\n{sage_line}"
    assert sage_line.startswith("RISK: NEWS2 6 (elevated) · incomplete set, minimum."), sage_line
    assert "HR 95," in ctx and "Temp 38.5°C" in ctx and "95.4" not in ctx, ctx


def test_voice_with_no_observation_states_the_stored_band():
    from services.voice_agent_service import build_patient_context
    pid = _mk_patient()
    _risk_row(pid, "critical", drivers=["Hypotension (SBP 95)"])
    ctx = build_patient_context(pid, {"id": "N2LADM", "role": "doctor"})
    assert ("RISK: NEWS2 not recorded; stored risk band critical. "
            "Drivers: Hypotension (SBP 95)") in ctx, ctx
    assert INDEX_TEXT not in ctx


def test_no_prompt_builder_interpolates_the_risk_index():
    """Source guard for the five sites: the index-bearing expressions that used
    to sit inside these prompts must not come back."""
    banned = {
        "services/sage_service.py": ["RISK SCORE: {", "({r['score']})"],
        "services/sage_narrator_service.py": ["risk {w['score']}"],
        "services/surgery_service.py": ["(score: {detail['risk_score']})"],
        "services/voice_agent_service.py": ["Risk: Score {"],
    }
    for rel, needles in banned.items():
        # Code only: the comments that record the old lines are history.
        src = "\n".join(ln.split("#", 1)[0] for ln in
                        (SERVER / rel).read_text(encoding="utf-8").splitlines())
        for needle in needles:
            assert needle not in src, f"{rel} prints the 0-1 index again: {needle!r}"


# ═════════════════════════════════════════════════════════════════════════════
#  4. The display surfaces take their facts from the same label
# ═════════════════════════════════════════════════════════════════════════════
# Every f-string that writes "NEWS2 {...}" in server code, except the ones
# listed here with the reason they are not the label.
_ALLOWED_HAND_ROLLED = [
    # The label itself.
    (r"services/news2_text\.py", r"NEWS2 \{total\}"),
    # The band alone, by design: the Relay reason line's last resort when there
    # is no breakdown (ruled 2026-10-03, G2), and the alert label's fallback
    # when there is no aggregate to print.
    (r"services/(bot|ingestion|nurse_shift)_service\.py",
     r'NEWS2 \{(risk_level|level)\}"'),
    (r"services/vigil_service\.py", r"NEWS2 \{news2\.get\('level', 'stable'\)\}"),
    # A trigger criterion, not a label: why the sepsis bundle opened.
    (r"services/sepsis_bundle_service\.py", r"NEWS2 \{raw\} \(>=7\)"),
    # I-PASS keeps its own band wording ("elevated band") and its own Scale 2
    # text with the target; the remaining clauses come from news2_qualifiers.
    (r"services/ipass_service\.py", r"NEWS2 \{news2\} \(\{_level_phrase"),
    (r"services/ipass_service\.py", r'f"NEWS2 \{raw_news2\}" if raw_news2'),
]


def test_every_news2_label_in_server_code_comes_from_news2_text():
    offenders = []
    for path in sorted([*(SERVER / "services").glob("*.py"), *(SERVER / "routes").glob("*.py")]):
        rel = path.relative_to(SERVER).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if not re.search(r'f["\'][^"\']*NEWS2 \{', code):
                continue
            if any(re.fullmatch(f, rel) and re.search(p, code)
                   for f, p in _ALLOWED_HAND_ROLLED):
                continue
            offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, ("hand-rolled NEWS2 label; use services.news2_text:\n"
                           + "\n".join(offenders))


async def test_a_monitor_feed_alert_says_its_score_is_a_minimum(monkeypatch):
    """The visible change. A monitor never sends ACVPU or O2, so the Relay
    alert's NEWS2 is a floor, and the nurse reading it is now told so."""
    from services import bot_service, ingestion_service
    monkeypatch.setattr(bot_service, "publish_vigil_critical", lambda *a, **k: True)
    pid = _mk_patient(case_room=True)
    payload = dict(MONITOR, diastolic_bp=70, source="ingested_json")
    risk = compute_news2(payload["heart_rate"], payload["systolic_bp"],
                         payload["respiratory_rate"], payload["temperature"],
                         payload["spo2"])

    await ingestion_service._check_ingested_alert(pid, payload, risk)

    with db_session() as db:
        row = db.execute(
            "SELECT m.content FROM messages m JOIN channels c ON c.id = m.channel_id "
            "WHERE c.patient_id = ? AND m.message_type = 'alert' ORDER BY m.id DESC LIMIT 1",
            (pid,)).fetchone()
    assert row is not None
    assert "\nNEWS2 6 (elevated) · incomplete set, minimum. Immediate review" in row["content"], \
        row["content"]


def test_the_escalation_chain_labels_its_events_and_its_relay_post(monkeypatch):
    from services import escalation_service as esc
    from services import ingestion_service
    posted: list[str] = []
    monkeypatch.setattr(ingestion_service, "_persist_relay_alert",
                        lambda _pid, content: (posted.append(content) or (False, None, None, None, None)))
    pid = _mk_patient()
    news2 = compute_news2(125, 88, 26, 38.5, 91)        # monitor-style: no ACVPU, no O2
    label = news2_description(news2)
    assert INCOMPLETE in label and SINGLE_PARAM_3 in label

    chain = esc.evaluate_observation(pid, news2, notify=True)
    assert chain is not None
    with db_session() as db:
        detail = db.execute(
            "SELECT detail FROM escalation_events WHERE escalation_id = ? "
            "AND event_type = 'raised'", (chain["id"],)).fetchone()["detail"]
    assert detail.startswith(f"{label} crossed the escalation threshold"), detail
    assert posted and f"]: {label}\n" in posted[0], posted


def test_the_handover_i_line_adds_the_missing_clauses_and_keeps_its_own_words():
    from services.ipass_service import _build_illness_severity
    isv = _build_illness_severity({
        "raw_news2": 3, "level": "elevated", "single_param_3": True, "complete": False,
        "spo2_scale": 2, "drivers": ["Severe hypoxia (SpO2 82%, Scale 2)"]})
    assert isv["text"] == (
        "Watcher: NEWS2 3 (elevated band, SpO2 Scale 2, target 88-92%) · a single "
        "parameter scores 3 · incomplete set, minimum. Key drivers: Severe hypoxia "
        "(SpO2 82%, Scale 2)."), isv["text"]


def test_the_downtime_pack_says_minimum_in_its_summary_and_its_cells():
    from services import downtime_service as dt
    news2 = compute_news2(95, 120, 23, 38.5, 93)
    summary = dt._build_summary({"name": "P", "mrn": "M"}, [], [], [], news2)
    assert summary.endswith("NEWS2 6 (elevated) · incomplete set, minimum."), summary

    cell = dt._render_vitals_news2({"latest_vitals": {"heart_rate": 95}, "news2": news2})
    assert "6 (elevated) · incomplete set, minimum" in cell, cell


def test_the_downtime_snapshot_reports_an_unassessed_set_as_incomplete():
    from services.downtime_service import capture_snapshot
    pid = _mk_patient()
    _chart(pid, MONITOR)
    env = capture_snapshot(pid, created_by="test")
    n = env["snapshot"]["news2"]
    assert n["raw_news2"] == 6 and n["complete"] is False, n
    assert env["summary"].endswith("NEWS2 6 (elevated) · incomplete set, minimum."), env["summary"]


def test_the_assessment_headline_uses_the_label():
    from services.sage_assessment_service import _tier1_headline
    headline = _tier1_headline({
        "news2": 7, "news2_band": "critical", "single_param_3": True,
        "spo2_scale": 1, "complete": False, "news2_delta": 2, "observations_used": 3,
        "coverage_note": "INCOMPLETE observation set: Supplemental O2 not measured."})
    assert headline.startswith(
        "NEWS2 7 (critical) · a single parameter scores 3 · incomplete set, minimum, "
        "up 2 across the last 3 observations."), headline
