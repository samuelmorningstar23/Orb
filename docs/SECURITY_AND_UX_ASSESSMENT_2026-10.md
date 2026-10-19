# Orb security and browser assessment — 12 October 2026

## Remediation update - 19 October 2026

All eight reproduced security defects and four UI findings below are now fixed in the local working tree. The original 12 October assessment is retained below as the historical reproduction record. This is not a production deployment or closure of every separately listed source-only concern.

| Finding | Implemented repair |
|---|---|
| SEC-01 | Scribe lifecycle routes use the scoped note loader. Edit, sign, reject, revision, acknowledgement and retry writes recheck current identity and patient/private-note ownership inside the write transaction. Structure checks scope before generation and at save. |
| SEC-02 / SEC-03 | HTTP and sockets share authoritative token/device/portal validation. Incoming protected socket events and every publication revalidate recipients; revoked, expired, inactive or changed-scope subscriptions are disconnected. Publications target only the validated recipient snapshot, including disconnect-time presence events. Presence and Scribe cleanup share one handler. |
| SEC-04 | Authenticated ward packs filter every included patient and render from scoped snapshots; they never reuse or publish a shared full-ward artifact. Existing operator-generated durable packs remain separate. |
| SEC-05 | Backup producers HMAC-bind database/media hashes in a .set.json manifest using a domain-separated escrow key. Restore copies and authenticates the set, bounds and validates all archive headers, rejects unexpected paths/links/devices, and stages regular media privately before installation. Installation failures roll back both members; prior generations are retained. |
| SEC-06 | One conditional SQLite UPDATE consumes a TOTP step. Concurrent attempts have one winner; absent users and sequential replays fail. |
| SEC-07 | Lens authorizes before publishing media, owns upload completion across cancellation, removes unregistered files on stream/registration failure or cancellation, and retains registered successful analyses. |
| SEC-08 | QR generation uses the private encrypted media writer. The scoped HTTP route decrypts it with no-store; legacy plaintext QR reads remain compatible. |
| UX-01 | Admission stacks form and summary on narrow screens; the body scrolls independently of fixed-height header/actions, with a single-column field grid. |
| UX-02 | Governance/radiation grid children and native form controls shrink to the viewport. Wrapping controls and bottom space keep equipment pagination clear of the orb. |
| UX-03 | The admission field uses the focus trap's explicit initial-focus marker rather than React autoFocus, so the trap records and restores the actual opener. |
| UX-04 | The mobile shell keeps the account-security button visible and keyboard/touch reachable. |

Verification: **185 passed / 1 intentional real-backup skip** in the focused security/restore selection; **928 passed** in the compatibility selection; **239 passed in 26 frontend files**; **17 real browser journeys passed**, including six new light/dark responsive journeys at 390, 1280 and 1440px. A final Lens database-registration cancellation refinement passed **711 overlapping focused checks**, including a controlled in-flight save/disconnect race. A commit-preparation Scribe scope-argument placement correction passed **166 focused checks**; **59 checks** also passed on a clean HEAD export with only the selected commit changes overlaid. Typecheck, production build, targeted ESLint, shell syntax and whitespace checks passed. Selections overlap; do not sum them as a unique test count. Source hashes, logs, screenshot paths and precise limits are in [the remediation verification record](audit-2026-10/security-remediation-20261019/verification.json).

**Recovery compatibility:** existing media backups without the signed set manifest are now refused before any installation. Create a fresh backup set on a trusted primary, or authenticate only independently trusted historical artifacts using the helper's `seal` operation; do not sign an untrusted share merely to bypass validation. Legacy database-only backups retain their explicit missing-media warning. No existing appliance backup was changed. Real-site restore/key-escrow and power-loss drills remain site acceptance work.

Related repairs also close the source-only Scribe structure-scope, patient-presence scope and overwritten-disconnect findings. The other source-only concerns below remain separately tracked; they are not covered by the statement that the 12 reproduced findings are fixed.

---

## Executive assessment

The assessment reproduced **eight security defects**: four High findings affecting authenticated authorization or session revocation, one conditional High finding at the operator/backup boundary, and three Medium findings. The strongest evidence is a scoped read returning 404 or an HTTP session returning 401 while a related operation still succeeds. Existing test success does not establish these paths are safe.

Browser testing also found a severely clipped mobile admission form, horizontally clipped radiation-safety content, lost keyboard focus after closing the admission dialog, and a hidden mobile self-service MFA enrollment entry. Those findings are distinct from setup mistakes retained in the raw browser runs.

