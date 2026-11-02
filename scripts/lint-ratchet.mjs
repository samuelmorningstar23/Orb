#!/usr/bin/env node
// Lint ratchet: the repo carries pre-existing ESLint debt; instead of blocking on
// it (or letting it grow unbounded), CI fails if the total problem count OR
// error count exceeds its baseline. When debt drops, --update lowers the budget.
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const BASELINE_FILE = new URL('../.lint-baseline.json', import.meta.url);

let out = '';
try {
  // Run the installed, lockfile-pinned binary. npx can otherwise fetch a tool
  // when dependencies are missing, and assumes npm is on the shell's PATH.
  out = execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/eslint/bin/eslint.js', import.meta.url)), '.', '--format', 'json'], {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
  });
} catch (e) {
  // eslint exits non-zero when problems exist — the JSON is still on stdout
  out = e.stdout?.toString() || '';
  if (!out) { console.error('eslint failed to run:', e.message); process.exit(2); }
}

const reports = JSON.parse(out);
const errors = reports.reduce((n, r) => n + r.errorCount, 0);
const warnings = reports.reduce((n, r) => n + r.warningCount, 0);
const total = errors + warnings;
const baseline = JSON.parse(readFileSync(BASELINE_FILE, 'utf8'));

if (process.argv.includes('--update')) {
  if (total > baseline.total || errors > baseline.errors) {
    console.error(`Refusing to increase lint debt: ${total} problems/${errors} errors exceeds baseline ${baseline.total}/${baseline.errors}. Fix the new issues before updating.`);
    process.exit(1);
  }
  writeFileSync(BASELINE_FILE, JSON.stringify({ total, errors, warnings, updated: new Date().toISOString() }, null, 2) + '\n');
  console.log(`baseline updated: ${total} problems (${errors} errors, ${warnings} warnings)`);
  process.exit(0);
}

console.log(`lint ratchet: ${total} problems (baseline ${baseline.total})`);
if (total > baseline.total || errors > baseline.errors) {
  console.error(`FAIL: lint debt exceeds baseline (${total}/${baseline.total} problems, ${errors}/${baseline.errors} errors). Fix the new issues before updating the baseline.`);
  process.exit(1);
}
if (total < baseline.total) {
  console.log(`Nice — count dropped from ${baseline.total} to ${total}. Consider ratcheting down: node scripts/lint-ratchet.mjs --update`);
}
process.exit(0);
