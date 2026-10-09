/**
 * Front Desk, Display boards (opd-9).
 *
 * The administrator makes a board for one clinic and sees its link once, with
 * the key in the fragment; the list never shows a key; switching a board off
 * needs a second tap; with outpatients switched off the screen says so in
 * words and the desk keeps its other views.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('../../../api/client', () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}))

import DisplayBoardsView, { viewId, viewLabel, viewOrder, viewPermission } from './DisplayBoardsView'

const BOARD = {
  id: 'BRD-1', department: 'General Medicine', label: 'Medicine waiting hall',
  created_by: 'ADM001', created_by_name: 'Asha Admin', created_at: '2026-10-09T04:00:00Z',
  revoked_by: null as string | null, revoked_by_name: null as string | null,
  revoked_at: null as string | null, active: true,
}

let boards = [BOARD]

beforeEach(() => {
  boards = [BOARD]
  apiFetch.mockReset()
  apiFetch.mockImplementation((path: string, init?: RequestInit) => {
    if (path === '/frontdesk/opd/boards' && !init?.method) {
      return Promise.resolve({ boards, departments: ['General Medicine', 'Cardiology'], poll_seconds: 10 })
    }
    if (path === '/frontdesk/opd/boards' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      return Promise.resolve({
        board: { ...BOARD, id: 'BRD-2', department: body.department, label: body.label ?? 'Cardiology waiting area' },
        key: 'secret+key/1',
      })
    }
    if (path === '/frontdesk/opd/boards/BRD-1/revoke') {
      boards = [{ ...BOARD, active: false, revoked_at: '2026-10-09T06:00:00Z' }]
      return Promise.resolve(boards[0])
    }
    return Promise.reject(new Error(`unexpected ${path}`))
  })
})

afterEach(() => cleanup())

function renderView(onFeatureOff = vi.fn()) {
  render(<DisplayBoardsView scopedDepartment={null} openView={() => {}} onFeatureOff={onFeatureOff} />)
  return onFeatureOff
}

describe('Display boards', () => {
  it('is discovered as the administrator-only Display boards view', () => {
    expect(viewId).toBe('display-boards')
    expect(viewLabel).toBe('Display boards')
    expect(viewOrder).toBe(50)
    expect(viewPermission).toBe('schedule.manage')
  })

  it('lists the boards without any key', async () => {
    renderView()
    expect(await screen.findByText('Medicine waiting hall')).toBeInTheDocument()
    expect(screen.getByText('Showing tokens')).toBeInTheDocument()
    expect(document.body.innerHTML).not.toMatch(/key_hash|#key=/)
  })

  it('makes a board and shows its link once, with the key in the fragment', async () => {
    renderView()
    await screen.findByText('Medicine waiting hall')
    fireEvent.change(screen.getByLabelText('Clinic'), { target: { value: 'Cardiology' } })
    fireEvent.change(screen.getByLabelText(/Where the screen is/), { target: { value: 'Heart clinic hall' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Make board' })) })
    const posted = apiFetch.mock.calls.find(([p, i]) => p === '/frontdesk/opd/boards' && i?.method === 'POST')
    expect(JSON.parse(String(posted?.[1]?.body))).toEqual({ department: 'Cardiology', label: 'Heart clinic hall' })
    const link = await screen.findByTestId('board-link')
    expect(link.textContent).toBe(`${window.location.origin}/display/opd#key=secret%2Bkey%2F1`)
    expect(link.textContent).not.toContain('?')
    fireEvent.click(screen.getByRole('button', { name: 'Done, the link is on the screen' }))
    expect(screen.queryByTestId('board-link')).toBeNull()
    expect(document.body.innerHTML).not.toContain('secret')
  })

  it('switches a board off only after a second tap', async () => {
    renderView()
    await screen.findByText('Medicine waiting hall')
    fireEvent.click(screen.getByRole('button', { name: 'Switch off' }))
    expect(apiFetch.mock.calls.some(([p]) => String(p).endsWith('/revoke'))).toBe(false)
    const table = screen.getByRole('table')
    await act(async () => {
      fireEvent.click(within(table).getByRole('button', { name: 'Switch off Medicine waiting hall' }))
    })
    await waitFor(() => expect(screen.getByText(/^Switched off/)).toBeInTheDocument())
  })

  it('says outpatients is switched off, and keeps the desk', async () => {
    apiFetch.mockImplementation(() => Promise.reject(Object.assign(new Error('off'), {
      status: 404, detail: { error: 'feature_disabled', feature: 'opd' },
    })))
    const onFeatureOff = renderView()
    expect(await screen.findByText(/need Outpatients switched on/)).toBeInTheDocument()
    expect(onFeatureOff).not.toHaveBeenCalled()
  })

  it('hands back to the desk when registration is switched off', async () => {
    apiFetch.mockImplementation(() => Promise.reject(Object.assign(new Error('off'), {
      status: 404, detail: { error: 'feature_disabled', feature: 'registration' },
    })))
    const onFeatureOff = renderView()
    await waitFor(() => expect(onFeatureOff).toHaveBeenCalled())
  })
})
