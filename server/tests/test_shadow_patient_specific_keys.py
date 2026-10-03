"""THE INSTRUMENT FOR AN ADVICE TIER THAT IS USUALLY SILENT.

Since the 2026-08-15 ruling the advice tier says only what depends on THIS
patient's own chart, so on most alerts it correctly says nothing. That makes the
pilot's headline question unanswerable from a delivery percentage alone:

    "advice delivered on 12% of alerts"

is either the design working exactly as intended (the pre-screen found nothing
on 88% of charts, and never even called the model) or the tier being broken on
88% of the ward. Those are OPPOSITE conclusions and the old 22%/0% figure could
not tell them apart, which is a large part of why the tier was ruled decoration.

So the shadow payload has to carry the OUTCOME WORD, not just counts, and
`shadow_service.sanitize_payload` silently DROPS any key that is not on the
whitelist. A key that the emitter sets and the whitelist does not know about is
therefore invisible — the scorecard would read zeros and nobody would see a
dropped column. These tests hold the whitelist to the distinction.

They are written to fail if the whitelist entry is removed: `test_..._is_load_
bearing` re-runs the same payload against the PRE-CHANGE key set and asserts the
outcome word is dropped, which is what proves the passing assertions above it
are measuring the whitelist rather than measuring nothing.
"""
from services import shadow_service


SILENT_BY_DESIGN = {
    "patient_specific": "no_candidates",
    "patient_specific_n": 0,
    "patient_specific_candidates": 0,
    # THE THROUGHPUT FACT: no candidates means the GPU was never touched.
    "patient_specific_model_called": False,
    "patient_specific_dropped_generic": 0,
    "patient_specific_dropped_unsourced": 0,
    "patient_specific_ms": 4,
}

SILENT_BECAUSE_IT_FAILED = {
    "patient_specific": "failed",
    "patient_specific_n": 0,
    "patient_specific_candidates": 3,
    "patient_specific_model_called": True,
    "patient_specific_dropped_generic": 0,
    "patient_specific_dropped_unsourced": 0,
    "patient_specific_ms": 6,
}


def test_the_outcome_word_survives_the_whitelist():
    out = shadow_service.sanitize_payload("sage.drafted", dict(SILENT_BY_DESIGN))
    assert out["patient_specific"] == "no_candidates"
    assert out["patient_specific_model_called"] is False
    assert out["patient_specific_candidates"] == 0
    assert out["patient_specific_ms"] == 4


def test_silent_by_design_and_silent_by_failure_do_not_collapse():
    """The one property this whole change exists to protect.

    Both rows report zero flags. If the instrument only stored the count they
    would be the same row, and a scorecard built on them would report a working
    tier and a broken one as one number.
    """
    designed = shadow_service.sanitize_payload("sage.drafted", dict(SILENT_BY_DESIGN))
    failed = shadow_service.sanitize_payload("sage.drafted", dict(SILENT_BECAUSE_IT_FAILED))

    assert designed["patient_specific_n"] == failed["patient_specific_n"] == 0, (
        "the fixture no longer exercises the collapse it was written for")
    assert designed["patient_specific"] != failed["patient_specific"]
    assert designed["patient_specific"] == "no_candidates"
    assert failed["patient_specific"] == "failed"


def test_every_outcome_word_round_trips_distinctly():
    seen = {
        outcome: shadow_service.sanitize_payload(
            "sage.drafted", {"patient_specific": outcome})["patient_specific"]
        for outcome in ("flags", "no_candidates", "no_material", "failed")
    }
    assert seen == {
        "flags": "flags",
        "no_candidates": "no_candidates",
        "no_material": "no_material",
        "failed": "failed",
    }


def test_the_model_call_fraction_is_recorded_as_a_fact_not_inferred():
    """"What fraction of a realistic ward triggers a model call" is the ruling's
    own throughput claim, and it must be countable directly.

    Inferring it from timings would make a fast draft and a skipped one
    indistinguishable — exactly the ambiguity the outcome word removes.
    """
    skipped = shadow_service.sanitize_payload("sage.drafted", dict(SILENT_BY_DESIGN))
    called = shadow_service.sanitize_payload(
        "sage.drafted",
        {"patient_specific": "flags", "patient_specific_n": 2,
         "patient_specific_candidates": 4, "patient_specific_model_called": True},
    )
    assert skipped["patient_specific_model_called"] is False
    assert called["patient_specific_model_called"] is True


def test_the_filter_counts_are_kept_apart_by_reason():
    """A line that restated the alert and a line that named a fact the
    pre-screen never supplied are different failures — the second is the model
    reaching into its own memory, which is the thing this tier's whole design
    exists to make impossible. One combined counter would hide it."""
    out = shadow_service.sanitize_payload("sage.drafted", {
        "patient_specific": "flags",
        "patient_specific_n": 1,
        "patient_specific_dropped_generic": 2,
        "patient_specific_dropped_unsourced": 1,
    })
    assert out["patient_specific_dropped_generic"] == 2
    assert out["patient_specific_dropped_unsourced"] == 1


def test_free_clinical_prose_still_cannot_ride_in_on_the_new_keys():
    """The whitelist is a PHI boundary, not just a schema. The new keys are an
    outcome word, counts and a duration — the flag text itself stays in the
    assessment row that already holds it, pointed at by subject_id."""
    out = shadow_service.sanitize_payload("sage.drafted", {
        "patient_specific": "flags",
        "patient_specific_flags": ["on metoprolol 25 mg BD"],
        "patient_specific_fact": "creatinine 180 six days ago",
        "caution": "Heart rate 78 may be falsely reassuring",
    })
    assert out == {"patient_specific": "flags"}


def test_the_whitelist_entry_is_load_bearing(monkeypatch):
    """PROVE-CAN-FAIL for every assertion above.

    Re-run the identical payload against the PRE-CHANGE key set. If the outcome
    word survives that too, `sanitize_payload` is not enforcing the whitelist and
    the tests above prove nothing about it.
    """
    before = shadow_service.PAYLOAD_KEYS["sage.drafted"] - {
        "patient_specific",
        "patient_specific_n",
        "patient_specific_candidates",
        "patient_specific_model_called",
        "patient_specific_dropped_generic",
        "patient_specific_dropped_unsourced",
        "patient_specific_ms",
    }
    monkeypatch.setitem(shadow_service.PAYLOAD_KEYS, "sage.drafted", before)

    out = shadow_service.sanitize_payload("sage.drafted", dict(SILENT_BY_DESIGN))
    assert out == {}, (
        "the outcome word survived a key set that does not contain it, so the "
        "whitelist is not what is admitting it and these tests measure nothing")

    # And the two silences really would collapse without it — this is the defect
    # the whitelist entry prevents, demonstrated rather than asserted.
    failed = shadow_service.sanitize_payload("sage.drafted", dict(SILENT_BECAUSE_IT_FAILED))
    assert out == failed, (
        "without the outcome word, 'nothing to flag' and 'the check never ran' "
        "are the same shadow row")


def test_the_existing_sage_drafted_keys_are_untouched():
    """The tier split's timing facts still ride on the same kind — a whitelist
    edit that dropped one of them would silently void the latency claim."""
    keys = shadow_service.PAYLOAD_KEYS["sage.drafted"]
    for existing in ("ttft_ms", "first_step_ms", "depth", "query_class",
                     "citation_basis", "cited_doc_ids", "cancelled"):
        assert existing in keys
