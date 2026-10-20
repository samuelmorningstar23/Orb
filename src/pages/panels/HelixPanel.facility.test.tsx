/**
 * Helix names the facility whose stock it shows (multisite-14).
 *
 * Once more than one facility is on record, pharmacy stock is per facility
 * (ruling MS-R9 (a)) and GET /helix/inventory names the facility the
 * pharmacist works at. The header says whose stock it is. On every other site
 * the answer carries no facility, and the header is exactly as before.
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string) => apiFetch(path),
  getHelixOrderSafety: vi.fn(),
  isSafetyAckRequired: () => false,
  transitionHelixOrder: vi.fn(),
}))

import HelixPanel from './HelixPanel'

let facility: unknown = undefined

beforeEach(() => {
  facility = undefined
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string) => {
    if (path === '/helix/orders') return { orders: [] }
    if (path === '/helix/inventory') {
      return facility === undefined ? { inventory: [] } : { inventory: [], facility }
    }
    if (path === '/helix/catalog') return { catalog: [] }
    throw new Error(`unexpected ${path}`)
  })
})

describe('HelixPanel facility label', () => {
  it('says whose stock it is when the server names the facility', async () => {
    facility = { id: 9871, code: 'QMS', name: 'Quellmarsh Satellite' }
    render(<HelixPanel />)
    const label = await screen.findByTestId('helix-stock-facility')
    expect(label).toHaveTextContent('Quellmarsh Satellite')
    expect(screen.getByText(/stock at/)).toBeInTheDocument()
  })

  it('falls back to the facility code when the facility has no name', async () => {
    facility = { id: 1, code: 'MAIN', name: null }
    render(<HelixPanel />)
    expect(await screen.findByTestId('helix-stock-facility')).toHaveTextContent('MAIN')
  })

  it('shows no facility on a site with one facility (no key in the answer)', async () => {
    render(<HelixPanel />)
    expect(await screen.findByText(/Medication orders, verification/)).toBeInTheDocument()
    expect(screen.queryByTestId('helix-stock-facility')).toBeNull()
    expect(screen.queryByText(/stock at/)).toBeNull()
  })

  it('shows no facility for a group-wide account (a null facility)', async () => {
    facility = null
    render(<HelixPanel />)
    expect(await screen.findByText(/Medication orders, verification/)).toBeInTheDocument()
    expect(screen.queryByTestId('helix-stock-facility')).toBeNull()
  })
})
