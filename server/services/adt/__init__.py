"""Orb: the beds and admissions (ADT) service package.

One module per concern, so several clusters can build the epic in the same
wave without editing a shared file (docs/HIS_BUILD_PLAN_2026-10.md, decision
X3). This file deliberately imports nothing: callers import the module they
need, and a later module (transfer, presence, reservations) is added without
touching this one.

  core      adt-3 (W3-B). The single writer of where an admission is:
            current_location (the one reader other epics use),
            record_movement_on (the single patient_movements writer) and
            locate_admission_on (the single writer of admissions.ward_id,
            bed_id and both text mirrors). Errors, the module switch and the
            in-transaction audit helper live here too.
  end_stay  adt-8a (W3-B). Ends of stay: close_admission_on (the status flips
            and bundle closure discharge_patient used to do inline),
            discharge_preconditions (every door calls it; returns [] until
            ruling R009 lands), end_stay_on and record_end_on (discharge type,
            the 'discharge' movement, the de-duplicated NABH disposition, the
            portal login on a death) and end_stay (the POST /end door).

Everything here is inert while ORB_ADT is off: no lane reaches it except
discharge_service.discharge_patient, which calls close_admission_on and
discharge_preconditions with output identical to the code they replaced.
"""
