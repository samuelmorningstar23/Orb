import { useEffect } from 'react'
/**
 * The shell's Sage runtime asks /sage/ai-posture once on mount. With the
 * models switched off (ORB_MODELS_ENABLED, or the Orb Ward Record, which forces
 * them off) that route answers 404 Switched off, so it is not asked at all, and
 * transcription reads as unavailable rather than still being checked.
 */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, setClinicalSwitches } from '../shellConfig'

const getSageAiPosture = vi.fn()
vi.mock('../api/client', () => ({
  getSageAiPosture: (...a: unknown[]) => getSageAiPosture(...a),
}))

import { SageRuntimeProvider } from './SageRuntimeProvider'
import { useSageRuntime } from './SageRuntimeContext'

let seen: boolean | null | undefined
function Probe() {
  const available = useSageRuntime().localAsrAvailable
  useEffect(() => { seen = available }, [available])
  return null
}

beforeEach(() => {
  getSageAiPosture.mockReset()
  getSageAiPosture.mockResolvedValue({ local_asr_available: true })
  seen = undefined
})
afterEach(() => { cleanup(); resetClinicalSwitches() })

async function mount() {
  render(<SageRuntimeProvider><Probe /></SageRuntimeProvider>)
  await act(async () => {})
}

describe('the AI posture request', () => {
  it('is not made on an Orb Ward Record computer, which forces the models off', async () => {
    act(() => { setClinicalSwitches({ ward_record: true }) })
    await mount()
    expect(getSageAiPosture).not.toHaveBeenCalled()
    expect(seen).toBe(false)
  })

  it('is not made with the models switched off alone', async () => {
    act(() => { setClinicalSwitches({ models_enabled: false }) })
    await mount()
    expect(getSageAiPosture).not.toHaveBeenCalled()
    expect(seen).toBe(false)
  })

  it('is made once with the models on', async () => {
    await mount()
    expect(getSageAiPosture).toHaveBeenCalledTimes(1)
    expect(seen).toBe(true)
  })
})
