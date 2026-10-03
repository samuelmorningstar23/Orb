/**
 * ProcurementPanel — the four things this screen must never get wrong.
 *
 * 1. THE HELIX BOUNDARY IS ON SCREEN BEFORE THE NETWORK ANSWERS. A storekeeper
 *    who cannot see which system owns drugs eventually creates a second row for
 *    a medication that already exists in Helix, and from that moment two
 *    ledgers disagree. The sentence must therefore survive a total network
 *    failure, which is exactly when a panel usually renders nothing.
 *
 * 2. A DISABLED MODULE IS NOT AN EMPTY STORE. When a site runs its own
 *    purchasing system the routes 404 with `module_disabled`. That must render
 *    as "not available at this site", never as a stock list with nothing in it —
 *    an empty stock list is a statement about the hospital's shelves.
 *
 * 3. AN UNPRICED ITEM READS AS WORDS. `₹0.00` is a price. "Not configured" is
 *    the absence of one. A screen that shows the first when it means the second
 *    is how a free item gets billed, or a stock value gets understated.
 *
 * 4. A PARTLY-UNPRICED TOTAL SAYS SO. A consumption total that silently omits
 *    the movements it could not value looks like a small month, not an
 *    incomplete one.
 *
 * Plus the over-receipt guard, which is checked in the browser as well as on the
 * server so the storekeeper is told at the field rather than by a 409.
 *
 * PROVEN ABLE TO FAIL (2026-08-12): each assertion was watched go red with its
 * defect reintroduced — the boundary literal replaced by the server-only value,
 * `isModuleDisabled` handling removed, `formatInrOrUnset` swapped for
 * `formatInr`, the `value_is_complete` banner deleted, and the client-side
 * outstanding check removed — then restored.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ProcurementPanel from './ProcurementPanel'
import { ProcurementApiError } from '../../api/procurement'

vi.mock('../../api/procurement', async () => {
  const actual = await vi.importActual<typeof import('../../api/procurement')>(
    '../../api/procurement',
  )
  return {
    ...actual,
    getMeta: vi.fn(),
    getStores: vi.fn(),
    getOverview: vi.fn(),
    getIndents: vi.fn(),
    getItems: vi.fn(),
    getVendors: vi.fn(),
    getPurchaseOrders: vi.fn(),
    getPurchaseOrder: vi.fn(),
    getStock: vi.fn(),
    getStockLedger: vi.fn(),
    getConsumption: vi.fn(),
    getIndent: vi.fn(),
    createGoodsReceipt: vi.fn(),
    saveItem: vi.fn(),
    saveVendor: vi.fn(),
  }
})

const api = await import('../../api/procurement')

const META = {
  item_categories: ['consumable', 'reagent', 'linen'],
  expiry_tracked_categories: ['consumable', 'implant', 'reagent', 'ppe'],
  vendor_types: ['supplier'],
  store_types: ['central'],
  movement_types: { inward: ['receipt'], outward: ['issue'], consumption: ['issue'] },
  transitions: { indent: {}, purchase_order: {}, goods_receipt: {} },
  receivable_po_statuses: ['issued', 'partially_received'],
  quantity_scale: 1000,
  pharmacy_boundary: {
    owner: 'helix',
    message: 'Medications are stocked in Helix (Pharmacy), not here.',
  },
}

const EMPTY_OVERVIEW = {
  low_stock: [], low_stock_count: 0,
  expiring: [], expiring_count: 0, expired_count: 0,
  open_purchase_orders: [], open_purchase_order_count: 0,
  open_purchase_order_value_paise: 0, open_purchase_order_value_display: '₹0.00',
  pending_indent_count: 0, draft_purchase_order_count: 0,
  active_item_count: 0, active_vendor_count: 0,
  pharmacy_boundary_note: 'Medications are not shown here. Drugs are held in Helix (Pharmacy).',
}

function stockRow(over: Partial<Record<string, unknown>> = {}) {
  return {
    item_code: 'GLOVE7', item_name: 'Sterile glove size 7', category: 'consumable',
    uom: 'box', store_code: 'CENTRAL', store_name: 'Central store',
    on_hand_milli: 4000, on_hand_display: '4 box',
    reorder_level_milli: 10000, reorder_level_display: '10 box',
    reorder_qty_milli: 50000, is_low: true,
    last_purchase_rate_paise: null, last_purchase_rate_display: 'Not configured',
    value_paise: null, value_display: 'Rate not configured',
    last_movement_at: '2026-08-10 09:00:00',
    ...over,
  }
}

beforeEach(() => {
  vi.mocked(api.getMeta).mockResolvedValue(META as never)
  vi.mocked(api.getStores).mockResolvedValue([
    { id: 1, code: 'CENTRAL', name: 'Central store', store_type: 'central',
      department: 'Central', ward: null, is_active: 1 },
  ] as never)
  vi.mocked(api.getOverview).mockResolvedValue(EMPTY_OVERVIEW as never)
  vi.mocked(api.getStock).mockResolvedValue([] as never)
  vi.mocked(api.getItems).mockResolvedValue([] as never)
  vi.mocked(api.getVendors).mockResolvedValue([] as never)
  vi.mocked(api.getIndents).mockResolvedValue([] as never)
  vi.mocked(api.getPurchaseOrders).mockResolvedValue([] as never)
})

// NOTE: no `document.body.innerHTML = ''` here. Testing Library's own cleanup
// unmounts the tree, and the drawer is a React PORTAL into document.body —
// wiping the body by hand pulls the portal node out from under React, which
// then throws "the node to be removed is not a child of this node" on the next
// unmount. Two of these tests deliberately end with the drawer still open.
afterEach(() => {
  vi.clearAllMocks()
})

/* ═══════════════════════════════════════════════════════════════════════════
   1. The Helix boundary
   ═══════════════════════════════════════════════════════════════════════════ */

