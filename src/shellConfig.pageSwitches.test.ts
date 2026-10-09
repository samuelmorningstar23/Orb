/**
 * The switches the server writes into the page (Samuel's ruling, 9 October
 * 2026: fix the startup flash). shellConfig reads window.__ORB_SWITCHES__ at
 * module load, before the first render, through the same validation as the
 * health probe's answer; the probe still wins when it answers; a failed probe
 * with nothing else known falls back to the switches this browser saved.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const KEY = 'orb_computer_switches'

const WARD_RECORD_PAGE = {
  clinical: {
    scoring_enabled: false, lens_enabled: false, sepsis_autostart: false,
    models_enabled: false, ward_record: true,
  },
  modules: { billing: false, adt: false },
  features: { opd: false },
}

async function freshShellConfig(page?: unknown) {
  vi.resetModules()
  if (page === undefined) delete window.__ORB_SWITCHES__
  else window.__ORB_SWITCHES__ = page
  return import('./shellConfig')
}

function answer(body: unknown): typeof fetch {
  return vi.fn(async () => ({ ok: true, status: 200, json: async () => body })) as unknown as typeof fetch
}

const down: typeof fetch = vi.fn(async () => { throw new TypeError('offline') }) as unknown as typeof fetch

beforeEach(() => { localStorage.clear() })
afterEach(() => {
  delete window.__ORB_SWITCHES__
  localStorage.clear()
})

describe('the page switches', () => {
  it('are adopted at module load, before anything renders', async () => {
    const m = await freshShellConfig(WARD_RECORD_PAGE)
    expect(m.currentSwitchesKnown()).toBe(true)
    expect(m.currentSwitchesSource()).toBe('page')
    expect(m.currentStartupState()).toBe('ready')
    expect(m.currentClinicalSwitches()).toEqual({
      scoringEnabled: false, lensEnabled: false, sepsisAutostart: false,
      modelsEnabled: false, wardRecord: true,
    })
    expect(m.currentModuleFlags().billing).toBe(false)
    expect(m.currentHealthProbe()).toBe('pending')
    // Saved for a later failed probe.
    expect(JSON.parse(localStorage.getItem(KEY) ?? 'null')).toEqual(WARD_RECORD_PAGE)
  })

  it('leave the client not knowing when the page carries none (the Vite dev server)', async () => {
    const m = await freshShellConfig()
    expect(m.currentSwitchesKnown()).toBe(false)
    expect(m.currentStartupState()).toBe('starting')
    expect(m.currentClinicalSwitches().wardRecord).toBe(false)
  })

  it('are validated like the probe: no modules object means not known', async () => {
    const m = await freshShellConfig({ clinical: { ward_record: true } })
    expect(m.currentSwitchesKnown()).toBe(false)
    expect(m.currentClinicalSwitches().wardRecord).toBe(false)
    const n = await freshShellConfig({ modules: {}, clinical: { ward_record: 'yes', scoring_enabled: 0 } })
    expect(n.currentSwitchesKnown()).toBe(true)
    expect(n.currentClinicalSwitches().wardRecord).toBe(false)
    expect(n.currentClinicalSwitches().scoringEnabled).toBe(true)
  })

  it('give way to the probe when it answers', async () => {
    const m = await freshShellConfig(WARD_RECORD_PAGE)
    await m.refreshModuleFlags(answer({
      modules: { billing: true }, features: {},
      clinical: { ward_record: false, scoring_enabled: true, models_enabled: true },
    }))
    expect(m.currentSwitchesSource()).toBe('probe')
    expect(m.currentClinicalSwitches().wardRecord).toBe(false)
    expect(m.currentClinicalSwitches().scoringEnabled).toBe(true)
    expect(m.currentModuleFlags().billing).toBe(true)
    expect(JSON.parse(localStorage.getItem(KEY) ?? 'null').clinical.ward_record).toBe(false)
  })

  it('let a switched-off role say "off" at once instead of "checking"', async () => {
    const page = await freshShellConfig(WARD_RECORD_PAGE)
    expect(page.getModuleOffNotice('lab_technician')?.state).toBe('off')
    const none = await freshShellConfig()
    none.markSwitchesKnownForTests()
    expect(none.getModuleOffNotice('lab_technician')?.state).toBe('checking')
  })

  it('stand when the probe fails, ahead of anything saved', async () => {
    localStorage.setItem(KEY, JSON.stringify({ modules: {}, clinical: { ward_record: false } }))
    const m = await freshShellConfig(WARD_RECORD_PAGE)
    await m.refreshModuleFlags(down)
    expect(m.currentHealthProbe()).toBe('failed')
    expect(m.currentSwitchesSource()).toBe('page')
    expect(m.currentClinicalSwitches().wardRecord).toBe(true)
  })
})

describe('a failed probe with no page switches', () => {
  it('uses the switches this browser saved', async () => {
    localStorage.setItem(KEY, JSON.stringify(WARD_RECORD_PAGE))
    const m = await freshShellConfig()
    await m.refreshModuleFlags(down)
    expect(m.currentSwitchesSource()).toBe('saved')
    expect(m.currentStartupState()).toBe('ready')
    expect(m.currentClinicalSwitches().wardRecord).toBe(true)
  })

  it('is unreachable when nothing was saved, and Try again goes back to starting', async () => {
    const m = await freshShellConfig()
    await m.refreshModuleFlags(down)
    expect(m.currentStartupState()).toBe('unreachable')
    expect(m.currentClinicalSwitches().wardRecord).toBe(false)
    const fetchSpy = vi.fn(() => new Promise<Response>(() => {}))
    vi.stubGlobal('fetch', fetchSpy)
    void m.retryHealthProbe()
    expect(m.currentStartupState()).toBe('starting')
    expect(fetchSpy).toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('ignores a damaged saved copy', async () => {
    localStorage.setItem(KEY, '{not json')
    const m = await freshShellConfig()
    await m.refreshModuleFlags(down)
    expect(m.currentStartupState()).toBe('unreachable')
  })

  it('a non OK answer also falls back to the saved switches', async () => {
    localStorage.setItem(KEY, JSON.stringify(WARD_RECORD_PAGE))
    const m = await freshShellConfig()
    await m.refreshModuleFlags(vi.fn(async () => ({ ok: false, status: 502 })) as unknown as typeof fetch)
    expect(m.currentSwitchesSource()).toBe('saved')
    expect(m.currentClinicalSwitches().wardRecord).toBe(true)
  })
})

describe('the test helpers', () => {
  it('mark the switches known and back again', async () => {
    const m = await freshShellConfig()
    m.markSwitchesKnownForTests()
    expect(m.currentStartupState()).toBe('ready')
    m.resetSwitchesKnown()
    expect(m.currentStartupState()).toBe('starting')
  })
})
