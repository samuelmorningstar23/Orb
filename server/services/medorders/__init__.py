"""Orb: the medication-order store service package (medorders epic).

Decision A1 (Samuel, 3 October 2026): one authoritative medication order,
med_orders, with its append-only ledger, med_order_events (migration 61). The
tables that hold medication facts today stay, each with one job: medications is
the ward projection the chart and the eMAR read, a helix_patient_med_orders row
is one supply request, a CPOE medication order is the draft view, and
medication_administrations is the MAR. Nothing is renamed or rebuilt.

One module per concern, so several clusters can build the epic in the same
wave without editing a shared file (decision X3). This file deliberately
imports nothing: callers import the module they need.

  core      medorders-1 (W4-E). The state machine and everything every lane
            shares: LEGAL_TRANSITIONS, order_allows_dose, legacy_status_for,
            projection_for, fhir_status_for, cpoe_status_for,
            screen_fingerprint, and the in-transaction writers
            create_in_txn, transition_in_txn, record_event_in_txn,
            link_projection_in_txn and project_in_txn. The only writer of
            med_orders and med_order_events.
  adoption  medorders-2 (W4-E). Adopting existing chart rows into the store
            (at boot with ORB_MED_ORDER_STORE on, from
            scripts/medorder_parity.py --adopt, and from the admin route) and
            the read-only parity report. Never on a GET path.

Modules later waves add here (docs/HIS_BUILD_PLAN_2026-10.md): chart_lane,
emar_check and queries (W5-E), verify and lifecycle (W6-D), supply (W7-E),
cpoe (W7-F), ai_lanes (W8-E), schedule (W8-I), screening (W9-C), outpatient
(W8-C). Each writes through core's in-transaction functions.

THE TWO INVARIANTS every module here keeps (design section PRINCIPLE):
  I1  The verification fact stays on medications.verified_by, which every
      verify path writes; the store adds only what the ward row cannot say
      (held). So a legacy writer can never make a verified drug ungivable.
  I2  Every code path that changes a LINKED ward row writes through the store,
      keyed on the link and not on the switch. Unlinked rows keep exactly
      today's behaviour.

THE SWITCHES. ORB_MED_ORDER_STORE (record mode) and ORB_MED_ORDER_LIFECYCLE
(authoritative mode, needs the store) are both off by default; read them with
config.feature_enabled('med_order_store') and
config.feature_enabled('med_order_lifecycle') at call time.
"""
