/**
 * The handled-card badge must say what actually happened to a confirmed order.
 *
 * Until 2026-10-02 a medication order the drug-safety screen STOPPED came back
 * as handoff status 'accepted' and rendered "Sent to Helix", so a doctor could
 * believe a septic patient's antibiotic had gone out when nothing was charted.
 * The screen's non-blocking alerts were thrown away too, although the gate only
 * blocks on severe, so a moderate alert charted the drug in silence.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { SageAction, SageAmbientMemory } from '../../api/client'

const api = vi.hoisted(() => ({
  getSageMemory: vi.fn(),
  getSageActions: vi.fn(),
  transitionSageAction: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  ...api,
  socket: { on: vi.fn(), off: vi.fn() },
}))

import SageAmbientSidebar from './SageAmbientSidebar'

const NOW = new Date().toISOString()

const memory = {
  room_id: 'R1', patient_id: 'P1', patient_summary: '', discussion_summary: 'Febrile, started antibiotics.',
  current_concern: '', likely_next_steps: [], unresolved_questions: [], active_intents: [], risk_flags: [],
  last_processed_at: NOW, updated_at: NOW,
} as unknown as SageAmbientMemory

function card(id: string, target: string, handoff: SageAction['handoff']): SageAction {
  return {
    id, room_id: 'R1', patient_id: 'P1', action_type: 'start_medication', target,
    confidence: 0.9, state: 'confirmed', version: 2, created_at: NOW, updated_at: NOW, handoff,
  } as SageAction
}

async function renderWith(actions: SageAction[]) {
  api.getSageMemory.mockResolvedValue(memory)
  api.getSageActions.mockResolvedValue(actions)
  render(<SageAmbientSidebar channelId="R1" />)
  await screen.findByText('Handled Actions')
}

describe('SageAmbientSidebar handled medication cards', () => {
  it('reports a blocked order as blocked, with the reason, never as sent', async () => {
    const reason = 'Not charted: the drug-safety screen blocked Ceftriaxone 1g. Patient is allergic to ceftriaxone (documented reaction: anaphylaxis).'
    await renderWith([card('A1', 'Ceftriaxone 1g', {
      id: 'H1', status: 'blocked', target_module: 'helix', error_summary: reason,
      safety: { blocking: true, screen_failed: false, coverage_note: null, alerts: [
        { type: 'allergy', severity: 'severe', match: 'direct', message: 'Patient is allergic to ceftriaxone (documented reaction: anaphylaxis).' },
      ] },
    })])

    expect(screen.getByText('Blocked')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toBe(reason)
    expect(screen.queryByText(/Sent to Helix/)).toBeNull()
  })

  it('shows a non-blocking alert on an order that went through', async () => {
    await renderWith([card('A2', 'Ceftriaxone 1g', {
      id: 'H2', status: 'accepted', target_module: 'helix', downstream_ref_id: 'ORD-2',
      safety: { blocking: false, screen_failed: false, coverage_note: null, alerts: [
        { type: 'allergy', severity: 'moderate', match: 'cross_reactivity',
          message: 'Cross-reactivity: Ceftriaxone 1g (cephalosporins) with documented penicillins allergy (penicillin).' },
      ] },
    })])

    expect(screen.getByText('Sent to Helix')).toBeTruthy()
    expect(screen.getByText('Drug-safety alerts when confirmed')).toBeTruthy()
    expect(screen.getByText(/with documented penicillins allergy/)).toBeTruthy()
  })

  it('says when interactions were not screened, and shows no alert block for a clean order', async () => {
    const note = "'Some-Herbal' is outside the interaction knowledge base. Drug interactions were NOT screened. Verify manually."
    await renderWith([card('A3', 'Some-Herbal', {
      id: 'H3', status: 'accepted', target_module: 'helix', downstream_ref_id: 'ORD-3',
      safety: { blocking: false, screen_failed: false, coverage_note: note, alerts: [] },
    })])

    expect(screen.getByText(note)).toBeTruthy()
    expect(screen.queryByText('Drug-safety alerts when confirmed')).toBeNull()
  })

  it('shows a blocked confirm from the response itself, with no socket refresh', async () => {
    // Found in the browser 2026-10-02: the card read "confirmed" with no badge
    // until a socket event reloaded it, so a dropped socket hid the block.
    const pending = { ...card('A4', 'Ceftriaxone 1g', undefined), state: 'suggested', version: 1 } as SageAction
    await renderWithPending(pending)
    const reason = 'Not charted: the drug-safety screen blocked Ceftriaxone 1g. Patient is allergic to ceftriaxone (documented reaction: anaphylaxis).'
    api.transitionSageAction.mockResolvedValue({
      status: 'ok',
      handoff: { id: 'H4', status: 'blocked', target_module: 'helix', error_summary: reason,
        safety: { blocking: true, screen_failed: false, coverage_note: null, alerts: [] } },
    })

    fireEvent.click(screen.getByText('Confirm'))

    expect(await screen.findByText('Blocked')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toBe(reason)
    expect(api.getSageActions).toHaveBeenCalledTimes(1) // no reload did this
  })
})

describe('SageAmbientSidebar outcomes that are not a clean send', () => {
  it('says "Charted only" with the reason when no pharmacy order exists', async () => {
    const note = 'Charted for review, but not sent to pharmacy: Medication Zyloquin 500mg not found in Helix master catalog. Place the order in Helix.'
    await renderWith([card('A5', 'Zyloquin 500mg', {
      id: 'H5', status: 'accepted', target_module: 'helix', downstream_ref_id: 'charted-only',
      error_summary: note, safety: { blocking: false, screen_failed: false, coverage_note: null, alerts: [] },
    })])

    expect(screen.getByText('Charted only')).toBeTruthy()
    expect(screen.getByText(note)).toBeTruthy()
    expect(screen.queryByText(/Sent to Helix/)).toBeNull()
  })

  it('shows a pending card in a room Sage has not summarised yet', async () => {
    // getSageMemory answers null for a room with no memory row. The panel used
    // to return null for the whole room, hiding the card it had to confirm.
    api.getSageMemory.mockResolvedValue(null)
    api.getSageActions.mockReset().mockResolvedValue([
      { ...card('A6', 'Sepsis screen and cultures', undefined), state: 'suggested', version: 1 } as SageAction,
    ])
    render(<SageAmbientSidebar channelId="R1" />)

    expect(await screen.findByText('Confirm')).toBeTruthy()
    expect(screen.getByText('Target: Sepsis screen and cultures')).toBeTruthy()
  })

  it('stays hidden when there is no memory and no card', async () => {
    api.getSageMemory.mockResolvedValue(null)
    api.getSageActions.mockReset().mockResolvedValue([])
    const { container } = render(<SageAmbientSidebar channelId="R1" />)
    await waitFor(() => expect(api.getSageActions).toHaveBeenCalled())
    await waitFor(() => expect(container.textContent).toBe(''))
  })
})

describe('SageAmbientSidebar room summary', () => {
  it('labels only the server retraction note, never a confirmed value', async () => {
    api.getSageMemory.mockResolvedValue({
      ...memory,
      discussion_summary: 'Potassium 6.8, critical. Repeat sample not haemolysed, potassium 6.9 confirmed. '
        + 'RETRACTED by ward message: "troponin was a labelling error, repeat normal"',
    })
    api.getSageActions.mockReset().mockResolvedValue([])
    render(<SageAmbientSidebar channelId="R1" />)

    const note = await screen.findByText(/Retracted by ward message: "troponin was a labelling error/)
    expect(note.textContent).toMatch(/^Correction/)
    expect(screen.getAllByText('Correction')).toHaveLength(1)
    expect(screen.getByText('Repeat sample not haemolysed, potassium 6.9 confirmed.').textContent)
      .not.toMatch(/Correction/)
  })
})

async function renderWithPending(action: SageAction) {
  api.getSageMemory.mockResolvedValue(memory)
  api.getSageActions.mockReset().mockResolvedValue([action])
  render(<SageAmbientSidebar channelId="R1" />)
  await screen.findByText('Confirm')
}
