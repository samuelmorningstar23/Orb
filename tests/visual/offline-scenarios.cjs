'use strict';
const assert = require('node:assert/strict');
const { makeOfflineFixture, MODULES, PATIENT } = require('./offline-fixtures.cjs');

const root = (page, module) => page.locator(`#panel-${module}`);
async function waitHeading(surface, text) {
  const heading = surface.getByRole('heading', { name: text, exact: true });
  await heading.waitFor();
  // Screenshots should show the requested record/result, including content
  // below a long form inside the real shell scroll container.
  await heading.evaluate(element => element.scrollIntoView({ block: 'start' }));
}
async function pickPatient(surface) {
  await surface.getByLabel('Search name, hospital number or record ID', { exact: true }).filter({ visible: true }).fill('Simulation');
  await surface.getByRole('button', { name: `${PATIENT.name} · ${PATIENT.mrn}`, exact: true }).click();
}
async function submit(page, surface, label, path, method = 'POST') {
  const [response] = await Promise.all([
    page.waitForResponse(r => new URL(r.url()).pathname === `/api${path}` && r.request().method() === method),
    surface.getByRole('button', { name: label, exact: true }).click(),
  ]);
  assert.ok([200, 201].includes(response.status()), `${method} ${path} must succeed in the synthetic workflow`);
}
async function fill(surface, fields) {
  for (const [label, value] of Object.entries(fields)) {
    const text = surface.getByRole('textbox', { name: label, exact: true }).filter({ visible: true });
    if (await text.count()) await text.fill(value);
    else await surface.getByLabel(label, { exact: true }).filter({ visible: true }).fill(value);
  }
}
const step = (name, act) => ({ name, act: async ({ page }) => act(page) });
function base(module, heading, steps, user = 'admin') {
  return {
    name: `offline/${module}`, user, path: '/dashboard', waitFor: `#nav-${module}`,
    fixtureFactory: makeOfflineFixture,
    setup: async ({ page }) => { await page.locator(`#nav-${module}`).click(); await waitHeading(root(page, module), heading); },
    assert: async ({ page }) => { assert.equal(await root(page, module).getByRole('heading', { name: heading, exact: true }).isVisible(), true); return []; },
    steps,
  };
}
function offlineScenarios() {
  const scenarios = [
    base('feedback', 'Feedback & complaints', [
      step('statement-and-history', async page => { const s = root(page, 'feedback'); await s.getByRole('button', { name: 'Simulation waiting area feedback · investigating', exact: true }).click(); await waitHeading(s, 'Simulation waiting area feedback'); }),
      step('resolution-form', async page => { const s = root(page, 'feedback'); await s.getByRole('combobox', { name: 'Case action', exact: true }).selectOption('resolve'); await s.getByLabel('Response or reason', { exact: true }).fill('Synthetic review completed; training signage reference recorded.'); }),
      step('recorded-resolution', async page => { const s = root(page, 'feedback'); await submit(page, s, 'Record action', '/feedback/FB-SIM-001/actions'); await waitHeading(s, 'Recorded resolution'); }),
      step('new-complaint-form', async page => { const s = root(page, 'feedback'); await s.locator('summary').filter({ hasText: 'Record feedback or a complaint' }).click(); await pickPatient(s); await fill(s, { Subject: 'Simulation local feedback record', 'Original statement': 'Synthetic feedback submitted for browser verification.', 'Reporter name (optional)': 'Simulation reporter' }); }),
      step('complaint-saved', async page => { const s = root(page, 'feedback'); await submit(page, s, 'Save local record', '/feedback'); await s.getByRole('button', { name: 'Simulation local feedback record · recorded', exact: true }).waitFor(); }),
    ]),
    base('mortuary', 'Mortuary custody', [
      step('custody-detail', async page => { const s = root(page, 'mortuary'); await s.getByRole('button', { name: 'SIM-BODY-001 · Synthetic custody training record · stored', exact: true }).click(); await waitHeading(s, 'SIM-BODY-001 · Synthetic custody training record'); }),
      step('authorised-handover-form', async page => { const s = root(page, 'mortuary'); await s.getByRole('combobox', { name: 'Custody action', exact: true }).selectOption('release'); await fill(s, { 'Existing clearance / release authority reference': 'TRAINING-RELEASE-001', 'Recipient name': 'Simulation recipient', 'Recipient relationship or authority': 'Training source authority', 'Identity check reference (do not copy a full identity document)': 'TRAINING-CHECK-001', 'Different authorized witness account ID': 'SIM-OTHER-ADMIN', 'Reason / custody note': 'Synthetic custody handover documentation only.' }); }),
      step('recorded-handover', async page => { const s = root(page, 'mortuary'); await submit(page, s, 'Record custody action', '/mortuary/MOR-SIM-001/actions'); await s.getByText(/Handover recorded/).waitFor(); }),
      step('receipt-form', async page => { const s = root(page, 'mortuary'); await s.locator('summary').filter({ hasText: 'Record receipt into custody' }).click(); await fill(s, { 'Physical body tag': 'SIM-BODY-002', 'Recorded identity or unidentified-body label': 'Synthetic unidentified training label', 'Existing death / custody authority reference': 'TRAINING-CUSTODY-002' }); await s.getByRole('combobox', { name: 'Free storage position', exact: true }).selectOption('SLOT-SIM-2'); }),
      step('receipt-saved', async page => { const s = root(page, 'mortuary'); await submit(page, s, 'Record receipt', '/mortuary'); await s.getByRole('button', { name: /SIM-BODY-002/ }).waitFor(); }),
    ]),
    base('cssd', 'Sterile supply & instrument custody', [
      step('cycle-checks', async page => { const s = root(page, 'cssd'); await s.getByRole('button', { name: 'SIM-CYCLE-001 · completed', exact: true }).click(); await s.getByRole('combobox', { name: 'Cycle action', exact: true }).selectOption('record_checks'); await fill(s, { 'Recorded cycle measurements / log reference': 'TRAINING-CYCLE-LOG-002', 'Operator review / reason': 'Synthetic operator review of documented checks.' }); }),
      step('checks-recorded', async page => { const s = root(page, 'cssd'); await submit(page, s, 'Record cycle action', '/cssd/cycles/CYCLE-SIM-1/actions'); await s.getByText('Recorded measurements: TRAINING-CYCLE-LOG-002', { exact: true }).waitFor(); }),
      step('case-linked-custody-form', async page => { const s = root(page, 'cssd'); await s.getByRole('button', { name: 'SIM-PACK-001 · released', exact: true }).click(); await s.getByRole('combobox', { name: 'Pack action', exact: true }).selectOption('issue'); await fill(s, { 'Receiving service / location': 'Training theatre desk', 'Custody reference': 'TRAINING-ISSUE-001', 'Existing surgery ID (optional case traceability)': 'SURG-SIM-1', 'Processing / custody note': 'Synthetic custody issue; no assertion of clinical use.' }); }),
      step('custody-issue-recorded', async page => { const s = root(page, 'cssd'); await submit(page, s, 'Record pack action', '/cssd/packs/PACK-SIM-1/actions'); await s.getByText(/Training theatre desk · TRAINING-ISSUE-001/).waitFor(); }),
      step('cycle-start-form', async page => { const s = root(page, 'cssd'); await s.locator('summary').filter({ hasText: 'Record a processing cycle' }).click(); await fill(s, { 'Unique cycle reference': 'SIM-CYCLE-002', 'Sterilizer equipment reference': 'SIM-EQUIPMENT-01', 'Hospital SOP reference': 'TRAINING-SOP-REFERENCE', 'Method recorded by operator': 'Hospital-authored training method' }); await s.getByLabel('SIM-PACK-002', { exact: true }).check(); }),
    ]),
    base('reporting', 'Reports & cohorts', [
      step('filtered-query', async page => { const s = root(page, 'reporting'); await s.getByRole('button', { name: 'Add filter', exact: true }).click(); await s.getByRole('combobox', { name: 'Field', exact: true }).selectOption('status'); await s.getByLabel('Value', { exact: true }).fill('admitted'); }),
      step('record-preview', async page => { const s = root(page, 'reporting'); await submit(page, s, 'Preview report', '/reporting/query'); await waitHeading(s, 'Report preview'); await s.getByRole('cell', { name: PATIENT.name, exact: true }).waitFor(); }),
      step('summary-preview', async page => { const s = root(page, 'reporting'); await s.getByRole('combobox', { name: 'Your saved definitions', exact: true }).selectOption('REPORT-SIM-1'); await submit(page, s, 'Preview report', '/reporting/query'); await s.getByRole('cell', { name: 'Training department', exact: true }).waitFor(); await waitHeading(s, 'Report preview'); }),
      step('definition-saved', async page => { const s = root(page, 'reporting'); await s.getByLabel('Definition name', { exact: true }).fill('Synthetic reviewed ward census'); await submit(page, s, 'Update saved definition', '/reporting/definitions/REPORT-SIM-1', 'PUT'); await s.getByText('Report definition saved. It will rerun with your current access.', { exact: true }).waitFor(); }),
      step('csv-export', async page => { const s = root(page, 'reporting'); const download = page.waitForEvent('download'); await s.getByRole('button', { name: 'Export CSV', exact: true }).click(); assert.equal((await download).suggestedFilename(), 'orb-report.csv'); await s.getByText('CSV saved through your browser.', { exact: true }).waitFor(); }),
    ]),
    base('finance-tools', 'Accounting & doctor payouts', [
      step('balanced-journal-preview', async page => { const s = root(page, 'finance-tools'); await fill(s, { 'From date': '2026-05-01', 'Through date': '2026-05-23' }); await submit(page, s, 'Preview journal', '/finance-tools/journal/preview'); await waitHeading(s, 'Balanced source journal'); }),
      step('explicit-account-form', async page => { const s = root(page, 'finance-tools'); await s.getByRole('button', { name: 'Account mappings', exact: true }).click(); await s.getByRole('combobox', { name: 'Journal role', exact: true }).selectOption('receivable'); await fill(s, { 'Your account code': 'TRAIN-REC-REVIEWED', 'Your account name': 'Training reviewed receivables' }); }),
      step('mapping-saved', async page => { const s = root(page, 'finance-tools'); await submit(page, s, 'Save mapping', '/finance-tools/account-mappings', 'PUT'); await s.getByText('Account mapping saved. Existing export snapshots retain their recorded mapping.', { exact: true }).waitFor(); }),
      step('attribution-form', async page => { const s = root(page, 'finance-tools'); await s.getByRole('button', { name: 'Doctor attribution', exact: true }).click(); await s.getByRole('button', { name: 'Load charge worklist', exact: true }).click(); await s.getByRole('button', { name: 'Attribute charge 91', exact: true }).click(); await waitHeading(s, 'Attribute Training consultation'); }),
      step('explicit-rule-form', async page => { const s = root(page, 'finance-tools'); await s.getByRole('button', { name: 'Payout rules', exact: true }).click(); await s.getByRole('combobox', { name: 'Doctor', exact: true }).selectOption('DOC001'); await s.getByRole('combobox', { name: 'Applies to', exact: true }).selectOption('service'); await s.getByLabel('Existing service code', { exact: true }).fill('SIM-REVIEW'); await s.getByRole('combobox', { name: 'Amount method', exact: true }).selectOption('fixed'); await s.getByRole('combobox', { name: 'Basis', exact: true }).selectOption('net_ex_tax'); await s.getByRole('combobox', { name: 'Recognition', exact: true }).selectOption('fully_collected'); await fill(s, { 'Fixed amount in rupees': '100.00', 'Effective from': '2026-06-01' }); }),
      step('payout-preview', async page => { const s = root(page, 'finance-tools'); await s.getByRole('button', { name: 'Payout statements', exact: true }).click(); await submit(page, s, 'Preview earnings', '/finance-tools/payouts/preview'); await waitHeading(s, 'Unclaimed source earnings'); }),
      step('draft-statement', async page => { const s = root(page, 'finance-tools'); await s.getByRole('button', { name: 'Open PAYOUT-S', exact: true }).click(); await waitHeading(s, 'Statement PAYOUT-S'); }),
      step('second-person-approval', async page => { const s = root(page, 'finance-tools'); await submit(page, s, 'Approve as second person', '/finance-tools/payout-statements/PAYOUT-SIM-1/approve'); await s.getByLabel('Reference for payment already made', { exact: true }).fill('TRAINING-PAYMENT-ATTESTATION'); }),
      step('payment-attested', async page => { const s = root(page, 'finance-tools'); await submit(page, s, 'Record payment already made', '/finance-tools/payout-statements/PAYOUT-SIM-1/paid'); await s.getByText('Payment attestation recorded. No funds were transmitted.', { exact: true }).waitFor(); await waitHeading(s, 'Statement PAYOUT-S'); }),
    ]),
    base('record-governance', 'Medical records', [
      step('patient-hold-detail', async page => { const s = root(page, 'record-governance'); await pickPatient(s); await s.getByRole('button', { name: 'Synthetic preservation request · active', exact: true }).click(); await waitHeading(s, 'Synthetic preservation request'); }),
      step('hold-release-form', async page => { const s = root(page, 'record-governance'); await fill(s, { Reason: 'Synthetic authority verified for training release', 'Release authority reference': 'TRAINING-HOLD-RELEASE' }); }),
      step('hold-released', async page => { const s = root(page, 'record-governance'); await submit(page, s, 'Record action', '/record-governance/hold/HOLD-SIM-1/transition'); await s.getByRole('button', { name: 'Synthetic preservation request · released', exact: true }).waitFor(); }),
      step('file-custody', async page => { const s = root(page, 'record-governance'); await s.getByRole('button', { name: 'File custody', exact: true }).click(); await s.getByRole('button', { name: 'SIM-MRD-001 · archived', exact: true }).click(); await s.getByRole('combobox', { name: 'Record action', exact: true }).selectOption('checked_out'); await fill(s, { Reason: 'Training file movement reference', 'New location': 'Simulation review desk', 'New custodian': 'Training receiving custodian' }); }),
      step('file-movement-recorded', async page => { const s = root(page, 'record-governance'); await submit(page, s, 'Record action', '/record-governance/file/FILE-SIM-1/transition'); await s.getByRole('button', { name: 'SIM-MRD-001 · checked out', exact: true }).waitFor(); }),
      step('release-request-detail', async page => { const s = root(page, 'record-governance'); await s.getByRole('button', { name: 'Release requests', exact: true }).click(); await s.getByRole('button', { name: 'Synthetic authorised copy request · requested', exact: true }).click(); await waitHeading(s, 'Synthetic authorised copy request'); }),
    ]),
    base('radiation-safety', 'Radiation safety documents', [
      step('source-evidence-detail', async page => { const s = root(page, 'radiation-safety'); await s.getByRole('button', { name: 'SIM-XR-001 · SIM-LICENCE-001 · within recorded dates', exact: true }).click(); await waitHeading(s, 'SIM-LICENCE-001'); }),
      step('officer-document-form', async page => { const s = root(page, 'radiation-safety'); await s.getByLabel('Search equipment tag, name or serial number', { exact: true }).fill('SIM-XR'); await s.getByRole('button', { name: 'Search equipment', exact: true }).click(); await s.getByRole('combobox', { name: 'Equipment', exact: true }).selectOption('9001'); await s.getByRole('combobox', { name: 'Document type', exact: true }).selectOption('officer'); await fill(s, { 'Document reference': 'SIM-OFFICER-001', 'Issuing authority': 'Synthetic appointment source', 'Valid from': '2026-01-01', 'Valid through': '2026-12-31', 'Officer name': 'Simulation safety officer', 'Qualification or approval reference': 'TRAINING-QUALIFICATION-001', 'Scope as recorded on the source document': 'Synthetic appointment evidence for training equipment only.' }); }),
      step('officer-evidence-saved', async page => { const s = root(page, 'radiation-safety'); await submit(page, s, 'Record evidence', '/radiation-safety'); await s.getByRole('button', { name: 'SIM-XR-001 · SIM-OFFICER-001 · within recorded dates', exact: true }).click(); await waitHeading(s, 'SIM-OFFICER-001'); }),
      step('withdrawal-recorded', async page => { const s = root(page, 'radiation-safety'); await s.getByLabel('Reason for withdrawal', { exact: true }).fill('Synthetic source superseded; training withdrawal recorded.'); await s.getByRole('button', { name: 'Withdraw recorded evidence', exact: true }).click(); await s.getByText('Withdrawal recorded.', { exact: true }).waitFor(); }),
    ]),
    base('infection-surveillance', 'Infection surveillance & isolation records', [
      step('manual-case-review', async page => { const s = root(page, 'infection-surveillance'); await s.getByRole('button', { name: 'Simulation open observation window · open', exact: true }).click(); await s.locator('summary').filter({ hasText: 'Review episode SIM-EPISODE-001' }).click(); await s.getByRole('combobox', { name: 'Authored case classification', exact: true }).selectOption('included'); await fill(s, { 'Classification reason under this definition': 'Synthetic clinician review against the authored training definition.', 'Clinician decision / reviewer reference': 'TRAINING-REVIEW-001' }); }),
      step('case-classification-recorded', async page => { const s = root(page, 'infection-surveillance'); await submit(page, s, 'Record case classification', '/infection-surveillance/cases/CASE-SIM-1/classification'); await s.locator('summary').filter({ hasText: 'Review episode SIM-EPISODE-001 · included' }).waitFor(); }),
      step('validated-susceptibility-form', async page => { const s = root(page, 'infection-surveillance'); await s.locator('summary').filter({ hasText: 'Record source evidence in this window' }).click(); await s.getByRole('combobox', { name: 'Source record type', exact: true }).selectOption('susceptibility'); await pickPatient(s); await fill(s, { 'Observed / specimen date (hospital calendar)': '2026-05-20', 'Distinct laboratory isolate reference': 'SIM-ISOLATE-001', 'Hospital organism code': 'SIM-ORG', 'Hospital specimen code': 'SIM-SPECIMEN', 'Hospital antibiotic code': 'SIM-ABX', 'Laboratory interpretation standard and edition': 'TRAINING-LAB-STANDARD', 'Recorded laboratory method': 'Training method', 'Validated result / laboratory reviewer reference': 'TRAINING-VALIDATION', 'Source evidence reference': 'TRAINING-LAB-SOURCE' }); await s.getByRole('combobox', { name: 'Validated laboratory interpretation', exact: true }).selectOption('S'); }),
      step('suppressed-reviewed-results', async page => { const s = root(page, 'infection-surveillance'); await s.getByRole('button', { name: 'Simulation suppressed reviewed window · finalized', exact: true }).click(); await s.getByText('Surveillance rate suppressed by the hospital minimum-cell policy.', { exact: true }).waitFor(); assert.equal(await s.getByRole('cell', { name: 'Suppressed', exact: true }).count(), 3); const results = s.getByRole('region', { name: 'Susceptibility results, scroll horizontally for all columns', exact: true }); await results.getByRole('cell', { name: 'Suppressed', exact: true }).last().scrollIntoViewIfNeeded(); assert.ok(await results.evaluate(element => element.scrollLeft > 0), 'narrow results pane supports scrolling to the suppressed count columns'); await waitHeading(s, 'Local denominator and antibiogram'); }),
      step('authored-isolation-end-form', async page => { const s = root(page, 'infection-surveillance'); await s.getByRole('button', { name: new RegExp(`Patient ${PATIENT.id} · active`) }).click(); await fill(s, { 'Recorded isolation end (browser local time)': '2026-05-23T09:00', 'Authored end reason': 'Synthetic clinical end decision recorded for training.', 'Clinical end decision / authority reference': 'TRAINING-ISOLATION-END' }); }),
      step('isolation-end-recorded', async page => { const s = root(page, 'infection-surveillance'); await submit(page, s, 'Record isolation end', '/infection-surveillance/isolations/ISO-SIM-1/end'); await s.getByText(/Recorded end:/).waitFor(); }),
    ]),
  ];
  for (const [domain, heading, seedTitle, draftFields] of [
    ['radiology', 'Radiology', 'Simulation chest radiograph report', { 'Body site': 'Training chest site', 'Indication': 'Synthetic request documentation', 'Report findings': 'Training findings only' }],
    ['care-plans', 'Care plans', 'Simulation mobility care plan', { 'Problem / focus': 'Synthetic mobility focus', 'Care goal': 'Hospital-authored training goal' }],
    ['emergency', 'Emergency records', 'Simulation emergency encounter', { 'Presenting complaint': 'Synthetic presenting complaint', 'Category recorded by staff': 'Hospital training category', 'Triage assessment': 'Authored training assessment; no automatic criteria' }],
    ['perioperative', 'Perioperative records', 'Simulation implant traceability', { 'Pre-anaesthesia assessment': 'Synthetic authored assessment', 'Recorded technique': 'Training source entry', 'Airway record': 'Training documentation only' }],
    ['dialysis', 'Dialysis records', 'Simulation dialysis session record', { 'Access': 'Training access reference', 'Indication': 'Synthetic clinician-authored indication' }],
  ]) {
    scenarios.push(base(domain, heading, [
      step('record-detail-history', async page => { const s = root(page, domain); await s.getByRole('button', { name: seedTitle, exact: true }).click(); await waitHeading(s, 'Recorded documentation'); await waitHeading(s, 'Record history'); await s.locator('[aria-label="Record history"] summary').first().click(); }),
      step('new-patient-documentation-form', async page => { const s = root(page, domain); await s.getByRole('button', { name: 'Close record', exact: true }).click(); await pickPatient(s); await s.getByRole('button', { name: 'New record', exact: true }).click(); await s.getByLabel('Record title', { exact: true }).fill(`Simulation new ${domain} documentation`); if (domain === 'perioperative') await s.getByRole('combobox', { name: 'Surgical case', exact: true }).selectOption('SURG-SIM-1'); if (domain === 'radiology') await s.getByRole('combobox', { name: 'Modality', exact: true }).selectOption('X-ray'); await fill(s, draftFields); if (domain === 'care-plans') { await s.getByRole('button', { name: 'Add intervention', exact: true }).click(); await fill(s, { 'Intervention': 'Training assisted review', 'Responsible staff': 'Simulation nurse' }); } if (['emergency', 'dialysis', 'perioperative'].includes(domain)) { await s.getByRole('button', { name: 'Add observation', exact: true }).click(); await s.getByLabel('Observation note', { exact: true }).fill('Synthetic observation only'); } if (domain === 'dialysis') { await s.getByRole('button', { name: 'Record prescription', exact: true }).click(); await fill(s, { 'Prescribed modality': 'Training modality', 'Prescribed duration (minutes)': '30', 'Prescriber instructions': 'Synthetic prescriber source; no treatment command' }); } }),
      step('new-documentation-saved', async page => { const s = root(page, domain); await submit(page, s, 'Create record', `/${domain}/records`); await s.getByText('Record saved.', { exact: true }).waitFor(); await s.getByRole('button', { name: `Simulation new ${domain} documentation`, exact: true }).waitFor(); await waitHeading(s, 'Recorded documentation'); }),
      step('revision-saved', async page => { const s = root(page, domain); await s.getByLabel('Change / cancellation reason', { exact: true }).fill('Synthetic source correction verified'); await s.getByLabel('Record title', { exact: true }).fill(`Simulation revised ${domain} documentation`); await s.getByRole('button', { name: 'Save revision', exact: true }).click(); await s.getByRole('button', { name: `Simulation revised ${domain} documentation`, exact: true }).waitFor(); await waitHeading(s, 'Recorded documentation'); }),
    ], 'doctor'));
  }
  assert.deepEqual(scenarios.map(s => s.name.slice('offline/'.length)).sort(), [...MODULES].sort());
  return scenarios;
}
module.exports = { offlineScenarios };
