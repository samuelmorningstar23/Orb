'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { makeOfflineFixture, MODULES } = require('./offline-fixtures.cjs');
const { offlineScenarios } = require('./offline-scenarios.cjs');
const { fixtureFor } = require('./fixtures.cjs');
const params = new URLSearchParams();

test('every new offline module has an interactive scenario in both themes', () => {
  const scenarios = offlineScenarios();
  assert.deepEqual(scenarios.map(s => s.name.replace('offline/', '')).sort(), [...MODULES].sort());
  for (const scenario of scenarios) {
    assert.equal(scenario.user, ['radiology', 'care-plans', 'emergency', 'perioperative', 'dialysis'].some(domain => scenario.name === `offline/${domain}`) ? 'doctor' : 'admin');
    assert.ok(scenario.steps.length >= 4, scenario.name);
    assert.equal(scenario.themes, undefined, 'use the real runner dark/light pair');
    assert.equal(typeof scenario.fixtureFactory, 'function');
  }
});

test('context-local writes persist for that scenario without changing another context', () => {
  const first = makeOfflineFixture(), second = makeOfflineFixture();
  const before = second('GET', '/api/feedback', params).body.cases.length;
  first('POST', '/api/feedback', params, null, { subject: 'Synthetic isolated record', statement: 'Training-only statement', kind: 'complaint', channel: 'in_person', patient_id: null });
  assert.equal(first('GET', '/api/feedback', params).body.cases.length, before + 1);
  assert.equal(second('GET', '/api/feedback', params).body.cases.length, before);
  assert.equal(first('GET', '/api/feedback/unmatched', params), null, 'missing domain fixtures must reach the driver gate');
  assert.equal(first('POST', '/api/unmatched-offline', params), null);
});

test('reviewed small-cell fixture contains no hidden numerator or susceptibility counts', () => {
  const fixture = makeOfflineFixture();
  const result = fixture('GET', '/api/infection-surveillance/windows/WIN-SIM-2/metrics', new URLSearchParams('group_by=organism,antibiotic')).body;
  assert.equal(result.rate.suppressed, true);
  assert.equal(result.rate.numerator, null);
  assert.equal(result.rate.denominator, null);
  assert.equal(result.antibiogram[0].counts, null);
  assert.equal(result.antibiogram[0].tested, null);
  const rows = fixture('GET', '/api/infection-surveillance', params).body.windows;
  assert.ok(rows.every(row => !Object.hasOwn(row, 'finalized_counts_json')));
});

test('workbench collections use the named envelopes returned by the real routers', () => {
  const fixture = makeOfflineFixture();
  assert.ok(Array.isArray(fixture('GET', '/api/reporting/definitions', params).body.definitions));
  assert.ok(Array.isArray(fixture('GET', '/api/finance-tools/payout-statements', params).body.statements));
  assert.ok(Array.isArray(fixture('GET', '/api/finance-tools/journal/exports', params).body));
});

test('known navigation mutations use explicit non-generating contracts', () => {
  assert.deepEqual(fixtureFor('POST', '/api/sage/prefetch', params, null, { patient_id: 'PAT001' }).body,
    { status: 'skipped', patient_id: 'PAT001' });
  assert.deepEqual(fixtureFor('POST', '/api/ipass/PAT001/reasoned-assessment/prepare', params).body,
    { status: 'unavailable', assessment: null, stale_after_hours: 12, reason: 'disabled',
      message: 'Sage assessments are switched off on this appliance.' });
  assert.equal(fixtureFor('POST', '/api/unknown-navigation-mutation', params), null);
});

// Mirror the established role grants rather than manufacturing admin clinical write access.
test('clinical metadata preserves actual doctor, nurse and administrator capabilities', () => {
  const fixture = makeOfflineFixture();
  const meta = (domain, role) => fixture('GET', `/api/${domain}/meta`, params, { role }).body;
  for (const domain of ['radiology', 'care-plans', 'emergency', 'perioperative', 'dialysis']) {
    assert.equal(meta(domain, 'doctor').can_write, true);
    assert.equal(meta(domain, 'admin').can_write, false);
    assert.equal(meta(domain, 'admin').can_sign, false);
    assert.equal(meta(domain, 'admin').can_prescribe, false);
  }
  assert.equal(meta('radiology', 'nurse').can_write, false);
  assert.equal(meta('care-plans', 'nurse').can_write, true);
  assert.equal(meta('dialysis', 'nurse').can_prescribe, false);
  assert.equal(meta('dialysis', 'doctor').can_prescribe, true);
});
