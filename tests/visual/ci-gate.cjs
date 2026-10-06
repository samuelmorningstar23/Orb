/**
 * Orb visual-audit CI gate.
 *
 *   node tests/visual/ci-gate.cjs [--in=tests/visual/out] [--verbose]
 *
 * Reads findings.json produced by driver.cjs, filters to GATING findings,
 * subtracts the known-intentional allowlist, and exits non-zero if anything
 * non-allowlisted remains. Prints capture count + a ranked summary.
 *
 * Gating semantics:
 *   - GATING types: invisible-text, contrast, icon-contrast, invisible-control,
 *     horizontal-overflow, clipped-text, semantic-loss, console-error,
 *     driver-error, audit-error, assert-error.
 *   - NON-gating (informational, tracked for the IMPROVE loop): aa-contrast,
 *     small-target.
 *   - Findings with `unverifiable: true` (background not modelable —
 *     url() images/canvas) are downgraded to visual-verify: reported, not gated.
 *   - Findings with `disabled: true` (inside [disabled]/[aria-disabled]) are
 *     reported but not gated — WCAG exempts disabled controls; intentional
 *     disabled states (e.g. the DischargeAccess lockout button) live here.
 *   - Findings with `printLayout: true` (inside the print portals) gate only
 *     for invisible-text — print styling is monochrome by design.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const IN = (args.find((a) => a.startsWith('--in=')) || '').replace('--in=', '') ||
  path.join(__dirname, 'out');
const VERBOSE = args.includes('--verbose');

const GATING_TYPES = new Set([
  'invisible-text', 'contrast', 'icon-contrast', 'invisible-control',
  'horizontal-overflow', 'clipped-text', 'semantic-loss', 'console-error',
  'driver-error', 'audit-error', 'assert-error',
]);

/**
 * Known-intentional allowlist. Each entry: { reason, match(finding) → bool }.
 * Keep this list SHORT and specific — every entry is a design decision.
 */
const ALLOWLIST = [
  {
    reason: 'DischargeAccess verify button is disabled until a DOB is entered / during brute-force lockout (by design)',
    match: (f) =>
      f.scenario.startsWith('public/discharge-access') &&
      /verify-btn/.test(f.selector || '') &&
      (f.type === 'contrast' || f.type === 'invisible-control' || f.disabled === true),
  },
  {
    reason: 'Sage FAB orb: the icon sits on the painted disc (.sage-fab__orb-visual, pointer-events:none), ~10:1 visually; compositing misses the decorative layer',
    match: (f) => f.type === 'icon-contrast' && /sage-fab__orb/.test(f.selector || ''),
  },
  {
    reason: 'Forced-401 scenarios (errors/forced-401, errors/session-expired) intentionally produce "Session expired"/401 console noise',
    match: (f) =>
      (f.scenario === 'errors/forced-401' || f.scenario === 'errors/session-expired') &&
      f.type === 'console-error' &&
      /(401|session (expired|revoked)|token expired|failed to (fetch|load)|unauthorized)/i.test(f.detail || ''),
  },
  {
    reason: 'Vite dev-server HMR/websocket noise is environmental, not an app defect',
    match: (f) => f.type === 'console-error' && /\[vite\]|vite.*(ws|hmr)/i.test(f.detail || ''),
  },
  {
    // Ported from the ambient-sage branch harness: its orb-omnipresence pass
    // (now sage/orb-omnipresence) walks every lazily-loaded panel chunk in one
    // context, where this dev/preview-server transient was observed.
    reason: 'Vite lazy-chunk preload transient (Unable to preload CSS/module, failed dynamic import) — a build/serve artifact recovered by the panel error boundary, not an app defect. Kept narrow (the specific preload messages) so a genuine panel crash still gates.',
    match: (f) => f.type === 'console-error' &&
      /Unable to preload (CSS|module)|Failed to fetch dynamically imported module/i.test(f.detail || ''),
  },
  {
    reason: 'Mocked mode intentionally aborts the socket.io transport (driver route.abort). All /api is fulfilled, so a resource net-error here is only that blocked socket — expected and app-handled (panels fetch over REST).',
    match: (f) => f.mode === 'mocked' && f.type === 'console-error' &&
      /Failed to load resource|net::ERR_/i.test(f.detail || ''),
  },
  {
    reason: 'Live mode: LLM/voice backends (Sage, MedGemma/Ollama, Whisper voice agent, analysis streams) are not provisioned in this environment',
    match: (f) => f.mode === 'live' && f.type === 'console-error' &&
      /(sage|analysis|llm|ollama|medgemma|whisper|voice agent|stream)/i.test(f.detail || ''),
  },
];