This was an assessment, not a remediation pass. Application behavior, clinical policies and model configuration were not changed. Orb remained offline: security checks ran in process against an owned source copy and synthetic SQLite data; browser checks used an owned loopback backend with inference disabled. No live patient data, `.env`, keys, uploads, backups or model weights were accessed. Held clinical decisions remain untouched.

The review covered HTTP and socket authentication, object/patient scope, Scribe lifecycle and streaming, Lens storage and completion, downtime packs, backup extraction, discharge media, configured model destinations, selected reporting/export and browser rendering boundaries. It is not an exhaustive penetration test, a clean bill of health, or a production deployment assessment. No denial-of-service load, model inference, external service or appliance endpoint was exercised.

## Prioritized issue matrix

| ID | Priority | Finding | Verification |
|---|---|---|---|
| SEC-01 | P1 / High | Scribe mutations bypass scoped reads; revising another author's private unfiled note discloses its transcript. | Actual authenticated HTTP and persisted database changes. |
| SEC-02 | P1 / High | Existing sockets continue receiving named patient vitals after session revocation. | Actual in-process Engine.IO/Socket.IO polling delivery. |
| SEC-03 | P1 / High | A revoked device-bound token can establish a new socket session although HTTP rejects it. | Actual in-process socket connection; synthetic signed token. |
| SEC-04 | P1 / High | A permitted patient's shared-ward downtime pack includes an out-of-scope patient. | Actual authenticated HTTP HTML response. |
| SEC-05 | P1 / conditional High | Companion media extraction can replace files outside the upload subtree during an authorized restore. | Exact extraction command against a harmless owned fixture; full restore not run. |
| SEC-06 | P2 / Medium | Concurrent consumers can reuse the same TOTP step. | Real function/SQLite with controlled scheduling; full HTTP race not run. |
| SEC-07 | P2 / Medium | Lens scope denial and failed streams leave unregistered uploads. | Actual route denial and controlled failed inference seam. |
| SEC-08 | P2 / Medium | Discharge QR files bypass encrypted/private media creation. | Actual writer creates plaintext PNG mode 0644 under umask 022. |
| UX-01 | P1 | Mobile admission dialog is severely clipped and obscured. | Screenshots and element geometry in light/dark modes. |
| UX-02 | P2 | Mobile radiation-safety content needs horizontal panning; the floating orb overlaps a navigation label. | Screenshots, geometry and actual horizontal pan. |
| UX-03 | P2 | Closing the admission dialog loses keyboard focus. | Escape-to-body reproduced across six viewport/theme profiles. |
| UX-04 | P2 | Authenticated self-service MFA enrollment entry is hidden on mobile. | Actual hidden trigger in both mobile themes plus source review; mandatory login enrollment remains available. |

## Reproduced security findings

### SEC-01 — Scribe object authorization

**Observed:** a Cardiology doctor cannot GET an Oncology draft (404), but can PATCH it (200); the stored note changes while retaining the other author. A different clinician's signed, unfiled note also returns 404, but the same caller can revise it (200), GET the resulting copy (200), and read the original raw transcript. Signed unfiled notes are a state the current signing route permits; the test did not change a clinical gate.

**Cause:** the scoped read helper at [scribe_routes.py:582](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/scribe_routes.py:582) is absent from the mutation paths. The verified edit path is at [line 792](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/scribe_routes.py:792); revision is at [line 996](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/scribe_routes.py:996). The revision service copies transcript/note/audio fields and assigns the requesting author at [scribe_service.py:1183](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/scribe_service.py:1183), allowing the copy to satisfy private-note ownership rules. Verify/sign/reject paths require the same review; they were source-reviewed, not all executed.

**Prerequisite/impact:** an authenticated account with the relevant Scribe capability and a foreign note identifier. The demonstrated effects are cross-scope draft modification and private transcript disclosure. This is not an anonymous access or automatic clinical-signature claim.

**Remediation:** enforce the common note/patient scope policy before every lifecycle read, model invocation or write; recheck it within the mutation transaction. Preserve private/unfiled ownership rules and existing signing decisions. Add negative tests for every lifecycle operation, including revision copies. The structure route's unchecked patient field is a related source-only gap below.

### SEC-02 — Connected socket sessions survive revocation

