from pathlib import Path
import json
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4

ROOT = Path(__file__).resolve().parents[1]
VERIFICATION = json.loads((ROOT / 'docs/audit-2026-10/legacy-lint-types-20261020/verification.json').read_text())
assert VERIFICATION['backend']['failures'] == 0 and VERIFICATION['backend']['errors'] == 0
OUT = ROOT / 'output/pdf/Orb_Work_Summary_2026-10-19.pdf'
NAVY = colors.HexColor('#102C3A')
TEAL = colors.HexColor('#087E82')
INK = colors.HexColor('#243D49')
MUTED = colors.HexColor('#58707C')
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name='TitleOrb', fontName='Helvetica-Bold', fontSize=31, leading=35, textColor=NAVY, spaceAfter=14))
styles.add(ParagraphStyle(name='SubOrb', fontName='Helvetica', fontSize=13, leading=19, textColor=MUTED, spaceAfter=18))
styles.add(ParagraphStyle(name='HeadingOrb', fontName='Helvetica-Bold', fontSize=21, leading=26, textColor=NAVY, spaceAfter=14))
styles.add(ParagraphStyle(name='H2Orb', fontName='Helvetica-Bold', fontSize=11.5, leading=15, textColor=TEAL, spaceBefore=11, spaceAfter=5))
styles.add(ParagraphStyle(name='BodyOrb', fontName='Helvetica', fontSize=10, leading=14.3, textColor=INK, spaceAfter=8))
styles.add(ParagraphStyle(name='SmallOrb', fontName='Helvetica', fontSize=8.3, leading=11.5, textColor=MUTED, spaceAfter=6))
styles.add(ParagraphStyle(name='CellOrb', fontName='Helvetica', fontSize=9.1, leading=12.4, textColor=INK))
styles.add(ParagraphStyle(name='CellHeadOrb', fontName='Helvetica-Bold', fontSize=9.2, leading=12, textColor=colors.white))
styles.add(ParagraphStyle(name='CellCompactOrb', fontName='Helvetica', fontSize=8.6, leading=11.8, textColor=INK))
story=[]
def p(t, style='BodyOrb'): return Paragraph(t, styles[style])
def add(t, style='BodyOrb'): story.append(p(t, style))
def h(t): add(t, 'H2Orb')
def page(n, title):
    if story: story.append(PageBreak())
    add(f'WORK SUMMARY  /  {n:02d}', 'SmallOrb')
    add(title, 'HeadingOrb')
def table(headers, rows, widths, compact=False):
    data=[[p(x,'CellHeadOrb') for x in headers]]+[[p(x,'CellCompactOrb' if compact else 'CellOrb') for x in row] for row in rows]
    t=Table(data,colWidths=widths,hAlign='LEFT',repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),NAVY),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),('TOPPADDING',(0,0),(-1,-1),6 if compact else 8),('BOTTOMPADDING',(0,0),(-1,-1),6 if compact else 8),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#EFF5F6'),colors.white]),('LINEBELOW',(0,1),(-1,-1),0.4,colors.HexColor('#DCE6E9'))]))
    story.append(t)

add('ORB / ENGINEERING RECAP', 'SmallOrb')
story.append(Spacer(1,25))
add('What we built.\n<br/>What we fixed.', 'TitleOrb')
add('A plain-English summary of the work completed in Orb, including capabilities that were previously missing.', 'SubOrb')
add('Prepared 19 October 2026  |  Updated with fixes and remaining work as of 20 October 2026', 'SmallOrb')
h('The overall result')
add('We strengthened Orb\'s existing ward-record system, repaired confirmed privacy and reliability defects, added 13 persistent local hospital workflows, improved the testing and release machinery, and made Sage faster in a measured local workload.')
add('The project has real backend services, database migrations, role-based screens, audit trails and browser-tested workflows. It remains a <b>substantial pre-pilot MVP</b>; implementation and synthetic tests do not establish clinical approval or hospital readiness. [1-6]')
table(['Workstream','What changed'],[
('Existing product','Ward Record behavior, patient context, medication safety, hospital time, Sage/Scribe handling and startup behavior were repaired.'),
('Previously missing work','13 local modules now have persistent records, APIs and screens, including radiology, emergency, care plans, dialysis and hospital operations.'),
('Engineering foundation','Stronger session/cache boundaries, private media handling, backup verification, setup scripts, test isolation and candidate release checks.'),
('Latest remediation','All 8 reproduced security defects and 4 UI issues are fixed locally, with security regressions and responsive browser checks. Remaining work is listed separately. [7, 10]')
],[112,387])
h('How this summary was assembled')
add('This is a retrospective from repository history, implementation reports and verification manifests. It covers earlier product work, the 11-12 October audit/completion passes, and the 19 October security/UI remediation. Test results are recorded snapshots with their limits. Existing uncommitted work and shipped commits are distinguished; this is not a deployment record.', 'SmallOrb')

