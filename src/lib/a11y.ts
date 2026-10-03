import type { KeyboardEvent } from 'react'

/**
 * Make a non-button element genuinely activatable.
 *
 * Prefer a real `<button>`. Use this only where the clickable region legitimately
 * contains block-level content that a `<button>` may not hold (a card with
 * stacked `<div>` rows, a table row), because a `<div onClick>` is invisible to
 * the keyboard: it takes no focus, fires no synthetic click on Enter or Space,
 * and announces nothing to a screen reader. On a ward that means a clinician in
 * gloves, or on a workstation with a dead trackpad, simply cannot reach it.
 *
 * Spreads `role="button"`, `tabIndex={0}`, the click handler and the Enter/Space
 * key handler — the four things WAI-ARIA requires together. The app-wide
 * `:focus-visible` ring (index.css) then applies for free, because the element
 * has become focusable.
 *
 *   <div {...activatable(() => openAlert(a), `Open alert: ${a.title}`)}>…</div>
 */
export function activatable(onActivate: () => void, label?: string) {
  return {
    role: 'button',
    tabIndex: 0,
    onClick: onActivate,
    onKeyDown: (e: KeyboardEvent) => {
      // Space must be intercepted or it scrolls the page instead of activating.
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        onActivate()
      }
    },
    ...(label ? { 'aria-label': label } : {}),
  } as const
}

export default activatable
