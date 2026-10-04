"""The clinician PIN limiter, and the suite's per-test reset of it.

services/pin_security.PIN_ATTEMPTS locks a user out of Relay agentic
confirmation after MAX_PIN_ATTEMPTS wrong PINs inside LOCKOUT_DURATION_SECONDS.
It lives in process and is keyed by user id, so a lock left by one test would
refuse every later confirmation by that user in the session.
conftest._isolate_clinician_pin_limiter resets it around each test.

The route-level lockout is test_optimized_agentic_orders.py::
test_wrong_pin_five_times_locks_out. These two work on the limiter directly,
with a seeded user every module shares, to prove the reset did not switch it
off and that it really does stop at the test boundary. They run in file order.
"""
from services import pin_security

#: Seeded by conftest for the whole session, so a lock on it would reach
#: every later test that confirms as this doctor.
SHARED_DOCTOR = "D001"


def test_the_pin_limiter_locks_a_user_within_one_test():
    for i in range(pin_security.MAX_PIN_ATTEMPTS):
        assert not pin_security.is_pin_locked(SHARED_DOCTOR), (
            f"locked after only {i} of {pin_security.MAX_PIN_ATTEMPTS} wrong PINs"
        )
        pin_security.record_pin_failure(SHARED_DOCTOR)

    assert pin_security.is_pin_locked(SHARED_DOCTOR)


def test_the_next_test_starts_with_no_pin_failures():
    """Runs straight after the test above has locked the shared doctor out."""
    left = {uid: len(stamps) for uid, stamps in pin_security.PIN_ATTEMPTS.items() if stamps}
    assert left == {}, f"PIN failures from an earlier test still count against this one: {left}"
    assert not pin_security.is_pin_locked(SHARED_DOCTOR)