page(2,'What we fixed in the product')
add('These are concrete before-and-after examples from the earlier fix waves and the recent audit. Some apply only when the relevant module is enabled. [1, 8, 9]')
table(['Before','What we changed'],[
('Patient context could cross between screens or requests.','Relay now checks room and request ownership so delayed sends and foreign-room socket events cannot repaint the active conversation. Proactive insights and Sage chart prefetch now check patient, department and facility access.'),
('Cached records or delayed responses could outlive an access refusal or account change.','An authoritative 401/403/404 clears the relevant active-page cache. Fetch, refresh, upload, stream and replay consumers recheck their initiating identity. Cross-tab and offline revocation policy still needs work.'),
('Medication safety and order previews had misleading gaps.','Fixed class-name allergy matching, including NSAIDs. Added clear patient/allergy banners, dose and interaction coverage labels, recent-application warnings and honest notices where medication orders are not routed to the ward/pharmacy.'),
('Clinical text could imply facts or freshness it did not have.','Removed invented surgical preparation facts/default success odds; constrained Scribe structuring; refreshed active insight facts; attached observation time; kept cached vitals consistent with the readings used for scoring.'),
('Hospital time and escalation behavior were inconsistent.','Reused the hospital timezone for shift windows and clinical printouts. Added a background escalation sweep so overdue tiers can progress without someone opening the list. External paging remains separate work.'),
('A profile change or rebuild could show the wrong interface.','Ward Record now hides switched-off drug/AI/scoring surfaces consistently. Startup waits for known switches, page navigation refreshes the shell, rebuilds refresh index content, and normal loading no longer flashes a false offline warning.'),
('Simulator cleanup and measurements mixed unlike populations.','Protected real risk-score history from simulator cleanup. Excluded simulated/unattributed readings from relevant impact counts and machine-generated drafts from the clinician time-saved proxy.'),
('Charges or portal facts could use the wrong owner/stay.','Enforced invoice-to-patient ownership, made OPD counter submission transactional, and applied current-stay filtering to Bridge readers. Patient exports now exclude other patients\' shared journal/payout entries.')
],[164,335])
story.append(Spacer(1,10))
add('The early October gap-fix log records four waves of 76, 49, 27 and 13 fixes. Those are historical work-log counts; they are not a fresh defect count or a claim that every current issue is closed. [8]', 'SmallOrb')

page(3,'What was missing and is now implemented')
add('The 11 October implementation pass added these <b>13 persistent local workflows</b>, with database migrations 81-86, API routes, permissions and navigation. All default to off until explicitly enabled. [2, 3]')
table(['New module','What we implemented'],[
('Complaints / feedback','Assignment, investigation, response, resolution, closure/reopening and history.'),
('Mortuary','Receiving/identity references, storage, custody, holds and authorised handover with a distinct witness reference.'),
('Sterile supply (CSSD)','Pack register, manually recorded cycle evidence, quarantine/release/recall, issue/return and custody linkage.'),
('Reporting / cohorts','Scoped allowlisted datasets, filters, grouped measures, saved report definitions and CSV export.'),
('Finance tools','Explicit doctor charge attribution, configured payout rules, draft/approved statements, payment references and balanced accounting exports.'),
('Radiology records','Study worklist, authored reports, performed/reported/signed states and addenda to signed records.'),
('Care plans','Problems, goals, interventions, ownership, due/completion tracking and evaluations.'),
('Emergency records','Manual triage, assessment, timepoints and disposition documentation within existing authority.'),
('Perioperative records','Surgery-linked team details, anaesthetic/recovery observations and implant lot documentation.'),
('Dialysis records','Prescribed and actual session values, complications and completion records; doctor-only prescription changes.'),
('Records governance','File custody, release requests with approval, legal holds, revision conflicts and history.'),
('Radiation safety','Equipment licence and officer-appointment references, validity dates, search, withdrawal and history.'),
('Infection surveillance','Authored definitions, exposure days, reviewed cases, isolation and transcribed susceptibility records.')
],[140,359])
story.append(Spacer(1,10))
add('<b>Scope:</b> these are local record workflows. They do not provide PACS/DICOM transport, device control, automated clinical interpretation, certification, bank transfer or external portal submission. Role and facility checks, version guards and transaction-linked audit were added. [2]', 'SmallOrb')

