/**
 * The Cmd/Ctrl-K patient search is offered only to a role the server lets
 * search patients.
 *
 * GET /api/search/patients needs patient.read_all (server/routes/
 * search_routes.py). A cashier, a pharmacist and the hospital-system desk and
 * lab roles do not hold it, so the palette used to open for them and call a
 * search the server always refused. Now it does not open, claims no shortcut
 * and fetches nothing for them, and every clinical role is unchanged.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, apiFetch: vi.fn() }
})

const client = await import('../api/client')
const apiFetch = vi.mocked(client.apiFetch)

import CommandPalette from './CommandPalette'

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({
    id: `U-${role}`, name: `A ${role}`, role, department: null,
  }))
}

/** Cmd-K as a real keypress at the window. Returns whether the palette
 *  claimed it (preventDefault). */
function pressCmdK(): boolean {
  const event = new KeyboardEvent('keydown', { key: 'k', metaKey: true, bubbles: true, cancelable: true })
  act(() => { window.dispatchEvent(event) })
  return event.defaultPrevented
}

beforeEach(() => {
  apiFetch.mockReset()
  apiFetch.mockResolvedValue([])
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  localStorage.clear()
})

describe('roles that may search patients', () => {
  it.each(['doctor', 'nurse', 'admin', 'clinician'])('%s opens the palette and searches', async (role) => {
    vi.useFakeTimers()
    signIn(role)
    render(<CommandPalette />)

    expect(pressCmdK()).toBe(true)
    const input = screen.getByRole('combobox')
    expect(screen.getByRole('dialog', { name: 'Search patients' })).toBeInTheDocument()

    fireEvent.change(input, { target: { value: 'Ra' } })
    await act(async () => { vi.advanceTimersByTime(250) })
    expect(apiFetch).toHaveBeenCalledWith('/search/patients?q=Ra')
  })

  it('Ctrl-K works too, and pressing it again closes the palette', () => {
    signIn('nurse')
    render(<CommandPalette />)
    act(() => { fireEvent.keyDown(window, { key: 'K', ctrlKey: true }) })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    act(() => { fireEvent.keyDown(window, { key: 'k', ctrlKey: true }) })
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('roles the search route refuses', () => {
  it.each([
    'cashier', 'pharmacist', 'patient',
    'receptionist', 'ward_clerk', 'lab_technician', 'pathologist', 'phlebotomist',
  ])('%s gets no palette, no shortcut and no search call', async (role) => {
    vi.useFakeTimers()
    signIn(role)
    render(<CommandPalette />)

    expect(pressCmdK()).toBe(false)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('no stored session gets nothing either', () => {
    render(<CommandPalette />)
    expect(pressCmdK()).toBe(false)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(apiFetch).not.toHaveBeenCalled()
  })
})
