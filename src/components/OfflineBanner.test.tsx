/**
 * The staleness label.
 *
 * Showing a cached vital as if it were live is worse than showing nothing, so
 * these tests are about WORDS, not layout: does the banner say it is offline,
 * does it say how old the data is, and does it say that writing is refused.
 * A banner that appears but hedges is the failure this component exists to
 * prevent.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import OfflineBanner from './OfflineBanner'
import type { OfflineSnapshot } from '../hooks/useOfflineSnapshot'

function snapshot<T>(over: Partial<OfflineSnapshot<T>>): OfflineSnapshot<T> {
  return {
    data: null,
    source: 'live',
    savedAt: null,
    online: true,
    unreachable: false,
    ...over,
  }
}

describe('OfflineBanner', () => {
  it('renders nothing while the data on screen is live', () => {
    const { container } = render(
      <OfflineBanner snapshot={snapshot({ data: {}, source: 'live' })} what="shift board" />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('says OFFLINE, gives the age in words, and gives the clock time it was saved', () => {
    const savedAt = Date.now() - 23 * 60_000
    render(
      <OfflineBanner
        snapshot={snapshot({ data: {}, source: 'cache', savedAt, online: false })}
        what="shift board"
      />,
    )
    const banner = screen.getByRole('alert')
    expect(banner.textContent).toMatch(/offline/i)
    expect(banner.textContent).toMatch(/23 minutes old/i)
    expect(banner.textContent).toMatch(
      new RegExp(new Date(savedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
    )
  })

  it('states that nothing can be recorded while offline', () => {
    render(
      <OfflineBanner
        snapshot={snapshot({ data: {}, source: 'cache', savedAt: Date.now() - 60_000, online: false })}
        what="shift board"
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/cannot be recorded|nothing can be recorded/i)
  })

  it('never claims to be live when it cannot date the copy', () => {
    render(
      <OfflineBanner
        snapshot={snapshot({ data: {}, source: 'cache', savedAt: null, online: false })}
        what="shift board"
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/not live/i)
    expect(screen.getByRole('alert').textContent).toMatch(/unknown/i)
  })

  it('says the server is unreachable — not that the tablet is offline — when the NIC is up', () => {
    render(
      <OfflineBanner
        snapshot={snapshot({
          data: {},
          source: 'cache',
          savedAt: Date.now() - 5 * 60_000,
          online: true,
          unreachable: true,
        })}
        what="shift board"
      />,
    )
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/can'?t reach|cannot reach/i)
    expect(text).toMatch(/5 minutes old/i)
  })

  it('says there is nothing saved rather than showing an empty screen silently', () => {
    render(
      <OfflineBanner
        snapshot={snapshot({ data: null, source: 'none', online: false })}
        what="shift board"
      />,
    )
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/offline/i)
    expect(text).toMatch(/no saved copy|nothing saved/i)
  })

  it('says nothing during a first load on a connected device, which is not offline', () => {
    // useOfflineSnapshot returns source none, online, not unreachable while the
    // first answer is on its way. The banner used to say Offline, not live there,
    // so it flashed on every sign in and reload.
    const { container } = render(
      <OfflineBanner
        snapshot={snapshot({ data: null, source: 'none', online: true, unreachable: false })}
        what="ward board"
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('still says the server cannot be reached when a connected device has nothing saved', () => {
    render(
      <OfflineBanner
        snapshot={snapshot({ data: null, source: 'none', online: true, unreachable: true })}
        what="ward board"
      />,
    )
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/can'?t reach|cannot reach/i)
    expect(text).toMatch(/no saved copy|nothing saved/i)
  })

  it('reads the age in hours once a copy is old enough to be dangerous', () => {
    render(
      <OfflineBanner
        snapshot={snapshot({
          data: {},
          source: 'cache',
          savedAt: Date.now() - 3 * 60 * 60_000 - 4 * 60_000,
          online: false,
        })}
        what="shift board"
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/3 hours 4 minutes old/i)
  })
})