page(4,'Reliability, security and AI improvements')
h('Requests, media and shared transport [1, 3]')
add('Request limits now count actual streamed bytes, including routes that previously relied on headers or broad multipart exceptions. Uploaded media uses private staging and atomic publication. Audio conversion and worker cleanup handle failure and cancellation. Lens materialisation moves blocking work off the event loop; typed stream errors replace raw exception strings.')
add('Bridge and six hospital-domain clients, plus Forecast and Appointments, use the shared cookie/session/CSRF transport. Sage history groups the latest 200 stored messages by original thread and local date; delayed microphone, ASR and speech callbacks cannot submit into a replacement conversation.')
h('Startup, backups and engineering tools [1, 3, 4]')
add('Added database-aware readiness, installer failure propagation and shutdown checks that target owned processes. Backup publication now uses unique private destinations, shared locks and integrity receipts before rotation. Restore verification covers 199 tables in 47 groups, including all 45 tables added by migrations 81-86. The later extraction-security finding is now fixed through authenticated staging and archive containment (page 6).')
add('Browser and speech benchmarks now use owned disposable databases and synthetic data. Removed confirmed unused code and duplicate parsers, updated supported timestamp/model APIs, bounded Vite discovery, added advisory scanning and a candidate packaging workflow. The earlier strict Python gate covered two utility files. The follow-ups expand the independent gate to six utilities and add full-backend regression budgets (pages 8-9); the configured legacy lint/type/format findings are closed in the latest cleanup (page 10).')
h('ML dependencies and honest answer completion [4]')
add('Replaced the legacy FlagEmbedding training dependency chain with local-only inference adapters, updated Torch/Transformers, and required safe checkpoint loading. Fresh candidate scans reported zero advisories across 70 installed packages and the 76-package Apple production resolution. Existing appliance environments were not upgraded.')
add('Sage now requires nonempty content, valid stream framing and explicit successful completion. Engine errors, premature EOF and length-truncated output do not become successful replayable answers. We also repaired an informational prompt being misclassified as a medication command and aligned shared-model context reuse.')
h('Sage speed: measured improvement [5]')
table(['Question scope','First readable answer across 3 warm repeats'],[
('General - target under 3s','1.631s / 0.723s / 0.969s'),
('Patient - target under 5s','3.545s / 3.532s / 3.416s'),
('Department - target under 10s','5.377s / 0.432s / 0.429s')
],[171,328])
story.append(Spacer(1,8))
add('All 9 warm checks passed with the same local 27B model on an idle M2 Max / 64 GiB setup. The cache policy preserves useful prompt state. Cold startup still took 19.982s to readable text; one chart-open journey took 25.409s including preparation. Busy-ward latency and full-answer completion are separate: patient completions took 36-47s.', 'SmallOrb')

