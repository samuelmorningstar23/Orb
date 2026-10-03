/** Orb keyboard layer — the pure half.
 *
 * Everything here is a function of its arguments so the rules can be tested
 * without a DOM, a shell, or a router. The listener that uses them lives in
 * components/KeyboardShortcuts.tsx.
 *
 * ── WHY CHORDS AND NOT MODIFIERS ─────────────────────────────────────────────
 * Orb runs in a browser on a ward workstation, and almost every Ctrl/Cmd/Alt
 * combination is already spoken for by the browser or the OS — Cmd-P is print
 * (which this product genuinely uses), Cmd-R reloads, Alt-letter opens a menu on
 * Windows. Stealing them either fails silently or breaks something a clinician
 * relies on. A two-key CHORD (`g` then a letter, the Gmail idiom) collides with
 * nothing, needs no modifier, and is faster to type one-handed while the other
 * hand is on a patient's chart.
 *
 * The unmodified single key `?` opens the reference. That one is safe precisely
 * because the guards below refuse to fire while anything is being typed into.
 */

/** The surfaces worth a shortcut: the ones a clinician opens fifty times a
 *  shift. Deliberately NOT every tab — a cheat-sheet nobody can memorise is a
 *  cheat-sheet nobody uses, and the command palette already reaches everything
 *  else. Ordered as they should read in the help sheet. */
export const NAV_SHORTCUTS: ReadonlyArray<{ key: string; tab: string; label: string }> = [
  { key: 'p', tab: 'vigil', label: 'Patients' },
  { key: 'r', tab: 'round', label: 'Ward Round' },
  { key: 'o', tab: 'orders', label: 'Orders' },
  { key: 'm', tab: 'relay', label: 'Messages' },
  { key: 's', tab: 'sage', label: 'Sage' },
  { key: 't', tab: 'shift', label: 'My Shift' },
  { key: 'h', tab: 'helix', label: 'Pharmacy' },
]

/** The chord prefix. One letter, lowercase, matched case-insensitively. */
export const CHORD_PREFIX = 'g'

/** How long the prefix stays armed. Long enough for a deliberate two-key press,
 *  short enough that a stray `g` cannot swallow the next letter someone types
 *  minutes later. */
export const CHORD_TIMEOUT_MS = 1500

/**
 * Is this event target something the user is typing into?
 *
 * The single most important guard in the file. Without it, typing a patient
 * name containing "g" then "p" into the search box navigates away mid-sentence,
 * and pressing `?` in a clinical note opens a dialog instead of a question mark.
 * `contentEditable` is included because the Relay composer and the Scribe editor
 * are not <input> elements.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || typeof el !== 'object' || !('tagName' in el)) return false
  const tag = String(el.tagName || '').toLowerCase()
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true
  if (el.isContentEditable) return true
  // A custom widget can declare itself a text surface without being one of the
  // tags above (combobox, searchbox, the command palette's own listbox owner).
  const role = el.getAttribute?.('role')
  if (role === 'textbox' || role === 'searchbox' || role === 'combobox') return true
  return false
}

/**
 * Is a modal dialog currently on screen?
 *
 * Navigation must not fire underneath one. Orb's modals are genuinely modal —
 * the discharge confirmation, the administer dialog, the safety interlock — and
 * a chord that switched tabs behind an open discharge dialog would leave the
 * clinician looking at a different patient's board with a live confirmation
 * still pending. `aria-modal="true"` is the property every Orb modal already
 * sets, so this needs no per-modal registration.
 */
export function isModalOpen(doc: Document = document): boolean {
  return !!doc.querySelector('[role="dialog"][aria-modal="true"]')
}

/**
 * Which tab does this second chord key mean, for a user who can reach `accessible`?
 *
 * Returns null for an unmapped key AND for a mapped key whose tab this role
 * cannot open — a nurse pressing `g o` must not land on a surface RBAC would
 * refuse. Silently doing nothing is correct: the alternative is a shell that
 * renders a tab the role has no permission for.
 */
export function resolveNavKey(key: string, accessible: readonly string[]): string | null {
  const k = String(key || '').toLowerCase()
  const hit = NAV_SHORTCUTS.find((s) => s.key === k)
  if (!hit) return null
  return accessible.includes(hit.tab) ? hit.tab : null
}

/** The shortcuts to show this user, in help-sheet order. */
export function visibleNavShortcuts(accessible: readonly string[]) {
  return NAV_SHORTCUTS.filter((s) => accessible.includes(s.tab))
}

/** Cmd on Apple hardware, Ctrl everywhere else — for LABELLING only. The
 *  handlers accept either, because a clinician on a Mac with a Windows keyboard
 *  is a real thing on a ward. */
export function modifierLabel(platform: string = navigator.platform || ''): string {
  return /mac|iphone|ipad|ipod/i.test(platform) ? '⌘' : 'Ctrl'
}
