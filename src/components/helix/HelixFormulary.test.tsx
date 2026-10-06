/**
 * Formulary stock: Available counts only what a dispense could take (in-date
 * lots with units left). It used to sum every lot, so expired stock read as
 * "In Stock" while the dispense refused it.
 *
 * Counts are named by the pack ("tablets", "vials"), never "units": on heparin
 * and insulin a unit is a dose, so "40 units" read as a dose on the shelf.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import HelixFormulary from './HelixFormulary'
import { localDateKey } from '../../lib/datetime'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'
import { isLowStock, serverReorderLevel } from './stock'

function daysFromNow(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() + n)
  return localDateKey(d)
}

const catalog = [
  { id: 'MED-A', name: 'Paracetamol 650mg Oral Tablet', generic_name: 'Paracetamol', form: 'Tablet', strength: '650mg' },
]

function lot(id: string, quantity: number, expiration_date: string | null, in_date?: number) {
  return { id, medication_id: 'MED-A', lot_number: id, quantity, expiration_date, name: 'Paracetamol', strength: '650mg', in_date }
}

describe('HelixFormulary', () => {
  it('expired and undated lots are not available stock', () => {
    render(<HelixFormulary catalog={catalog} inventory={[
      lot('LOT-OLD', 120, daysFromNow(-10), 0),
      lot('LOT-NODATE', 30, null, 0),
    ]} />)
    expect(screen.getByText('0 tablets')).toBeInTheDocument()
    expect(screen.getByText('Out of Stock')).toBeInTheDocument()
    expect(screen.getByText('150 tablets expired or undated, not counted')).toBeInTheDocument()
    expect(screen.getByText(/LOT-OLD: 120 tablets, exp .*\. Expired, not dispensable/)).toBeInTheDocument()
    expect(screen.getByText(/LOT-NODATE: 30 tablets, no expiry recorded\. No expiry recorded, not dispensable/)).toBeInTheDocument()
  })

  it('counts in-date lots, flags one expiring within 90 days, and hides empty lots', () => {
    render(<HelixFormulary catalog={catalog} inventory={[
      lot('LOT-SOON', 20, daysFromNow(30), 1),
      lot('LOT-LATER', 100, daysFromNow(400), 1),
      lot('LOT-EMPTY', 0, daysFromNow(400), 1),
    ]} />)
    expect(screen.getByText('120 tablets')).toBeInTheDocument()
    expect(screen.getByText('In Stock')).toBeInTheDocument()
    expect(screen.getByText(/LOT-SOON: 20 tablets, exp .*\. Expires within 90 days/)).toBeInTheDocument()
    expect(screen.getByText(/LOT-LATER: 100 tablets, exp /)).toBeInTheDocument()
    expect(screen.queryByText(/LOT-EMPTY/)).not.toBeInTheDocument()
  })

  it('follows the server verdict over its own clock when one is given', () => {
    // The server says the lot is out of date (its cutoff may already be
    // tomorrow in the hospital's zone); the screen must not count it.
    render(<HelixFormulary catalog={catalog} inventory={[lot('LOT-EDGE', 40, daysFromNow(0), 0)]} />)
    expect(screen.getByText('0 tablets')).toBeInTheDocument()
  })

  it('names stock by the pack, never "units", with Indian digit grouping', () => {
    const heparin = [{ id: 'MED-H', name: 'Heparin 5000 IU/mL Injection', generic_name: 'Heparin', form: 'Vial', strength: '5000 IU/mL' }]
    render(<HelixFormulary catalog={heparin} inventory={[
      { ...lot('LOT-H1', 120000, daysFromNow(400), 1), medication_id: 'MED-H' },
      { ...lot('LOT-H2', 1, daysFromNow(400), 1), medication_id: 'MED-H' },
    ]} />)
    expect(screen.getByText('1,20,001 vials')).toBeInTheDocument()
    expect(screen.getByText(/LOT-H2: 1 vial, exp /)).toBeInTheDocument()
    expect(screen.queryByText(/units/)).not.toBeInTheDocument()
  })

  it('falls back to "packs" for a form it has no noun for', () => {
    const odd = [{ id: 'MED-X', name: 'Something', generic_name: 'Something', form: 'Lozenge', strength: '' }]
    render(<HelixFormulary catalog={odd} inventory={[{ ...lot('LOT-X', 12, daysFromNow(400), 1), medication_id: 'MED-X' }]} />)
    expect(screen.getByText('12 packs')).toBeInTheDocument()
  })
})

/**
 * Pharmacy store reorder levels (grn-9). The drug master's reorder level
 * replaces the fixed 30 only while the pharmacy store module is on; with it
 * off the screen reads exactly as before, whatever levels are stored, and a
 * level nobody set is never read as 0.
 */
describe('HelixFormulary reorder level', () => {
  // Unmount first, so resetting the flags notifies no mounted screen.
  afterEach(() => { cleanup(); resetModuleFlags() })

  const levelled = [{ ...catalog[0], reorder_level_units: 100 }]

  it('ignores a stored level while the pharmacy store is off', () => {
    render(<HelixFormulary catalog={levelled} inventory={[lot('LOT-1', 50, daysFromNow(400), 1)]} />)
    expect(screen.getByText('In Stock')).toBeInTheDocument()
  })

  it('reads low at or below the server level while the pharmacy store is on', () => {
    setModuleFlags({ 'pharmacy-store': true })
    render(<HelixFormulary catalog={levelled} inventory={[lot('LOT-1', 100, daysFromNow(400), 1)]} />)
    expect(screen.getByText('Low Stock')).toBeInTheDocument()
  })

  it('keeps the fixed 30 when the store is on but no level is set', () => {
    setModuleFlags({ 'pharmacy-store': true })
    const unset = [{ ...catalog[0], reorder_level_units: null }]
    const { unmount } = render(<HelixFormulary catalog={unset} inventory={[lot('LOT-1', 29, daysFromNow(400), 1)]} />)
    expect(screen.getByText('Low Stock')).toBeInTheDocument()
    unmount()
    render(<HelixFormulary catalog={unset} inventory={[lot('LOT-2', 30, daysFromNow(400), 1)]} />)
    expect(screen.getByText('In Stock')).toBeInTheDocument()
  })

  it('an explicit level of 0 is a real level, and out of stock stays out', () => {
    setModuleFlags({ 'pharmacy-store': true })
    const zero = [{ ...catalog[0], reorder_level_units: 0 }]
    const { unmount } = render(<HelixFormulary catalog={zero} inventory={[lot('LOT-1', 5, daysFromNow(400), 1)]} />)
    expect(screen.getByText('In Stock')).toBeInTheDocument()
    unmount()
    render(<HelixFormulary catalog={levelled} inventory={[]} />)
    expect(screen.getByText('Out of Stock')).toBeInTheDocument()
  })

  it('the helpers never read an unset level as 0', () => {
    expect(serverReorderLevel({ reorder_level_units: null }, true)).toBeNull()
    expect(serverReorderLevel({}, true)).toBeNull()
    expect(serverReorderLevel({ reorder_level_units: 40 }, false)).toBeNull()
    expect(serverReorderLevel({ reorder_level_units: 40 }, true)).toBe(40)
    expect(isLowStock(29, {}, false)).toBe(true)
    expect(isLowStock(30, {}, false)).toBe(false)
    expect(isLowStock(0, { reorder_level_units: 40 }, true)).toBe(false)
  })
})
