## 10. Forecast, What-If Simulator & Command Center

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![Forecast. The provenance badge states plainly when a projection is heuristic rather than a trained model.](assets/manual/forecast-census.png)

*Forecast. The provenance badge states plainly when a projection is heuristic rather than a trained model.*

![The Command Center: house-wide census, acuity heatmap, active deteriorations, and today's operating room list.](assets/manual/command-center.png)

*The Command Center: house-wide census, acuity heatmap, active deteriorations, and today's operating room list.*

![The What-If Simulator models surges, bed closures, and discharge acceleration against the live baseline.](assets/manual/what-if-simulator.png)

*The What-If Simulator models surges, bed closures, and discharge acceleration against the live baseline.*

Orb includes three closely related operational modules that look forward instead of at the bedside: **Forecast** predicts the hospital's census, bed capacity, and discharge flow over the coming week; the **What-If Simulator** stress-tests that forecast against surge scenarios you define yourself; and the **Command Center** condenses the entire house into a single live operations board. All three draw on the same underlying numbers, the same bed capacities, census, and admission model, so a figure in one module never contradicts the same figure in another. This chapter explains how to open each module, what every control does, how to read the projections, and exactly what the AI is and is not doing behind the numbers.

### 10.1 Who can see these modules

Access is role-based. All three modules are governed by the same operational read permission, but the sidebar tabs differ by role:

| Role | Forecast | Simulator | Command |
|---|---|---|---|
| Doctor | Yes | Yes | Yes |
| Nurse | Yes | No | No |
| Admin | Yes | Yes | Yes |
| Pharmacist | No | No | No |

Doctors and administrators see all three tabs in the left sidebar: **Forecast**, **Command**, and **Simulator**. Nurses see **Forecast** only; pharmacists work in the pharmacy modules and see none of the three.

---

### 10.2 Orb Forecast

Forecast answers the questions charge nurses, bed managers, and unit leaders ask every morning: *How full will we be this week? Which ward is the pressure point? Who is likely to go home in the next 72 hours?* It combines a live read of the current census with a day-by-day projection of admissions, discharges, and occupancy for the next seven days.

#### Opening Forecast

1. Sign in and click **Forecast** in the left sidebar.
2. The panel opens with a header reading **Orb Forecast Engine** and a row of four section pills: **Census Forecast**, **Ward Capacity**, **Discharge Board**, and **Admission Patterns**.
3. Click any pill to switch sections; **Census Forecast** is shown first by default.

The panel refreshes itself every five minutes, and the underlying hospital-level forecast is recomputed at most once per hour, numbers are deliberately stable and will not jump between visits.

#### The honesty badge: what "AI Untrained" means

Next to the **Orb Forecast Engine** title is a small provenance chip labelled **AI**, showing a version number and a status word: **Validated**, **Unvalidated**, or **Untrained**. This chip appears on every AI-generated output in Orb, the system's "honesty layer" made visible at the point of use. Hover over it for the model's task, version, training data, and status, with a pointer to **Admin → Model Governance** for the full lineage.

Forecast is architecturally built around a multi-task LSTM neural network for length-of-stay, discharge, and acuity prediction, but **on a fresh installation that network has not been trained or validated on real clinical data**. Orb refuses to present the output of an untrained network as a neural prediction. Instead the system *fails closed*: while no validated checkpoint is present, every per-patient prediction is routed to a transparent, rule-based heuristic (typical length of stay by ward, adjusted for how long the patient has already been in), and the chip reads **Untrained**. The same honesty is repeated in plain language at the bottom of the panel:

> *Decision-support estimates. The Forecast model is not yet trained or validated on real clinical data, figures are rule-based projections for planning, not validated neural predictions. See Admin → Model Governance.*

Practically, this means you should treat every Forecast number as a **planning estimate**, comparable to a well-organized whiteboard calculation, useful for staffing and bed management, never a substitute for clinical judgement about an individual patient.

#### Section 1: Census Forecast

The default section shows the seven-day hospital-level projection.

**KPI cards** across the top:

| Card | Meaning |
|---|---|
| **Current Census** | Admitted patients against total capacity (e.g. 24 / 32), with percentage occupied beneath. |
| **Beds Available in 48h** | Projected free beds two days out; red when two or fewer. |
| **7-Day Discharges** | Discharges predicted over the week, with predicted admissions as the subtitle. |
| **Peak Day** | The day projected to have the highest census, with the patient count. |

The **7-Day Occupancy Projection** chart shows an amber occupancy line with a numbered data point for each day, green bars for predicted admissions, blue bars for predicted discharges, and a dashed red line marking total bed capacity, occupancy approaching the dashed line means a near-capacity day.

Below the chart, a daily breakdown table lists **Day**, **Admissions** (green, +N), **Discharges** (blue, −N), **Census**, and **Occupancy** (a small bar plus percentage). Any day projected above 90% occupancy is highlighted in red so capacity-risk days stand out at a glance.

**How the projection is built.** The admission forecast is an expected-value model: the average daily admission rate is fitted from the hospital's own last 30 days of admission history, then adjusted by day-of-week patterns (Mondays busiest, weekends quietest) and by live environmental factors from Orb Pulse, thunderstorms (trauma), extreme heat or cold snaps (cardiac and respiratory), poor air quality, or elevated flu surveillance can each raise a day's expected admissions. Discharges are aggregated from the per-patient length-of-stay estimates. Because the model reports expected values rather than random draws, the same conditions always produce the same forecast.

#### Section 2: Ward Capacity

Click the **Ward Capacity** pill to see one card per ward, **ICU**, **W1**, **W2**, and **W3**, each with 8 beds (32 total).

Each ward card contains a status badge, **OK** (green), **HIGH** (amber, above 70% utilization), or **⚠ FULL** (red, 90% or above, with a red-tinted card), a **bed grid** with one icon per physical bed (amber occupied, green available; hover for "Occupied"/"Available"), a **Now** column showing current occupancy (e.g. 6/8) with a utilization bar, and a **48h** column showing projected occupancy two days out based on which patients are estimated to leave within two days (a green down-arrow marks beds expected to free up).

Below the ward cards, a summary strip totals the house: **Total Occupied**, **Available Now**, **Total Capacity**, and **Utilization Rate** (red above 85%).

#### Section 3: Discharge Board

Click the **Discharge Board** pill to open **Discharge Readiness, Next 72 Hours**: every admitted patient, ranked most-discharge-ready first (the header confirms *sorted by discharge likelihood*), with the top five rows visually highlighted.

| Column | Meaning |
|---|---|
| **Patient** | Name, with the working diagnosis beneath. |
| **Ward/Bed** | Current location (e.g. W2 · B4). |
| **Days In** | Days since admission. |
| **Est. Remaining** | Estimated days until discharge; two days or fewer shows green. |
| **Discharge Prob.** | A bar plus percentage, the estimated probability of discharge in the window. |
| **Trajectory** | **↗ Improving**, **→ Stable**, or **↘ Worsening**, colour-coded green/amber/red. |

Use the board in morning huddles to focus discharge planning, pharmacy reconciliation, transport, and family communication for the top of the list; a second look at anyone marked **↘ Worsening**. A high discharge probability is a prompt to *review* the patient for discharge, never a decision.

#### Section 4: Admission Patterns

Click the **Admission Patterns** pill for a retrospective view of the last 30 days of admission history: summary cards for **30-Day Admissions** (with the per-day average), **Avg Length of Stay**, and **Discharge Rate**; an **Admissions by Day of Week** bar chart (weekend bars muted so the weekday pattern reads clearly); and a **Distribution Analysis** breaking admissions down **By Severity** (critical / elevated / stable) and **By Ward** (ICU / W1 / W2 / W3).

This section contains no prediction at all, it is a plain summary of what actually happened, and the historical basis the census forecast's admission rate is fitted from.

---

### 10.3 What-If Simulator

Forecast tells you what is *expected*. The **What-If Simulator** answers "what if it doesn't go that way?", 20 trauma admissions tonight, a flu season at 1.5× normal admissions, eight beds closed for maintenance, a sicker cohort staying longer. You set the scenario with sliders; the simulator projects occupancy hour by hour against capacity and tells you when you would overflow, how many beds short you would be, how many extra discharges would avoid it, and how many extra nurses the peak would require.

Two properties are worth knowing up front. First, it is **built on the live Forecast baseline, not a separate model**: the starting census, bed capacity, and expected admission rate are read directly from the Forecast module, so the simulator can never contradict the numbers Forecast already shows. Second, it is **deterministic**: there is no randomness anywhere in the projection, so the same scenario always produces exactly the same numbers and results can be shared and re-run in a bed meeting without changing under you.

#### Opening the Simulator

Click **Simulator** in the left sidebar. The page opens with the title **What-If Simulator**, and in the top-right a **baseline chip** shows the live starting point: **Census now**, **Beds**, and **Adm / day** (the expected daily admission rate). While a projection is being recomputed, a small **simulating** indicator pulses in this chip.

The simulator runs immediately on opening with everything at baseline and re-runs automatically a fraction of a second after any control changes, there is no "Run" button. Drag a slider and watch the chart and tiles update live.

#### Scenario presets

Six one-click presets set up common situations. A preset **resets all other controls to baseline first**, then applies its settings, always a clean starting point, never layered on what you had before.

| Preset | What it sets |
|---|---|
| **🌙 20 admissions tonight** | +20 surge admissions over a 24-hour horizon. |
| **🛏️ Close 8 beds** | Bed change −8. |
| **🤒 Flu surge (1.5×)** | Admission rate ×1.5 over a 72-hour horizon. |
| **🚑 Mass-casualty (+30)** | +30 surge admissions compressed into a 12-hour horizon. |
| **⚡ Aggressive discharge** | Discharge boost +60%. |
| **📈 Open 6 beds** | Bed change +6. |

To combine effects (say, a flu surge *and* eight closed beds), click the preset first, then adjust additional sliders by hand.

#### Horizon

The **Horizon** row selects how far forward to project: **12h**, **24h**, **48h**, **72h**, or **7d** (the selection is outlined in gold). Short horizons suit acute surge planning; **7d** suits seasonal planning.

#### The six sliders

Each slider shows its label, a one-line hint, and its current value.

| Slider | Range | What it models |
|---|---|---|
| **Surge admissions** | +0 to +60 | A one-off block of extra admissions spread evenly across the horizon. |
| **Bed change** | −16 to +16 | Beds opened (+) or closed (−) versus current capacity. |
| **Admission-rate ×** | 0.5× to 3× | Scales the expected admission rate; the hint offers calibration: *1.5× ≈ flu surge, 2× ≈ epidemic.* |
| **Length-of-stay ×** | 0.5× to 2× | Scales how long patients stay; above 1 models a sicker, slower-turnover cohort. |
| **Discharge boost** | +0% to +100% | How much faster than baseline you discharge, the aggressive-discharge lever. |
| **Nurse : patient** | 1:1 to 1:8 | The staffing ratio for the nursing calculation; the default 1:4 is the common med-surg safe-staffing benchmark. |

The **Reset to baseline** button below the sliders returns every control to its default (48-hour horizon, no scenario changes, 1:4 staffing).

#### Reading the projections

Five stat tiles summarize the scenario, each colour-coded green (fine), amber (pressure), or red (breach):

| Tile | Meaning |
|---|---|
| **Peak occupancy** | Highest projected occupancy as a percentage of effective capacity, with the bed count and hour it occurs (e.g. "34 beds · +18h"). Amber from 85%, red at 100%+. |
| **Hours to overflow** | Hours until occupancy first exceeds capacity: **Clear** (green) if never breached in the horizon, **Now** if already over, red when six hours away or less. |
| **Beds short at peak** | Beds over effective capacity at the worst moment. |
| **Discharges needed** | Extra discharges required to keep the peak at or under capacity, directly actionable on the discharge board. |
| **Extra nurses** | Additional nurses needed at peak versus the current census, at your chosen ratio, with total nurses required at peak beneath. |

Below the tiles, the **Projected occupancy vs capacity** chart plots the hour-by-hour projection: a gold occupancy area, a dashed horizontal **Capacity** line labelled with the effective bed count (baseline beds plus your bed change), and any portion of the curve above capacity shaded red as **Overflow**. Hover anywhere on the chart for a tooltip showing the exact occupancy, capacity, and overflow at that hour.

#### Model assumptions & citations

At the bottom, an expandable panel titled **Model assumptions & citations** discloses exactly what the arithmetic rests on: the average length of stay behind the baseline discharge rate, the admission-rate source (the same 30-day statistical fit Forecast uses), the bed-capacity source, the staffing benchmark behind the default 1:4 ratio, and the fallback admission rate used if history is empty, and it states plainly that the projection is deterministic. When leadership asks "where do these numbers come from?", this panel is the answer.

#### Worked example: preparing for a flu surge

1. Open **Simulator** and note the baseline chip (say, census 24 of 32 beds, 4.5 admissions/day).
2. Click **🤒 Flu surge (1.5×)**. The horizon switches to 72h and the admission rate scales to 1.5×.
3. Winter cases also stay longer, so drag **Length-of-stay ×** to 1.3×.
4. Read the tiles. If **Hours to overflow** shows a number rather than **Clear**, **Discharges needed** tells you how many extra discharges would keep you inside capacity.
5. Test mitigations: raise **Discharge boost** until **Hours to overflow** reads **Clear**, or add beds with **Bed change**, and compare.
6. Check **Extra nurses** for the staffing cost, with **Nurse : patient** set to your unit's real ratio.
7. Take the resulting numbers ("4 extra discharges and 2 extra nurses absorb a 1.5× flu surge") to the bed meeting, anyone re-running the same scenario sees the same figures.

---

### 10.4 Command Center

The **Command Center** is the single pane of glass for whoever is running the house, a CMO, COO, bed manager, or duty administrator. One screen answers: how full are we, who is deteriorating right now, are our sepsis bundles on track, could we survive an IT outage, what is coming in the next 24 hours, and what is happening in the operating rooms today.

It is strictly **read-only aggregation** (with one deliberate exception, the downtime **Capture all** button) and is engineered to *degrade rather than crash*: if one data source hiccups, that tile falls back to zeros while the rest of the board keeps rendering.

#### Opening the Command Center

Click **Command** in the left sidebar. The board opens with the title **Command Center**, the subtitle *"House-wide operations · single pane of glass"*, a pulsing **LIVE** indicator, and an **Updated** timestamp. It refreshes automatically **every 20 seconds**; if a refresh fails, a **reconnecting…** note appears while the board keeps showing the last good snapshot, clearing on the next successful refresh.

#### The four KPI tiles

Across the top, four colour-coded tiles give the house vitals:

| Tile | Shows | Colour logic |
|---|---|---|
| **Census / Occupancy** | Admitted patients over total capacity (e.g. 24/32), a fill bar, and "% beds occupied". | Green under 75%, amber 75–89%, red 90%+. |
| **Critical Patients** | Count of patients at critical acuity, with "N elevated · N stable" beneath. | Red whenever any patient is critical; green otherwise. |
| **Sepsis Bundle Compliance** | Percentage of active sepsis bundles still on track against their 1-hour targets, with the fraction (e.g. "3/4 bundles on track"). No active bundles reads 100%. | Green 90%+, amber 70–89%, red below 70%. |
| **Downtime Readiness** | Percentage of admitted patients with a downtime snapshot fresher than 24 hours, the fraction, and the age of the *stalest* snapshot (e.g. "oldest 3.2h"). | Green 80%+, amber 50–79%, red below 50%. |

**The Capture all button.** The Downtime Readiness tile carries the board's only action: **Capture all**. Clicking it captures a fresh downtime snapshot for every admitted patient, the safety net each ward would rely on if the network, server, or AI stack went down. While running, the button shows **Capturing…** with a spinner; on success a confirmation such as "24 snapshots captured" appears briefly and the readiness percentage refreshes immediately. Treat a red or amber readiness tile as a prompt to click **Capture all** before the end of the shift.

#### Ward Acuity Heatmap

The **Ward Acuity Heatmap** card shows one cell per ward, with a legend for **Critical**, **Elevated**, and **Stable**. Each cell displays the ward name, its occupancy (e.g. 6/8), a stacked bar whose red, amber, and green segments are proportional to the critical, elevated, and stable counts, and the three counts themselves.

Cells shade hotter as acuity load rises, a ward heavy with critical patients glows even if it is not full, because the heat weighting counts a critical patient far more than a stable one, and any ward with at least one critical patient is additionally flagged. Hover over a cell for a plain-text summary ("ICU: 2 critical, 3 elevated, 1 stable"). The heatmap exposes the mismatch raw occupancy hides: a 6/8 ward of stable patients and a 6/8 ward of critical patients are very different problems.

#### Active Deteriorations

The **Active Deteriorations** card lists every patient currently at critical or elevated acuity, with a count badge in the header. Critical patients sort first, then by NEWS2 score, highest first. Each row shows the patient's name, location (ward · bed), a colour-coded level badge (**critical** or **elevated**), and a live **NEWS2** score computed from their most recent vitals. When nobody is deteriorating, the card states: *"No active deteriorations. House is stable."* This is the board's "who needs eyes now" column; for the bedside detail behind any name, switch to the **Patients** tab (Vigil).

#### Capacity Forecast · 24h

The **Capacity Forecast · 24h** card is the Forecast module in miniature, using the identical hospital-flow model: **projected admissions** for the next 24 hours (+N), **projected occupancy** against total capacity, and **projected utilization** (amber at 75%, red at 90%). If tomorrow looks tight, open **Forecast** for the seven-day picture or **Simulator** to test responses. The Forecast honesty caveat applies equally here: planning estimates from the rule-based projection, not validated neural predictions.

#### Operating Room · Today

The **Operating Room · Today** card lists today's surgical schedule with a case-count badge and a table of **Time**, **Procedure**, **Patient**, **OR**, and **Status** (colour-coded, e.g. scheduled, in progress, completed). When the slate is empty it reads: *"No cases scheduled today."* Most of these cases will need a bed tonight, read this card alongside the census tile and the 24-hour forecast.

---

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Forecast never finishes loading, or shows an error with a **Retry** button | The backend could not be reached or a query failed. Click **Retry**; if it persists, confirm the Orb server is running and your session has not expired (sign out and back in). |
| Forecast numbers seem "stuck" | By design: the hospital-level forecast is cached for up to an hour to keep planning numbers stable, and the panel re-fetches every five minutes. |
| The provenance chip next to "Orb Forecast Engine" is missing | The chip loads model-registry data separately; a transient failure hides it briefly and it retries automatically. The forecast itself is unaffected. |
| Simulator shows an error banner and no chart | A simulation request failed. Nudge any slider to re-run; if the error persists, check the server connection. |
| Simulator numbers differ from Forecast's | The simulator's *effective capacity* includes your **Bed change**. At **Reset to baseline** the baseline chip matches Forecast exactly, it reads from the same source. |
| Command Center says "Command Center unavailable" | The initial snapshot failed to load. Click **Retry**. |
| Command Center shows **reconnecting…** | A 20-second refresh failed; the board keeps the last good data and recovers on the next successful poll. |
| One Command Center tile shows zeros while the rest look right | That subsystem degraded (each section is independently protected). The rest of the board is trustworthy; report the zeroed section to your administrator. |
| **Capture all** reports a failure | The snapshot service could not write for one or more patients. Retry; if it persists, escalate, downtime readiness is a safety control. |
| A tab (Simulator, Command) is missing from your sidebar | Tabs are role-based (see 10.1). Nurses see Forecast only; pharmacists see none of the three. |

### Safety & limitations

- **The forecast model ships untrained.** Until a validated checkpoint is installed and marked validated in **Admin → Model Governance**, every length-of-stay, discharge-probability, and census figure comes from a transparent rule-based heuristic, and the provenance chip says so (**Untrained**). Orb never lets an untrained network's raw output masquerade as a prediction.
- **Planning aid, not clinical decision-making.** Discharge probabilities and trajectories rank patients for *review*; the decision to discharge remains entirely clinical. Never discharge, transfer, or downgrade monitoring because a board said a patient was "likely to go".
- **The Simulator is arithmetic, not prophecy.** It projects smooth, deterministic flows from stated assumptions, real surges arrive in lumps, and the model knows nothing of case mix, isolation requirements, or individual bed constraints. Its value is comparing scenarios and sizing responses, and its assumptions are always one click away in **Model assumptions & citations**.
- **Aggregate acuity is not a monitor.** The Command Center's acuity counts and NEWS2 scores summarize the latest recorded data on a 20-second poll; they complement, never replace, bedside monitoring and the Vigil patient views.
- **Environmental adjustments are coarse.** Weather, air-quality, and flu-surveillance multipliers apply fixed percentage bumps to expected admissions, directionally useful for planning, not calibrated epidemiological forecasts.
- **Everything here is read-only** except the downtime **Capture all** action, which only *adds* safety snapshots. Nothing on these three screens can alter a patient record.
