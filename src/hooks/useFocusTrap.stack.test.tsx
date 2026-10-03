/**
 * Stacked dialogs: only the TOP trap handles the keyboard.
 *
 * Every trap listens on document in the capture phase, so with a detail
 * dialog open over a list dialog both saw each Escape and both closed.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useFocusTrap } from './useFocusTrap'

function Dialog({ label, onClose }: { label: string; onClose: () => void }) {
  const ref = useFocusTrap<HTMLDivElement>(onClose)
  return (
    <div ref={ref} role="dialog" aria-modal="true" aria-label={label}>
      <button type="button">{label} one</button>
      <button type="button">{label} two</button>
    </div>
  )
}

function Stack({ showTop, onLower, onTop }: { showTop: boolean; onLower: () => void; onTop: () => void }) {
  return (
    <>
      <Dialog label="List" onClose={onLower} />
      {showTop && <Dialog label="Detail" onClose={onTop} />}
    </>
  )
}

function escape() {
  const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
  ;(document.activeElement || document.body).dispatchEvent(event)
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
  document.body.style.overflow = ''
})

describe('useFocusTrap stack', () => {
  it('sends Escape to the top dialog only', () => {
    const onLower = vi.fn()
    const onTop = vi.fn()
    const { rerender } = render(<Stack showTop={false} onLower={onLower} onTop={onTop} />)
    rerender(<Stack showTop onLower={onLower} onTop={onTop} />)

    escape()
    expect(onTop).toHaveBeenCalledTimes(1)
    expect(onLower).not.toHaveBeenCalled()
  })

  it('hands the keyboard back to the dialog underneath when the top one closes', () => {
    const onLower = vi.fn()
    const onTop = vi.fn()
    const { rerender } = render(<Stack showTop={false} onLower={onLower} onTop={onTop} />)
    rerender(<Stack showTop onLower={onLower} onTop={onTop} />)
    rerender(<Stack showTop={false} onLower={onLower} onTop={onTop} />)

    escape()
    expect(onLower).toHaveBeenCalledTimes(1)
    expect(onTop).not.toHaveBeenCalled()
  })

  it('keeps Tab inside the top dialog', () => {
    const { rerender } = render(<Stack showTop={false} onLower={vi.fn()} onTop={vi.fn()} />)
    rerender(<Stack showTop onLower={vi.fn()} onTop={vi.fn()} />)
    const detailTwo = document.querySelector('[aria-label="Detail"] button:last-child') as HTMLButtonElement
    detailTwo.focus()
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    detailTwo.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(true)
    // Still inside the top dialog: the list underneath did not pull focus back.
    expect(document.querySelector('[aria-label="Detail"]')?.contains(document.activeElement)).toBe(true)
  })
})