**Observed:** after an actual `users.token_version` increment, `/api/auth/me` returns 401 and a fresh socket authentication check rejects the token. An already connected socket still receives a `vitals:update` frame containing the synthetic patient's name and heart rate.

**Cause:** [realtime.py:382](/Users/samuelmorningstar/Documents/Projects/Orb/server/ws/realtime.py:382) saves user identity/role/department without the token's expiry, version or device identity. Room publication at [line 545](/Users/samuelmorningstar/Documents/Projects/Orb/server/ws/realtime.py:545) trusts retained membership. The real vitals publisher uses this path at [nurse_shift_service.py:1161](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/nurse_shift_service.py:1161).

**Prerequisite/impact:** a connection established while the session was valid. Revocation fails to stop demonstrated subsequent PHI delivery. Expiry, department/role changes and Relay delivery have similar source concerns, but those variants were not dynamically exercised.

**Remediation:** retain authoritative session/device/expiry information, revalidate it on a bounded schedule and sensitive publication, and evict affected sockets/rooms on revocation or scope change. Test an existing connection after revocation rather than only testing fresh handshakes.

### SEC-03 — Revoked tablet HTTP/socket parity

**Observed:** a correctly signed synthetic token with a `did` referring to an already revoked enrolled-device row returns HTTP 401 but receives the actual socket namespace acceptance frame. It can also receive the demonstrated vitals event.

**Cause:** HTTP authentication checks device revocation at [auth.py:513](/Users/samuelmorningstar/Documents/Projects/Orb/server/auth.py:513). Socket authentication at [realtime.py:199](/Users/samuelmorningstar/Documents/Projects/Orb/server/ws/realtime.py:199) checks the user and token version but omits the device check. Device revocation at [auth_routes.py:1169](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/auth_routes.py:1169) does not increment the user's token version.

**Prerequisite/impact:** possession of a still-unexpired previously issued device-bound token after device revocation. The real PIN issuance flow normally uses a 15-minute lifetime. The fixture created its own signed token; it did not exercise PIN issuance or demonstrate an ability to forge tokens. Existing signature, active-user, fresh token-version and origin checks remain effective.

**Remediation:** share one authoritative authentication policy between HTTP and sockets, including enrolled-device status; invalidate connected sessions when a device is revoked.

### SEC-04 — Downtime pack widens patient scope

**Observed:** a doctor's direct downtime read for another department's patient returns 404. The doctor's permitted patient occupies the same physical ward; its ward-pack request returns 200 and contains the foreign patient's name. Shared-ward boarders are supported by the bed-management model, so ward equality does not prove department scope.

**Cause:** [downtime_routes.py:104](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/downtime_routes.py:104) checks the anchor patient, while [downtime_service.py:1568](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/downtime_service.py:1568) selects the pack by physical ward and [line 1737](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/downtime_service.py:1737) serves the whole pack. Boarder support is at [bed_master_service.py:1738](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/bed_master_service.py:1738).

**Remediation:** scope every included patient and the cached artifact to the requesting principal's effective access. Do not use a permitted anchor patient as authority for an entire ward. Preserve the explicitly designed degraded-mode behavior; this finding does not require changing held read-audit decisions.

### SEC-05 — Restore archive containment

**Observed:** the exact `tar -xf ... -C dirname(UPLOADS_DIR)` extraction primitive replaces an owned harmless file beside uploads. A normal top-level archive member suffices; no traversal, absolute path or symlink was used. The full restore script and its gates were not executed.

**Cause:** [standby-restore.sh:321](/Users/samuelmorningstar/Documents/Projects/Orb/scripts/standby-restore.sh:321) extracts the media companion into the parent directory without validating that all members belong to the expected upload subtree. Database verification does not authenticate the archive's headers, paths or contents. Per-file media encryption does not authenticate archive structure.

**Prerequisite/impact:** control of the supplied companion archive or writable backup share, an authorized operator invoking restore, successful existing restore gates, and filesystem write access by that process. This is a conditional operator/supply-chain boundary defect, not an unauthenticated remote-code-execution proof. No real application/configuration file was targeted.

**Remediation:** authenticate the backup set and media companion; extract into a private staging directory; allow only expected regular files under the upload root; reject links, devices and unexpected paths; validate before installation. Ensure failed validation cannot leave a partially installed database/media pair.

### SEC-06 — TOTP single-use race

**Observed:** two real `consume_totp` calls accept the same valid step (`true, true`) when both actual SELECTs complete before either UPDATE. A normal sequential replay returns false.

