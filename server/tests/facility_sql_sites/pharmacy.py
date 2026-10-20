"""SQL-site registry: pharmacy store lanes.

Owned by W13-C (multisite-8h3, multisite-14). The rows and route groups this
area's sweep owns; see __init__.py for the kinds and the rules. A sweep that
makes a lane facility-aware moves its rows to a safe kind (with tables,
count and reason) and its route group to 'cleared', then regenerates the
fence's lists (python -m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # services/ndps_service.py
    ('services/ndps_service.py', '_patient_rows'): blind('multisite-8h3'),
    # services/pharmacy_stock_service.py
    ('services/pharmacy_stock_service.py', '_patient_location'): blind('multisite-8h3'),
    # services/pharmacy_store_service.py
    ('services/pharmacy_store_service.py', '_RETURN_SELECT'): blind('multisite-8h3'),
    ('services/pharmacy_store_service.py', '_patient_department'): blind('multisite-8h3'),
    ('services/pharmacy_store_service.py', 'returnable_orders'): blind('multisite-8h3'),
}

SURFACES = {
    '/api/pharmacy-store': blind_surface(
        'multisite-8h3',
        'Pharmacy stores, stock, transfers and ward returns.'),
}
