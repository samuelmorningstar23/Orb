/**
 * Sign-out from the expired-session modal reaches the server and does not
 * bring the modal back.
 *
 * App.logout() used to clear only local state, so the session cookie stayed
 * valid and the socket reconnected as the previous user on the next load. It
 * now posts /auth/logout first. That request answers 401 when the session has
 * already died (the very reason the modal is up), and that 401 must not latch
 * the expired-session modal a second time over the sign-in screen.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./pages/ClinicianShell', () => ({ default: () => <div>Clinician shell</div> }))
vi.mock('./pages/PatientShell', () => ({ default: () => <div>Patient shell</div> }))
vi.mock('./pages/Login', () => ({ default: () => <div>Sign-in screen</div> }))
vi.mock('./pages/DischargeAccess', () => ({ default: () => null }))
vi.mock('./pages/DowntimeMode', () => ({ default: () => null }))
vi.mock('./pages/HandoffPage', () => ({ default: () => null }))

import App from './AppRoot'

beforeEach(() => {
  // jsdom has no matchMedia; App's theme listener needs one.
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }))
  localStorage.clear()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'DOC001', name: 'Dr Rao', role: 'doctor' }))
  document.cookie = 'orb_csrf=tok-live; path=/'
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'orb_csrf=; Max-Age=0; path=/'
})

describe('App sign-out', () => {
  it('posts /auth/logout and does not re-latch the expired-session modal', async () => {
    const fetchMock = vi.fn(async () => new Response('{"detail":"Token expired"}', { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    expect(await screen.findByText('Clinician shell')).toBeInTheDocument()

    act(() => { window.dispatchEvent(new CustomEvent('orb:session-expired')) })
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/api\/auth\/logout$/)
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('tok-live')

    expect(await screen.findByText('Sign-in screen')).toBeInTheDocument()
    // Let the 401 settle, then check nothing brought the modal back.
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
    expect(localStorage.getItem('orb_user')).toBeNull()
    expect(document.cookie).not.toMatch(/orb_csrf=tok-live/)
  })
})
