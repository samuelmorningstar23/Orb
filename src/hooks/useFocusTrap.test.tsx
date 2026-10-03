/**
 * useFocusTrap with nothing to trap must do nothing.
 *
 * A caller that is mounted while its dialog is closed (renders null) used to
 * arm the trap anyway: Tab was cancelled everywhere, Escape never reached any
 * other dialog, and the body could not scroll. With a container it must still
 * trap as before.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useFocusTrap } from './useFocusTrap'

function ClosedDialog({ onClose }: { onClose: () => void }) {
  useFocusTrap<HTMLDivElement>(onClose)
  return null
}

function OpenDialog({ onClose }: { onClose: () => void }) {
  const ref = useFocusTrap<HTMLDivElement>(onClose)
  return (
    <div ref={ref} role="dialog" aria-modal="true">
      <button type="button">One</button>
      <button type="button">Two</button>
    </div>
  )
}

function press(target: Element, key: string, init: KeyboardEventInit = {}) {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
  target.dispatchEvent(event)
  return event
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
  document.body.style.overflow = ''
})

describe('useFocusTrap with a null container', () => {
  it('installs no listener and takes no scroll lock', () => {
    const onClose = vi.fn()
    const input = document.createElement('input')
    document.body.appendChild(input)
    render(<ClosedDialog onClose={onClose} />)
    input.focus()

    expect(press(input, 'Tab').defaultPrevented).toBe(false)

    const other = vi.fn()
    document.addEventListener('keydown', other)
    try {
      press(input, 'Escape')
      expect(other).toHaveBeenCalledTimes(1)
      expect(onClose).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', other)
    }
    expect(document.body.style.overflow).not.toBe('hidden')
  })
})

describe('useFocusTrap with a container', () => {
  it('closes on Escape, locks scroll and releases both on unmount', () => {
    const onClose = vi.fn()
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()

    const { unmount, getByText } = render(<OpenDialog onClose={onClose} />)
    expect(document.body.style.overflow).toBe('hidden')

    press(getByText('One'), 'Escape')
    expect(onClose).toHaveBeenCalledTimes(1)

    unmount()
    expect(document.body.style.overflow).not.toBe('hidden')
    expect(document.activeElement).toBe(trigger)
  })
})