**Cause:** [auth_routes.py:305](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/auth_routes.py:305) performs a read/check followed by an unconditional update. The read does not acquire the write transaction needed to serialize consumption. The test only controls scheduling after the SELECT; verification, update and commit remain unchanged. Complete concurrent HTTP logins were not run.

**Prerequisite/impact:** a valid current MFA code plus the other required authentication credentials, submitted concurrently. This weakens single-use enforcement; it does not bypass MFA without a valid code.

**Remediation:** consume the step with an atomic conditional update and check the affected-row count, or an equivalent correctly serialized transaction. Add a concurrent regression alongside the existing sequential replay test.

### SEC-07 — Lens orphan retention

**Observed:** a foreign-patient request returns 404 but creates one encrypted mode-0600 upload with no analysis row. A controlled generator failure likewise produces a visible typed terminal error and leaves one unregistered upload. An unauthenticated request returns 401 and creates no file.

**Cause:** [lens_routes.py:222](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/lens_routes.py:222) publishes the encrypted upload before the scope check at line 245. Analysis registration happens only after stream completion at line 272, without cleanup ownership on denial or failure.

**Prerequisite/impact:** an authenticated Lens-capable caller with Lens enabled. Lens remains investigational and disabled by default. Demonstrated impact is orphan retention/storage consumption, not plaintext disclosure or encryption bypass. Cancellation cleanup is a source-only variant.

**Remediation:** authorize before publishing, retain explicit ownership of temporary media until successful registration, clean it on denial/failure/cancellation, and apply bounded quotas/retention. Preserve Lens's unfiled-first clinical review gate.

### SEC-08 — Discharge QR storage bypass

**Observed:** `generate_qr_code` creates a plaintext PNG with mode 0644 under umask 022 despite a configured synthetic upload-encryption key.

**Cause:** [discharge_service.py:785](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/discharge_service.py:785) calls `img.save` directly instead of the existing encrypted/private media writer. The QR encodes a bearer link, while the database stores its hash. The file's read exposure depends on parent-directory permissions and local account access.

**Prerequisite/impact:** local filesystem read access to the upload directory. The scoped HTTP QR route and discharge DOB check remain defenses; anonymous remote download was not demonstrated.

**Remediation:** create QR media privately and store/read it through the existing encrypted media abstraction, keeping the decoded QR content and patient-facing flow unchanged.

## Additional source findings requiring targeted regressions

These were independently source-reviewed. They are not additional executed exploit proofs.

| Concern | Source and bounded assessment | Recommended verification/fix |
|---|---|---|
| P1 — Scribe structure accepts foreign patient ownership | [scribe_routes.py:475](/Users/samuelmorningstar/Documents/Projects/Orb/server/routes/scribe_routes.py:475), save at line 530; [scribe_service.py:584](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/scribe_service.py:584). A doctor/nurse with `scribe.create_draft`, models enabled and successful generation can save a draft under a caller-supplied foreign patient ID. It does not itself sign a note or read existing patient facts. | Enforce patient scope before model work and within save. Use a controlled generation seam for a negative route test. |
| P2 — Presence room patient scope | [realtime.py:682](/Users/samuelmorningstar/Documents/Projects/Orb/server/ws/realtime.py:682) accepts caller-named patient rooms using `patient.read_all` without patient scope. The matching publisher at line 673 carries viewer IDs/names/roles, not chart/vitals/transcript. Patient and pharmacist roles fail the permission gate. | Test foreign-patient room entry; scope the patient and preserve capability checks. |
| P2 — Live Scribe capability and resource limits | [scribe_stream.py:171](/Users/samuelmorningstar/Documents/Projects/Orb/server/ws/scribe_stream.py:171) uses authentication/model gates without Scribe capability or patient scope. Lines 204/217 grow cumulative audio and create transcription tasks without a session bound. A one-million-byte packet limit does not bound cumulative memory or task count. Raw hint disclosure and exhaustion were not tested. | Gate capability/patient context, cap session bytes/duration/queued work and apply backpressure; test with small bounded fixtures. |
| P2 — Lens accepts incomplete upstream completion | [lens_service.py:126](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/lens_service.py:126) treats natural EOF as completion, ignores malformed JSON frames and does not require a valid `done:true` frame. Partial nonempty prose can receive the disclaimer and reach route persistence. | Use fake upstream frames to test missing/malformed terminal events; require explicit valid completion. Keep clinical review gates. |
| P2 — ASR hint data in process arguments | [scribe_service.py:228](/Users/samuelmorningstar/Documents/Projects/Orb/server/services/scribe_service.py:228) sends patient-derived decoder hints in the fallback subprocess argument list; hint construction is at line 57. Local process inspection may expose the text. Argument-list execution avoids shell injection; remote disclosure was not demonstrated. | Use private stdin/IPC or another non-argv channel for patient text. |
| P2 — Offline model destination is not enforced at configuration boundary | [config.py:219](/Users/samuelmorningstar/Documents/Projects/Orb/server/config.py:219) normalizes `ORB_OLLAMA_BASE_URL` without a loopback restriction. [run-orb.sh:96](/Users/samuelmorningstar/Documents/Projects/Orb/run-orb.sh:96) refuses remote probing/startup but can continue the backend with that destination configured. No caller-controlled API setter or SSRF was found; this requires configuration control. | Fail closed for off-box inference in the offline policy, with an explicit separately approved policy if LAN inference is supported. No external destination was contacted in this assessment. |
| P3 — Presence disconnect cleanup is overwritten | [realtime.py:723](/Users/samuelmorningstar/Documents/Projects/Orb/server/ws/realtime.py:723), later [scribe_stream.py:241](/Users/samuelmorningstar/Documents/Projects/Orb/server/ws/scribe_stream.py:241), import at realtime line 866. The actual registered handler is the Scribe handler. Stale roster effects are source-derived; registration identity was inspected at runtime. | Compose both cleanups in one registered handler and assert roster/session cleanup on disconnect. |

