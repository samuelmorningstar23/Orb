/**
 * THE DRY RUN SHOWS ITS WARNINGS, AND A BLANKED DATE IS A DECISION.
 *
 * The dry run said "3 warnings" and never showed them, then the commit wrote
 * patients with their date of birth and admission date silently blanked. Counts
 * rendered as raw keys ("3 total_rows") and "1 errors" was painted green. These
 * tests pin: every row's issues are on screen, counts are words with the right
 * tone, the date order defaults to day first, and Apply is held while any date
 * would be left blank until the admin accepts it.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

vi.mock('../../../api/rosterImport', () => ({
  getRosterFields: vi.fn(),
  listRosterRuns: vi.fn(),
  previewRoster: vi.fn(),
  dryRunRoster: vi.fn(),
  commitRoster: vi.fn(),
}))

import {
  commitRoster, dryRunRoster, getRosterFields, listRosterRuns, previewRoster,
} from '../../../api/rosterImport'
import RosterImport from './RosterImport'

const CATALOGUE = {
  fields: [
    { name: 'mrn', label: 'MRN / Hospital number', required: true, kind: 'mrn', note: '' },
    { name: 'name', label: 'Patient name', required: true, kind: 'text', note: '' },
    { name: 'date_of_birth', label: 'Date of birth', required: false, kind: 'date', note: '' },
  ],
  required: ['mrn', 'name'],
  date_orders: ['auto', 'dmy', 'mdy'],
  max_bytes: 5 * 1024 * 1024,
  max_rows: 5000,
}

const PREVIEW = {
  delimiter: ',', headers: ['Hospital No.', 'Patient Name', 'DOB'], header_line: 1,
  header_confident: true, header_candidates: [], row_count: 2, rejected_row_count: 0,
  suggested_mapping: { mrn: 'Hospital No.', name: 'Patient Name', date_of_birth: 'DOB' },
  unmapped_headers: [], missing_required: [], sample_rows: [], issues: [], filename: 'ward.csv',
}

const BLANKED = 'date_of_birth \'03/04/71\' uses a two-digit year: the century is a guess, so it was left blank.'

const DRY_RUN = {
  delimiter: ',', mapping: {}, date_order: 'dmy', update_existing: true, header_line: 1,
  default_department: null,
  counts: {
    create: 1, update: 0, unchanged: 0, skip: 1, total_rows: 2, errors: 1, warnings: 1,
    admissions_create: 1, admissions_update: 0, admissions_close: 0, allergies_add: 0,
    dates_left_blank: 1,
  },
  rows: [
    { line: 2, mrn: 'H100', name: 'Asha Rao', action: 'create', reason: null, patient_id: null,
      values: {}, changes: {}, admission: { action: 'create' },
      issues: [{ line: 2, code: 'two_digit_year', severity: 'warning', message: BLANKED,
                 field: 'date_of_birth', value: '03/04/71' }] },
    { line: 3, mrn: null, name: 'No Number', action: 'skip', reason: 'missing_mrn', patient_id: null,
      values: {}, changes: {}, admission: {},
      issues: [{ line: 3, code: 'missing_mrn', severity: 'error', message: 'No MRN on this row, not imported.',
                 field: 'mrn', value: null }] },
  ],
  file_issues: [], absent_from_roster: { wards: [], patients: [], count: 0, note: '' },
  applied: false, filename: 'ward.csv',
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(getRosterFields as Mock).mockResolvedValue(CATALOGUE)
  ;(listRosterRuns as Mock).mockResolvedValue({ runs: [] })
  ;(previewRoster as Mock).mockResolvedValue(PREVIEW)
  ;(dryRunRoster as Mock).mockResolvedValue(DRY_RUN)
  ;(commitRoster as Mock).mockResolvedValue({
    run_id: 'RUN-1', summary: { created: 1, updated: 0, unchanged: 0, skipped: 1, errors: 1, warnings: 1 },
    admissions: { created: 1, updated: 0, closed: 0 }, created_patient_ids: [], updated_patient_ids: [],
    created_admission_ids: [],
  })
})

async function dryRun() {
  render(<RosterImport />)
  expect(await screen.findByRole('button', { name: 'Choose CSV file' })).toBeInTheDocument()
  const file = new File(['Hospital No.,Patient Name,DOB\n'], 'ward.csv', { type: 'text/csv' })
  fireEvent.change(screen.getByLabelText('Roster CSV file'), { target: { files: [file] } })
  fireEvent.click(await screen.findByRole('button', { name: /Dry run/ }))
  await screen.findByText(/Review, then apply/)
}

describe('roster dry run', () => {
  it('defaults the date order to day first and says so in words', async () => {
    await dryRun()
    expect(dryRunRoster).toHaveBeenCalledWith(
      expect.anything(), expect.anything(), expect.objectContaining({ dateOrder: 'dmy' }))
    expect(screen.getByRole('option', { name: 'Day / Month / Year (India)' })).toBeInTheDocument()
  })

  it('shows every row warning, in the table and in the warnings list', async () => {
    await dryRun()
    const list = screen.getByTestId('roster-row-warnings')
    expect(within(list).getByText(`Line 2: ${BLANKED}`)).toBeInTheDocument()
    expect(within(list).getByText('Line 3: No MRN on this row, not imported.')).toBeInTheDocument()
    const row = screen.getByText('Asha Rao').closest('tr')!
    expect(within(row).getByText(BLANKED)).toBeInTheDocument()
    expect(within(row).getByText('Opens an admission')).toBeInTheDocument()
    const skipped = screen.getByText('No Number').closest('tr')!
    expect(within(skipped).getByText('No MRN')).toBeInTheDocument()
    expect(within(skipped).queryByText('missing_mrn')).toBeNull()
  })

  it('labels counts in words, errors in danger and warnings in warn', async () => {
    await dryRun()
    const counts = screen.getByTestId('roster-counts')
    expect(counts.textContent).not.toMatch(/total_rows|dates_left_blank|_/)
    expect(within(counts).getByText('2 rows in the file')).toBeInTheDocument()
    expect(within(counts).getByText('1 error').className).toContain('admin-stat-chip--danger')
    expect(within(counts).getByText('1 warning').className).toContain('admin-stat-chip--warn')
    expect(within(counts).getByText('1 error').className).not.toContain('--ok')
  })

  it('holds Apply while a date would be left blank, until the admin accepts it', async () => {
    await dryRun()
    expect(screen.getByTestId('roster-blank-dates')).toHaveTextContent('1 date left blank')
    const apply = screen.getByRole('button', { name: 'Apply this import' })
    expect(apply).toBeDisabled()
    fireEvent.click(screen.getByLabelText('Import with these dates left blank'))
    expect(apply).toBeEnabled()
    fireEvent.click(apply)
    await waitFor(() => expect(commitRoster).toHaveBeenCalledTimes(1))
    const done = await screen.findByTestId('roster-committed-counts')
    expect(within(done).getByText('1 error').className).toContain('admin-stat-chip--danger')
    expect(within(done).getByText('1 admission opened')).toBeInTheDocument()
  })

  it('does not hold Apply when no date is blanked', async () => {
    ;(dryRunRoster as Mock).mockResolvedValue({
      ...DRY_RUN, counts: { ...DRY_RUN.counts, dates_left_blank: 0 },
    })
    await dryRun()
    expect(screen.queryByTestId('roster-blank-dates')).toBeNull()
    expect(screen.getByRole('button', { name: 'Apply this import' })).toBeEnabled()
  })
})
