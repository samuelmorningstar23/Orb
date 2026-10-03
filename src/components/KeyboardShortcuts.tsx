import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { onShowShortcuts } from '../lib/orbBus'
import {
  CHORD_PREFIX,
  CHORD_TIMEOUT_MS,
  isModalOpen,
  isTypingTarget,
  modifierLabel,
  resolveNavKey,
  visibleNavShortcuts,
} from '../lib/shortcuts'
import './KeyboardShortcuts.css'

/**
 * The global keyboard layer, and the reference card that makes it discoverable.
 *
 * Mount once inside the clinician shell, exactly like CommandPalette. It owns
 * its own listener and its own dialog; the shell passes it the tabs this role
 * can reach and the setter that switches them.
 *
 *   ?          the shortcut sheet
 *   g then p   Patients, and six more (see lib/shortcuts.NAV_SHORTCUTS)
 *   Esc        closes the sheet
 *
 * ── THE THREE THINGS A KEYBOARD LAYER GETS WRONG ─────────────────────────────
 * 1. **Firing while someone is typing.** A clinician writing "Progress: patient
 *    settled" must not have the `g`…`p` in "progress"…"patient" navigate the
 *    shell out from under them. Guarded by isTypingTarget, and the chord expires
 *    after 1.5s so an armed prefix cannot lie in wait.
 * 2. **Firing under a modal.** Switching tabs behind an open discharge
 *    confirmation leaves a live clinical dialog floating over a different
 *    patient's board. Guarded by isModalOpen.
 * 3. **Stealing a browser or product accelerator.** Anything with Meta/Ctrl/Alt
 *    held is not ours — Cmd-P is print, and this product actually prints. The
 *    handler returns immediately on any modifier, which is also what keeps the
 *    existing Cmd-K palette working untouched.
 */
export default function KeyboardShortcuts({
  accessibleTabs,
  onGoto,
}: {
  accessibleTabs: readonly string[]
  onGoto: (tab: string) => void
}) {
  const [open, setOpen] = useState(false)
  // The chord prefix is a ref, not state: arming it must not re-render the
  // shell (this component sits above every panel), and the keydown handler
  // needs to read the CURRENT value, not the one captured when it was bound.
  const armedAt = useRef<number>(0)

  const close = useCallback(() => setOpen(false), [])

  // The header's keyboard button. A shortcut layer whose only entry point is a
  // shortcut is undiscoverable by definition — nobody presses `?` in software
  // they have never been told has shortcuts.
  useEffect(() => onShowShortcuts(() => setOpen(true)), [])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // Not ours: every modified combination belongs to the browser, the OS, or
      // the command palette.
      if (e.metaKey || e.ctrlKey || e.altKey) return

      /* A LOCAL BINDING BEATS THE GLOBAL ONE.
         The Ward Round binds bare `s` / `n` / `p` / `k` (seen / next / previous /
         skip) on its own panel root and calls preventDefault when it acts. React
         attaches its listener inside the document, so it runs BEFORE this
         window-level one — which means without this check, `g` then `s` on the
         Ward Round would mark the current bed SEEN *and* navigate to Sage. One
         of those is a clinical action.
         So: a key another surface has already consumed is not ours, and the
         armed prefix is dropped rather than left to eat the next keystroke. */
      if (e.defaultPrevented) {
        armedAt.current = 0
        return
      }

      if (e.key === 'Escape' && open) {
        e.preventDefault()
        setOpen(false)
        armedAt.current = 0
        return
      }

      if (isTypingTarget(e.target)) {
        // Also disarm: a `g` typed into a note must not stay armed and then eat
        // the first letter the clinician types after clicking away.
        armedAt.current = 0
        return
      }

      // '?' is Shift+/ on most layouts and its own key on others; match the
      // produced character rather than the physical key.
      if (e.key === '?') {
        e.preventDefault()
        armedAt.current = 0
        setOpen((prev) => !prev)
        return
      }

      // Navigation is refused while a modal is up (see the header comment) and
      // while the sheet itself is open — the sheet is a modal too, and reading
      // the reference should not move you.
      if (open || isModalOpen()) {
        armedAt.current = 0
        return
      }

      const key = e.key.toLowerCase()
      const armed = armedAt.current > 0 && Date.now() - armedAt.current < CHORD_TIMEOUT_MS

      if (armed) {
        armedAt.current = 0
        const tab = resolveNavKey(key, accessibleTabs)
        if (tab) {
          e.preventDefault()
          onGoto(tab)
        }
        // An unmapped second key (or one this role cannot reach) does nothing at
        // all. Deliberate: falling through to "arm again" would make `g g p`
        // behave differently from `g p` for no reason a user could predict.
        return
      }

      if (key === CHORD_PREFIX) {
        armedAt.current = Date.now()
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [accessibleTabs, onGoto, open])

  // The sheet is its own component so its focus trap exists only while it is
  // open. Called up here, in a component that is always mounted, the trap was
  // armed for the whole session with nothing to trap: every Tab in the shell
  // was cancelled and every other dialog's Escape was swallowed.
  return open ? <ShortcutSheet accessibleTabs={accessibleTabs} onClose={close} /> : null
}

function ShortcutSheet({
  accessibleTabs,
  onClose,
}: {
  accessibleTabs: readonly string[]
  onClose: () => void
}) {
  const modalRef = useFocusTrap<HTMLDivElement>(onClose)
  const close = onClose
  const shortcuts = visibleNavShortcuts(accessibleTabs)
  const mod = modifierLabel()

  return createPortal(
    <div className="kbd-backdrop" onClick={close}>
      <div
        className="kbd-sheet glass-strong"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="kbd-sheet-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="kbd-sheet__header">
          <div>
            <h2 className="kbd-sheet__title" id="kbd-sheet-title">Keyboard shortcuts</h2>
            <p className="kbd-sheet__subtitle">
              Press <kbd className="kbd">g</kbd> then a letter. Shortcuts are off while you are
              typing and while a dialog is open.
            </p>
          </div>
          <button type="button" className="kbd-sheet__close" onClick={close} aria-label="Close">
            &times;
          </button>
        </div>

        <div className="kbd-sheet__body">
          <section className="kbd-group">
            <h3 className="kbd-group__title">Go to</h3>
            <ul className="kbd-list">
              {shortcuts.map((s) => (
                <li key={s.tab} className="kbd-row">
                  <span className="kbd-row__keys">
                    <kbd className="kbd">g</kbd>
                    <kbd className="kbd">{s.key}</kbd>
                  </span>
                  <span className="kbd-row__label">{s.label}</span>
                </li>
              ))}
              {shortcuts.length === 0 && (
                <li className="kbd-row kbd-row--empty">
                  No navigation shortcuts apply to your role.
                </li>
              )}
            </ul>
          </section>

          <section className="kbd-group">
            <h3 className="kbd-group__title">Anywhere</h3>
            <ul className="kbd-list">
              <li className="kbd-row">
                <span className="kbd-row__keys">
                  <kbd className="kbd">{mod}</kbd>
                  <kbd className="kbd">K</kbd>
                </span>
                <span className="kbd-row__label">Find a patient by name, MRN or bed</span>
              </li>
              <li className="kbd-row">
                <span className="kbd-row__keys"><kbd className="kbd">?</kbd></span>
                <span className="kbd-row__label">This list</span>
              </li>
              <li className="kbd-row">
                <span className="kbd-row__keys"><kbd className="kbd">Esc</kbd></span>
                <span className="kbd-row__label">Close a dialog</span>
              </li>
            </ul>
          </section>
        </div>
      </div>
    </div>,
    document.body,
  )
}
