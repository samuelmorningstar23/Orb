/**
 * Downtime snapshot schema 2 on screen (F082 / F083, 2026-10-03).
 *
 * The snapshot now carries code status, open escalations, recent chart notes
 * and recent lab results; the page must show them, must say so when an older
 * copy does not carry them (never print a missing section as "none"), and
 * must mark an AI-extracted drug no pharmacist has checked. The tab is named
 * after the patient, and the toolbar wraps so both print actions stay
 * reachable at phone width.
 */
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getLatestSnapshot = vi.fn()

vi.mock('../api/downtime', () => ({
  getLatestSnapshot: (...a: unknown[]) => getLatestSnapshot(...a),
  captureSnapshot: vi.fn(),
  fetchWardPackHtml: vi.fn(),
}))

import DowntimeMode from './DowntimeMode'
import { markSwitchesKnownForTests } from '../shellConfig'

const BASE = {
  schema_version: 1,
  captured_at: '2026-10-03T08:00:00Z',
  patient_id: 'P-TEST-1',
  demographics: { name: 'Asha Rao', mrn: 'MRN-1', bed: 'B2', ward: 'Ward 7' },
  allergies: [], problems: [],
  medications: [
    { drug_name: 'Furosemide', dosage: '40mg', route: 'IV', frequency: 'BD',
      status: 'active', source: 'llm_extracted', verified_by: null },
  ],
  latest_vitals: null, news2: null, recent_notes: [], recent_risk_scores: [],
  counts: { allergies: 0, problems: 0, medications: 1, notes: 0 },
}

const SCHEMA_2 = {
  ...BASE,
  schema_version: 2,
  code_status: 'dnr',
  open_escalations: [
    { status: 'open', current_tier: 2, max_tier: 3, tier_label: 'Registrar review',
      raised_at: '2026-10-03T07:30:00Z' },
  ],
  recent_documents: [
    { id: 7, doc_type: 'Progress Note', title: 'Evening review',
      content: 'Chest clearer.', created_at: '2026-10-03 05:30:00' },
  ],
  recent_lab_reports: [
    { id: 8, title: 'Renal panel', content: 'Creatinine: 2.5 mg/dL [H] (ref: 0.7-1.3)',
      created_at: '2026-10-03 04:00:00' },
  ],
  lab_results: [
    { observation_name: 'Potassium', value_numeric: 5.9, unit: 'mmol/L',
      reference_range: '3.5-5.1', abnormal_flag: 'H', observed_at: '2026-10-03T03:00:00Z' },
  ],
}

function envelope(snapshot: unknown) {
  return { id: 1, patient_id: 'P-TEST-1', summary: 's', created_by: 'auto',
           created_at: '2026-10-03T08:00:00Z', snapshot }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DowntimeMode patientId="P-TEST-1" />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  getLatestSnapshot.mockReset()
  document.title = 'Orb'
})

// These screens render before any health probe answers. The switches count
// as known, as on a page the server served; the startup rule itself is
// tested in ClinicianShell.startup.test.tsx.
beforeEach(() => { markSwitchesKnownForTests() })

describe('schema 2 sections', () => {
  it('shows code status, the open escalation, labs and chart notes', async () => {
    getLatestSnapshot.mockResolvedValue(envelope(SCHEMA_2))
    renderPage()

    expect(await screen.findByText('DNR (do not resuscitate)')).toBeInTheDocument()
    expect(screen.getByText(/Open, tier 2 of 3, Registrar review/)).toBeInTheDocument()
    expect(screen.getByText('Potassium')).toBeInTheDocument()
    expect(screen.getByText('5.9 mmol/L')).toBeInTheDocument()
    expect(screen.getByText(/Creatinine: 2\.5 mg\/dL/)).toBeInTheDocument()
    expect(screen.getByText('Chest clearer.')).toBeInTheDocument()
  })

  it('says an older copy does not carry them, never "none"', async () => {
    getLatestSnapshot.mockResolvedValue(envelope(BASE))
    renderPage()

    await screen.findByText('Asha Rao')
    // Code status, escalation, labs and chart notes: four sections, one wording.
    expect(screen.getAllByText('Not included in this copy. Check the chart.')).toHaveLength(4)
    expect(screen.queryByText('Not recorded')).toBeNull()
    expect(screen.queryByText(/None open/)).toBeNull()
  })

  it('marks an AI-extracted drug no pharmacist has verified', async () => {
    getLatestSnapshot.mockResolvedValue(envelope(SCHEMA_2))
    renderPage()
    expect(await screen.findByText('AI-extracted, not verified by a pharmacist')).toBeInTheDocument()
  })
})

describe('the page as a page', () => {
  it('names the tab after the patient and bed', async () => {
    getLatestSnapshot.mockResolvedValue(envelope(SCHEMA_2))
    const { unmount } = renderPage()
    await screen.findByText('Asha Rao')
    expect(document.title).toBe('Downtime: Asha Rao (B2)')
    unmount()
    expect(document.title).toBe('Orb')
  })

  it('wraps the toolbar so both print actions stay on screen at phone width', () => {
    const css = readFileSync(resolve(__dirname, 'DowntimeMode.css'), 'utf8')
    const rule = (selector: string) => {
      const m = css.match(new RegExp(`${selector.replace(/[.]/g, '\\.')}\\s*\\{([^}]*)\\}`))
      return m ? m[1] : ''
    }
    expect(rule('.dt-toolbar')).toMatch(/flex-wrap:\s*wrap/)
    expect(rule('.dt-toolbar__actions')).toMatch(/flex-wrap:\s*wrap/)
  })
})