## Browser findings

The [browser evidence package](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/browser-evidence/ui-ux-20261012/README.md) contains corrected accepted coverage and retained original attempts. Broad inventory screenshots are not a claim that every interaction passed. Missing patient selection, a missed logout, hidden panels and disabled setup actions were classified as harness/setup limitations rather than application crashes. The corrected focused and settled-register runs cover 58 states with 116 screenshots and zero scenario, console or page errors. No external or inference requests were recorded.

### UX-01 — Mobile admission form is obscured

At 390 × 844, the dialog keeps a 280px summary beside the form. The measured content column collapses to about 12px while form controls remain about 430px wide. The summary covers the identity/admission tabs and core fields; the footer also overlaps content. The behavior reproduces in light and dark modes. Some controls retain a tiny hit area, so this is not a claim that every control is technically unclickable.

The fixed horizontal layout is in [AdmitPatientModal.css:87](/Users/samuelmorningstar/Documents/Projects/Orb/src/components/AdmitPatientModal.css:87) and its fixed sidebar at [line 328](/Users/samuelmorningstar/Documents/Projects/Orb/src/components/AdmitPatientModal.css:328). Stack or collapse the summary at narrow widths, allow the form to shrink, and make the footer participate in a bounded scroll layout. Verify tabs, every field and submission at mobile width. The [light screenshot](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/browser-evidence/ui-ux-20261012/focused-final/screenshots/036-390-light-admission-overlap-viewport.png) and [dark screenshot](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/browser-evidence/ui-ux-20261012/focused-final/screenshots/045-390-dark-admission-overlap-viewport.png) show the obstruction.

### UX-02 — Radiation-safety mobile clipping and overlay

The settled mobile radiation-safety content is about 665px wide inside a 366px container. Labels, fields and next-page content are initially clipped, but horizontal panning recovers them; the test measured approximately 299px of pan. Document-level overflow checks alone miss this nested scrolling defect. The floating Sage orb overlaps the “Next equipment page” label. The seeded button was disabled, so blocked live navigation/mutation was not established.

The shared [RecordGovernancePanel.css layout](/Users/samuelmorningstar/Documents/Projects/Orb/src/pages/panels/RecordGovernancePanel.css:1) is used by the radiation panel. Give the form controls a mobile layout and reserve space for floating controls. Medical-records content in the corrected run fit at 366px and is not included as a clipping finding. Compare the [initial viewport](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/browser-evidence/ui-ux-20261012/registers-verified/screenshots/002-390-light-radiation-safety-viewport.png) with the [panned viewport](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/browser-evidence/ui-ux-20261012/registers-verified/screenshots/003-390-light-radiation-safety-horizontal-pan-end-viewport.png).

### UX-03 — Admission dialog loses focus on close

