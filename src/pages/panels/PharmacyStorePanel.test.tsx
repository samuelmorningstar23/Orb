/**
 * PharmacyStorePanel: the shell of an opt-in module whose screens arrive one
 * file at a time. What it must never get wrong:
 *
 * 1. SWITCHED OFF IS NOT EMPTY. A 404 module_disabled is said in words and is
 *    never drawn as a store with nothing in it.
 * 2. NO SCREENS YET IS SAID HONESTLY, and says where drug stock is kept
 *    meanwhile, rather than rendering a blank frame.
 * 3. A VIEW GATED ON A PERMISSION IS HIDDEN unless the permission is held, and
 *    a permission the client mirror does not know reads as NOT held.
 * 4. THE PANEL NAMES NO VIEW. Views come in as entries (from the glob in the
 *    real build), in their declared order, and can switch to one another.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PharmacyStorePanel from './PharmacyStorePanel'
import {
  discoverViews,
  type PharmacyStoreViewEntry,
  type PharmacyStoreViewProps,
} from './pharmacyStore/viewRegistry'
import { PharmacyStoreApiError } from '../../api/pharmacyStore'

vi.mock('../../api/pharmacyStore', async () => {
  const actual = await vi.importActual<typeof import('../../api/pharmacyStore')>(
    '../../api/pharmacyStore',
  )
  return { ...actual, getPharmacyStoreMeta: vi.fn() }
})

const api = await import('../../api/pharmacyStore')
const getMeta = vi.mocked(api.getPharmacyStoreMeta)

function OverviewView({ meta, openView }: PharmacyStoreViewProps) {
  return (
    <div>
      <p>Overview body, near expiry window {String(meta.near_expiry_days)}</p>
      <button type="button" onClick={() => openView('receipts')}>Go to receipts</button>
      <button type="button" onClick={() => openView('not-installed')}>Go nowhere</button>
    </div>
  )
}

function ReceiptsView({ onModuleOff }: PharmacyStoreViewProps) {
  return (
    <div>
      <p>Receipts body</p>
      <button type="button" onClick={onModuleOff}>Report the module switched off</button>
    </div>
  )
}

function NdpsView() {
  return <p>NDPS register body</p>
}

/** Built the way the real glob result is, so the registry is exercised too. */
const VIEWS: PharmacyStoreViewEntry[] = discoverViews({
  './pharmacyStore/ReceiptsView.tsx': { default: ReceiptsView, viewOrder: 30 },
  './pharmacyStore/OverviewView.tsx': { default: OverviewView, viewOrder: 10 },
  './pharmacyStore/NdpsView.tsx': {
    default: NdpsView, viewOrder: 80, viewLabel: 'NDPS register',
    viewPermission: 'pharmacy_store.ndps',
  },
})

const holdsEverything = () => true
const holdsNothing = () => false

