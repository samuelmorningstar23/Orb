/**
 * Downtime Mode — the ward pack button and the scope-refusal wording.
 *
 * Two invariants pinned here:
 *
 *  1. "Print ward pack" opens the ward-level pack (raw HTML, not JSON) in a new
 *     tab — one print action per ward instead of one per patient
 *     (docs/DRILLS.md §2). The fetch itself now lives in api/downtime and is
 *     pinned in api/downtime.test.ts; what this file owns is that the button
 *     asks for it and opens the result.
 *  2. A 404 "Patient not found" is deliberately ambiguous on the server (an
 *     unknown id and an out-of-ward id answer identically, so the refusal
 *     never confirms the record exists). The page must therefore word the
 *     refusal carefully instead of flatly telling a clinician the patient
 *     does not exist (DEMO_READINESS §15.1). The wording itself is shared with
 *     the I-PASS surfaces in lib/scopeRefusal.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getLatestSnapshot = vi.fn()
const captureSnapshot = vi.fn()
const fetchWardPackHtml = vi.fn()

vi.mock('../api/downtime', () => ({
  getLatestSnapshot: (...a: unknown[]) => getLatestSnapshot(...a),
  captureSnapshot: (...a: unknown[]) => captureSnapshot(...a),
  fetchWardPackHtml: (...a: unknown[]) => fetchWardPackHtml(...a),
}))

import DowntimeMode from './DowntimeMode'
import { markSwitchesKnownForTests } from '../shellConfig'

function renderPage() {
  return render(
    <MemoryRouter>
      <DowntimeMode patientId="P-TEST-1" />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  getLatestSnapshot.mockReset()
  captureSnapshot.mockReset()
  fetchWardPackHtml.mockReset()
})

// These screens render before any health probe answers. The switches count
// as known, as on a page the server served; the startup rule itself is
// tested in ClinicianShell.startup.test.tsx.
beforeEach(() => { markSwitchesKnownForTests() })

describe('scope refusal rendering', () => {
  it('shows the careful refusal, not a flat "Patient not found"', async () => {
    getLatestSnapshot.mockRejectedValue(new Error('Patient not found'))
    renderPage()

    await waitFor(() =>
      expect(screen.getByText(/not available to you/i)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/^Patient not found$/)).not.toBeInTheDocument()
  })
})

describe('the Print ward pack button', () => {
  it('asks the downtime api for the pack and opens it in a new tab', async () => {
    getLatestSnapshot.mockRejectedValue(new Error('No snapshot captured for this patient'))
    fetchWardPackHtml.mockResolvedValue('<!DOCTYPE html>ward pack')
    const openMock = vi.fn()
    vi.stubGlobal('open', openMock)
    URL.createObjectURL = vi.fn(() => 'blob:ward-pack')
    URL.revokeObjectURL = vi.fn()

    renderPage()
    const button = await screen.findByRole('button', { name: /print ward pack/i })
    fireEvent.click(button)

    await waitFor(() => expect(openMock).toHaveBeenCalledWith('blob:ward-pack', '_blank', 'noopener'))
    expect(fetchWardPackHtml).toHaveBeenCalledWith('P-TEST-1')

    vi.unstubAllGlobals()
  })

  it('words a scope refusal from the pack carefully too', async () => {
    getLatestSnapshot.mockRejectedValue(new Error('No snapshot captured for this patient'))
    fetchWardPackHtml.mockRejectedValue(new Error('Patient not found'))

    renderPage()
    const button = await screen.findByRole('button', { name: /print ward pack/i })
    fireEvent.click(button)

    expect(await screen.findByText(/not available to you/i)).toBeInTheDocument()
  })
})

/* ──────────────────────────────────────────────────────────────────────────
 * THIS PAGE IS PAPER. It prints on one tap and gets taped above a bed while
 * the EHR is down, so it is read by whoever is standing there under time
 * pressure — the worst possible place for a number that looks more precise
 * than the measurement was. A stored 110.8 printed as "110.8/62.3": blood
 * pressure is read off a cuff to the nearest whole mmHg, and a pulse and a
 * respiratory rate are COUNTED. Temperature is the one vital genuinely
 * charted to a tenth, and it keeps its decimal (lib/vitals).
 * ────────────────────────────────────────────────────────────────────────── */
const SNAPSHOT_WITH_FLOAT_VITALS = {
  id: 1,
  patient_id: 'P-TEST-1',
  summary: 'snapshot',
  created_by: 'D001',
  created_at: '2026-08-14T08:00:00Z',
  snapshot: {
    schema_version: 1,
    captured_at: '2026-08-14T08:00:00Z',
    patient_id: 'P-TEST-1',
    demographics: { name: 'Asha Rao', mrn: 'MRN-1' },
    allergies: [], problems: [], medications: [],
    latest_vitals: {
      timestamp: '2026-08-14T07:50:00Z',
      heart_rate: 88.4, systolic_bp: 110.8, diastolic_bp: 62.3,
      spo2: 93.6, temperature: 37.44, respiratory_rate: 19.5,
      consciousness: 'Alert', on_oxygen: 0,
    },
    news2: null, recent_notes: [], recent_risk_scores: [],
    counts: { allergies: 0, problems: 0, medications: 0, notes: 0 },
  },
}

describe('vitals on the printed downtime pack', () => {
  it('prints blood pressure in whole mmHg, never to a tenth', async () => {
    getLatestSnapshot.mockResolvedValue(SNAPSHOT_WITH_FLOAT_VITALS)
    const { container } = renderPage()

    expect(await screen.findByText('111/62')).toBeInTheDocument()
    expect(container.textContent).not.toContain('110.8')
    expect(container.textContent).not.toContain('62.3')
  })

  it('rounds the counted vitals and keeps temperature to a tenth', async () => {
    getLatestSnapshot.mockResolvedValue(SNAPSHOT_WITH_FLOAT_VITALS)
    const { container } = renderPage()

    await screen.findByText('111/62')
    expect(screen.getByText('88')).toBeInTheDocument()      // HR
    expect(screen.getByText('20')).toBeInTheDocument()      // RR
    expect(screen.getByText('94%')).toBeInTheDocument()     // SpO2
    expect(screen.getByText('37.4')).toBeInTheDocument()    // Temp keeps its tenth
    expect(container.textContent).not.toContain('88.4')
    expect(container.textContent).not.toContain('19.5')
    expect(container.textContent).not.toContain('93.6')
  })

  it('prints a dash for a vital that was never recorded, not a zero', async () => {
    getLatestSnapshot.mockResolvedValue({
      ...SNAPSHOT_WITH_FLOAT_VITALS,
      snapshot: {
        ...SNAPSHOT_WITH_FLOAT_VITALS.snapshot,
        latest_vitals: {
          ...SNAPSHOT_WITH_FLOAT_VITALS.snapshot.latest_vitals,
          systolic_bp: null, diastolic_bp: null, heart_rate: null,
        },
      },
    })
    renderPage()

    expect(await screen.findByText('—/—')).toBeInTheDocument()
    expect(screen.queryByText('0/0')).toBeNull()
  })
})
