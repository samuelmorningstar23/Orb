/**
 * Load-once lists refresh on the shell's "data changed" event: at once in the
 * panel on screen, once on return in a hidden one, never on mount.
 */
import { act, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PanelActiveContext, announceDataChanged } from '../contexts/PanelActiveContext'
import { useDataChanged } from './useDataChanged'

function Listener({ onChange }: { onChange: () => void }) {
  useDataChanged(onChange)
  return null
}

function Panel({ active, onChange }: { active: boolean; onChange: () => void }) {
  return (
    <PanelActiveContext.Provider value={active}>
      <Listener onChange={onChange} />
    </PanelActiveContext.Provider>
  )
}

describe('useDataChanged', () => {
  it('does nothing on mount', () => {
    const fn = vi.fn()
    render(<Panel active onChange={fn} />)
    expect(fn).not.toHaveBeenCalled()
  })

  it('refreshes at once in the panel on screen', () => {
    const fn = vi.fn()
    render(<Panel active onChange={fn} />)
    act(() => { announceDataChanged() })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('in a hidden panel, refreshes once when shown, however many changes were missed', () => {
    const fn = vi.fn()
    const view = render(<Panel active={false} onChange={fn} />)
    act(() => { announceDataChanged(); announceDataChanged() })
    expect(fn).not.toHaveBeenCalled()

    view.rerender(<Panel active onChange={fn} />)
    expect(fn).toHaveBeenCalledTimes(1)
    view.rerender(<Panel active={false} onChange={fn} />)
    view.rerender(<Panel active onChange={fn} />)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('stops listening once unmounted', () => {
    const fn = vi.fn()
    const view = render(<Panel active onChange={fn} />)
    view.unmount()
    act(() => { announceDataChanged() })
    expect(fn).not.toHaveBeenCalled()
  })
})