page(5,'How we verified the work')
add('These are recorded historical results, with their original scope. Overlapping selections must not be added together. The later security assessment demonstrates that passing suites did not cover every important path. [1, 3-7]')
table(['Evidence','Recorded result and practical meaning'],[
('Backend application suite','16,103 passed, 8 explicit skips, no failures/errors from 16,111 cases in 514 files across six shards. This snapshot preceded the final launcher refinement.'),
('Final launcher/configuration delta','160 focused tests passed after the full-suite snapshot. Application/built-asset parity was checked separately; this is not a claim of a new combined full-suite run.'),
('Frontend suite','3,700 tests passed across 366 files in the completion report; typecheck and production build passed. Lint recorded 0 errors and 8 existing warnings.'),
('Real browser acceptance','11 real-server journeys passed with synthetic records, covering selected authentication, navigation, persistence, conflict, reporting, radiology and role-refusal behaviors.'),
('New-module visual acceptance','156 effective captures across 78 states and 13 modules in light/dark themes, with zero findings in that gate.'),
('Existing visual acceptance','245 effective captures across 49 scenarios, with zero gating findings; existing allowlisted and informational findings were retained.'),
('ML security and local adapters','Fresh installed-runtime and complete Apple production-resolution scans reported zero advisories. Cached local embedding/reranker behavior was exercised; native speech build and site acceptance remained separate.'),
('Sage performance','9/9 warm speed checks and 11 complete answers passed in the final idle synthetic browser run. Cold/preparation misses remain documented.'),
('Documentation consistency','9,020 path/line citations and 490 symbol anchors resolved. This checks references, not the truth of every historical statement.')
],[151,348])
h('What we can reasonably conclude')
add('The work is implemented and has substantial automated and selected browser evidence. The new modules are more than mock screens: they save, reload, enforce version conflicts and expose history. That evidence supports continued development and controlled acceptance testing.')
h('What the evidence does not establish')
add('It does not establish real-patient clinical safety, complete endpoint coverage, a successful hosted CI run, target-site restore/key escrow, power-loss behavior, busy-ward GPU capacity or production deployment. New modules remain default-off, and the candidate environment is distinct from the installed appliance.')

page(6,'Eight security defects fixed')
add('The 12 October assessment reproduced eight defects. The 19 October remediation closes these specific findings in the local working tree. The original assessment and proof scripts remain as historical evidence. [7, 10]')
table(['Finding','Implemented fix'],[
('SEC-01 / Scribe authorization','Edit, sign, reject, revise and retry paths use the scoped note loader. Write transactions recheck the current actor and note/patient ownership. Structure checks scope before generation and at the initial save.'),
('SEC-02 / Revoked live sockets','Protected events and publications revalidate the session. Revoked, inactive, expired or changed-scope subscriptions are disconnected; publication targets only the validated recipient snapshot.'),
('SEC-03 / Revoked tablet tokens','HTTP and socket authentication now share authoritative user, token-version, enrolled-device and portal checks. A revoked tablet token cannot open a new socket session.'),
('SEC-04 / Shared-ward downtime leak','Each patient included in an authenticated pack is checked against the caller. The response is rendered from scoped snapshots and does not reuse a shared full-ward artifact.'),
('SEC-05 / Restore containment','Backup sets authenticate database/media hashes in a signed manifest. Restore validates all paths and entry types, rejects links/devices/unexpected members, bounds expansion and stages media privately before installation. Failed installs roll back both members.'),
('SEC-06 / TOTP replay race','A single conditional SQLite UPDATE consumes the time step. Concurrent requests have one winner; sequential replays and missing users fail.'),
('SEC-07 / Lens orphaned uploads','Authorization precedes file publication. Upload and registration workers retain ownership through cancellation. Failed/unregistered media is removed; media belonging to a durably saved analysis is retained.'),
('SEC-08 / Plaintext discharge QR','QR generation uses the private encrypted media writer. The scoped route decrypts it and sends no-store. Existing plaintext QR files can still be read through the protected route.')
],[158,341])
h('Recovery behavior changed')
add('A media backup without a valid signed .set.json manifest is now refused before installation. Produce a fresh set on a trusted primary, or authenticate only independently trusted historical artifacts. Existing appliance backups were not changed. Legacy database-only restores retain an explicit missing-media warning. Real-site recovery and power-loss drills remain outstanding. [7, 10]', 'SmallOrb')

