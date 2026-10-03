/**
 * Call-site contrast scanner.
 *
 * WHY THIS EXISTS
 * check-contrast.mjs validates the PALETTE: every text token against every
 * surface token, in three themes. That is necessary and it is not sufficient —
 * it can only tell you that a *correct* pairing would pass. It never sees
 * which token a component actually reached for. The gate reported 378/378
 * green while `.suite__ai-badge` painted white on --accent-gold at 3.12:1 and
 * the Start Surgery button's disabled state measured 1.93:1, because both use
 * pairs the palette test was never asked about.
 *
 * This module closes that gap by reading the components themselves:
 *   - every CSS rule in src/** that declares a `color` and a background
 *     (its own, or one inherited from the same selector's base rule);
 *   - every inline `style={{ … }}` object in src/**.tsx that declares both,
 *     including the ternary forms (`cond ? 'var(--a)' : 'var(--b)'`), which
 *     are how most of Orb's stateful buttons are written.
 *
 * Each pair is resolved through the real theme tables — var() chains,
 * color-mix(in srgb, …), rgba() overlays — and measured with the SAME WCAG
 * functions the palette gate uses, in all three themes. Translucent fills are
 * composited over each base surface and scored at their worst case, because
 * that is the surface a clinician actually sees.
 *
 * It is deliberately conservative: anything it cannot resolve with certainty
 * (gradients, currentColor, computed strings) is skipped rather than guessed.
 * A skipped site is invisible to the gate — it is not a pass.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

/* ── colour maths — shared with check-contrast.mjs ── */

export function srgbToLinear(c) {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

export function luminance([r, g, b]) {
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
}

export function contrast(fg, bg) {
  const a = luminance(fg), b = luminance(bg);
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}

/** Composite a possibly-translucent colour over an opaque backdrop. */
export function over([r, g, b, a = 1], backdrop) {
  if (a >= 1) return [r, g, b];
  return [0, 1, 2].map((i) => Math.round(backdrop[i] + ([r, g, b][i] - backdrop[i]) * a));
}

const NAMED = {
  white: [255, 255, 255, 1],
  black: [0, 0, 0, 1],
  transparent: [0, 0, 0, 0],
};

/* ── value resolution ── */

/**
 * Resolve a CSS colour expression to [r,g,b,a].
 * Returns null for anything not confidently resolvable — gradients,
 * currentColor, inherit, computed template strings.
 */
export function resolveValue(raw, tokens, base, depth = 0) {
  if (raw == null || depth > 10) return null;
  const v = String(raw).trim().replace(/\s*!important$/, '');
  if (!v) return null;

  if (NAMED[v.toLowerCase()]) return NAMED[v.toLowerCase()].slice();

  let m = /^#([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(v);
  if (m) {
    let h = m[1];
    if (h.length === 3 || h.length === 4) h = h.split('').map((c) => c + c).join('');
    const a = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), a];
  }

  m = /^rgba?\(([^()]+)\)$/i.exec(v);
  if (m) {
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(parseFloat);
    if (p.length < 3 || p.some(Number.isNaN)) return null;
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  }

  m = /^var\(\s*(--[a-z0-9-]+)\s*(?:,\s*([\s\S]+))?\)$/i.exec(v);
  if (m) {
    const token = tokens[m[1]] ?? base[m[1]];
    if (token != null) return resolveValue(token, tokens, base, depth + 1);
    return m[2] ? resolveValue(m[2], tokens, base, depth + 1) : null;
  }

  // color-mix(in srgb, <A> <P>%, <B>)  — B may be omitted-percentage or transparent.
  m = /^color-mix\(\s*in\s+srgb\s*,\s*([\s\S]+)\)$/i.exec(v);
  if (m) {
    const parts = splitTopLevel(m[1]);
    if (parts.length !== 2) return null;
    const a = parseMixPart(parts[0], tokens, base, depth);
    const b = parseMixPart(parts[1], tokens, base, depth);
    if (!a || !b) return null;
    // Percentages that do not sum to 100 are normalised, per CSS Color 5.
    let pa = a.pct, pb = b.pct;
    if (pa == null && pb == null) { pa = 0.5; pb = 0.5; }
    else if (pa == null) pa = 1 - pb;
    else if (pb == null) pb = 1 - pa;
    const total = pa + pb;
    if (total <= 0) return null;
    pa /= total; pb /= total;
    // Mix in premultiplied alpha so `X 12%, transparent` behaves like a wash.
    const aa = a.color[3] ?? 1, ba = b.color[3] ?? 1;
    const alpha = aa * pa + ba * pb;
    if (alpha <= 0) return [0, 0, 0, 0];
    const rgb = [0, 1, 2].map((i) =>
      Math.round((a.color[i] * aa * pa + b.color[i] * ba * pb) / alpha),
    );
    return [rgb[0], rgb[1], rgb[2], alpha];
  }

  return null;
}

