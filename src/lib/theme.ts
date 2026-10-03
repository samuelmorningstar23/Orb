import { useEffect, useState } from 'react'

/* Theme cycle: dark → light → night (high-contrast OR / night-shift) → dark.
   Lifted out of ClinicianShell so the pre-login screens can carry the SAME
   switch. Duplicating it would have meant duplicating the transition-suppression
   dance below, and a second copy is exactly how one of them drifts. */

export type Theme = 'dark' | 'light' | 'night'

export const THEME_ORDER: Theme[] = ['dark', 'light', 'night']

/** The theme this click switches TO — used for the control's label. */
export const nextTheme = (theme: Theme): Theme =>
  THEME_ORDER[(THEME_ORDER.indexOf(theme) + 1) % THEME_ORDER.length]

/** The user's explicitly chosen theme, or null if they never chose one.
 *
 *  Canonical storage key is 'orb-theme'; 'orb_theme' is read as a legacy
 *  fallback so an existing user's choice migrates instead of resetting.
 *
 *  ONE reader for both keys is the point. App.tsx used to boot from 'orb_theme'
 *  alone while the shell wrote 'orb-theme' and then REMOVED 'orb_theme' — so
 *  after the first toggle the boot path could never see a saved choice again.
 *  Every cold load fell back to the OS preference and the system-theme listener
 *  stayed live over an explicit choice, which is why a clinician who picked
 *  Night Shift got dark (or light) back on every reload. */
export function storedTheme(): Theme | null {
  try {
    const raw = (localStorage.getItem('orb-theme') || localStorage.getItem('orb_theme')) as Theme | null
    return raw && THEME_ORDER.includes(raw) ? raw : null
  } catch {
    return null // Safari private mode / storage disabled — fall back to system.
  }
}

/** The theme to boot with before React mounts: an explicit choice, else the OS
 *  preference. Applied at module scope in App.tsx so the first paint is already
 *  correct rather than flashing the wrong palette. */
export function bootTheme(): Theme {
  const chosen = storedTheme()
  if (chosen) return chosen
  const prefersLight = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-color-scheme: light)').matches
  return prefersLight ? 'light' : 'dark'
}

/** Falling back to the CURRENT data-theme (not 'dark') preserves the
 *  system-preference match App.tsx applied before anything mounted. */
export function readStoredTheme(): Theme {
  const chosen = storedTheme()
  if (chosen) return chosen
  const attr = document.documentElement.getAttribute('data-theme') as Theme | null
  return (attr && THEME_ORDER.includes(attr)) ? attr : 'dark'
}

/**
 * A THEME FLIP MUST REPAINT, NOT ANIMATE.
 *
 * Every element whose `transition` list includes `background` kept the PREVIOUS
 * theme's background for good when the theme changed — the transition is driven
 * by a custom property that changes underneath it, and the computed value stayed
 * pinned at the old one instead of landing on the new. Measured live: flipping
 * light → night left 12 of 34 painted elements frozen, among them
 * `.shell__nav-item` (the whole left navigation), `.shell__admit-patient` (the
 * primary CTA), `.clock-widget` and `.pulse-trigger` in the header. The result is
 * a half-converted UI: a true-black page still wearing an ivory nav rail and a
 * white clock chip.
 *
 * It never showed up in the theme sweeps because those set localStorage and
 * RELOAD, and a fresh load has no old value to be stuck on. It only appears when
 * someone presses the toggle — which is exactly what happens when an investor
 * asks to see Night Shift mode.
 *
 * Suppressing transitions across the swap fixes it: with nothing to interpolate,
 * every element takes the new value directly. All four steps happen in ONE
 * synchronous task, and the forced reflow in the middle is the load-bearing
 * line. It flushes style recalculation while transitions are still suppressed,
 * so the new theme's values are committed outright instead of becoming the end
 * point of an interpolation that never runs.
 *
 * It has to be synchronous. The first attempt released the class on
 * requestAnimationFrame, which does not fire at all while the document is hidden
 * — verified in the browser: visibilityState 'hidden', no rAF callback in 500ms
 * — so the kill-switch stuck on and killed every transition in the product. A
 * setTimeout backstop released the class but did not fix the colours, because
 * without a forced flush a hidden document simply coalesces the whole sequence
 * and never observes the transitions-off state. Reading offsetHeight makes the
 * browser observe it whether or not it intends to paint.
 */
export function applyTheme(theme: Theme) {
  const root = document.documentElement
  root.classList.add('orb-theme-switching')
  root.setAttribute('data-theme', theme)
  void root.offsetHeight // force style flush with transitions suppressed
  root.classList.remove('orb-theme-switching')

  localStorage.setItem('orb-theme', theme)
  localStorage.removeItem('orb_theme')
  // The Aurora canvas (and anything else painting outside CSS) listens for this
  // — without it the WebGL background stays in the previous theme.
  window.dispatchEvent(new CustomEvent('theme-changed'))
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(readStoredTheme)

  useEffect(() => { applyTheme(theme) }, [theme])

  // Another surface on this page (or a second tab's storage write) can move the
  // theme; follow it so the toggle's icon never lies about the current state.
  useEffect(() => {
    const sync = () => setTheme(readStoredTheme())
    window.addEventListener('theme-changed', sync)
    return () => window.removeEventListener('theme-changed', sync)
  }, [])

  const toggleTheme = () => setTheme(nextTheme)

  return { theme, setTheme, toggleTheme }
}