page(7,'Four UI fixes, with fresh verification')
table(['Finding','What now works'],[
('UX-01 / Mobile admission','The form and summary stack on a narrow screen. Fields use one column, the body scrolls, and header/actions remain usable.'),
('UX-02 / Radiation-safety layout','Grid children and native form controls shrink within the viewport. Wrapping controls and bottom space keep equipment pagination clear of the floating orb.'),
('UX-03 / Focus restoration','An explicit initial-focus marker lets the focus trap capture the opener before moving into the name field. Closing the dialog returns focus to that opener.'),
('UX-04 / Mobile account security','The compact shell keeps the account-security button visible and reachable by touch or keyboard; its dialog opens on mobile.')
],[158,341])
h('19 October verification results [10]')
table(['Check','Recorded result'],[
('Security / restore selection','185 passed; one intentional real-backup skip.'),
('Compatibility selection','928 passed, covering authentication, scope, downtime, sockets, Scribe, Lens and vital-event behavior.'),
('Frontend components','239 passed across 26 files.'),
('Real browser journeys','17 passed, including six new responsive journeys: 390px, 1280px and 1440px, each in light and dark themes.'),
('Final cancellation delta','711 overlapping focused checks passed after the Lens registration/cancellation refinement.'),
('Commit-preparation correction','166 focused checks passed after moving the Scribe save scope argument to the correct call; includes authorized save, scope change during generation and background verification.'),
('Build and static checks','Typecheck, production build, targeted ESLint, shell syntax and changed-file whitespace checks passed.')
],[158,341])
h('What these results establish')
add('The specific defects have regression coverage and the affected user journeys were exercised against disposable synthetic data. Counts overlap and must not be added as unique tests. Broad runs and later focused deltas were separate snapshots; the evidence records those boundaries. No live patient data, appliance environment, backup or model weights were changed, and these results do not establish clinical go-live readiness.')

page(8,'Independent engineering follow-up')
add('Completed the confirmed follow-ups that could be repaired using existing product and offline-policy decisions. The eight earlier security defects and four UI findings remain closed. No clinical rules, integrations or feature-enablement decisions were invented. [14]')
table(['Area','What is now implemented'],[
('Live Scribe boundaries','Requires Scribe capability and current patient scope before hints or audio, then rechecks access before work/publication. Hints run off the event loop. Caps sessions, utterance bytes, duration, queued work and concurrent native workers; rejects overload visibly. Retired sessions cannot publish into replacements.'),
('Lens / Scribe completion','Requires successful HTTP status, valid JSON/message/content frames, nonempty output and explicit done:true. Truncated, malformed and failed streams do not become saved successful analyses or drafts. Connections, queue slots and failed uploads are released.'),
('Private speech transport','Patient-derived hints travel through a bounded stdin pipe, never process arguments. Workers reject the legacy argument form. Failed ASR is a typed failure, including the HTTP chunk route; genuine silence stays valid. Shared private WAV conversion cleans failed/interrupted output.'),
('Offline inference','Configuration rejects remote, wildcard, credential-bearing and malformed model origins. Only HTTP(S) loopback origins are allowed. The gateway validates again at call time before opening a request.'),
('Browser dictation','AudioWorklet replaces deprecated main-thread ScriptProcessor capture/VAD. Bounded message credits apply receipt backpressure. Candidate speech is retained, continuous speech splits below the server limit, and stop drains captured PCM. Late permission, chart changes, errors and unmount release resources.'),
('Vigil performance','A bounded in-memory cache reuses only identical complete feature/time tensors, patient context, model identity/state and normalisation metadata. Fresh inputs are assembled on every call. Corrections, age/history/time or model changes invalidate reuse; NEWS2 behavior is unchanged.'),
('Engineering quality','An independent CI job checks strict typing, lint/format and 100% statement/branch coverage for the origin validator, stdin protocol and inference cache. Pinned developer tools are separate from the appliance runtime.')
],[145,354],compact=True)
h('Fresh verification and boundaries')
add('The recorded checks cover synthetic authenticated sockets, failed Lens persistence/cleanup, bounded native concurrency, stale-session isolation, private speech hints, model/input cache invalidation, component lifecycles and real Chromium audio. 279 selected-commit backend checks and 87 frontend checks passed. Two final browser audio cases passed; the three-utility quality gate measured 100% statement/branch coverage. Source hashes and overlapping selections are recorded in the evidence. [14]', 'SmallOrb')
add('These are local engineering checks. They do not establish native speech accuracy, approved clinical models, busy-ward GPU capacity, Safari/iPad acceptance, hosted CI success or site deployment. Existing unrelated workspace changes are excluded from the selected commit.', 'SmallOrb')

