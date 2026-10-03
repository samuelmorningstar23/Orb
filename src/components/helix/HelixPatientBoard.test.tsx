/**
 * Patient Board stock.
 *
 * The board used to look stock up by drug NAME, sum every lot (expired ones
 * included) and call a medicine low under 100, while the formulary counted
 * only dispensable lots and called it low under 30. It also grouped orders by
 * patient name, so two patients who share a name became one row. It now uses
 * the drug the dispense will draw on (the substitute when one was chosen), by
 * id, with the formulary's rule and threshold, and groups by patient id.
 */
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import HelixPatientBoard from './HelixPatientBoard'
import { LOW_STOCK_THRESHOLD } from './stock'
import { localDateKey } from '../../lib/datetime'

function daysFromNow(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() + n)
  return localDateKey(d)
}

const catalog = [
  { id: 'MED-CEF', form: 'Vial' },
  { id: 'MED-AZI', form: 'Tablet' },
]

function lot(id: string, medication_id: string, quantity: number, expiration_date: string | null, in_date?: number) {
  return { id, medication_id, lot_number: id, quantity, expiration_date, name: 'Shared Name', strength: '', in_date }
}

function order(id: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    patient_id: 'P001',
    patient_name: 'Asha Rao',
    medication_id: 'MED-CEF',
    substitute_medication_id: null,
    medication_name: 'Ceftriaxone',
    dosage: '1 g',
    frequency: 'OD',
    route: 'IV',
    status: 'pending',
    created_at: '2026-10-03 04:00:00',
    prescribed_by_name: 'Dr Mehta',
    ward: 'GM',
    bed: '4',
    ...extra,
  }
}

describe('HelixPatientBoard stock column', () => {
  it('reads the substitute drug by id, not the ordered drug or a name match', () => {
    render(<HelixPatientBoard catalog={catalog} orders={[
      order('O1', { status: 'substitute_suggested', substitute_medication_id: 'MED-AZI', substitute_name: 'Azithromycin' }),
    ]} inventory={[
      // Same display name on both drugs: a name lookup would add these together.
      lot('L-CEF', 'MED-CEF', 500, daysFromNow(400), 1),
      lot('L-AZI', 'MED-AZI', 80, daysFromNow(400), 1),
    ]} />)
    expect(screen.getByText('80 tablets')).toBeInTheDocument()
    expect(screen.queryByText(/500/)).not.toBeInTheDocument()
  })

  it('counts only in-date lots with packs left, the rule the dispense uses', () => {
    render(<HelixPatientBoard catalog={catalog} orders={[order('O1')]} inventory={[
      lot('L-OLD', 'MED-CEF', 200, daysFromNow(-3), 0),
      lot('L-NODATE', 'MED-CEF', 50, null, 0),
      lot('L-GOOD', 'MED-CEF', 40, daysFromNow(200), 1),
    ]} />)
    expect(screen.getByText('40 vials')).toBeInTheDocument()
  })

  it('shows Out of Stock when every lot on the shelf is expired', () => {
    render(<HelixPatientBoard catalog={catalog} orders={[order('O1')]} inventory={[
      lot('L-OLD', 'MED-CEF', 200, daysFromNow(-3), 0),
    ]} />)
    expect(screen.getByText('Out of Stock')).toBeInTheDocument()
  })

  it('calls stock low below the same threshold as the formulary', () => {
    render(<HelixPatientBoard catalog={catalog} orders={[order('O1')]} inventory={[
      lot('L-1', 'MED-CEF', LOW_STOCK_THRESHOLD - 1, daysFromNow(200), 1),
    ]} />)
    expect(screen.getByText(`Low: ${LOW_STOCK_THRESHOLD - 1} vials`)).toBeInTheDocument()
  })

  it('is not low at the threshold itself', () => {
    render(<HelixPatientBoard catalog={catalog} orders={[order('O1')]} inventory={[
      lot('L-1', 'MED-CEF', LOW_STOCK_THRESHOLD, daysFromNow(200), 1),
    ]} />)
    expect(screen.getByText(`${LOW_STOCK_THRESHOLD} vials`)).toBeInTheDocument()
    expect(screen.queryByText(/^Low/)).not.toBeInTheDocument()
  })
})

describe('HelixPatientBoard grouping', () => {
  it('keeps two patients who share a name on separate rows', () => {
    render(<HelixPatientBoard catalog={catalog} inventory={[]} orders={[
      order('O1', { patient_id: 'P001', bed: '4' }),
      order('O2', { patient_id: 'P002', bed: '9', medication_name: 'Azithromycin', medication_id: 'MED-AZI' }),
    ]} />)
    expect(screen.getByText('2 patients')).toBeInTheDocument()
    const rows = screen.getAllByText('Asha Rao').map(el => el.closest('tr') as HTMLElement)
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('GM / 4')).toBeInTheDocument()
    expect(within(rows[0]).queryByText('Azithromycin')).not.toBeInTheDocument()
    expect(within(rows[1]).getByText('GM / 9')).toBeInTheDocument()
  })
})
