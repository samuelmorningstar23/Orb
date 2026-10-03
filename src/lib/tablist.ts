import type { KeyboardEvent } from 'react'

/**
 * The keyboard half of the WAI-ARIA tabs pattern.
 *
 * `role="tablist"` is a promise: a screen reader announces "tab list", and the
 * user presses the arrow keys because that is what the contract says will
 * work. Declaring the roles without the key handling breaks that promise
 * silently — the announcement is right and nothing moves. It also leaves every
 * tab in the Tab sequence, which the same contract says should hold exactly one
 * stop.
 *
 * Pair this with `tabIndex={active === id ? 0 : -1}` on each tab and
 * `aria-controls` pointing at a `role="tabpanel"` wrapper.
 *
 * ClinicianShell's pillar rail and NursePanel's shift strip both use it, so the
 * two cannot drift apart again.
 */
export function rovingTabKeyDown<T extends string>(
  ids: readonly T[],
  active: T,
  setActive: (id: T) => void,
  domIdFor: (id: T) => string,
) {
  return (e: KeyboardEvent<HTMLElement>) => {
    const current = ids.indexOf(active)
    if (current === -1) return
    let next = -1
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowRight': next = (current + 1) % ids.length; break
      case 'ArrowUp':
      case 'ArrowLeft': next = (current - 1 + ids.length) % ids.length; break
      case 'Home': next = 0; break
      case 'End': next = ids.length - 1; break
      default: return
    }
    e.preventDefault()
    const nextId = ids[next]
    setActive(nextId)
    document.getElementById(domIdFor(nextId))?.focus()
  }
}

export default rovingTabKeyDown