Pressing Escape returns focus to `BODY` rather than the admission trigger across six viewport/theme profiles, including 1440px, 1280px and 390px widths. The dialog's [autoFocus input](/Users/samuelmorningstar/Documents/Projects/Orb/src/components/AdmitPatientModal.tsx:795) has already taken focus when [useFocusTrap captures it](/Users/samuelmorningstar/Documents/Projects/Orb/src/hooks/useFocusTrap.ts:94), so the trap does not retain the original trigger. Capture the opener before moving focus into the dialog and restore it on every close path; verify both Escape and close-button behavior by keyboard.

### UX-04 — Mobile self-service MFA enrollment is hidden

The authenticated shell's [account-security trigger](/Users/samuelmorningstar/Documents/Projects/Orb/src/pages/ClinicianShell.tsx:547) sits inside `.shell__user-card`, which [the mobile CSS hides](/Users/samuelmorningstar/Documents/Projects/Orb/src/pages/ClinicianShell.css:1082) at widths of 768px or less. Both mobile themes retain the trigger in the DOM but hide it; no alternate authenticated mobile trigger was found. The [mandatory enrollment flow at login](/Users/samuelmorningstar/Documents/Projects/Orb/src/pages/Login.tsx:513) still renders when required by the server. This is a self-service access/discoverability gap, not an MFA enforcement bypass. Provide a reachable account-security entry in the mobile shell.

## Defenses and coverage limits

- HTTP session and enrolled-device revocation correctly returned 401 in the controls. Direct foreign Scribe/patient reads returned 404. Fresh socket authentication rejects token-version revocation. No invalid-signature, anonymous JWT or origin/host bypass was demonstrated.
- Anonymous Lens requests were denied without file creation. Its orphan files remain encrypted and mode 0600. The controlled failed stream reports a visible typed error rather than silently succeeding.
- Source review found reporting column/operator allowlists, parameter binding and CSV formula escaping; no reporting injection was reproduced. Selected HTML sanitization paths were reviewed, and the browser feedback HTML-like text control remained escaped without script execution. These are bounded controls, not comprehensive injection coverage.
- Previously recorded 12 October dependency snapshots reported zero advisories for the installed Python environment (70 packages), Apple ML metadata (76) and npm (752). They are dated historical scan evidence, not a fresh registry query made by this assessment.
- Some older security documentation describes defenses that have since changed. Treat those documents as architectural history until reconciled against current source; they are not proof that a vulnerability remains.
- The prior full backend regression baseline remains historical evidence. It was not rerun for this assessment because production source was not changed. A fresh [application parity comparison](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/security-assessment-20261012/application-parity.json) confirms all 1,283 application files in that baseline and all 181 built assets retain their hashes; there are no added built assets. The new three synthetic checks all exited 0 by confirming their assertions about the defects, not by proving security correctness.

## Remediation order and acceptance criteria

1. Close Scribe mutation/structure scope and downtime-pack inclusion boundaries. Prove every foreign object operation returns scoped denial and creates no copy, mutation or artifact. Keep existing clinical-signature/review decisions.
2. Unify HTTP/socket identity checks and invalidate live memberships. Prove revoked user/device, expired token and department change stop delivery on existing and new sessions.
3. Validate/authenticate restore media before installation. Prove harmless sibling, unexpected path and link fixtures cannot escape staging or partially replace the installed state; retain all existing restore guards.
4. Make TOTP consumption atomic; add bounded concurrent login/function regressions. Clean Lens temporary ownership and privately encrypt QR files without changing clinical or discharge workflows.
5. Resolve the source-only Scribe/presence/streaming/offline-policy concerns with controlled tests, keeping inference offline and avoiding load attacks.
6. Repair the mobile admission layout and focus restoration, then radiation-safety layout/overlay and mobile account-security access. Repeat the corrected browser states in both themes and retain before/after screenshots.

The evidence bundle is [security-assessment-20261012](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/security-assessment-20261012/README.md), including raw results, scripts, logs, hashes and exact interpretation limits. Independent operations review is [security-review-source-notes-20261012.json](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/security-review-source-notes-20261012.json). The [browser package](/Users/samuelmorningstar/Documents/Projects/Orb/docs/audit-2026-10/browser-evidence/ui-ux-20261012/README.md) preserves all six original and corrected attempts, 262 captured states, 366 PNGs, source provenance, cleanup records and artifact hashes. The raw attempts are retained without treating setup failures as UI crashes.