function parseMixPart(part, tokens, base, depth) {
  const t = part.trim();
  const pm = /\s(\d+(?:\.\d+)?)%$/.exec(t);
  const colorText = pm ? t.slice(0, pm.index).trim() : t;
  const color = resolveValue(colorText, tokens, base, depth + 1);
  if (!color) return null;
  return { color, pct: pm ? parseFloat(pm[1]) / 100 : null };
}

/** Split "a, b" at top level, ignoring commas inside parentheses. */
function splitTopLevel(s) {
  const out = [];
  let depth = 0, cur = '';
  for (const ch of s) {
    if (ch === '(') depth++;
    else if (ch === ')') depth--;
    if (ch === ',' && depth === 0) { out.push(cur); cur = ''; continue; }
    cur += ch;
  }
  if (cur.trim()) out.push(cur);
  return out;
}

/* ── source discovery ── */

export function walk(dir, exts, out = []) {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.')) continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) walk(full, exts, out);
    else if (exts.some((e) => entry.endsWith(e))) out.push(full);
  }
  return out;
}

/* ── CSS call sites ── */

const COLOR_PROPS = new Set(['color']);
const BG_PROPS = new Set(['background', 'background-color']);

/**
 * Extract { selector, color, background, fontSize, fontWeight, file, line }
 * from every rule that declares a colour. `background` may come from the
 * rule itself; a rule that only restates a background (a :hover) inherits its
 * colour from the same selector's base rule, which is how `.sage-btn-stop`
 * and its `:hover` twin actually render.
 */
