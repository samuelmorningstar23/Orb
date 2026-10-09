/**
 * Downtime Mode: which SpO₂ table the NEWS2 on this page was scored on.
 *
 * Found 2026-10-02 (NEWS2 render-site sweep). This page is printed and a nurse
 * re-scores NEWS2 from it while the EHR is down. A Scale 2 patient (COPD /
 * chronic type-2 respiratory failure, target 88–92%) on air at SpO₂ 90 showed
 * "NEWS2 0 · stable" beside "90%" with no marker, and the same 90% scores 3 on
 * the default Scale 1 paper chart. The snapshot already carries
 * news2.spo2_scale; the page now prints it, and only when it is 2.
 */
import { render, screen } from '@testing-library/react'
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

function envelope(news2: Record<string, unknown> | null, onOxygen = 0) {
  return {
    id: 1,
    patient_id: 'P-TEST-1',
    summary: 'snapshot',
    created_by: 'auto',
    created_at: '2026-10-02T08:00:00Z',
    snapshot: {
      schema_version: 1,
      captured_at: '2026-10-02T08:00:00Z',
      patient_id: 'P-TEST-1',
      demographics: { name: 'Asha Rao', mrn: 'MRN-1' },
      allergies: [], problems: [], medications: [],
      latest_vitals: {
        timestamp: '2026-10-02T07:50:00Z',
        heart_rate: 80, systolic_bp: 125, diastolic_bp: 75,
        spo2: 90, temperature: 37.0, respiratory_rate: 16,
        consciousness: 'Alert', on_oxygen: onOxygen,
      },
      news2,
      recent_notes: [], recent_risk_scores: [],
      counts: { allergies: 0, problems: 0, medications: 0, notes: 0 },
    },
  }
}

/** The NEWS2 badge in the "Latest vitals" heading. */
async function badgeText(): Promise<string> {
  const heading = await screen.findByRole('heading', { name: /latest vitals/i })
  const badge = heading.querySelector('.dt-badge')
  expect(badge).not.toBeNull()
  return badge!.textContent ?? ''
}

/** The label under the SpO₂ value in the vitals grid. */
async function spo2Label(): Promise<string> {
  const value = await screen.findByText('90%')
  return value.parentElement!.querySelector('.dt-vital__k')!.textContent ?? ''
}

// These screens render before any health probe answers. The switches count
// as known, as on a page the server served; the startup rule itself is
// tested in ClinicianShell.startup.test.tsx.
beforeEach(() => { markSwitchesKnownForTests() })

describe('the SpO₂ scale on the downtime page', () => {
  it('marks the badge and the SpO₂ label for a Scale 2 patient', async () => {
    getLatestSnapshot.mockResolvedValue(
      envelope({ raw_news2: 0, level: 'stable', spo2_scale: 2, drivers: ['All vitals within normal range'] }),
    )
    renderPage()

    expect(await badgeText()).toBe('NEWS2 0 · stable · SpO₂ Scale 2 (target 88–92%)')
    expect(await spo2Label()).toBe('SpO₂ (Scale 2)')
  })

  it('keeps the oxygen note on a Scale 2 patient on O₂', async () => {
    getLatestSnapshot.mockResolvedValue(
      // SpO2 90 on O2 on Scale 2: 0 for SpO2 + 2 for oxygen = NEWS2 2.
      envelope({ raw_news2: 2, level: 'stable', spo2_scale: 2 }, 1),
    )
    renderPage()

    expect(await badgeText()).toBe('NEWS2 2 · stable · SpO₂ Scale 2 (target 88–92%)')
    expect(await spo2Label()).toBe('SpO₂ (Scale 2, O₂)')
  })

  it('prints no marker for a Scale 1 patient', async () => {
    getLatestSnapshot.mockResolvedValue(
      envelope({ raw_news2: 3, level: 'elevated', spo2_scale: 1 }),
    )
    const { container } = renderPage()

    expect(await badgeText()).toBe('NEWS2 3 · elevated')
    expect(await spo2Label()).toBe('SpO₂')
    expect(container.textContent).not.toContain('Scale 2')
    expect(container.textContent).not.toContain('88–92')
  })

  it('prints no marker for a snapshot stored before spo2_scale existed', async () => {
    getLatestSnapshot.mockResolvedValue(
      envelope({ raw_news2: 3, level: 'elevated' }, 1),
    )
    const { container } = renderPage()

    expect(await badgeText()).toBe('NEWS2 3 · elevated')
    expect(await spo2Label()).toBe('SpO₂ (O₂)')
    expect(container.textContent).not.toContain('Scale 2')
  })
})
