"""SQL-site registry: the pharmacy queue, money and the front desk.

Owned by W12-E (multisite-9a, multisite-17). The rows and route groups this
area's sweep owns; see __init__.py for the kinds and the rules. A sweep that
makes a lane facility-aware moves its rows to a safe kind (with tables,
count and reason) and its route group to 'cleared', then regenerates the
fence's lists (python -m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # routes/frontdesk_routes.py
    ('routes/frontdesk_routes.py', '_registration_audit._write'): blind('multisite-9a'),
    # routes/helix_routes.py
    ('routes/helix_routes.py', '_resolve_approve_patient'): blind('multisite-9a'),
    # services/billing_service.py
    ('services/billing_service.py', '_clinical_candidates'): blind('multisite-9a'),
    ('services/billing_service.py', '_existing_credit_note'): blind('multisite-9a'),
    ('services/billing_service.py', '_patient_exists'): blind('multisite-9a'),
    ('services/billing_service.py', '_refresh_patient_invoices'): blind('multisite-9a'),
    ('services/billing_service.py', '_resolve_encounter_context'): blind('multisite-9a'),
    ('services/billing_service.py', 'cancel_invoice'): blind('multisite-9a'),
    ('services/billing_service.py', 'create_credit_note'): blind('multisite-9a'),
    ('services/billing_service.py', 'create_invoice'): blind('multisite-9a'),
    ('services/billing_service.py', 'get_invoice'): blind('multisite-9a'),
    ('services/billing_service.py', 'issue_invoice'): blind('multisite-9a'),
    ('services/billing_service.py', 'list_invoices'): blind('multisite-9a'),
    ('services/billing_service.py', 'list_op_patients'): blind('multisite-9a'),
    ('services/billing_service.py', 'patient_financial_summary'): blind('multisite-9a'),
    ('services/billing_service.py', 'post_bed_day_charges'): blind('multisite-9a'),
    ('services/billing_service.py', 'refresh_invoice_payment_state'): blind('multisite-9a'),
    # services/facility_stamp.py (multisite-7b, W11-C)
    ('services/facility_stamp.py', 'admission_facility'): facility(
        ['admissions'], 1,
        ("Which facility one patient's own admission sits at, so a money row written for it "
         'is stamped there (multisite-7b). It reads the facility id only.')),
    ('services/facility_stamp.py', 'invoice_facility'): facility(
        ['invoices'], 1,
        ('Which facility one invoice sits at, so a receipt, refund or claim against it is '
         'stamped there (multisite-7b). It reads the facility id only.')),
    ('services/facility_stamp.py', 'payment_facility'): facility(
        ['payments'], 1,
        ('Which facility one receipt sits at, so a refund or adjustment against it is stamped '
         'there (multisite-7b). It reads the facility id only.')),
    # services/front_office_mis.py
    ('services/front_office_mis.py', '_no_shows'): blind('multisite-9a'),
    ('services/front_office_mis.py', '_registration_split'): blind('multisite-9a'),
    ('services/front_office_mis.py', '_visits_on'): blind('multisite-9a'),
    # services/frontdesk_service.py
    ('services/frontdesk_service.py', '_next_token'): blind('multisite-9a'),
    ('services/frontdesk_service.py', '_opd_charges_at_registration'): blind('multisite-9a'),
    ('services/frontdesk_service.py', '_patient_has_active_admission'): blind('multisite-9a'),
    ('services/frontdesk_service.py', '_patient_has_admission'): blind('multisite-9a'),
    ('services/frontdesk_service.py', '_patient_row'): blind('multisite-9a'),
    ('services/frontdesk_service.py', '_refuse_cross_facility_transfer'): facility(
        ['admissions'], 1,
        ('Finds the active admission a front desk transfer would move, to refuse a move into '
         'another facility (multisite-3).')),
    ('services/frontdesk_service.py', '_transfer_result'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'cancel_opd_visit'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'complete_opd_visit'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'get_opd_visit'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'list_opd_queue'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'list_transfer_destinations'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'patient_facility_ids_on'): facility(
        ['admissions'], 1,
        ('The facilities a patient has admissions at, for the cross-facility transfer refusal '
         '(multisite-3).')),
    ('services/frontdesk_service.py', 'register_opd_visit'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'start_opd_consultation'): blind('multisite-9a'),
    ('services/frontdesk_service.py', 'transfer_patient'): blind('multisite-9a'),
    # services/helix_service.py
    ('services/helix_service.py', 'get_all_orders'): blind('multisite-9a'),
    ('services/helix_service.py', 'get_order'): blind('multisite-9a'),
    ('services/helix_service.py', 'get_pending_orders'): blind('multisite-9a'),
    ('services/helix_service.py', 'orders_awaiting_receipt'): blind('multisite-9a'),
    # services/opd_charges_service.py
    ('services/opd_charges_service.py', 'raise_consultation_at_registration_on'): blind('multisite-9a'),
    ('services/opd_charges_service.py', 'resolve_consultation_code_on'): blind('multisite-9a'),
    # services/patient_matching.py
    ('services/patient_matching.py', '_STALE_SQL'): blind('multisite-17'),
    ('services/patient_matching.py', '_hard_uhid_ids'): blind('multisite-17'),
    ('services/patient_matching.py', 'find_candidates_on'): blind('multisite-17'),
    ('services/patient_matching.py', 'sync_keys'): blind('multisite-17'),
    # services/payer_service.py
    ('services/payer_service.py', '_CLAIM_SELECT'): blind('multisite-9a'),
    ('services/payer_service.py', '_insert_claim'): blind('multisite-9a'),
    ('services/payer_service.py', 'capture_policy'): blind('multisite-9a'),
    ('services/payer_service.py', 'create_claim'): blind('multisite-9a'),
    ('services/payer_service.py', 'denial_log'): blind('multisite-9a'),
    ('services/payer_service.py', 'list_policies'): blind('multisite-9a'),
    # services/payments_service.py
    ('services/payments_service.py', '_assert_totals_reconcile'): blind('multisite-9a'),
    ('services/payments_service.py', '_find_by_idempotency'): blind('multisite-9a'),
    ('services/payments_service.py', '_load_invoice'): blind('multisite-9a'),
    ('services/payments_service.py', '_method_breakdown'): blind('multisite-9a'),
    ('services/payments_service.py', '_recompute_invoice'): blind('multisite-9a'),
    ('services/payments_service.py', '_refundable'): blind('multisite-9a'),
    ('services/payments_service.py', '_require_registered_patient'): blind('multisite-9a'),
    ('services/payments_service.py', 'apply_deposit_to_invoice'): blind('multisite-9a'),
    ('services/payments_service.py', 'attach_people'): blind('multisite-9a'),
    ('services/payments_service.py', 'day_end_summary'): blind('multisite-9a'),
    ('services/payments_service.py', 'get_payment'): blind('multisite-9a'),
    ('services/payments_service.py', 'invoice_for_payment'): blind('multisite-9a'),
    ('services/payments_service.py', 'list_payments'): blind('multisite-9a'),
    ('services/payments_service.py', 'open_invoices_for_payment'): blind('multisite-9a'),
    ('services/payments_service.py', 'patient_financial_snapshot'): blind('multisite-9a'),
    ('services/payments_service.py', 'pay_refund'): blind('multisite-9a'),
    ('services/payments_service.py', 'receipt_print_view'): blind('multisite-9a'),
    ('services/payments_service.py', 'record_payment'): blind('multisite-9a'),
    ('services/payments_service.py', 'reverse_payment'): blind('multisite-9a'),
    ('services/payments_service.py', 'search_patients'): blind(
        'multisite-9a',
        'House-wide patient search at the counter.'),
    # services/pharmacy_verification_service.py
    ('services/pharmacy_verification_service.py', '_fetch_med'): blind('multisite-9a'),
    ('services/pharmacy_verification_service.py', 'list_pending_verification'): blind(
        'multisite-9a',
        "The pharmacist's queue: no scope call at all (pharmacy is hospital-wide by design)."),
    ('services/pharmacy_verification_service.py', 'reject_medication'): blind('multisite-9a'),
    ('services/pharmacy_verification_service.py', 'source_note'): blind('multisite-9a'),
    ('services/pharmacy_verification_service.py', 'verify_medication'): blind('multisite-9a'),
}

SURFACES = {
    '/api/billing': blind_surface(
        'multisite-9a',
        'Invoices and the outpatient list.'),
    '/api/payments': blind_surface(
        'multisite-9a',
        'Receipts, deposits and the counter search.'),
    '/api/payer': blind_surface(
        'multisite-9a',
        'Policies and claims.'),
    '/api/frontdesk': blind_surface(
        'multisite-9a',
        'The front desk queue and transfers.'),
    '/api/helix': blind_surface(
        'multisite-9a',
        'Helix pharmacy orders.'),
    '/api/pharmacy': blind_surface(
        'multisite-9a',
        "The pharmacist's verify queue has no scope call at all."),
}