export function scanCss(file, root) {
  const src = readFileSync(file, 'utf8');
  const rel = relative(root, file);
  const rules = [];

  // Strip comments, keeping length so line numbers stay honest.
  const clean = src.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '));

  /* Ranges covered by `@media print`. Print rules land on white paper, not on
     an app surface, so measuring them against --bg-* would be nonsense (and
     did report `#000 on transparent = 1.00:1` for the discharge printout). */
  const printRanges = [];
  const mediaRe = /@media[^{]*\bprint\b[^{]*\{/gi;
  let mm;
  while ((mm = mediaRe.exec(clean)) !== null) {
    let depth = 1;
    let i = mediaRe.lastIndex;
    for (; i < clean.length && depth > 0; i++) {
      if (clean[i] === '{') depth++;
      else if (clean[i] === '}') depth--;
    }
    printRanges.push([mm.index, i]);
  }
  const inPrint = (idx) => printRanges.some(([a, b]) => idx >= a && idx < b);

  const re = /([^{}@;]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(clean)) !== null) {
    const selector = m[1].trim().replace(/\s+/g, ' ');
    if (!selector || selector.startsWith('@') || /^\d+%$/.test(selector) || selector === 'from' || selector === 'to') continue;
    if (inPrint(m.index)) continue;
    const line = clean.slice(0, m.index).split('\n').length;
    const decls = {};
    for (const d of m[2].split(';')) {
      const dm = /^\s*([a-z-]+)\s*:\s*([\s\S]+?)\s*$/i.exec(d);
      if (dm) decls[dm[1].toLowerCase()] = dm[2].trim();
    }
    /* A selector scoped to one theme is only ever painted in that theme —
       `[data-theme="night"] .btn-primary:hover` must not be scored against the
       dark palette, which is how it looked like a 3.90:1 failure. */
    const themeMatch = /\[data-theme=["']?([a-z]+)["']?\]/i.exec(selector);
    rules.push({ selector, decls, file: rel, line, onlyTheme: themeMatch ? themeMatch[1] : null });
  }

  // Base-selector colour lookup for pseudo-class-only overrides.
  const baseColor = new Map();
  for (const r of rules) {
    const base = r.selector.replace(/:(hover|focus|focus-visible|active|disabled|checked|first-child|last-child)\b(\([^)]*\))?/g, '').trim();
    for (const p of COLOR_PROPS) if (r.decls[p] && !baseColor.has(base)) baseColor.set(base, r.decls[p]);
  }

  const sites = [];
  for (const r of rules) {
    const bg = BG_PROPS.has('background') && r.decls.background != null ? r.decls.background : r.decls['background-color'];
    if (bg == null) continue;
    const base = r.selector.replace(/:(hover|focus|focus-visible|active|disabled|checked|first-child|last-child)\b(\([^)]*\))?/g, '').trim();
    const color = r.decls.color ?? baseColor.get(base);
    if (color == null) continue;
    sites.push({
      kind: 'css',
      where: `${r.file}:${r.line} ${r.selector}`,
      file: r.file,
      line: r.line,
      onlyTheme: r.onlyTheme,
      colors: [{ value: color, branch: null }],
      backgrounds: [{ value: bg, branch: null }],
      fontSize: r.decls['font-size'],
      fontWeight: r.decls['font-weight'],
      opacity: parseOpacity(r.decls.opacity),
      inheritedColor: r.decls.color == null,
    });
  }
  return sites;
}

/* ── TSX inline-style call sites ── */

/**
 * Pull every `style={{ … }}` object out of a .tsx file and read its
 * background/color pair. Ternaries contribute BOTH branches, because both
 * render — the enabled and the disabled face of the same button. That is
 * exactly the form the previous audit's scanner missed.
 */
export function scanTsx(file, root) {
  const src = readFileSync(file, 'utf8');
  const rel = relative(root, file);
  const sites = [];
  const marker = 'style={{';
  let idx = 0;
  while ((idx = src.indexOf(marker, idx)) !== -1) {
    const start = idx + marker.length - 1; // at the inner '{'
    let depth = 0, end = -1;
    for (let i = start; i < src.length; i++) {
      const ch = src[i];
      if (ch === '{') depth++;
      else if (ch === '}') { depth--; if (depth === 0) { end = i; break; } }
    }
    if (end === -1) break;
    const body = src.slice(start + 1, end);
    idx = end;

    const colors = variantsFor(body, 'color');
    const backgrounds = [
      ...variantsFor(body, 'background'),
      ...variantsFor(body, 'backgroundColor'),
    ];
    if (!colors.length || !backgrounds.length) continue;
    sites.push({
      kind: 'tsx',
      where: `${rel}:${src.slice(0, start).split('\n').length}`,
      file: rel,
      line: src.slice(0, start).split('\n').length,
      onlyTheme: null,
      colors,
      backgrounds,
      fontSize: variantsFor(body, 'fontSize')[0]?.value,
      fontWeight: variantsFor(body, 'fontWeight')[0]?.value ?? numericFor(body, 'fontWeight'),
      /* Element opacity is the mechanism behind the worst numbers in the app:
         it fades label and fill by the SAME factor, so contrast can only get
         worse, and it is invisible to any check that looks at colours alone.
         The lowest value any branch can take is the one that matters. */
      opacity: lowestOpacity(body),
    });
  }
  return sites;
}

/**
 * Values assigned to `prop`, tagged with which ternary branch they belong to.
 *
 * Orb writes stateful buttons as two parallel ternaries on ONE condition:
 *   background: ok ? 'var(--accent-deep)' : 'var(--bg-glass)',
 *   color:      ok ? 'var(--btn-primary-text)' : 'var(--text-muted)',
 * A naive cross-product measures white-on-glass and muted-on-clay — two faces
 * that never render together — and buries the one pair that does. Tagging each
 * literal with {condition, branch} lets the scorer pair like with like.
 */
function variantsFor(body, prop) {
  const re = new RegExp(`(?:^|[,{\\s])${prop}\\s*:\\s*`, 'g');
  const out = [];
  let m;
  while ((m = re.exec(body)) !== null) {
    const expr = valueExpr(body, m.index + m[0].length).trim();
    const t = splitTernary(expr);
    if (t) {
      for (const l of stringLiterals(t.whenTrue)) out.push({ value: l, cond: t.cond, branch: 'T' });
      for (const l of stringLiterals(t.whenFalse)) out.push({ value: l, cond: t.cond, branch: 'F' });
    } else {
      for (const l of stringLiterals(expr)) out.push({ value: l, cond: null, branch: null });
    }
  }
  return out;
}

/**
 * The expression assigned to a property: everything up to the comma or brace
 * that ends it at the top level, skipping over quotes and brackets.
 *
 * The regex this replaced allowed one level of parentheses, so
 * `'color-mix(in srgb, var(--status-ok) 10%, transparent)'` was cut at the
 * comma inside the mix, the string literal was never closed, and the site was
 * counted as unresolvable. Every tinted chip in the app went unmeasured.
 */
function valueExpr(body, from) {
  let depth = 0;
  let quote = null;
  for (let i = from; i < body.length; i++) {
    const ch = body[i];
    if (quote) {
      if (ch === '\\') i++;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') quote = ch;
    else if (ch === '(' || ch === '[' || ch === '{') depth++;
    else if (ch === ')' || ch === ']' || ch === '}') {
      if (depth === 0) return body.slice(from, i);
      depth--;
    } else if (ch === ',' && depth === 0) return body.slice(from, i);
  }
  return body.slice(from);
}

function stringLiterals(s) {
  return (s.match(/'([^']*)'|"([^"]*)"/g) || []).map((l) => l.slice(1, -1));
}

/** Split `cond ? a : b` at the top level. Returns null if it is not a ternary. */
function splitTernary(expr) {
  let depth = 0, qi = -1;
  for (let i = 0; i < expr.length; i++) {
    const ch = expr[i];
    if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth--;
    else if (ch === '?' && depth === 0) {
      if (expr[i + 1] === '.' || expr[i + 1] === '?') { i++; continue; } // ?. and ??
      qi = i;
      break;
    }
  }
  if (qi === -1) return null;
  depth = 0;
  let ci = -1, nested = 0;
  for (let i = qi + 1; i < expr.length; i++) {
    const ch = expr[i];
    if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth--;
    else if (ch === '?' && depth === 0 && expr[i + 1] !== '.' && expr[i + 1] !== '?') nested++;
    else if (ch === ':' && depth === 0) {
      if (nested > 0) { nested--; continue; }
      ci = i;
      break;
    }
  }
  if (ci === -1) return null;
  return {
    cond: expr.slice(0, qi).trim(),
    whenTrue: expr.slice(qi + 1, ci),
    whenFalse: expr.slice(ci + 1),
  };
}

function numericFor(body, prop) {
  const m = new RegExp(`(?:^|[,{\\s])${prop}\\s*:\\s*(\\d+)`).exec(body);
  return m ? m[1] : undefined;
}

function parseOpacity(raw) {
  if (raw == null) return 1;
  const n = parseFloat(String(raw));
  return Number.isFinite(n) && n > 0 && n < 1 ? n : 1;
}

/** Smallest numeric literal assigned to `opacity` anywhere in a style object. */
function lowestOpacity(body) {
  const m = /(?:^|[,{\s])opacity\s*:\s*([^,\n]*(?:\([^)]*\)[^,\n]*)*)/.exec(body);
  if (!m) return 1;
  const nums = (m[1].match(/\d*\.?\d+/g) || []).map(Number).filter((n) => n > 0 && n < 1);
  return nums.length ? Math.min(...nums) : 1;
}

/* ── scoring ── */

/** WCAG large text: ≥18.66px bold, or ≥24px. Anything else needs 4.5:1. */
export function thresholdFor(fontSize, fontWeight) {
  const px = toPx(fontSize);
  if (px == null) return 4.5;
  const bold = fontWeight != null && (parseInt(fontWeight, 10) >= 700 || /bold/i.test(String(fontWeight)));
  if (px >= 24) return 3.0;
  if (bold && px >= 18.66) return 3.0;
  return 4.5;
}

function toPx(v) {
  if (v == null) return null;
  const s = String(v).trim();
  let m = /^(\d+(?:\.\d+)?)px$/.exec(s);
  if (m) return parseFloat(m[1]);
  m = /^(\d+(?:\.\d+)?)rem$/.exec(s);
  if (m) return parseFloat(m[1]) * 16; // index.css pins html { font-size: 16px }
  m = /^(\d+(?:\.\d+)?)em$/.exec(s);
  if (m) return parseFloat(m[1]) * 16;
  return null;
}

/**
 * Measure one call site in one theme against every base surface it could land
 * on, and return the WORST ratio. A translucent fill (a 10% status wash) is
 * composited over the surface first; an opaque fill ignores the surface.
 */
export function measureSite(site, tokens, base, surfaces, themeName) {
  if (site.onlyTheme && site.onlyTheme !== themeName) return null;
  const need = thresholdFor(site.fontSize, site.fontWeight);
  let worst = null;
  for (const c of site.colors) {
    const cRaw = c.value;
    const fg = resolveValue(cRaw, tokens, base);
    if (!fg) continue;
    for (const b of site.backgrounds) {
      // Two ternaries on the SAME condition only ever render branch-with-branch.
      if (c.cond && b.cond && c.cond === b.cond && c.branch !== b.branch) continue;
      const bRaw = b.value;
      const bgc = resolveValue(bRaw, tokens, base);
      if (!bgc) continue;
      const alpha = site.opacity ?? 1;
      const opaque = (bgc[3] ?? 1) >= 1 && alpha >= 1;
      /* An opaque fill at full opacity needs no surface: it IS the backdrop.
         Anything translucent — by its own alpha or by element opacity — is
         scored against every base surface at its worst. */
      const bases = opaque ? [over(bgc, [0, 0, 0])] : surfaces;
      for (const surface of bases) {
        let bg, fgOn;
        if (opaque) {
          bg = surface;
          fgOn = over(fg, bg);
        } else {
          const fillOnSurface = over(bgc, surface);
          const labelOnFill = over(fg, fillOnSurface);
          // Element opacity composites the whole rendered element — fill AND
          // label together — over the surface. Both fade by the same factor.
          bg = over([...fillOnSurface, alpha], surface);
          fgOn = over([...labelOnFill, alpha], surface);
        }
        const ratio = contrast(fgOn, bg);
        if (worst == null || ratio < worst.ratio) {
          worst = { ratio, need, fg: cRaw, bg: bRaw, opacity: alpha };
        }
      }
    }
  }
  return worst;
}

/* ── HOVER / FOCUS SURFACES — text a state fill lands UNDER ──────────────────
 *
 * WHY A THIRD PASS EXISTS
 * The palette pass composites --bg-glass over each base surface, and the
 * call-site pass above pairs a `color` and a `background` declared on the same
 * selector. Neither can see the commonest interactive pattern in this codebase:
 *
 *     .task-row        { background: var(--bg-surface); }
 *     .task-row:hover  { background: var(--bg-glass-hover); }   <- no colour
 *     .task-row__detail{ color: var(--text-muted); }            <- no background
 *
 * The hover rule declares no colour, so no pair is formed and the site is
 * skipped. The colour lives on a CHILD, so it is never measured against the
 * fill its parent just changed. Hovering any nurse task row dropped two lines
 * of --text-muted to 4.17:1 in dark, below AA, and the gate reported 378/378
 * green while it happened — for months.
 *
 * WHAT THIS SCANNER DOES
 * Finds rules that repaint the background for an interaction state
 * (:hover/:focus/:focus-visible/:focus-within/:active) with a TRANSLUCENT fill,
 * and collects the text that renders inside them: the element's own colour, plus
 * descendants and modifiers written in this file's BEM vocabulary.
 *
 * WHAT IT DELIBERATELY EXCLUDES, so it reports defects and not noise:
 *   - a candidate that paints its own background (or whose ancestor segment
 *     does) — that text sits on ITS fill, not on the hovered one;
 *   - a candidate whose selector carries an interaction state the hover rule
 *     does not (a :disabled colour never renders under a :hover fill);
 *   - the hovered element's own colour when the state rule restates it — that
 *     pair is already measured by the call-site pass.
 *
 * Backdrop: the element's own opaque background where it declares one (a
 * .task-row is a --bg-surface card), otherwise every base surface at its worst,
 * which is the same conservative model the call-site pass uses.
 */

const INTERACTION_STATES = new Set([':hover', ':focus', ':focus-visible', ':focus-within', ':active']);
const ANY_PSEUDO = /:{1,2}[a-z-]+(\([^)]*\))?/g;

function pseudosOf(selector) {
  return (selector.match(ANY_PSEUDO) || []).map((s) => s.replace(/\(.*\)/, ''));
}

function withoutPseudos(selector) {
  return selector.replace(ANY_PSEUDO, '').trim();
}

/** Rules of one stylesheet, comments blanked, line numbers preserved. */
function parseRules(file, root) {
  const src = readFileSync(file, 'utf8');
  const clean = src.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '));
  const rel = relative(root, file);
  const rules = [];
  const re = /([^{}@;]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(clean)) !== null) {
    const selector = m[1].trim().replace(/\s+/g, ' ');
    if (!selector || selector.startsWith('@') || /^\d+%$/.test(selector) || selector === 'from' || selector === 'to') continue;
    const decls = {};
    for (const d of m[2].split(';')) {
      const dm = /^\s*([a-z-]+)\s*:\s*([\s\S]+?)\s*$/i.exec(d);
      if (dm) decls[dm[1].toLowerCase()] = dm[2].trim();
    }
    const themeMatch = /\[data-theme=["']?([a-z]+)["']?\]/i.exec(selector);
    rules.push({
      selector,
      decls,
      file: rel,
      line: clean.slice(0, m.index).split('\n').length,
      onlyTheme: themeMatch ? themeMatch[1] : null,
    });
  }
  return rules;
}

const backgroundOf = (rule) => rule.decls.background ?? rule.decls['background-color'];

export function scanHoverSurfaces(file, root) {
  const rules = parseRules(file, root);

  /** Every selector that declares a background, for the ancestor test below. */
  const paintsOwnFill = new Set();
  for (const r of rules) {
    if (backgroundOf(r) == null) continue;
    for (const part of r.selector.split(',')) paintsOwnFill.add(withoutPseudos(part.trim()));
  }
  /** True when `seg` itself, or any BEM modifier of it, paints a background.
   *  `.task-row__kind` carries no fill of its own, but `.task-row__kind--med`
   *  does, and the tag never renders without one of its modifiers — so its
   *  --status-on-fill label sits on the tag's own colour, not on the row's
   *  hover fill. Missing that is how a scanner produces 1.5:1 nonsense. */
  const paintsFillFor = (seg) => {
    if (paintsOwnFill.has(seg)) return true;
    for (const painted of paintsOwnFill) {
      if (painted.startsWith(seg + '--') || painted.startsWith(seg + '.')) return true;
    }
    return false;
  };
  /** A selector is "on its own fill" if it, a modifier of it, or any ancestor
   *  segment (or a modifier of one) declares a background. */
  const onOwnFill = (selector) => {
    const segments = selector.split(' ');
    for (let i = 0; i < segments.length; i++) {
      if (paintsFillFor(segments.slice(0, i + 1).join(' '))) return true;
      if (paintsFillFor(segments[i])) return true;
    }
    return false;
  };

  /** font-size / font-weight declared for a selector, or for its BEM base. */
  const typeBySelector = new Map();
  for (const r of rules) {
    if (!r.decls['font-size'] && !r.decls['font-weight']) continue;
    for (const part of r.selector.split(',')) {
      const key = withoutPseudos(part.trim());
      const prev = typeBySelector.get(key) ?? {};
      typeBySelector.set(key, {
        fontSize: prev.fontSize ?? r.decls['font-size'],
        fontWeight: prev.fontWeight ?? r.decls['font-weight'],
      });
    }
  }
  const typographyFor = (selector) => {
    const own = typeBySelector.get(selector) ?? {};
    const last = selector.split(' ').pop() ?? selector;
    const bemBase = last.replace(/--[a-z0-9-]+$/i, '');
    const inherited = typeBySelector.get(bemBase) ?? typeBySelector.get(last) ?? {};
    return {
      fontSize: own.fontSize ?? inherited.fontSize,
      fontWeight: own.fontWeight ?? inherited.fontWeight,
    };
  };

  const sites = [];
  for (const rule of rules) {
    const fill = backgroundOf(rule);
    if (fill == null) continue;
    for (const rawPart of rule.selector.split(',')) {
      const part = rawPart.trim();
      if (!part) continue;
      const states = pseudosOf(part).filter((p) => INTERACTION_STATES.has(p));
      if (!states.length) continue;
      const base = withoutPseudos(part);
      if (!base) continue;
      const allowed = new Set(pseudosOf(part));

      const baseRule = rules.find((r) => r.selector === base);
      const baseBackground = baseRule ? backgroundOf(baseRule) : undefined;

      const texts = [];
      for (const candidate of rules) {
        if (!candidate.decls.color) continue;
        if (backgroundOf(candidate) != null) continue;
        for (const rawSel of candidate.selector.split(',')) {
          const sel = rawSel.trim();
          if (!sel) continue;
          if (pseudosOf(sel).some((p) => !allowed.has(p))) continue;
          const bare = withoutPseudos(sel);
          const isSelf = bare === base;
          const isInside =
            bare.startsWith(base + '__') || bare.startsWith(base + '--') ||
            bare.startsWith(base + ' ') || bare.startsWith(base + '.');
          if (!isSelf && !isInside) continue;
          // The element's own colour restated on the state rule is already a
          // measured call-site pair; only descendants add new information.
          if (isSelf && rule.decls.color) continue;
          if (!isSelf && onOwnFill(bare)) continue;
          /* Type comes from the BEM base when the modifier only recolours.
             `.pulse-aqi__value--orange` declares nothing but a colour; its
             1.5rem/700 lives on `.pulse-aqi__value`, and reading 4.5:1 instead
             of the 3:1 WCAG allows for ≥24px is how a scanner invents a
             failure in a number you can read from across the room. */
          const type = typographyFor(bare);
          texts.push({
            selector: sel,
            color: candidate.decls.color,
            fontSize: candidate.decls['font-size'] ?? type.fontSize,
            fontWeight: candidate.decls['font-weight'] ?? type.fontWeight,
            line: candidate.line,
          });
        }
      }
      if (!texts.length) continue;
      sites.push({
        where: `${rule.file}:${rule.line} ${part}`,
        file: rule.file,
        line: rule.line,
        onlyTheme: rule.onlyTheme,
        fill,
        baseBackground,
        texts,
      });
    }
  }
  return sites;
}

/**
 * Worst measured pair for one hover surface in one theme, or null when nothing
 * about it could be resolved with certainty.
 */
export function measureHoverSurface(site, tokens, base, surfaces, themeName) {
  if (site.onlyTheme && site.onlyTheme !== themeName) return null;
  const fill = resolveValue(site.fill, tokens, base);
  if (!fill) return null;

  let backdrops = surfaces;
  if (site.baseBackground) {
    const own = resolveValue(site.baseBackground, tokens, base);
    // An opaque base background IS the backdrop; a translucent one still needs
    // the surface list underneath it.
    if (own && (own[3] ?? 1) >= 1) backdrops = [over(own, surfaces[0])];
  }

  let worst = null;
  for (const text of site.texts) {
    const fg = resolveValue(text.color, tokens, base);
    if (!fg) continue;
    const need = thresholdFor(text.fontSize, text.fontWeight);
    for (const backdrop of backdrops) {
      const painted = over(fill, backdrop);
      const ratio = contrast(over(fg, painted), painted);
      if (worst == null || ratio - need < worst.ratio - worst.need) {
        worst = { ratio, need, fg: text.color, bg: site.fill, text: text.selector };
      }
    }
  }
  return worst;
}
