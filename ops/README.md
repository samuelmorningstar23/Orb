# ops/: operator-side pieces

Things an operator sets up beside the appliance, on the hospital LAN. Orb is
strictly offline, so nothing here reaches outside the building.

| File | What it is |
|---|---|
| `grafana-dashboard.json` | Dashboard for the appliance's `/metrics` endpoint (LAN-side Prometheus). |
