/**
 * Pulse is "Season and conditions" from the calendar built into Orb, and it
 * never reaches past the app's own server.
 *
 * Orb is strictly offline. The old panel fetched weather, air quality, US flu
 * and FDA recall feeds, asked the browser for the device location, looked it up
 * on nominatim.openstreetmap.org straight from the browser, kept it in
 * localStorage and polled every 15 minutes. These tests hold the rebuilt panel
 * to one request, to the app's own /api/pulse/season, made when it is opened.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PulseWidget from './PulseWidget'
import type { PulseSeason } from '../api/pulse'

const IMD = { name: 'India Meteorological Department', url: 'https://mausam.imd.gov.in/', accessed: '2026-10-04' }
const NCVBDC = { name: 'National Center for Vector Borne Diseases Control', url: 'https://ncvbdc.mohfw.gov.in/', accessed: '2026-10-04' }
const MCGM = { name: 'Municipal Corporation of Greater Mumbai', url: 'https://portal.mcgm.gov.in/', accessed: '2026-10-04' }

function season(over: Partial<PulseSeason> = {}): PulseSeason {
  return {
    date: '2026-07-19',
    state: { code: 'MH', name: 'Maharashtra' },
    zone: { id: 'west', name: 'Western India' },
    setup_needed: false,
    setup_message: null,
    calendar: { version: 1, status: 'draft', reviewed_by: null, reviewed_on: null, compiled_on: '2026-10-04' },
    active: [
      {
        id: 'sw-monsoon', kind: 'season', title: 'South-west monsoon',
        start: '06-01', end: '09-30', in_peak: true, ends_on: '2026-09-30',
        summary: 'Heavy rain across the state, with waterlogging in towns.',
        typically_rises: ['Leptospirosis', 'Malaria', 'Diarrhoeal illness'],
        sources: [IMD, NCVBDC],
      },
      {
        id: 'waterlogging', kind: 'environment', title: 'Waterlogging',
        start: '06-15', end: '09-15', in_peak: false, ends_on: '2026-09-15',
        summary: 'Low-lying streets flood after heavy rain.',
        typically_rises: [],
        sources: [MCGM],
      },
    ],
    upcoming: [
      {
        id: 'scrub-typhus', kind: 'disease', title: 'Scrub typhus season',
        start: '07-20', end: '11-30', in_peak: false, starts_on: '2026-07-20', days_away: 1,
        summary: 'Fever with an eschar after field work.', typically_rises: ['Scrub typhus'], sources: [NCVBDC],
      },
      {
        id: 'dengue', kind: 'disease', title: 'Dengue season',
        start: '07-31', end: '11-30', in_peak: false, starts_on: '2026-07-31', days_away: 12,
        summary: 'Cases climb after rain leaves standing water.', typically_rises: ['Dengue'], sources: [NCVBDC],
      },
    ],
    ...over,
  }
}

type Reply = { status: number; body: unknown } | Error

/** fetch, answering each call from `replies` in turn (the last one repeats). */
function stubFetch(...replies: Reply[]) {
  let call = 0
  const fn = vi.fn(async (_url: string, _init?: RequestInit) => {
    const reply = replies[Math.min(call++, replies.length - 1)]
    if (reply instanceof Error) throw reply
    return {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      headers: new Headers(),
      json: async () => reply.body,
    }
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

const geolocation = {
  getCurrentPosition: vi.fn(),
  watchPosition: vi.fn(),
  clearWatch: vi.fn(),
}

beforeEach(() => {
  Object.defineProperty(window.navigator, 'geolocation', { value: geolocation, configurable: true })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
  geolocation.getCurrentPosition.mockClear()
  geolocation.watchPosition.mockClear()
  // jsdom has no geolocation of its own; take the stub away again.
  delete (window.navigator as { geolocation?: unknown }).geolocation
})

const trigger = () => screen.getByRole('button', { name: 'Pulse: season and conditions' })

async function openPanel() {
  const button = trigger()
  button.focus()
  await act(async () => { fireEvent.click(button) })
  return screen.getByRole('dialog', { name: 'Pulse' })
}

describe('PulseWidget: season and conditions', () => {
  it('shows the state, the date, this season and what is coming up', async () => {
    stubFetch({ status: 200, body: season() })
    render(<PulseWidget />)
    const dialog = await openPanel()

    expect(within(dialog).getByText('Season and conditions')).toBeInTheDocument()
    await within(dialog).findByText('South-west monsoon')
    expect(within(dialog).getByText('Maharashtra (Western India)')).toBeInTheDocument()
    expect(within(dialog).getByText('19 Jul 2026')).toBeInTheDocument()

    const cards = within(dialog).getAllByRole('listitem').filter(li => li.classList.contains('pulse-season'))
    expect(cards).toHaveLength(2)

    const monsoon = within(dialog).getByRole('listitem', { name: 'South-west monsoon' })
    expect(within(monsoon).getByText('June to September')).toBeInTheDocument()
    expect(within(monsoon).getByText('Peak now')).toBeInTheDocument()
    expect(within(monsoon).getByText('Heavy rain across the state, with waterlogging in towns.')).toBeInTheDocument()
    expect(within(monsoon).getByText('Typically rises: Leptospirosis, Malaria, Diarrhoeal illness')).toBeInTheDocument()
    expect(within(monsoon).getByText(
      'Sources: India Meteorological Department; National Center for Vector Borne Diseases Control',
    )).toBeInTheDocument()

    const water = within(dialog).getByRole('listitem', { name: 'Waterlogging' })
    expect(within(water).getByText('15 June to 15 September')).toBeInTheDocument()
    expect(within(water).queryByText('Peak now')).toBeNull()
    expect(within(water).queryByText(/Typically rises/)).toBeNull()
    expect(within(water).getByText('Source: Municipal Corporation of Greater Mumbai')).toBeInTheDocument()

    expect(within(dialog).getByRole('heading', { name: 'This season' })).toBeInTheDocument()
    expect(within(dialog).getByRole('heading', { name: 'Coming up' })).toBeInTheDocument()
    expect(within(dialog).getByText('Tomorrow: Scrub typhus season')).toBeInTheDocument()
    expect(within(dialog).getByText('In 12 days: Dengue season')).toBeInTheDocument()

    expect(within(dialog).getByText('Built into Orb. Calendar compiled 4 Oct 2026.')).toBeInTheDocument()
  })

  it('prints sources as names only, never as links or addresses', async () => {
    stubFetch({ status: 200, body: season() })
    render(<PulseWidget />)
    const dialog = await openPanel()
    await within(dialog).findByText('South-west monsoon')
    expect(dialog.querySelectorAll('a')).toHaveLength(0)
    expect(dialog.textContent).not.toMatch(/https?:|www\.|\.gov\.in/)
  })

  it('labels a draft calendar as not yet reviewed', async () => {
    stubFetch({ status: 200, body: season() })
    render(<PulseWidget />)
    const dialog = await openPanel()
    const label = await within(dialog).findByText('Draft: not yet reviewed by a clinician')
    expect(label).toHaveAttribute('role', 'note')
    expect(within(dialog).queryByText(/^Reviewed/)).toBeNull()
  })

  it('treats an unknown calendar status as a draft', async () => {
    const body = season()
    stubFetch({ status: 200, body: { ...body, calendar: { ...body.calendar, status: 'pending' } } })
    render(<PulseWidget />)
    const dialog = await openPanel()
    expect(await within(dialog).findByText('Draft: not yet reviewed by a clinician')).toBeInTheDocument()
  })

  it('names the reviewer and the date once a clinician has reviewed it', async () => {
    stubFetch({
      status: 200,
      body: season({
        calendar: { version: 1, status: 'reviewed', reviewed_by: 'Dr. Anjali Rao', reviewed_on: '2026-10-02', compiled_on: '2026-10-01' },
      }),
    })
    render(<PulseWidget />)
    const dialog = await openPanel()
    expect(await within(dialog).findByText('Reviewed by Dr. Anjali Rao on 2 Oct 2026')).toBeInTheDocument()
    expect(within(dialog).queryByText(/Draft/)).toBeNull()
  })

  it('shows the setup message instead of cards when the state is not set', async () => {
    const message = 'Set the hospital\'s state in site.toml ([site] state = "Maharashtra") to see this hospital\'s seasonal calendar.'
    stubFetch({
      status: 200,
      body: season({ state: null, zone: null, setup_needed: true, setup_message: message, active: [], upcoming: [] }),
    })
    render(<PulseWidget />)
    const dialog = await openPanel()
    expect(await within(dialog).findByText(message)).toBeInTheDocument()
    expect(within(dialog).getByText('State not set')).toBeInTheDocument()
    expect(within(dialog).queryByRole('heading', { name: 'This season' })).toBeNull()
    expect(within(dialog).queryByRole('heading', { name: 'Coming up' })).toBeNull()
    expect(within(dialog).queryByText(/Draft/)).toBeNull()
  })

  it('says so when nothing in the calendar covers today', async () => {
    stubFetch({ status: 200, body: season({ active: [], upcoming: [] }) })
    render(<PulseWidget />)
    const dialog = await openPanel()
    expect(await within(dialog).findByText('Nothing in the calendar covers today.')).toBeInTheDocument()
    expect(within(dialog).queryByRole('heading', { name: 'Coming up' })).toBeNull()
  })

  it('says the calendar could not be loaded, with the server\'s reason, and can try again', async () => {
    const reason = "Pulse's seasonal calendar is not installed on this Orb, so there is nothing to show. Ask the person who installed Orb to check it."
    const fetchMock = stubFetch({ status: 503, body: { detail: reason } }, { status: 200, body: season() })
    render(<PulseWidget />)
    const dialog = await openPanel()

    const alert = await within(dialog).findByRole('alert')
    expect(within(alert).getByText('The seasonal calendar could not be loaded.')).toBeInTheDocument()
    expect(within(alert).getByText(reason)).toBeInTheDocument()
    expect(within(dialog).queryByText('South-west monsoon')).toBeNull()

    await act(async () => { fireEvent.click(within(alert).getByRole('button', { name: 'Try again' })) })
    expect(await within(dialog).findByText('South-west monsoon')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not print a browser network error to the clinician', async () => {
    stubFetch(new TypeError('Failed to fetch'))
    render(<PulseWidget />)
    const dialog = await openPanel()
    const alert = await within(dialog).findByRole('alert')
    expect(within(alert).getByText('The seasonal calendar could not be loaded.')).toBeInTheDocument()
    expect(dialog.textContent).not.toMatch(/Failed to fetch/)
  })
})

describe('PulseWidget: strictly offline', () => {
  it('asks only the app\'s own API, never the location, and never polls', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const fetchMock = stubFetch({ status: 200, body: season() })

    render(<PulseWidget />)
    // Nothing is fetched until the panel is opened.
    expect(fetchMock).not.toHaveBeenCalled()

    const dialog = await openPanel()
    await within(dialog).findByText('South-west monsoon')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const url = new URL(String(fetchMock.mock.calls[0][0]))
    expect(url.origin).toBe(window.location.origin)
    expect(url.pathname).toBe('/api/pulse/season')
    expect(url.search).toBe('')

    // The old panel refreshed every 15 minutes while open.
    await act(async () => { vi.advanceTimersByTime(60 * 60 * 1000) })
    expect(fetchMock).toHaveBeenCalledTimes(1)

    for (const [input] of fetchMock.mock.calls) {
      expect(new URL(String(input)).origin).toBe(window.location.origin)
    }
    expect(geolocation.getCurrentPosition).not.toHaveBeenCalled()
    expect(geolocation.watchPosition).not.toHaveBeenCalled()
    expect(getItem.mock.calls.some(([key]) => key === 'orb_pulse_location')).toBe(false)
    expect(setItem.mock.calls.some(([key]) => key === 'orb_pulse_location')).toBe(false)
  })

  it('reads the calendar again each time it is opened', async () => {
    const fetchMock = stubFetch({ status: 200, body: season() })
    render(<PulseWidget />)
    let dialog = await openPanel()
    await within(dialog).findByText('South-west monsoon')

    await act(async () => { fireEvent.keyDown(dialog, { key: 'Escape' }) })
    dialog = await openPanel()
    await within(dialog).findByText('South-west monsoon')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('PulseWidget: keyboard and screen reader', () => {
  it('is a dialog the trigger announces, and Escape closes it and returns focus', async () => {
    stubFetch({ status: 200, body: season() })
    render(<PulseWidget />)
    expect(trigger()).toHaveAttribute('aria-expanded', 'false')
    expect(trigger()).toHaveAttribute('aria-haspopup', 'dialog')
    expect(trigger()).toHaveAttribute('id', 'btn-pulse')

    const dialog = await openPanel()
    await within(dialog).findByText('South-west monsoon')
    expect(dialog).toHaveClass('pulse-panel')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(trigger()).toHaveAttribute('aria-expanded', 'true')
    expect(trigger()).toHaveAttribute('aria-controls', dialog.id)
    // Focus moved into the panel.
    expect(dialog.contains(document.activeElement)).toBe(true)

    await act(async () => { fireEvent.keyDown(dialog, { key: 'Escape' }) })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(trigger()).toHaveAttribute('aria-expanded', 'false')
    expect(document.activeElement).toBe(trigger())
  })

  it('closes from the close button and from the backdrop', async () => {
    stubFetch({ status: 200, body: season() })
    const { container } = render(<PulseWidget />)

    let dialog = await openPanel()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Close Pulse' })) })
    expect(screen.queryByRole('dialog')).toBeNull()

    dialog = await openPanel()
    expect(dialog).toBeInTheDocument()
    await act(async () => { fireEvent.click(container.querySelector('.pulse-backdrop')!) })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('forgets the browser location the old Pulse saved', () => {
    localStorage.setItem('orb_pulse_location', JSON.stringify({ lat: 19.07, lon: 72.87, city: 'Mumbai' }))
    render(<PulseWidget />)
    expect(localStorage.getItem('orb_pulse_location')).toBeNull()
  })
})