page(9,'Engineering follow-up and fresh evidence')
add('20 October follow-up: completed additional local reliability and maintainability work using the existing product behavior. No deployment, live database, appliance runtime, model weights or external integration was changed. The exact selected commit is checked separately from concurrent workspace changes. [15]')
table(['Area','What changed'],[
('Backend structure','Extracted security headers, escaped SPA bootstrap and model-warming policy into typed modules. Preserved routing, header, streaming and operator-unload behavior. Time budgets reject non-finite values; retry exponents remain bounded.'),
('API and room isolation','Moved 98 public contracts into a type-only module. Added bounded UTF-8 SSE framing, split line-ending support, incomplete-event failures and reader cleanup. Relay rejects foreign-room socket events and stale send/refresh callbacks. Drafts and send status belong to the current room visit.'),
('Known defects closed','Ambient whole-word triggers preserve clinical inflections and intentional stems without matching ordinary prefix words. Action outcomes retain their originating assessment lane and episode. Background alert drafts no longer inflate the clinician time-saved proxy. Removed unreachable substitution code and repaired undefined bindings; clinical rules remain intact.'),
('Reliable acceptance tests','Removed fixed-date expiry from simulated-result fixtures, supplied synthetic QR encryption keys, updated Lens fixtures to the successful-stream contract and gave absent-patient bed-locator tests their own census. Explicit lexical seeding, real socket-manager fixtures and an owned test interpreter remove model-cache and checkout-venv assumptions. Restore scripts honor the selected interpreter and use portable file times. Product rules remain intact.'),
('Backend-wide quality checks','Added a standalone full-scope lint/type/annotation inventory and regression gate. Critical undefined-name, syntax and duplicate-binding checks have no allowlist. Per-module branch/statement coverage budgets and a reusable isolated acceptance runner accompany CI wiring.'),
('Strict utility checks','Expanded the strict gate from three to six utilities: lint, formatting, mypy strict and complete statement/branch coverage. The 67 pure checks cover 122 statements and 34 branches at 100%. Broader backend checks use measured legacy budgets, not a claim of full strict compliance.')
],[145,354],compact=True)
h('Verification and the remaining engineering debt')
add('Full working-copy evidence: 17,410 backend passes and 8 explicit skips; 3,722 frontend tests passed. Final selected frontend: 3,252 passed, with typecheck and production build. A final selected backend fix selection passed 696 tests with one skip; all seven previously expected backend failures now pass normally. The coverage cohort, fixture reruns and later defect delta have separate source records. Statement/branch coverage for the working-copy snapshot was 88.37% / 80.18%. [15]', 'SmallOrb')
add('Historical inventory: the earlier engineering snapshot recorded 7,835 lint diagnostics, 996 typing diagnostics, 4,117 missing annotation slots and 371 unformatted files. Page 10 supersedes its lint/type/format status using the newer 59ee1bc starting point. Hosted CI and site/hardware acceptance remain unperformed.', 'SmallOrb')