beforeEach(() => {
  getMeta.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('PharmacyStorePanel', () => {
  it('names itself and probes the module before showing anything else', async () => {
    let answer: (value: Record<string, unknown>) => void = () => {}
    getMeta.mockReturnValue(new Promise(resolve => { answer = resolve }))
    render(<PharmacyStorePanel views={[]} />)

    expect(screen.getByRole('heading', { level: 1, name: 'Pharmacy store' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Loading the pharmacy store')
    expect(screen.queryByText('No pharmacy store screens yet')).not.toBeInTheDocument()

    answer({ module: 'pharmacy-store' })
    expect(await screen.findByText('No pharmacy store screens yet')).toBeInTheDocument()
    expect(getMeta).toHaveBeenCalledTimes(1)
  })

  it('says honestly that no screens are installed, and where drug stock lives', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    render(<PharmacyStorePanel views={[]} />)

    expect(await screen.findByText('No pharmacy store screens yet')).toBeInTheDocument()
    expect(screen.getByText(/drug stock and dispensing\s+are kept in the Pharmacy screen/i))
      .toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('shows a switched-off module as switched off, never as an empty store', async () => {
    getMeta.mockRejectedValue(new PharmacyStoreApiError(
      'switched off', 404, 'module_disabled', { error: 'module_disabled' }))
    render(<PharmacyStorePanel views={VIEWS} holds={holdsEverything} />)

    expect(await screen.findByRole('heading', {
      name: 'The pharmacy store is switched off on this site',
    })).toBeInTheDocument()
    expect(screen.queryByText('No pharmacy store screens yet')).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    expect(screen.queryByText('Overview body', { exact: false })).not.toBeInTheDocument()
  })

  it('maps a raw apiFetch 404 module_disabled the same way', async () => {
    getMeta.mockRejectedValue(Object.assign(new Error('off'), {
      status: 404, detail: { error: 'module_disabled', message: 'off' },
    }))
    render(<PharmacyStorePanel views={[]} />)
    expect(await screen.findByText('The pharmacy store is switched off on this site'))
      .toBeInTheDocument()
  })

  it('says when the server cannot be reached, and tries again on request', async () => {
    getMeta.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    getMeta.mockResolvedValueOnce({ module: 'pharmacy-store' })
    render(<PharmacyStorePanel views={[]} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not reach the pharmacy store. Check the connection and try again.')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('No pharmacy store screens yet')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(getMeta).toHaveBeenCalledTimes(2)
  })

  it('offers installed views as tabs in their declared order, first one open', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store', near_expiry_days: 90 })
    render(<PharmacyStorePanel views={VIEWS} holds={holdsEverything} />)

    const nav = await screen.findByRole('navigation', { name: 'Pharmacy store screens' })
    const labels = Array.from(nav.querySelectorAll('button')).map(b => b.textContent)
    expect(labels).toEqual(['Overview', 'Receipts', 'NDPS register'])
    expect(screen.getByRole('button', { name: 'Overview' })).toHaveAttribute('aria-current', 'page')
    // The view is handed the meta the panel read.
    expect(screen.getByText('Overview body, near expiry window 90')).toBeInTheDocument()
  })

  it('switches views from the tab strip and from inside a view', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    render(<PharmacyStorePanel views={VIEWS} holds={holdsEverything} />)

    fireEvent.click(await screen.findByRole('button', { name: 'NDPS register' }))
    expect(screen.getByText('NDPS register body')).toBeInTheDocument()
    expect(screen.queryByText('Receipts body')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
    fireEvent.click(screen.getByRole('button', { name: 'Go to receipts' }))
    expect(screen.getByText('Receipts body')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Receipts' })).toHaveAttribute('aria-current', 'page')
  })

  it('replaces every view with the switched-off notice when a view reports it', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    render(<PharmacyStorePanel views={VIEWS} holds={holdsEverything} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Receipts' }))
    fireEvent.click(screen.getByRole('button', { name: 'Report the module switched off' }))

    expect(screen.getByText('The pharmacy store is switched off on this site')).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    expect(screen.queryByText('Receipts body')).not.toBeInTheDocument()
  })

  it('ignores a request to open a view that is not installed', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    render(<PharmacyStorePanel views={VIEWS} holds={holdsEverything} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Go nowhere' }))
    expect(screen.getByText('Overview body', { exact: false })).toBeInTheDocument()
  })

  it('hides a view whose permission the user does not hold', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    render(<PharmacyStorePanel views={VIEWS}
      holds={(permission) => permission !== 'pharmacy_store.ndps'} />)

    await screen.findByRole('navigation')
    expect(screen.queryByRole('button', { name: 'NDPS register' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Receipts' })).toBeInTheDocument()
  })

  it('reads a permission the client mirror does not carry as not held', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    // Default `holds`: the roles.ts mirror, with nobody signed in.
    render(<PharmacyStorePanel views={VIEWS} />)

    await screen.findByRole('navigation')
    expect(screen.queryByRole('button', { name: 'NDPS register' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Overview' })).toBeInTheDocument()
  })

  it('says so when views exist but none is open to this role', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    const gated = VIEWS.map(view => ({ ...view, permission: 'pharmacy_store.read' }))
    render(<PharmacyStorePanel views={gated} holds={holdsNothing} />)

    expect(await screen.findByText('Nothing here for your role')).toBeInTheDocument()
    expect(screen.queryByText('No pharmacy store screens yet')).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('mounts with no props, as the shell mounts it', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    render(<PharmacyStorePanel />)
    await waitFor(() => expect(getMeta).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('heading', { level: 1, name: 'Pharmacy store' })).toBeInTheDocument()
  })

  it('never calls anything a demo or a mock', async () => {
    getMeta.mockResolvedValue({ module: 'pharmacy-store' })
    const { container } = render(<PharmacyStorePanel views={[]} />)
    await screen.findByText('No pharmacy store screens yet')
    expect(container.textContent).not.toMatch(/\b(demo|mock)\b/i)
  })
})
