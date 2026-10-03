#!/usr/bin/env node
/**
 * Token contrast gate for the "Quiet Clinical" design system.
 *
 * The visual audit (tests/visual/) catches contrast failures *after* they are
 * rendered, one capture at a time — 1,272 captures to learn that one token is
 * 0.24 short. This checks the palette itself, before a build, in a second.
 *
 * It reads the three theme blocks out of src/index.css, resolves var() chains
 * and rgba() overlays, and asserts every text token against every surface it
 * can actually land on. Crucially that includes the COMPOSITED surfaces —
 * --bg-glass over each base background — because those are what components
 * really paint on (a chip inside a card), and they are where the palette was
 * previously passing on paper but failing on screen.
 *
 * Usage:  node scripts/check-contrast.mjs [--verbose]
 * Exit 1 if any required pair is below its threshold.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
  walk, scanCss, scanTsx, measureSite, scanHoverSurfaces, measureHoverSurface,
} from './lib/contrast-callsites.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CSS = readFileSync(join(ROOT, 'src/index.css'), 'utf8');
const VERBOSE = process.argv.includes('--verbose');

// ── colour maths (WCAG 2.1) ──

function srgbToLinear(c) {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function luminance([r, g, b]) {
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
}

function contrast(fg, bg) {
  const a = luminance(fg), b = luminance(bg);
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}

/** Composite a possibly-translucent colour over an opaque backdrop. */
function over([r, g, b, a = 1], backdrop) {
  if (a >= 1) return [r, g, b];
  return [0, 1, 2].map((i) => Math.round(backdrop[i] + (( [r, g, b][i]) - backdrop[i]) * a));
}

function parseColor(raw) {
  const v = raw.trim();
  let m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
  if (m) {
    const h = m[1].length === 3 ? m[1].split('').map((c) => c + c).join('') : m[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1];
  }
  m = /^rgba?\(([^)]+)\)$/i.exec(v);
  if (m) {
    const p = m[1].split(',').map((x) => parseFloat(x));
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  }
  return null;
}

// ── theme extraction ──

const THEME_SELECTORS = {
  dark: /:root\s*\{([\s\S]*?)\n\}/,
  light: /\[data-theme="light"\]\s*\{([\s\S]*?)\n\}/,
  night: /\[data-theme="night"\]\s*\{([\s\S]*?)\n\}/,
};

function readTheme(name) {
  const block = THEME_SELECTORS[name].exec(CSS);
  if (!block) throw new Error(`theme block not found: ${name}`);
  const tokens = {};
  for (const line of block[1].split('\n')) {
    const m = /^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);/i.exec(line);
    if (m) tokens[m[1]] = m[2].trim();
  }
  return tokens;
}

/** Resolve a token through var() chains; dark is the fallback layer for light/night. */
function resolve(tokens, base, name, depth = 0) {
  if (depth > 8) return null;
  const raw = tokens[name] ?? base[name];
  if (!raw) return null;
  const v = /^var\(\s*(--[a-z0-9-]+)\s*\)$/i.exec(raw);
  if (v) return resolve(tokens, base, v[1], depth + 1);
  return parseColor(raw);
}

// ── the matrix ──

/**
 * Base surfaces every theme paints on. Each also gets a --bg-glass composite,
 * because a chip/hover fill inside a card lands there and it is measurably
 * lighter (dark: #2f2e2b + white 5.5% = rgb(58,57,55) — the exact surface that
 * carried 237 of the pre-fix contrast findings).
 */
const BASE_SURFACES = ['--bg-primary', '--bg-secondary', '--bg-surface', '--bg-elevated'];

/** Text tokens that must clear 4.5:1 on every surface above. */
const BODY_TEXT = ['--text-primary', '--text-secondary', '--text-muted'];

/**
 * Semantic colours used AS TEXT. These are the *-text variants, deliberately
 * separate from the fill colours of the same name: a colour tuned to be legible
 * as small text on a dark canvas is too light to carry white text when used as
 * a solid fill, and vice versa. Keeping one token for both jobs is what pushed
 * --status-danger to 4.03:1 as text while its fill still needed to stay dark.
 */
const SEMANTIC_TEXT = [
  '--accent-text',
  '--status-ok-text',
  '--status-warn-text',
  '--status-danger-text',
  '--status-info-text',
];

/** Solid fills that carry a label, and the token that labels them. */
const FILL_PAIRS = [
  ['--accent-deep', '--btn-primary-text'],
  ['--status-ok', '--status-on-fill'],
  ['--status-warn', '--status-on-fill'],
  ['--status-danger', '--status-on-fill'],
  ['--status-info', '--status-on-fill'],
  ['--btn-disabled-bg', '--btn-disabled-text'],
];

const AA_TEXT = 4.5;
const AA_LARGE = 3.0; // icons / ≥18.66px bold

/** Strongest status tint a chip background may use and still hold its label. */
const CHIP_TINT_MAX = 0.2;

const dark = readTheme('dark');
const themes = { dark, light: readTheme('light'), night: readTheme('night') };