page(10,'Legacy lint and type cleanup')
add('20 October 2026 follow-up. Removed the configured diagnostics from the reviewed committed source, including the HIS changes present at starting commit 59ee1bc. [16]')
table(['Check','Before cleanup','After cleanup'],[
('Production Python scope','398 files','398 files'),
('Ruff diagnostics','8,314','0'),
('Mypy errors','1,051','0'),
('Unformatted Python files','382','0'),
('Frontend ESLint','1 error / 9 warnings','0 errors / 0 warnings'),
('Enforced diagnostic budgets','Legacy allowances','Zero lint/type/format')
],[215,142,142])
h('What we fixed')
add('Modernized Python imports, annotations and formatting; corrected configuration, provider, payload and nullable-value types; and made exception chaining explicit. Named FastAPI dependencies retain authorization checks. Fixed React effect dependencies, backup retention arguments and the reset tool database exception tuples. Regression tests preserve Sage streaming, deferred import barriers and existing API contracts.')
h('Fresh verification')
add(f"The full disposable backend run passed {VERIFICATION['backend']['passed']:,} tests, with {VERIFICATION['backend']['skipped']} explicit skips and no failures or errors. The frontend passed 3,481 tests across 357 files. TypeScript, ESLint and the production build passed. All 24 quality-tool tests and 67 strict utility tests passed; the six utilities retain 100% coverage of 122 statements and 34 branches.")
add('Documentation checks pass for all 9,028 citations and 490 symbol anchors. A final whitespace-only SQL edit passed 163 Relay tests, with one skip. Counts overlap; exact source hashes and run boundaries are recorded. Tests used synthetic, disposable state. No appliance deployment, integration connection or hosted CI run is claimed.', 'SmallOrb')
h('Remaining quality work')
add('The backend check is clean under its configured mypy rules. Full strict annotation coverage is a separate future migration: 4,334 preexisting missing parameter/return slots remain, down from 4,345 at the starting commit. The annotation gate prevents new debt. Optional SDKs and concurrent unfinished changes are outside this compliance claim. The per-module coverage budget check still fails: older baseline drift and uncovered defensive guards remain. Its budgets were not increased; a full CI pass is not claimed.', 'SmallOrb')

page(11,'What is still left to do')
add('This is the remaining-work summary as of 20 October. Built-but-disabled modules are not counted as missing. Open engineering, owner decisions and site acceptance require different next steps. [1-3, 7, 11-13, 15]')
table(['Area / status','Remaining work and next step'],[

('Session policy / decision + build','Set expiry/renewal policy, cross-tab session coherence and the offline cache revocation contract, including persistent-storage deletion failure. Implement and test the agreed behavior.'),
('Medication workflows / held epics','Finish the remaining verbal/read-back/countersign, substitution, stat completion, encounter reconciliation and legacy-writer retirement work where still held. Resolve R001/R002/R009/R010/R011/W111/W099 and dependent epics against current implementation; do not treat the whole medication system as absent.'),
('Clinical governance / decisions','Resolve discharge/escalation handling, laboratory critical-result and restricted-record/break-glass rules. Obtain approved local ranges, thresholds, protocols and sign-offs before enabling dependent behavior.'),
('Sage bed locator / follow-ups','R026 itself shipped on 19 October (f5a0afb). Four separate items remain: an unrecognised off-ward drug destination, order-notice wording, bare-number matching on coded wards, and relative/unsupported patient references carried in a confirmed reply. These require their own rulings.'),
('AI / validation + performance','Measure cold startup, chart preparation and busy-ward concurrency. Complete clinical model validation, train/serve parity, benchmark/promotion gates and licensed-data work. The existing non-sepsis extract adapter is not evidence of a trained or approved model.'),
('Infrastructure / remaining debt','Six utilities now pass strict typing and full scoped coverage; full-backend diagnostic and coverage regression gates are implemented. The configured legacy lint/type/format findings are now closed. Full strict annotations (4,334 missing slots), per-module coverage budget failures and further decomposition remain. Hosted CI/release acceptance is unperformed. PostgreSQL requires its own integration and environment.'),
('Recovery / site acceptance','Rebuild/validate the candidate runtime on the intended appliance. Run restore/key-escrow, signed-media recovery, power-loss, backup-under-load and hardware soak/concurrency drills.'),
('Hospital adoption / field work','Confirm the pilot site, accountable clinical lead, workflows, training, acceptance criteria and approved feature enablement. The 13 local modules remain default-off until deliberately accepted.'),
('External integration / scope decision','ABDM, claims/insurers, payment gateways, PACS/DICOM, analysers/devices and external reporting remain unconnected. Reconcile any proposed transport with the strict offline policy before implementation.'),
('Product expansion / backlog','Inter-facility transfer and unswept multi-site surfaces remain gated. Later Pulse stages (authored warnings/LAN sensors, local disease trends and recall/stock workflows) remain future work; existing local records do not complete every HIS epic.')
],[154,345], compact=True)

