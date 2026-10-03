/**
 * The shell keeps visited panels mounted under display:none. A poller in a
 * hidden panel must stand down, catch up once when its panel is shown again,
 * and answer the "data changed" event only while it is live.
 */
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PanelActiveContext, announceDataChanged } from '../contexts/PanelActiveContext'
import { useVisiblePolling } from './useVisiblePolling'

function Poller({ onTick, ms = 1000, enabled = true }: { onTick: () => void; ms?: number; enabled?: boolean }) {
  useVisiblePolling(onTick, ms, enabled)
  return null
}

function Panel({ active, onTick }: { active: boolean; onTick: () => void }) {
  return (
    <PanelActiveContext.Provider value={active}>
      <Poller onTick={onTick} />
    </PanelActiveContext.Provider>
  )
}

describe('useVisiblePolling inside a shell panel', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('polls while its panel is on screen', () => {
    const tick = vi.fn()
    render(<Panel active onTick={tick} />)
    act(() => { vi.advanceTimersByTime(3000) })
    expect(tick).toHaveBeenCalledTimes(3)
  })

  it('stops polling while its panel is hidden', () => {
    const tick = vi.fn()
    const view = render(<Panel active onTick={tick} />)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(tick).toHaveBeenCalledTimes(1)

    view.rerender(<Panel active={false} onTick={tick} />)
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(tick).toHaveBeenCalledTimes(1)
  })

  it('fetches at once when the panel is shown again, then resumes', () => {
    const tick = vi.fn()
    const view = render(<Panel active onTick={tick} />)
    view.rerender(<Panel active={false} onTick={tick} />)
    act(() => { vi.advanceTimersByTime(5000) })
    expect(tick).not.toHaveBeenCalled()

    view.rerender(<Panel active onTick={tick} />)
    expect(tick).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(tick).toHaveBeenCalledTimes(2)
  })

  it('does not fetch on first mount (callers load on mount themselves)', () => {
    const tick = vi.fn()
    render(<Panel active onTick={tick} />)
    expect(tick).not.toHaveBeenCalled()
  })

  it('refreshes on the data-changed event only while live', () => {
    const live = vi.fn()
    const hidden = vi.fn()
    render(
      <>
        <Panel active onTick={live} />
        <Panel active={false} onTick={hidden} />
      </>,
    )
    act(() => { announceDataChanged() })
    expect(live).toHaveBeenCalledTimes(1)
    expect(hidden).not.toHaveBeenCalled()
  })

  it('outside any panel it polls as before', () => {
    const tick = vi.fn()
    render(<Poller onTick={tick} />)
    act(() => { vi.advanceTimersByTime(2000) })
    expect(tick).toHaveBeenCalledTimes(2)
  })
})
