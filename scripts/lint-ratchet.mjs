#!/usr/bin/env node
// Lint ratchet: the repo carries pre-existing ESLint debt; instead of blocking on
// it (or letting it grow unbounded), CI fails only if the TOTAL problem count
// EXCEEDS the recorded baseline. When the count drops, run with --update to
// lower the baseline so it can never rise again.
import { execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';

const BASELINE_FILE = new URL('../.lint-baseline.json', import.meta.url);

let out = '';
try {
  out = execSync('npx eslint . --format json', { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
} catch (e) {
  // eslint exits non-zero when problems exist — the JSON is still on stdout
  out = e.stdout?.toString() || '';
  if (!out) { console.error('eslint failed to run:', e.message); process.exit(2); }
}

const reports = JSON.parse(out);
const errors = reports.reduce((n, r) => n + r.errorCount, 0);
const warnings = reports.reduce((n, r) => n + r.warningCount, 0);
const total = errors + warnings;

if (process.argv.includes('--update')) {
  writeFileSync(BASELINE_FILE, JSON.stringify({ total, errors, warnings, updated: new Date().toISOString() }, null, 2) + '\n');
  console.log(`baseline updated: ${total} problems (${errors} errors, ${warnings} warnings)`);
  process.exit(0);
}

const baseline = JSON.parse(readFileSync(BASELINE_FILE, 'utf8'));
console.log(`lint ratchet: ${total} problems (baseline ${baseline.total})`);
if (total > baseline.total) {
  console.error(`FAIL: lint problems rose from ${baseline.total} to ${total}. Fix the new issues (or, if intentional, update .lint-baseline.json with: node scripts/lint-ratchet.mjs --update).`);
  process.exit(1);
}
if (total < baseline.total) {
  console.log(`Nice — count dropped from ${baseline.total} to ${total}. Consider ratcheting down: node scripts/lint-ratchet.mjs --update`);
}
process.exit(0);