const failures = [];
const rows = [];

for (const [themeName, tokens] of Object.entries(themes)) {
  // Build the surface list: each base, plus glass composited over it.
  const surfaces = [];
  for (const s of BASE_SURFACES) {
    const c = resolve(tokens, dark, s);
    if (!c) continue;
    surfaces.push([s, over(c, [0, 0, 0])]);
    const glass = resolve(tokens, dark, '--bg-glass');
    if (glass) surfaces.push([`${s} + glass`, over(glass, over(c, [0, 0, 0]))]);
  }

  const check = (fgName, bgName, fg, bg, threshold) => {
    if (!fg || !bg) {
      failures.push(`${themeName}: missing token ${!fg ? fgName : bgName}`);
      return;
    }
    const ratio = contrast(over(fg, bg), bg);
    const pass = ratio >= threshold;
    rows.push({ theme: themeName, fg: fgName, bg: bgName, ratio: +ratio.toFixed(2), need: threshold, pass });
    if (!pass) {
      failures.push(
        `${themeName}: ${fgName} on ${bgName} = ${ratio.toFixed(2)}:1 (needs ≥${threshold})`
      );
    }
  };

  for (const t of [...BODY_TEXT, ...SEMANTIC_TEXT]) {
    const fg = resolve(tokens, dark, t);
    for (const [bgName, bg] of surfaces) check(t, bgName, fg, bg, AA_TEXT);
  }

  for (const [fillName, labelName] of FILL_PAIRS) {
    const bg = resolve(tokens, dark, fillName);
    const fg = resolve(tokens, dark, labelName);
    check(labelName, fillName, fg, bg ? over(bg, [0, 0, 0]) : null, AA_TEXT);
  }

  /**
   * Status CHIPS: a tinted pill (`color-mix(in srgb, var(--status-x) N%, transparent)`
   * over a surface) carrying `--status-x-text`. These were invisible to the
   * check above, which only knew about the four flat backgrounds — and they are
   * exactly where the residual failures lived, because a 12% red wash lightens
   * the backdrop just enough to push 4.6:1 down to 4.4:1.
   *
   * The percentages below are the range actually used for chip BACKGROUNDS in
   * src (6%–20%; the 28% figure that also appears in the stylesheets is a
   * border tint, which carries no text, and the 72–88% ones are near-solid
   * fills that correctly use --status-on-fill instead). CHIP_TINT_MAX is
   * therefore a design rule as well as a test constant: a status chip
   * background must not exceed it, or its label leaves AA.
   */
  for (const status of ['ok', 'warn', 'danger', 'info']) {
    const tint = resolve(tokens, dark, `--status-${status}`);
    const label = resolve(tokens, dark, `--status-${status}-text`);
    if (!tint || !label) continue;
    for (const pct of [0.06, 0.12, CHIP_TINT_MAX]) {
      for (const base of BASE_SURFACES) {
        const surface = resolve(tokens, dark, base);
        if (!surface) continue;
        const chipBg = over([tint[0], tint[1], tint[2], pct], over(surface, [0, 0, 0]));
        check(`--status-${status}-text`, `${base} + ${Math.round(pct * 100)}% ${status} tint`, label, chipBg, AA_TEXT);
      }
    }
  }

  // Focus ring must be visible against every surface it outlines (non-text 3:1).
  const ring = resolve(tokens, dark, '--focus-ring');
  for (const [bgName, bg] of surfaces) check('--focus-ring', bgName, ring, bg, AA_LARGE);
}

if (VERBOSE) {
  for (const r of rows) {
    console.log(
      `${r.pass ? 'ok  ' : 'FAIL'} ${r.theme.padEnd(6)} ${r.fg.padEnd(24)} on ${r.bg.padEnd(26)} ${String(r.ratio).padStart(6)}:1 (≥${r.need})`
    );
  }
  console.log('');
}

/* ══════════════════════════════════════════════════════════════════════════
   PASS 2 — REAL CALL SITES

   Everything above validates the palette. It cannot tell you which token a
   component actually used, so it stayed green at 378/378 while a shipped
   button measured 1.93:1. This pass reads the components: every CSS rule and
   every inline style object in src/ that declares both a colour and a
   background, resolved through the same theme tables and measured with the
   same WCAG functions, in all three themes.

   Deliberately conservative — a pair it cannot resolve with certainty is
   SKIPPED, not passed. `--verbose` prints the skip count so the blind spot is
   visible rather than silent.
   ══════════════════════════════════════════════════════════════════════════ */
const SRC = join(ROOT, 'src');
const callSites = [
  ...walk(SRC, ['.css']).flatMap((f) => scanCss(f, ROOT)),
  ...walk(SRC, ['.tsx']).flatMap((f) => scanTsx(f, ROOT)),
];

const siteFailures = [];
let siteChecked = 0;
let siteSkipped = 0;

