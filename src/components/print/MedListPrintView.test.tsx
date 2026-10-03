/**
 * THE PRINTED MEDICATION LIST (F073).
 *
 * It headed every row "Active Medications" (a stopped drug printed as one to
 * give), carried no allergy band, printed an AI-extracted order nobody had
 * verified exactly like a prescription, hard-coded a hospital name, and showed
 * the pharmacist's rejection as a login id and a raw UTC string.
 */
import { render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MedListPrintView, { UNVERIFIED_LABEL } from './MedListPrintView'
import { readableMedNotes } from './medNotes'

const MEDS = [
  { drug_name: 'Paracetamol', dosage: '1 g', route: 'PO', frequency: 'QDS', status: 'active' },
  { drug_name: 'Vancomycin', dosage: '1 g', route: 'IV', frequency: 'BD', status: 'active', unverified: true },
  {
    drug_name: 'Enoxaparin', dosage: '40 mg', route: 'SC', frequency: 'OD', status: 'discontinued',
    notes: '[REJECTED by A. Fernandes @ 2026-10-03T05:30:00+00:00] Duplicate of the charted heparin',
  },
]

beforeEach(() => {
  vi.spyOn(window, 'print').mockImplementation(() => {})
})

function renderList(props: Partial<Parameters<typeof MedListPrintView>[0]> = {}) {
  return render(
    <MedListPrintView
      patientName="Asha Menon" mrn="MRN-1" bed="4A" medications={MEDS}
      allergies={[]} siteName="St. Example Hospital" onAfterPrint={() => {}}
      {...props}
    />,
  )
}

describe('MedListPrintView', () => {
  it('prints stopped drugs under their own heading, never as active', () => {
    renderList()
    const active = screen.getByRole('heading', { name: 'Active medications' }).parentElement!
    const stopped = screen.getByRole('heading', { name: /Stopped medications/ }).parentElement!
    expect(within(active).queryByText('Enoxaparin')).toBeNull()
    expect(within(stopped).getByText('Enoxaparin')).toBeTruthy()
    expect(within(active).getByText('Paracetamol')).toBeTruthy()
  })

  it('labels an unverified AI-extracted order', () => {
    renderList()
    const row = screen.getByText('Vancomycin').closest('tr')!
    expect(within(row).getByText(UNVERIFIED_LABEL)).toBeTruthy()
    const plain = screen.getByText('Paracetamol').closest('tr')!
    expect(within(plain).queryByText(UNVERIFIED_LABEL)).toBeNull()
  })

  it('carries the allergy band, and an empty list is none recorded', () => {
    renderList()
    expect(screen.getByText('No allergies recorded. Confirm with the patient.')).toBeTruthy()
  })

  it('names charted allergies and says when the list could not be read', () => {
    const { unmount } = renderList({ allergies: [{ substance: 'Penicillin', severity: 'severe', reaction: 'anaphylaxis' }] })
    expect(screen.getByText('Penicillin')).toBeTruthy()
    unmount()
    renderList({ allergies: null })
    expect(screen.getByText(/The allergy list could not be read/)).toBeTruthy()
  })

  it('takes the site name from configuration and invents none', () => {
    const { unmount } = renderList()
    expect(screen.getByRole('heading', { name: 'St. Example Hospital' })).toBeTruthy()
    expect(screen.queryByText('Orb Hospital System')).toBeNull()
    unmount()
    renderList({ siteName: null })
    expect(screen.queryByText('Orb Hospital System')).toBeNull()
    expect(screen.getByText('Medication list')).toBeTruthy()
  })

  it('prints the rejection with the pharmacist\'s name and a local time', () => {
    const text = readableMedNotes(MEDS[2].notes)
    expect(text).toMatch(/^Rejected by A\. Fernandes, 3 Oct 2026, \d{2}:\d{2} (AM|PM): Duplicate/)
    expect(text).not.toMatch(/2026-10-03T/)
  })
})
