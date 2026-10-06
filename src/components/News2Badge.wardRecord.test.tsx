/**
 * The NEWS2 badge with scoring switched off (the Orb Ward Record, 6 October
 * 2026): nothing at all, for a score and for a missing one. The "—" a badge
 * draws for a patient nobody has measured would read as a score that is
 * missing, when on this computer there is no score to be had.
 */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import News2Badge from './News2Badge'
import { resetClinicalSwitches, setClinicalSwitches } from '../shellConfig'

const SCORED = {
  score: 0.45, level: 'critical' as const, raw_news2: 9, single_param_3: false,
  drivers: [], breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1 as const,
}

afterEach(() => { cleanup(); resetClinicalSwitches() })

describe('the NEWS2 badge', () => {
  it('draws nothing with scoring off, for a null score or a stale one', () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    expect(render(<News2Badge news2={null} />).container.innerHTML).toBe('')
    cleanup()
    expect(render(<News2Badge news2={SCORED as never} />).container.innerHTML).toBe('')
  })

  it('with scoring on, a missing score is still the not recorded dash', () => {
    const { container } = render(<News2Badge news2={null} />)
    expect(container.textContent).toContain('—')
  })
})