for (const [themeName, tokens] of Object.entries(themes)) {
  /* Backdrops for a TRANSLUCENT declared background (a 10% status wash, or
     --bg-glass itself). Base surfaces only, deliberately: the declared value IS
     the glass/tint layer, so compositing it over "surface + glass" as well
     would count the same wash twice and invent failures the design system
     never shipped. This is the same surface model the chip rule above uses. */
  const surfaceList = [];
  for (const s of BASE_SURFACES) {
    const c = resolve(tokens, dark, s);
    if (c) surfaceList.push(over(c, [0, 0, 0]));
  }

  for (const site of callSites) {
    const worst = measureSite(site, tokens, dark, surfaceList, themeName);
    if (!worst) { siteSkipped++; continue; }
    siteChecked++;
    if (worst.ratio < worst.need) {
      siteFailures.push(
        `${themeName}: ${site.where}\n      ${worst.fg} on ${worst.bg} = ${worst.ratio.toFixed(2)}:1 (needs ≥${worst.need})`
      );
    }
  }
}

if (VERBOSE) {
  console.log(
    `call sites: ${callSites.length} found, ${siteChecked} measurable pairs, ${siteSkipped} unresolvable (gradients / currentColor / computed)\n`
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   PASS 3 — HOVER AND FOCUS SURFACES

   The two passes above share a blind spot, and it was hiding live AA failures
   on the surface a clinician touches most:

       .task-row        { background: var(--bg-surface); }
       .task-row:hover  { background: var(--bg-glass-hover); }
       .task-row__detail{ color: var(--text-muted); }

   Pass 1 never composited --bg-glass-hover at all — only --bg-glass — because
   no rule declares a colour beside it. Pass 2 pairs a colour and a background
   declared on the SAME selector, and here the colour is on a child. So nobody
   measured --text-muted against the fill its parent had just changed:
   4.17:1 in dark, on every task row a nurse hovers, for months, while the gate
   printed 378/378 green.

   This pass takes every interaction-state rule that repaints the background
   with a translucent fill and measures the text that actually renders inside
   it — the element's own, plus BEM descendants and modifiers — against that
   composited surface, at its worst base surface, in all three themes.

   The rule it enforces, stated once: --bg-glass-hover may not sit under
   --text-muted or --accent-text. Use --bg-glass (4.51 / 4.50 worst case); it
   still marks the hover.
   ══════════════════════════════════════════════════════════════════════════ */
const hoverSites = walk(SRC, ['.css']).flatMap((f) => scanHoverSurfaces(f, ROOT));
const hoverFailures = [];
let hoverChecked = 0;
let hoverSkipped = 0;

for (const [themeName, tokens] of Object.entries(themes)) {
  const surfaceList = [];
  for (const s of BASE_SURFACES) {
    const c = resolve(tokens, dark, s);
    if (c) surfaceList.push(over(c, [0, 0, 0]));
  }
  for (const site of hoverSites) {
    const worst = measureHoverSurface(site, tokens, dark, surfaceList, themeName);
    if (!worst) { hoverSkipped++; continue; }
    hoverChecked++;
    if (worst.ratio < worst.need) {
      hoverFailures.push(
        `${themeName}: ${site.where}\n      ${worst.text} (${worst.fg}) on ${worst.bg} = ${worst.ratio.toFixed(2)}:1 (needs ≥${worst.need})`
      );
    }
  }
}

if (VERBOSE) {
  console.log(`hover/focus surfaces: ${hoverSites.length} found, ${hoverChecked} measured, ${hoverSkipped} unresolvable\n`);
}

if (failures.length || siteFailures.length || hoverFailures.length) {
  if (failures.length) {
    console.error(`✗ ${failures.length} palette contrast failure(s):\n`);
    for (const f of failures) console.error('  ' + f);
  }
  if (siteFailures.length) {
    console.error(`\n✗ ${siteFailures.length} call-site contrast failure(s):\n`);
    for (const f of siteFailures) console.error('  ' + f);
  }
  if (hoverFailures.length) {
    console.error(`\n✗ ${hoverFailures.length} hover/focus-surface contrast failure(s):\n`);
    for (const f of hoverFailures) console.error('  ' + f);
    console.error(
      '\n  A state fill is a backdrop for everything inside it. --bg-glass-hover\n' +
      '  may not sit under --text-muted or --accent-text; --bg-glass marks the\n' +
      '  hover just as well and holds AA.'
    );
  }
  console.error(`\n${rows.filter((r) => r.pass).length}/${rows.length} palette pairs pass; ${siteChecked - siteFailures.length}/${siteChecked} call sites pass; ${hoverChecked - hoverFailures.length}/${hoverChecked} hover surfaces pass.`);
  process.exit(1);
}

console.log(
  `✓ all ${rows.length} token/surface pairs pass (${Object.keys(themes).length} themes)\n` +
  `✓ all ${siteChecked} resolved component call sites pass (${callSites.length} colour/background pairs found in src/)\n` +
  `✓ all ${hoverChecked} hover/focus surfaces pass (${hoverSites.length} state fills, measured against the text inside them)`
);
