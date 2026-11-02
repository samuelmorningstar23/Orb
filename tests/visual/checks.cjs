/**
 * Orb visual-audit checks — runs INSIDE the page via page.evaluate(runAudit).
 *
 * Checks implemented (see FEATURES_AND_CHECKS.md):
 *   1. contrast          — text under 3:1 against its alpha-composited background
 *   2. invisible-text    — text under 1.6:1 (effectively invisible)
 *   3. aa-contrast       — text under WCAG AA (4.5:1 normal / 3:1 large) — informational, non-gating
 *   4. icon-contrast     — SVG icon fill/stroke under 3:1
 *   5. invisible-control — focusable control with ~zero size or ~zero opacity
 *   6. small-target      — interactive target under 24×24 px (WCAG 2.2) — informational, non-gating
 *   7. horizontal-overflow — content wider than the viewport (html clips it, so it's lost)
 *   8. clipped-text      — nowrap text truncated without text-overflow: ellipsis
 *
 * Compositing model: text color (× accumulated ancestor opacity) is composited
 * over the ancestor background-color stack down to the first opaque layer (or
 * the <html> background). Gradients (computed linear/radial background-image)
 * are handled by extracting their color stops and taking the WORST contrast
 * across stops — so gradient-filled gold buttons compute correctly instead of
 * producing false positives. Backgrounds we cannot model (url() images, canvas
 * behind a fully transparent chain) mark the finding `unverifiable: true`,
 * which the gate treats as visual-verify-only (non-gating).
 */
'use strict';

