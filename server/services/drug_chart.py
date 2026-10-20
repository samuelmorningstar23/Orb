"""The drug chart switch for an Orb Ward Record computer.

Samuel's ruling, 9 October 2026: hide the drug lists. The founding programme
sells the observation record only; the drug chart stays on paper and there are
no orders (SALES_PLAN_2026-10.md section 3, step 2). With ORB_WARD_RECORD on:

  * every screen and every API that feeds a screen sends no drug data: no
    medication rows, no medication events, no medicine section, and no
    sentence about medicines at all (not even "none recorded");
  * every medication and order write answers 404 in DRUG_CHART_OFF, before any
    patient or drug is read (require_drug_chart, as a router or route
    dependency);
  * the allergy list stays: it is a safety record the chart and the clerk's
    screen still show;
  * the two legal exports (the patient's FHIR export and the DPDP data
    principal export) still hand over any medication rows that are stored. An
    export that hides stored records is wrong in law. A fresh Ward Record site
    has none.

Read at call time, so a test or an operator flip is seen without re-importing.
"""

from __future__ import annotations

from fastapi import HTTPException

#: What every medication and order write answers on an Orb Ward Record computer.
DRUG_CHART_OFF = "The drug chart is not part of Orb Ward Record"


def drug_chart_enabled() -> bool:
    """False on an Orb Ward Record computer (ORB_WARD_RECORD)."""
    import config

    return not getattr(config, "WARD_RECORD", False)


def require_drug_chart() -> None:
    """A FastAPI dependency: 404 in DRUG_CHART_OFF under the profile. Declared
    on the router or the route, so it runs before any patient or drug is read."""
    if not drug_chart_enabled():
        raise HTTPException(status_code=404, detail=DRUG_CHART_OFF)
