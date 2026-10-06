/** The clinical switches on /api/health (Orb Ward Record, 6 October 2026). */
import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import {
  currentClinicalSwitches,
  refreshModuleFlags,
  resetClinicalSwitches,
  resetFeatureFlags,
  resetModuleFlags,
  useClinicalSwitches,
} from './shellConfig'

function healthFetch(body: unknown): typeof fetch {
  return (async () => ({ ok: true, json: async () => body })) as unknown as typeof fetch
}

afterEach(() => {
  resetClinicalSwitches()
  resetModuleFlags()
  resetFeatureFlags()
})

describe('clinical switches', () => {
  it('default to all on, and the Ward Record profile off, before the probe answers', () => {
    expect(currentClinicalSwitches()).toEqual({
      scoringEnabled: true, lensEnabled: true, sepsisAutostart: true,
      modelsEnabled: true, wardRecord: false,
    })
  })

  it('adopt the clinical key of the health answer', async () => {
    await refreshModuleFlags(healthFetch({
      modules: {},
      clinical: { scoring_enabled: false, lens_enabled: false, sepsis_autostart: true },
    }))
    expect(currentClinicalSwitches()).toEqual({
      scoringEnabled: false, lensEnabled: false, sepsisAutostart: true,
      modelsEnabled: true, wardRecord: false,
    })
  })

  it('adopt the Ward Record profile and the models switch', async () => {
    await refreshModuleFlags(healthFetch({
      modules: {},
      clinical: {
        scoring_enabled: false, lens_enabled: false, sepsis_autostart: false,
        models_enabled: false, ward_record: true,
      },
    }))
    expect(currentClinicalSwitches()).toEqual({
      scoringEnabled: false, lensEnabled: false, sepsisAutostart: false,
      modelsEnabled: false, wardRecord: true,
    })
  })

  it('leave the profile off and the models on for an older server that sends neither', async () => {
    await refreshModuleFlags(healthFetch({ modules: {}, clinical: { scoring_enabled: false } }))
    expect(currentClinicalSwitches().wardRecord).toBe(false)
    expect(currentClinicalSwitches().modelsEnabled).toBe(true)
  })

  it('ignore a value that is not a strict boolean', async () => {
    await refreshModuleFlags(healthFetch({
      modules: {}, clinical: { scoring_enabled: 'false', lens_enabled: 0, ward_record: 'true', models_enabled: 0 },
    }))
    expect(currentClinicalSwitches().scoringEnabled).toBe(true)
    expect(currentClinicalSwitches().lensEnabled).toBe(true)
    expect(currentClinicalSwitches().wardRecord).toBe(false)
    expect(currentClinicalSwitches().modelsEnabled).toBe(true)
  })

  it('stay on when the probe fails', async () => {
    await refreshModuleFlags((async () => { throw new Error('offline') }) as unknown as typeof fetch)
    expect(currentClinicalSwitches().scoringEnabled).toBe(true)
  })

  it('re-render a component through useClinicalSwitches', async () => {
    function Probe() {
      const sw = useClinicalSwitches()
      return <span>{sw.scoringEnabled ? 'score on' : 'score off'}</span>
    }
    render(<Probe />)
    expect(screen.getByText('score on')).toBeTruthy()
    await act(async () => {
      await refreshModuleFlags(healthFetch({ modules: {}, clinical: { scoring_enabled: false } }))
    })
    expect(screen.getByText('score off')).toBeTruthy()
  })
})