function classify(f) {
  if (!GATING_TYPES.has(f.type)) return 'info';
  if (f.unverifiable) return 'visual-verify';
  // WCAG exempts disabled controls from contrast; disabled findings are
  // reported (this bucket) but don't gate. Intentional disabled states
  // (e.g. the DischargeAccess lockout button) land here.
  if (f.disabled) return 'disabled-exempt';
  if (f.printLayout && f.type !== 'invisible-text') return 'print-exempt';
  for (const a of ALLOWLIST) {
    if (a.match(f)) return `allowlisted: ${a.reason}`;
  }
  return 'GATE';
}

const findingsPath = path.join(IN, 'findings.json');
const capturesPath = path.join(IN, 'captures.json');
if (!fs.existsSync(findingsPath)) {
  console.error(`ci-gate: ${findingsPath} not found — run driver.cjs first`);
  process.exit(2);
}
const findings = JSON.parse(fs.readFileSync(findingsPath, 'utf8'));
const captures = fs.existsSync(capturesPath) ? JSON.parse(fs.readFileSync(capturesPath, 'utf8')) : [];

const buckets = { GATE: [], info: [], 'visual-verify': [], 'disabled-exempt': [], 'print-exempt': [], allowlisted: [] };
for (const f of findings) {
  const c = classify(f);
  if (c.startsWith('allowlisted')) buckets.allowlisted.push({ ...f, why: c });
  else buckets[c].push(f);
}

const SEV_ORDER = { critical: 0, major: 1, minor: 2, info: 3 };
buckets.GATE.sort((a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));

console.log(`Orb visual gate — ${captures.length} captures audited`);
console.log(`  gating findings:      ${buckets.GATE.length}`);
console.log(`  allowlisted:          ${buckets.allowlisted.length}`);
console.log(`  disabled-exempt:      ${buckets['disabled-exempt'].length}`);
console.log(`  print-exempt:         ${buckets['print-exempt'].length}`);
console.log(`  visual-verify only:   ${buckets['visual-verify'].length} (gradient/image backgrounds — check the gallery)`);
console.log(`  informational:        ${buckets.info.length} (aa-contrast, small-target)`);

function printFinding(f) {
  const loc = `${f.capture || f.scenario} [${f.theme}]`;
  const ratio = f.ratio ? ` ${f.ratio}:1` : '';
  console.log(`  [${f.severity}] ${f.type}${ratio} @ ${loc}`);
  console.log(`      ${f.selector || '(page)'} ${f.snippet ? JSON.stringify(f.snippet.slice(0, 50)) : ''}`);
  console.log(`      ${f.detail || ''}${f.fg ? ` fg=${f.fg} bg=${f.bg}` : ''}`);
}

if (buckets.GATE.length) {
  console.log('\n── GATING FINDINGS (ranked by severity) ──');
  const cap = VERBOSE ? Infinity : 80;
  buckets.GATE.slice(0, cap).forEach(printFinding);
  if (buckets.GATE.length > cap) console.log(`  … and ${buckets.GATE.length - cap} more (use --verbose)`);
}

if (VERBOSE && buckets['visual-verify'].length) {
  console.log('\n── VISUAL-VERIFY (non-gating; confirm in gallery) ──');
  buckets['visual-verify'].slice(0, 40).forEach(printFinding);
}
if (VERBOSE && buckets.allowlisted.length) {
  console.log('\n── ALLOWLISTED ──');
  for (const f of buckets.allowlisted) console.log(`  ${f.type} @ ${f.capture} [${f.theme}] — ${f.why}`);
}

// summary file for tooling
fs.writeFileSync(path.join(IN, 'gate-summary.json'), JSON.stringify({
  captures: captures.length,
  gating: buckets.GATE.length,
  allowlisted: buckets.allowlisted.length,
  visualVerify: buckets['visual-verify'].length,
  info: buckets.info.length,
  gate: buckets.GATE,
}, null, 2));

if (buckets.GATE.length) {
  console.log(`\n✗ GATE FAILED — ${buckets.GATE.length} non-allowlisted findings`);
  process.exit(1);
}
console.log('\n✓ GATE GREEN — 0 non-allowlisted findings');
