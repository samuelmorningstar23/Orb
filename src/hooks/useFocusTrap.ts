import { useEffect, useEffectEvent, useRef } from 'react'

/**
 * Reference-counted body scroll lock.
 *
 * A `position: fixed` scrim covers the page but does not stop it scrolling
 * underneath: on a bedside tablet, scrolling to the bottom of a modal chains
 * the gesture to the document behind it, so dismissing the modal returns the
 * clinician to a chart scrolled somewhere they did not choose. Counting is
 * required because modals stack (a safety alert over a med sheet) — the lock
 * must only lift when the LAST trap unmounts.
 */
let scrollLockCount = 0
let previousBodyOverflow = ''

/**
 * The open traps, oldest first. Only the LAST one handles keys. Every trap
 * listens on document in the capture phase, so with a detail dialog open over
 * a list dialog both saw each Escape (closing both) and both fought over Tab.
 */
const trapStack: symbol[] = []

function lockBodyScroll() {
  if (scrollLockCount === 0) {
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  }
  scrollLockCount += 1
}

function unlockBodyScroll() {
  scrollLockCount = Math.max(0, scrollLockCount - 1)
  if (scrollLockCount === 0) document.body.style.overflow = previousBodyOverflow
}

/**
 * Accessibility focus-trap for modal dialogs.
 *
 * While a modal is open, keyboard focus must stay inside it (WCAG 2.1 SC 2.4.3 /
 * 2.1.2). This hook, applied to the modal's container ref:
 *   - moves focus into the modal on open (first focusable element, or the
 *     container itself);
 *   - cycles Tab / Shift+Tab within the modal's focusable elements;
 *   - calls `onClose` when Escape is pressed;
 *   - restores focus to the element that was focused before the modal opened
 *     (usually the trigger button) when the modal unmounts.
 *
 * Usage:
 *   const ref = useFocusTrap<HTMLDivElement>(onClose)
 *   return <div ref={ref} role="dialog" aria-modal="true">…</div>
 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export function useFocusTrap<T extends HTMLElement>(
  onClose?: () => void,
): React.RefObject<T | null> {
  const containerRef = useRef<T>(null)
  // Capture the trigger so focus can be restored to it on close.
  const previouslyFocused = useRef<HTMLElement | null>(null)
  /* An effect event, and the effect below runs ONCE, deliberately.
     If `onClose` were a dependency, every caller passing an inline arrow (or a
     value that flips, like `submitting ? undefined : onClose`) would tear the
     effect down and rebuild it on each render — and teardown restores focus to
     the trigger OUTSIDE the modal, so focus would jump out and back to the
     first field on every keystroke in a modal form. The effect event always
     sees the latest handler without re-running the trap. (It replaced a ref
     written during render, react-hooks/refs.) With no handler the key is left
     alone, so it still reaches whatever is underneath. */
  const closeOnEscape = useEffectEvent((e: KeyboardEvent) => {
    if (!onClose) return
    e.stopPropagation()
    onClose()
  })

  useEffect(() => {
    const container = containerRef.current
    /* NO CONTAINER, NO TRAP. A caller that is mounted while its dialog is not
       rendered (returns null when closed) has nothing to trap, and arming the
       trap anyway was a global outage: a capture-phase document listener that
       preventDefaults every Tab (no focusable element in a null container) and
       swallows every Escape, plus a body scroll lock, for as long as the caller
       stays mounted. Render the dialog in a child that mounts only when open. */
    if (!container) return
    previouslyFocused.current = document.activeElement as HTMLElement | null
    lockBodyScroll()
    const token = Symbol('focus-trap')
    trapStack.push(token)

    const getFocusable = (): HTMLElement[] => {
      return Array.from(
        container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter(el => el.offsetParent !== null || el === document.activeElement)
    }

    // Move focus into the modal on open, without stealing an autoFocus'd field
    // that already lives inside the container.
    if (!container.contains(document.activeElement)) {
      const focusable = getFocusable()
      ;(focusable[0] ?? container).focus()
    }

    const handleKeyDown = (e: KeyboardEvent) => {
      // A dialog underneath the top one ignores the keyboard entirely.
      if (trapStack[trapStack.length - 1] !== token) return
      if (e.key === 'Escape') {
        closeOnEscape(e)
        return
      }
      if (e.key !== 'Tab') return

      const focusable = getFocusable()
      if (focusable.length === 0) {
        // Nothing focusable — keep focus pinned to the container.
        e.preventDefault()
        container.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement as HTMLElement | null

      if (e.shiftKey) {
        if (active === first || !container.contains(active)) {
          e.preventDefault()
          last.focus()
        }
      } else if (active === last || !container.contains(active)) {
        e.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown, true)
    return () => {
      document.removeEventListener('keydown', handleKeyDown, true)
      const at = trapStack.lastIndexOf(token)
      if (at >= 0) trapStack.splice(at, 1)
      unlockBodyScroll()
      // Restore focus to the trigger when the modal closes.
      previouslyFocused.current?.focus?.()
    }
  }, [])

  return containerRef
}

export default useFocusTrap