function runAudit() {
  const findings = [];
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  // ── color helpers ──────────────────────────────────────────────
  function parseColor(str) {
    if (!str) return null;
    str = str.trim();
    if (str === 'transparent') return [0, 0, 0, 0];
    let m = str.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
    if (m) return [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]];
    // modern space syntax: rgb(12 34 56 / 0.5)
    m = str.match(/^rgba?\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(?:\/\s*([\d.%]+)\s*)?\)$/);
    if (m) {
      let a = m[4] === undefined ? 1 : (m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]);
      return [+m[1], +m[2], +m[3], a];
    }
    m = str.match(/^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(?:\/\s*([\d.]+))?\)$/);
    if (m) return [+m[1] * 255, +m[2] * 255, +m[3] * 255, m[4] === undefined ? 1 : +m[4]];
    return null;
  }

  function over(fg, bg) {
    // standard source-over compositing; both [r,g,b,a]; returns [r,g,b,a]
    const a = fg[3] + bg[3] * (1 - fg[3]);
    if (a === 0) return [0, 0, 0, 0];
    return [
      (fg[0] * fg[3] + bg[0] * bg[3] * (1 - fg[3])) / a,
      (fg[1] * fg[3] + bg[1] * bg[3] * (1 - fg[3])) / a,
      (fg[2] * fg[3] + bg[2] * bg[3] * (1 - fg[3])) / a,
      a,
    ];
  }

  function relLum(c) {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  }

  function contrast(c1, c2) {
    const l1 = relLum(c1);
    const l2 = relLum(c2);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }

  function fmt(c) {
    return `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},${(+c[3].toFixed(2))})`;
  }

  // Extract color stops from a computed gradient string. Chromium computes
  // stop colors to rgb()/rgba(); good enough to bound the worst case.
  function gradientStops(bgImage) {
    if (!bgImage || bgImage === 'none') return null;
    if (/url\(/.test(bgImage)) return 'unverifiable';
    const stops = [];
    const re = /rgba?\([^)]*\)/g;
    let m;
    while ((m = re.exec(bgImage))) {
      const c = parseColor(m[0]);
      if (c) stops.push(c);
    }
    if (!stops.length) return 'unverifiable';
    return stops;
  }

  // ── visibility helpers ─────────────────────────────────────────
  const opacityCache = new Map();
  function accOpacity(el) {
    if (opacityCache.has(el)) return opacityCache.get(el);
    let o = 1;
    let n = el;
    while (n && n.nodeType === 1) {
      o *= parseFloat(getComputedStyle(n).opacity || '1');
      n = n.parentElement;
    }
    opacityCache.set(el, o);
    return o;
  }

  function isRendered(el) {
    // checkVisibility covers display:none SUBTREES too (a child of a
    // display:none ancestor keeps its own computed display, but has no boxes)
    if (typeof el.checkVisibility === 'function' &&
        !el.checkVisibility({ visibilityProperty: true, contentVisibilityAuto: true })) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    // fully offscreen (allow fullPage capture: only exclude far-left/-top sr-only style)
    if (r.right < -20 || r.bottom < -20) return false;
    return true;
  }

  function inHiddenSubtree(el) {
    if (typeof el.checkVisibility === 'function') {
      return !el.checkVisibility({ visibilityProperty: true, contentVisibilityAuto: true });
    }
    return el.getClientRects().length === 0;
  }

  // True when `el` is covered by an unrelated element at its center point — e.g.
  // page chrome sitting behind an open modal's backdrop. Such text is dimmed and
  // the user isn't reading it, so auditing its contrast produces false failures.
  function isOccluded(el) {
    const r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2;
    const cy = r.y + r.height / 2;
    if (cx < 1 || cy < 1 || cx > vw - 1 || cy > vh - 1) return false; // offscreen: don't guess
    let top;
    try { top = document.elementFromPoint(cx, cy); } catch { return false; }
    if (!top) return false;
    // Not occluded if the top hit is the element, its descendant, or its ancestor.
    if (top === el || el.contains(top) || top.contains(el)) return false;
    return true;
  }

  function isSrOnly(el) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return (
      (cs.clip && cs.clip !== 'auto' && r.width <= 1 && r.height <= 1) ||
      (cs.clipPath === 'inset(50%)') ||
      (r.width <= 1 && r.height <= 1 && cs.overflow === 'hidden' && cs.position === 'absolute')
    );
  }

  function selectorFor(el) {
    if (el.id) return `#${el.id}`;
    const parts = [];
    let n = el;
    let depth = 0;
    while (n && n.nodeType === 1 && depth < 4) {
      if (n.id) { parts.unshift(`#${n.id}`); break; }
      let part = n.tagName.toLowerCase();
      const cls = (typeof n.className === 'string' ? n.className : (n.className && n.className.baseVal) || '')
        .trim().split(/\s+/).filter(Boolean).slice(0, 2);
      if (cls.length) part += '.' + cls.join('.');
      parts.unshift(part);
      n = n.parentElement;
      depth++;
    }
    return parts.join(' > ');
  }

  // Elements painting beneath `el` at its center point, top-most first.
  // elementsFromPoint sees the real paint order — including positioned
  // SIBLING layers an ancestor walk misses (e.g. an icon whose colored
  // "background" is an absolutely-positioned sibling disc). Falls back to
  // null when the point is outside the viewport (fullPage content below the
  // fold) or hit-testing fails; caller then uses the ancestor chain.
  function paintStackBelow(el, includeSelf) {
    const r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2;
    const cy = r.y + r.height / 2;
    if (cx < 1 || cy < 1 || cx > vw - 1 || cy > vh - 1) return null;
    let stack;
    try { stack = document.elementsFromPoint(cx, cy); } catch { return null; }
    if (!stack || !stack.length) return null;
    let idx = stack.indexOf(el);
    // If the element itself is not in the hit-test stack (covered by a
    // floating layer, zero-sized at this instant, or hit-test quirks), do NOT
    // substitute a containing ancestor: slicing from the ancestor composites
    // an unrelated stack and silently drops every background BETWEEN the
    // element and that ancestor (e.g. the emergency row's solid danger fill),
    // producing false invisible-text findings. Returning null falls back to
    // the structural ancestor walk in effectiveBg, which keeps those layers.
    if (idx === -1) return null;
    return stack.slice(includeSelf ? idx : idx + 1);
  }

  // Effective background stack behind `el` (starting at el itself when
  // includeSelf, else from what paints beneath it).
  // Returns {candidates, unverifiable, viaGradient}.
  function effectiveBg(el, includeSelf) {
    const layers = []; // top-most first
    let unverifiable = false;
    let viaGradient = false;
    let reachedOpaque = false;

    const stack = paintStackBelow(el, includeSelf);
    let chain;
    if (stack) {
      chain = stack;
    } else {
      chain = [];
      let n = includeSelf ? el : el.parentElement;
      while (n && n.nodeType === 1) { chain.push(n); n = n.parentElement; }
    }
    for (const n of chain) {
      const cs = getComputedStyle(n);
      const stops = gradientStops(cs.backgroundImage);
      const bc = parseColor(cs.backgroundColor) || [0, 0, 0, 0];
      const o = parseFloat(cs.opacity || '1'); // element opacity applies to its bg
      if (stops === 'unverifiable') unverifiable = true;
      else if (stops) {
        viaGradient = true;
        layers.push({ kind: 'gradient', stops, opacity: o });
      }
      if (bc[3] > 0) layers.push({ kind: 'solid', color: bc, opacity: o });
      // Raster content beneath the text can't be modelled (photos, QR codes).
      // Canvases are excluded: the app's canvases are decorative fixed
      // backgrounds with pointer-events:none (invisible to elementsFromPoint)
      // whose dim wash barely shifts the base color.
      if (n.tagName === 'IMG' || n.tagName === 'VIDEO') unverifiable = true;
      if (bc[3] >= 0.999 && o >= 0.999) { reachedOpaque = true; break; }
    }
    if (!reachedOpaque) {
      // base canvas: html/body background or theme background
      const htmlBg = parseColor(getComputedStyle(document.documentElement).backgroundColor);
      const bodyBg = parseColor(getComputedStyle(document.body).backgroundColor);
      let base = bodyBg && bodyBg[3] > 0 ? bodyBg : htmlBg;
      if (!base || base[3] < 0.999) {
        // resolve --bg-primary as the app's known base
        const v = getComputedStyle(document.documentElement).getPropertyValue('--bg-primary').trim();
        base = parseColor(v) || [8, 9, 13, 1];
        base[3] = 1;
      }
      layers.push({ kind: 'solid', color: [base[0], base[1], base[2], 1], opacity: 1 });
    }
    // composite bottom-up; for gradients produce candidate set (worst case kept later)
    // We track a set of candidate composited backgrounds (one per gradient-stop path, capped)
    let candidates = [[0, 0, 0, 0]];
    for (let i = layers.length - 1; i >= 0; i--) {
      const L = layers[i];
      if (L.kind === 'solid') {
        const c = [L.color[0], L.color[1], L.color[2], L.color[3] * L.opacity];
        candidates = candidates.map((base) => over(c, base));
      } else {
        const next = [];
        for (const base of candidates) {
          for (const s of L.stops.slice(0, 4)) {
            next.push(over([s[0], s[1], s[2], s[3] * L.opacity], base));
            if (next.length >= 8) break;
          }
          if (next.length >= 8) break;
        }
        candidates = next.length ? next : candidates;
      }
    }
    return { candidates, unverifiable, viaGradient };
  }

  function worstContrast(fg, bgResult) {
    let worst = Infinity;
    let worstBg = null;
    for (const bg of bgResult.candidates) {
      const solidBg = [bg[0], bg[1], bg[2], 1];
      const composited = over(fg, solidBg);
      const r = contrast([composited[0], composited[1], composited[2]], solidBg);
      if (r < worst) { worst = r; worstBg = solidBg; }
    }
    return { ratio: worst, bg: worstBg };
  }

  function push(f) {
    findings.push(f);
  }

  // Skip audit content inside containers marked as intentionally non-themed
  function inPrintLayout(el) {
    return !!el.closest('.discharge-print-layout, .handoff-print-layout');
  }

  const seen = new Set();
  function dedupeKey(type, sel, snippet) {
    return `${type}|${sel}|${snippet}`;
  }

  // ── 1-3. text contrast ─────────────────────────────────────────
  const all = document.querySelectorAll('body *');
  const MAX_ELS = 20000;
  let count = 0;
  for (const el of all) {
    if (++count > MAX_ELS) break;
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') continue;
    // direct text?
    let text = '';
    for (const child of el.childNodes) {
      if (child.nodeType === 3) text += child.nodeValue;
    }
    text = text.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    // Emoji render as full-color glyphs — text-color contrast is meaningless
    if (/^[\p{Extended_Pictographic}\p{Emoji_Presentation}️‍\s\d#*]+$/u.test(text) &&
        /[\p{Extended_Pictographic}\p{Emoji_Presentation}]/u.test(text)) continue;
    if (!isRendered(el) || isSrOnly(el)) continue;
    if (isOccluded(el)) continue; // covered by a modal/overlay — user isn't reading it
    const op = accOpacity(el);
    if (op < 0.05) continue; // handled by invisible-control if focusable
    const cs = getComputedStyle(el);

    let fg = parseColor(cs.webkitTextFillColor && cs.webkitTextFillColor !== '' ? cs.webkitTextFillColor : cs.color);
    if (!fg) continue;
    let gradientText = false;
    if (fg[3] === 0) {
      // transparent fill — gradient text via background-clip:text?
      if ((cs.webkitBackgroundClip === 'text' || cs.backgroundClip === 'text') && cs.backgroundImage !== 'none') {
        const stops = gradientStops(cs.backgroundImage);
        if (Array.isArray(stops)) {
          gradientText = true;
          fg = stops[0]; // evaluate each stop below
          var fgStops = stops;
        }
      }
      if (!gradientText) {
        const sel = selectorFor(el);
        const key = dedupeKey('invisible-text', sel, text.slice(0, 40));
        if (!seen.has(key)) {
          seen.add(key);
          push({ type: 'invisible-text', severity: 'critical', selector: sel, snippet: text.slice(0, 60),
                 detail: 'text has fully transparent color', ratio: 1 });
        }
        continue;
      }
    }
    fg = [fg[0], fg[1], fg[2], fg[3] * op];

    // Gradient text (background-clip:text) paints its own background-image AS
    // the glyphs — so the backdrop starts at the parent, not the element.
    const bgSelf = gradientText ? effectiveBg(el, false) : effectiveBg(el, true);
    const stopsToTest = gradientText ? fgStops : [fg];
    let worst = { ratio: Infinity, bg: null };
    for (const s of stopsToTest) {
      const sfg = [s[0], s[1], s[2], (s[3] !== undefined ? s[3] : 1) * op];
      const w = worstContrast(sfg, bgSelf);
      if (w.ratio < worst.ratio) worst = { ratio: w.ratio, bg: w.bg, fg: sfg };
    }
    const fs = parseFloat(cs.fontSize);
    const fwRaw = cs.fontWeight;
    const fw = fwRaw === 'bold' ? 700 : parseInt(fwRaw, 10) || 400;
    const large = fs >= 24 || (fs >= 18.66 && fw >= 700);
    const aaNeed = large ? 3 : 4.5;
    const r = worst.ratio;
    const rect = el.getBoundingClientRect();
    const sel = selectorFor(el);
    const base = {
      selector: sel,
      snippet: text.slice(0, 60),
      ratio: +r.toFixed(2),
      fg: fmt(worst.fg || fg),
      bg: worst.bg ? fmt(worst.bg) : null,
      fontSize: fs,
      fontWeight: fw,
      unverifiable: bgSelf.unverifiable || undefined,
      viaGradient: bgSelf.viaGradient || undefined,
      disabled: (el.closest('[disabled], [aria-disabled="true"]') != null) || undefined,
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    };
    if (inPrintLayout(el)) base.printLayout = true;
    if (r < 1.6) {
      const key = dedupeKey('invisible-text', sel, base.snippet);
      if (!seen.has(key)) { seen.add(key); push({ type: 'invisible-text', severity: 'critical', detail: `contrast ${base.ratio}:1 — effectively invisible`, ...base }); }
    } else if (r < 3) {
      const key = dedupeKey('contrast', sel, base.snippet);
      if (!seen.has(key)) { seen.add(key); push({ type: 'contrast', severity: 'major', detail: `contrast ${base.ratio}:1 (needs ≥3:1)`, ...base }); }
    } else if (r < aaNeed) {
      const key = dedupeKey('aa-contrast', sel, base.snippet);
      if (!seen.has(key)) { seen.add(key); push({ type: 'aa-contrast', severity: 'info', detail: `contrast ${base.ratio}:1 (AA needs ≥${aaNeed}:1)`, ...base }); }
    }
  }

  // ── 4. icon contrast (SVG) ─────────────────────────────────────
  const svgs = document.querySelectorAll('svg');
  for (const svg of svgs) {
    if (!isRendered(svg)) continue;
    if (isOccluded(svg)) continue;
    const op = accOpacity(svg);
    if (op < 0.05) continue;
    if (inPrintLayout(svg)) continue;
    const rect = svg.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) continue;
    const cs = getComputedStyle(svg);
    // Most Orb icons use stroke/fill = currentColor
    const candidates = [];
    const cFill = cs.fill && cs.fill !== 'none' ? parseColor(cs.fill) : null;
    const cStroke = cs.stroke && cs.stroke !== 'none' ? parseColor(cs.stroke) : null;
    if (cFill && cFill[3] > 0) candidates.push(cFill);
    if (cStroke && cStroke[3] > 0) candidates.push(cStroke);
    if (!candidates.length) {
      const col = parseColor(cs.color);
      if (col && col[3] > 0) candidates.push(col);
    }
    if (!candidates.length) continue;
    const bgRes = effectiveBg(svg, false);
    let worst = { ratio: Infinity, bg: null, fg: null };
    for (const c of candidates) {
      const w = worstContrast([c[0], c[1], c[2], c[3] * op], bgRes);
      if (w.ratio < worst.ratio) worst = { ratio: w.ratio, bg: w.bg, fg: c };
    }
    if (worst.ratio < 3) {
      // Icons that convey an affordance gate; decorative/chart SVGs (gauge
      // tracks, sparkline rails) are informational — reviewed visually.
      const interactive = svg.closest('button, a[href], [role="button"], [role="tab"], [role="menuitem"]');
      const sel = selectorFor(interactive || svg);
      const type = interactive ? 'icon-contrast' : 'chart-contrast';
      const key = dedupeKey(type, sel, '');
      if (!seen.has(key)) {
        seen.add(key);
        push({
          type, severity: interactive ? (worst.ratio < 1.6 ? 'major' : 'minor') : 'info',
          selector: sel,
          snippet: (interactive && (interactive.getAttribute('aria-label') || interactive.title)) || 'svg icon',
          disabled: (svg.closest('[disabled], [aria-disabled="true"]') != null) || undefined,
          ratio: +worst.ratio.toFixed(2), fg: fmt(worst.fg), bg: worst.bg ? fmt(worst.bg) : null,
          unverifiable: bgRes.unverifiable || undefined,
          detail: `icon contrast ${worst.ratio.toFixed(2)}:1 (needs ≥3:1)`,
          rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
        });
      }
    }
  }

  // ── 5-6. invisible / tiny interactive controls ─────────────────
  const controls = document.querySelectorAll(
    'button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=tab], [role=menuitem], [role=checkbox], [role=switch], [tabindex]'
  );
  for (const el of controls) {
    if (el.tabIndex < 0 && !el.matches('button, a[href], input, select, textarea')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue; // not focusable → fine
    if (inHiddenSubtree(el)) continue; // display:none ancestor (hidden tab panel) → not focusable
    if (isSrOnly(el)) continue;
    const r = el.getBoundingClientRect();
    const op = accOpacity(el);
    const sel = selectorFor(el);
    const label = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('title') || '').trim().slice(0, 40);
    if (op <= 0.05 || r.width < 2 || r.height < 2) {
      // focusable but effectively invisible
      const key = dedupeKey('invisible-control', sel, label);
      if (!seen.has(key)) {
        seen.add(key);
        push({
          type: 'invisible-control', severity: 'major', selector: sel, snippet: label,
          detail: `focusable control is ${op <= 0.05 ? `opacity ${op.toFixed(2)}` : `${Math.round(r.width)}×${Math.round(r.height)}px`}`,
          rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
        });
      }
    } else if ((r.width < 24 || r.height < 24) && el.matches('button, a[href], [role=button], [role=tab]')) {
      const key = dedupeKey('small-target', sel, label);
      if (!seen.has(key)) {
        seen.add(key);
        push({
          type: 'small-target', severity: 'info', selector: sel, snippet: label,
          detail: `target ${Math.round(r.width)}×${Math.round(r.height)}px (< 24px)`,
          rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
        });
      }
    }
  }

  // ── 7. horizontal overflow ─────────────────────────────────────
  const docW = document.documentElement.scrollWidth;
  if (docW > vw + 1) {
    const offenders = [];
    for (const el of all) {
      if (offenders.length >= 10) break;
      if (!isRendered(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.right > vw + 8 && r.width < docW * 1.5) {
        // skip if inside a scrollable ancestor
        let n = el.parentElement;
        let scrollable = false;
        while (n) {
          const c = getComputedStyle(n);
          if (/(auto|scroll)/.test(c.overflowX)) { scrollable = true; break; }
          n = n.parentElement;
        }
        if (!scrollable) offenders.push({ selector: selectorFor(el), right: Math.round(r.right) });
      }
    }
    push({
      type: 'horizontal-overflow', severity: 'major', selector: 'html',
      snippet: '', detail: `document scrollWidth ${docW}px > viewport ${vw}px`,
      offenders,
    });
  }

  // ── 8. clipped text ────────────────────────────────────────────
  count = 0;
  for (const el of all) {
    if (++count > MAX_ELS) break;
    if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
    let text = '';
    for (const child of el.childNodes) if (child.nodeType === 3) text += child.nodeValue;
    text = text.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    if (!isRendered(el) || isSrOnly(el)) continue;
    const cs = getComputedStyle(el);
    const hidX = /(hidden|clip)/.test(cs.overflowX) || /(hidden|clip)/.test(cs.overflow);
    if (!hidX) continue;
    if (cs.textOverflow === 'ellipsis') continue;
    if (cs.webkitLineClamp && cs.webkitLineClamp !== 'none') continue;
    if (cs.whiteSpace.includes('nowrap') || cs.whiteSpace === 'pre') {
      if (el.scrollWidth > el.clientWidth + 3) {
        const sel = selectorFor(el);
        const key = dedupeKey('clipped-text', sel, text.slice(0, 40));
        if (!seen.has(key)) {
          seen.add(key);
          const r = el.getBoundingClientRect();
          push({
            type: 'clipped-text', severity: 'minor', selector: sel, snippet: text.slice(0, 60),
            detail: `nowrap text clipped without ellipsis (${el.scrollWidth}px in ${el.clientWidth}px)`,
            rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
          });
        }
      }
    }
  }

  return findings;
}

module.exports = { runAudit };
