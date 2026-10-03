/**
 * Downtime Mode when Orb does not answer.
 *
 * The page claimed to work offline but read only GET /downtime/{id}/latest, so
 * with the server unreachable it showed "Failed to fetch". It now shows this
 * device's last copy of the snapshot, labelled as a copy with its capture time,
 * and with no copy it says to use the printed ward pack. A refusal is an
 * answer, never replaced by a cached copy.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readCache, writeCache } from '../lib/offline/phiCache'

const getLatestSnapshot = vi.fn()
const captureSnapshot = vi.fn()
const fetchWardPackHtml = vi.fn()

vi.mock('../api/downtime', () => ({
  getLatestSnapshot: (...a: unknown[]) => getLatestSnapshot(...a),
  captureSnapshot: (...a: unknown[]) => captureSnapshot(...a),
  fetchWardPackHtml: (...a: unknown[]) => fetchWardPackHtml(...a),
}))

import DowntimeMode from './DowntimeMode'

const USER = 'NUR-DT-1'

function envelope() {
  return {
    id: 7,
    patient_id: 'P-TEST-1',
    summary: 'snapshot',
    created_by: 'auto',
    created_at: '2026-10-02 08:00:00',
    snapshot: {
      schema_version: 1,
      captured_at: '2026-10-02 08:00:00',
      patient_id: 'P-TEST-1',
      demographics: { name: 'Asha Rao', mrn: 'MRN-1' },
      allergies: [{ substance: 'Penicillin', severity: 'severe', reaction: 'anaphylaxis' }],
      problems: [], medications: [],
      latest_vitals: null,
      news2: null,
      recent_notes: [], recent_risk_scores: [],
      counts: { allergies: 1, problems: 0, medications: 0, notes: 0 },
    },
  }
}

const networkDown = () => Object.assign(new TypeError('Failed to fetch'), {})

function renderPage() {
  return render(
    <MemoryRouter>
      <DowntimeMode patientId="P-TEST-1" />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('orb_user', JSON.stringify({ id: USER, name: 'Nurse', role: 'nurse' }))
  getLatestSnapshot.mockReset()
  captureSnapshot.mockReset()
  fetchWardPackHtml.mockReset()
})

afterEach(() => localStorage.clear())

describe('Downtime Mode with Orb unreachable', () => {
  it('with no copy on the device, gives guidance instead of a raw error', async () => {
    getLatestSnapshot.mockRejectedValue(networkDown())
    renderPage()
    expect(await screen.findByText(/Use the printed ward pack kept on the ward/)).toBeInTheDocument()
    expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument()
    expect(screen.queryByText(/No downtime snapshot has been captured/)).not.toBeInTheDocument()
  })

  it('shows the device copy, labelled as a copy with its capture time', async () => {
    writeCache('downtime:P-TEST-1', envelope(), USER)
    getLatestSnapshot.mockRejectedValue(networkDown())
    renderPage()
    expect(await screen.findByText('OFFLINE COPY OF DOWNTIME SNAPSHOT')).toBeInTheDocument()
    expect(screen.getByText(/Offline copy captured at 2 Oct 2026/)).toBeInTheDocument()
    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
    expect(screen.getByText('Penicillin')).toBeInTheDocument()
    expect(screen.getByTestId('offline-banner')).toBeInTheDocument()
  })

  it('a server error falls back the same way, with no raw message', async () => {
    writeCache('downtime:P-TEST-1', envelope(), USER)
    getLatestSnapshot.mockRejectedValue(Object.assign(new Error('Unknown error'), { status: 502 }))
    renderPage()
    expect(await screen.findByText('OFFLINE COPY OF DOWNTIME SNAPSHOT')).toBeInTheDocument()
    expect(screen.queryByText('Unknown error')).not.toBeInTheDocument()
  })

  it('a live read stores the copy that a later failed reload falls back to', async () => {
    getLatestSnapshot.mockResolvedValueOnce(envelope())
    renderPage()
    expect(await screen.findByText('DOWNTIME SNAPSHOT')).toBeInTheDocument()
    await waitFor(() => expect(readCache('downtime:P-TEST-1', USER)).not.toBeNull())

    getLatestSnapshot.mockRejectedValueOnce(networkDown())
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(await screen.findByText('OFFLINE COPY OF DOWNTIME SNAPSHOT')).toBeInTheDocument()
    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
  })

  it('a refusal is never replaced by the cached copy', async () => {
    writeCache('downtime:P-TEST-1', envelope(), USER)
    getLatestSnapshot.mockRejectedValue(Object.assign(new Error('Patient not found'), { status: 404 }))
    renderPage()
    expect(await screen.findByText(/This patient record is not available to you/)).toBeInTheDocument()
    expect(screen.queryByText('Asha Rao')).not.toBeInTheDocument()
    expect(screen.queryByText(/OFFLINE COPY/)).not.toBeInTheDocument()
  })

  it('no longer claims to work offline unconditionally', async () => {
    getLatestSnapshot.mockResolvedValue(envelope())
    renderPage()
    await screen.findByText('DOWNTIME SNAPSHOT')
    expect(screen.queryByText(/works offline/)).not.toBeInTheDocument()
    expect(screen.getByText(/readable offline only if this device opened it this shift/)).toBeInTheDocument()
  })
})
