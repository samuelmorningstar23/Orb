/**
 * Model Governance lists what the appliance actually runs, in words.
 *
 * It listed four hand-written rows that called the 27B "the only inference
 * lane" while the 4B answered every thinking-off turn, printed internal keys
 * in monospace, hid the notes, and used a table class that does not exist.
 */
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../../../api/client', () => ({
  getModelRegistry: vi.fn().mockResolvedValue({
    models: [
      {
        model_key: 'sage_llm_fast', display_name: 'Sage, fast model', version: 'medgemma:4b',
        task: 'Quick answers when a clinician turns thinking off', training_data: 'Google MedGemma (pretrained)',
        validated: 0, is_active: 1, checkpoint_present: 1, role: 'Thinking off',
        impacts: ['Sage answers with thinking off, for general questions only'],
        notes: 'The safety floor sends patient and dosing questions to the careful model.',
      },
      {
        model_key: 'vigil_news2', display_name: 'NEWS2 early-warning score', version: 'rcp-news2-2017',
        task: 'Early-warning score', training_data: 'Rule-based', validated: 1, is_active: 1,
        checkpoint_present: 1, role: 'The deterioration score on every board', impacts: [], notes: '',
      },
    ],
  }),
}))

import ModelGovernance from './ModelGovernance'

describe('model governance', () => {
  it('names each model in words, shows what it serves and its notes, on the admin table', async () => {
    render(<ModelGovernance />)
    const table = await screen.findByRole('table', { name: 'Models' })
    expect(table.classList.contains('admin-table')).toBe(true)
    const fast = screen.getByText('Sage, fast model').closest('tr')!
    expect(within(fast).getByText('Thinking off')).toBeTruthy()
    expect(fast.textContent).toContain('safety floor')
    expect(fast.textContent).toContain('medgemma:4b')
    expect(within(fast).getByText('Not validated')).toBeTruthy()
    expect(screen.queryByText('sage_llm_fast')).toBeNull()
    expect(within(screen.getByText('NEWS2 early-warning score').closest('tr')!)
      .getByText('Validated')).toBeTruthy()
  })
})