page(12,'Source record and status boundaries')
h('How to use the remaining-work list')
add('Engineering items can move through implementation and focused acceptance. Held behavior needs the owner or clinical authority to decide first. Site/hardware evidence requires a real controlled exercise. External integrations require both an explicit product decision and compatible permissions/transport. Earlier foundation logs are historical: later executable behavior and current rulings take precedence.')
h('Sources used')
add('Paths are relative to the Orb repository. Bracketed numbers refer to these local records. The 19 October remediation supersedes the original 12 October open status for the twelve reproduced findings; the latest bed-locator shipment supersedes older reports calling R026 unshipped.', 'SmallOrb')
sources=[
('1','Repository audit and completed fixes','docs/REPOSITORY_AUDIT_2026-10.md'),
('2','13 local modules and implementation boundaries','docs/OFFLINE_IMPLEMENTATION_2026-10.md'),
('3','Completion, browser acceptance and engineering follow-ups','docs/OFFLINE_COMPLETION_AND_BROWSER_2026-10.md'),
('4','ML security, adapters and earlier timing','docs/ML_SECURITY_AND_SAGE_LATENCY_2026-10.md'),
('5','Sage cache policy, speed and full-suite evidence','docs/SAGE_SPEED_FIX_2026-10.md'),
('6','Earlier capability narrative','docs/BUILD_LOG_PLAIN_ENGLISH.md'),
('7','Assessment plus 19 October remediation status','docs/SECURITY_AND_UX_ASSESSMENT_2026-10.md'),
('8','Earlier October fixes and verification','docs/GAP_FIXES_2026-10-02.md'),
('9','Historical fixes and working decisions','docs/WORKLOG.md; docs/ORB_FIX_LOG.md'),
('10','Fresh remediation results, logs and screenshots','docs/audit-2026-10/security-remediation-20261019/verification.json'),
('11','Latest bed-locator shipment and four follow-ups','docs/SAGE_BED_LOCATOR_FINDINGS_2026-10-03.md'),
('12','Owner decisions and clinical policy holds','docs/RULINGS_NEEDED_2026-10.md'),
('13','HIS epic plan and held follow-ons','docs/HIS_BUILD_PLAN_2026-10.md'),
('14','Independent engineering fixes and fresh verification','docs/ORB_AUTONOMOUS_FIXES_2026-10-19.md; docs/audit-2026-10/autonomous-fixes-20261019/verification.json'),
('15','20 October engineering follow-up and verification','docs/ORB_ENGINEERING_COMPLETION_2026-10-20.md; docs/audit-2026-10/engineering-20261020/verification.json'),
('16','20 October legacy lint/type cleanup and acceptance','docs/audit-2026-10/legacy-lint-types-20261020/verification.json'),
]
for n,label,path in sources: add(f'<b>[{n}] {label}</b><br/>{path}','SmallOrb')
add('Repository reviewed through 59ee1bc plus this lint/type cleanup, including bed-locator shipment f5a0afb, the security/UI remediation, engineering commit a1fd336 and the intervening HIS/Sage commits. Existing concurrent changes remain separate from this work. Updated 20 October 2026; this document records implementation and evidence, not deployment or clinical approval.', 'SmallOrb')

def footer(c, doc):
    w,hh=A4
    c.setFillColor(TEAL); c.rect(0,hh-10,w,10,fill=1,stroke=0)
    c.setStrokeColor(colors.HexColor('#DCE6E9')); c.line(48,43,w-48,43)
    c.setFont('Helvetica',8);c.setFillColor(MUTED)
    c.drawString(48,29,'ORB  /  Work completed and remaining gaps')
    c.drawRightString(w-48,29,f'20 OCT 2026   |   {doc.page}')

doc=SimpleDocTemplate(str(OUT),pagesize=A4,rightMargin=48,leftMargin=48,topMargin=40,bottomMargin=57,title='Orb - Work Summary',author='Orb',subject='Completed implementation, fixes, verification and remaining gaps')
doc.build(story,onFirstPage=footer,onLaterPages=footer)
print(OUT)