describe('the pharmacy boundary', () => {
  it('states that drugs live in Helix before any request resolves', async () => {
    // Deliberately no `await`: this asserts the FIRST paint, with every promise
    // still pending. The one message that must never be missing cannot be one
    // that depends on the network being up.
    render(<ProcurementPanel />)
    const notice = screen.getByText(/Medications are not stocked here/i)
    expect(notice).toBeInTheDocument()
    expect(notice.textContent).toMatch(/Helix/)
    // Settle the in-flight fetches so React's act() bookkeeping is clean; the
    // assertion above already ran against the very first paint.
    await waitFor(() => expect(api.getMeta).toHaveBeenCalled())
  })

  it('still states it when every request fails', async () => {
    vi.mocked(api.getMeta).mockRejectedValue(new Error('network down'))
    vi.mocked(api.getStores).mockRejectedValue(new Error('network down'))
    vi.mocked(api.getOverview).mockRejectedValue(new Error('network down'))

    render(<ProcurementPanel />)
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/network down/i))
    expect(screen.getByText(/Helix/)).toBeInTheDocument()
  })

  it('prefers the server’s own wording once it has it', async () => {
    render(<ProcurementPanel />)
    await waitFor(() =>
      expect(screen.getByText(/Medications are stocked in Helix \(Pharmacy\), not here/i))
        .toBeInTheDocument())
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   2. A disabled module is not an empty store
   ═══════════════════════════════════════════════════════════════════════════ */

describe('when the site has switched procurement off', () => {
  it('says the module is not available, and shows no stock UI at all', async () => {
    const disabled = new ProcurementApiError(
      'The procurement & stores module is switched off for this deployment '
      + '(ORB_PROCUREMENT=0). This site runs its own purchasing system.',
      404, 'module_disabled',
    )
    vi.mocked(api.getMeta).mockRejectedValue(disabled)
    vi.mocked(api.getStores).mockRejectedValue(disabled)
    vi.mocked(api.getOverview).mockRejectedValue(disabled)

    render(<ProcurementPanel />)

    await waitFor(() =>
      expect(screen.getByText(/Not available at this site/i)).toBeInTheDocument())
    expect(screen.getByText(/ORB_PROCUREMENT=0/)).toBeInTheDocument()
    // The critical half: an empty stock table would be a statement about the
    // hospital's shelves. There must not be one.
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stock/i })).not.toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   3. Not configured is not zero
   ═══════════════════════════════════════════════════════════════════════════ */

describe('money honesty', () => {
  it('renders an unpriced item as words, never as ₹0.00', async () => {
    vi.mocked(api.getStock).mockResolvedValue([stockRow()] as never)
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Stock' }))
    const table = await screen.findByRole('table')
    const row = within(table).getByText('GLOVE7 · Consumable').closest('tr') as HTMLElement

    expect(within(row).getByText('Not configured')).toBeInTheDocument()
    expect(within(row).getByText('Rate not configured')).toBeInTheDocument()
    expect(within(row).queryByText('₹0.00')).not.toBeInTheDocument()
  })

  it('does not present a stock value as complete when some item has no rate', async () => {
    vi.mocked(api.getStock).mockResolvedValue([
      stockRow(),
      stockRow({ item_code: 'SUTURE30', item_name: 'Suture 3-0',
                 value_paise: 250000, value_display: '₹2,500.00',
                 last_purchase_rate_paise: 25000,
                 last_purchase_rate_display: '₹250.00', is_low: false }),
    ] as never)
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Stock' }))
    // NOT "₹2,500.00" — that would read as the whole store's value when it is
    // only the part that could be valued.
    expect(await screen.findByText('Partly unpriced')).toBeInTheDocument()
    expect(screen.getByText(/1 item has no purchase rate yet/i)).toBeInTheDocument()
  })

  it('flags an incomplete consumption total rather than showing a small number',
     async () => {
    vi.mocked(api.getConsumption).mockResolvedValue({
      date_from: null, date_to: null,
      lines: [{
        department: 'Surgery', store_code: 'CENTRAL', item_code: 'GLOVE7',
        item_name: 'Sterile glove size 7', category: 'consumable', uom: 'box',
        quantity_milli: 2000, quantity_display: '2 box',
        value_paise: 0, value_display: 'Partly unpriced',
        unvalued_movement_count: 1, movement_count: 1,
      }],
      departments: [{ department: 'Surgery', value_paise: 0,
                      value_display: 'Partly unpriced', line_count: 1,
                      unvalued_movement_count: 1 }],
      total_value_paise: 0, total_value_display: 'Partly unpriced',
      unvalued_movement_count: 1, value_is_complete: false,
    } as never)
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Consumption' }))
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent(/1 movement has no purchase rate/i)
    expect(banner).toHaveTextContent(/incomplete, not low/i)
    expect(screen.queryByText('₹0.00')).not.toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   4. Low stock and the reorder flag
   ═══════════════════════════════════════════════════════════════════════════ */

describe('low stock', () => {
  it('marks a low row with a word, not only a colour', async () => {
    vi.mocked(api.getStock).mockResolvedValue([stockRow()] as never)
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Stock' }))
    const table = await screen.findByRole('table')
    const row = within(table).getByText('GLOVE7 · Consumable').closest('tr') as HTMLElement
    // A colour-blind storekeeper reads the word. The border is decoration.
    expect(within(row).getByText('Reorder')).toBeInTheDocument()
  })

  it('explains that an item with no reorder level is unconfigured, not fine',
     async () => {
    render(<ProcurementPanel />)
    expect(await screen.findByText(/is not 'fine'. It is unconfigured/i))
      .toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   5. Over-receipt is caught at the field, not only by a 409
   ═══════════════════════════════════════════════════════════════════════════ */

describe('receiving goods', () => {
  const PO = {
    id: 7, po_number: 'PO-0007', vendor_code: 'ACME', vendor_name: 'Acme Surgical',
    vendor_state_code: '27', store_code: 'CENTRAL', status: 'issued',
    order_date: '2026-08-01', expected_date: '2026-08-09', is_interstate: 0,
    subtotal_paise: 100000, discount_paise: 0, taxable_value_paise: 100000,
    cgst_paise: 6000, sgst_paise: 6000, igst_paise: 0, tax_paise: 12000,
    other_charges_paise: 0, rounding_adjustment_paise: 0, total_paise: 112000,
    outstanding_quantity_milli: 10000, is_fully_received: false, can_receive: true,
    legal_transitions: ['partially_received', 'received', 'cancelled', 'closed'],
    lines: [{
      id: 1, line_no: 1, item_code: 'GLOVE7', description: 'Sterile glove size 7',
      hsn_sac: '4015', uom: 'box',
      quantity_milli: 10000, received_quantity_milli: 0,
      outstanding_quantity_milli: 10000,
      quantity_display: '10 box', received_quantity_display: '0 box',
      outstanding_quantity_display: '10 box',
      unit_rate_paise: 10000, gross_paise: 100000, discount_paise: 0,
      gst_treatment: 'taxable' as const, gst_rate_bp: 1200,
      taxable_value_paise: 100000, cgst_paise: 6000, sgst_paise: 6000,
      igst_paise: 0, tax_paise: 12000, total_paise: 112000, status: 'open',
    }],
  }

  it('refuses an over-receipt in the browser and never calls the server', async () => {
    vi.mocked(api.getPurchaseOrders).mockResolvedValue([PO] as never)
    vi.mocked(api.getPurchaseOrder).mockResolvedValue(PO as never)
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Purchase orders' }))
    fireEvent.click(await screen.findByRole('button', { name: /PO-0007/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Receive goods' }))

    fireEvent.change(await screen.findByLabelText('Accepted quantity for GLOVE7'),
                     { target: { value: '11' } })
    fireEvent.click(screen.getByRole('button', { name: 'Post receipt' }))

    expect(await screen.findByRole('alert'))
      .toHaveTextContent(/would over-receive the order/i)
    expect(api.createGoodsReceipt).not.toHaveBeenCalled()
  })

  it('accepts a partial delivery and sends the quantity in milli', async () => {
    vi.mocked(api.getPurchaseOrders).mockResolvedValue([PO] as never)
    vi.mocked(api.getPurchaseOrder).mockResolvedValue(PO as never)
    vi.mocked(api.createGoodsReceipt).mockResolvedValue(
      { id: 1, status: 'posted' } as never)
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Purchase orders' }))
    fireEvent.click(await screen.findByRole('button', { name: /PO-0007/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Receive goods' }))
    fireEvent.change(await screen.findByLabelText('Accepted quantity for GLOVE7'),
                     { target: { value: '6' } })
    fireEvent.click(screen.getByRole('button', { name: 'Post receipt' }))

    await waitFor(() => expect(api.createGoodsReceipt).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(api.createGoodsReceipt).mock.calls[0][0]
    expect(payload.purchase_order_id).toBe(7)
    // 6 boxes is 6000 milli, not 6 and not 6.0 — the wire is integer thousandths.
    expect(payload.lines[0].accepted_quantity_milli).toBe(6000)
    expect(Number.isInteger(payload.lines[0].accepted_quantity_milli)).toBe(true)
    // Retry-safe: a double-tap on a slow connection must replay, not double-post.
    expect(payload.idempotency_key).toBeTruthy()
  })

  it('demands a reason before it will record a rejection', async () => {
    vi.mocked(api.getPurchaseOrders).mockResolvedValue([PO] as never)
    vi.mocked(api.getPurchaseOrder).mockResolvedValue(PO as never)
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Purchase orders' }))
    fireEvent.click(await screen.findByRole('button', { name: /PO-0007/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Receive goods' }))
    fireEvent.change(await screen.findByLabelText('Rejected quantity for GLOVE7'),
                     { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Post receipt' }))

    expect(await screen.findByRole('alert'))
      .toHaveTextContent(/say why the 2 rejected were rejected/i)
    expect(api.createGoodsReceipt).not.toHaveBeenCalled()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   F103: the item master refuses a medicine, and says so in the drawer
   ═══════════════════════════════════════════════════════════════════════════ */

describe('adding an item that is a medicine', () => {
  it('shows the server refusal inside the drawer and keeps the form open', async () => {
    const sentence = '"Amoxicillin 500 mg" matches the medication "Amoxicillin 500mg Oral Capsule" in Helix (pharmacy), so it was not added.'
    vi.mocked(api.saveItem).mockRejectedValue(
      new ProcurementApiError(sentence, 422, 'pharmacy_item_refused'))
    render(<ProcurementPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Items & vendors' }))
    // The hint no longer claims a database refusal that never happened.
    expect(screen.getByText(/matches a medicine in the Helix/i)).toBeInTheDocument()
    expect(screen.queryByText(/The database itself refuses/i)).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: 'Add an item' }))
    const drawer = await screen.findByRole('dialog', { name: 'Add a non-pharmacy item' })
    fireEvent.change(within(drawer).getByLabelText('Code'), { target: { value: 'AMOX' } })
    fireEvent.change(within(drawer).getByLabelText('Name'), { target: { value: 'Amoxicillin 500 mg' } })
    fireEvent.click(within(drawer).getByRole('button', { name: 'Save item' }))

    await waitFor(() => expect(api.saveItem).toHaveBeenCalledTimes(1))
    expect(await within(drawer).findByRole('alert')).toHaveTextContent(sentence)
    expect(screen.getByRole('dialog', { name: 'Add a non-pharmacy item' })).toBeInTheDocument()
  })
})


/* ═══════════════════════════════════════════════════════════════════════════
   F098: Add creates, it never overwrites
   ═══════════════════════════════════════════════════════════════════════════ */

describe('adding a code that is already in use', () => {
  it('sends create_only for an item', async () => {
    vi.mocked(api.saveItem).mockResolvedValue({} as never)
    render(<ProcurementPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Items & vendors' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Add an item' }))
    const drawer = await screen.findByRole('dialog', { name: 'Add a non-pharmacy item' })
    fireEvent.change(within(drawer).getByLabelText('Code'), { target: { value: 'GAUZE10' } })
    fireEvent.change(within(drawer).getByLabelText('Name'), { target: { value: 'Gauze swab' } })
    fireEvent.click(within(drawer).getByRole('button', { name: 'Save item' }))
    await waitFor(() => expect(api.saveItem).toHaveBeenCalledWith(
      expect.objectContaining({ code: 'GAUZE10', create_only: true })))
  })

  it('shows a vendor code refusal inside the drawer, which stays open', async () => {
    const sentence = 'LIFE01 already exists as Lifeline Surgicals. Nothing was changed.'
    vi.mocked(api.saveVendor).mockRejectedValue(
      new ProcurementApiError(sentence, 409, 'already_exists'))
    render(<ProcurementPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Items & vendors' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Add a vendor' }))
    const drawer = await screen.findByRole('dialog', { name: 'Add a vendor' })
    fireEvent.change(within(drawer).getByLabelText('Code'), { target: { value: 'life01' } })
    fireEvent.change(within(drawer).getByLabelText('Name'), { target: { value: 'Another' } })
    fireEvent.click(within(drawer).getByRole('button', { name: 'Save vendor' }))
    await waitFor(() => expect(api.saveVendor).toHaveBeenCalledWith(
      expect.objectContaining({ code: 'LIFE01', create_only: true })))
    expect(await within(drawer).findByRole('alert')).toHaveTextContent(sentence)
    expect(screen.getByRole('dialog', { name: 'Add a vendor' })).toBeInTheDocument()
  })
})
