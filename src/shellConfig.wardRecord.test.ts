/**
 * The Orb Ward Record rail (6 October 2026).
 *
 * With clinical.ward_record on, the rail shows only what Ward Record sells:
 * the ward list and chart (vigil), observation entry (the nurse's shift),
 * Beds when ORB_ADT is on, and Admin. Sage, Scribe, Lens, Relay, Orders,
 * Helix, Forecast, the Simulator, Command, Revenue, Appointments, the Surgical
 * Suite and every back-office module are hidden, and a direct visit to one
 * of them is named by switchedOffTab so the shell can show the "Not part of
 * Orb Ward Record" page. With only the models switched off, the three model
 * tabs go.
 */
import { afterEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_CLINICAL_SWITCHES,
  HOSPITAL_MODULE_IDS,
  getAccessibleTabs,
  offersFloatingSage,
  resetClinicalSwitches,
  resetFeatureFlags,
  resetModuleFlags,
  setClinicalSwitches,
  setFeatureFlags,
  switchedOffTab,
  type ClinicalSwitches,
  type HospitalModuleId,
  type Tab,
} from './shellConfig'
import { featureTabInstalled } from './shellConfig'

const ALL_ON = Object.fromEntries(HOSPITAL_MODULE_IDS.map(id => [id, true])) as Record<HospitalModuleId, boolean>
const ALL_INSTALLED = ALL_ON
const ADT_OFF = { ...ALL_ON, adt: false }
const FEATURES_ON = { registration: true, opd: true } as never

const WARD_RECORD: ClinicalSwitches = {
  scoringEnabled: false, lensEnabled: false, sepsisAutostart: false,
  modelsEnabled: false, wardRecord: true,
}
const MODELS_OFF: ClinicalSwitches = { ...DEFAULT_CLINICAL_SWITCHES, modelsEnabled: false }

const tabs = (role: string, clinical: ClinicalSwitches, modules = ALL_ON, department?: string) =>
  getAccessibleTabs(role, department, modules, ALL_INSTALLED, FEATURES_ON, featureTabInstalled, clinical)

const SOLD: readonly Tab[] = ['vigil', 'shift', 'adt', 'observations', 'admin']
const NEVER: readonly Tab[] = [
  'sage', 'scribe', 'lens', 'relay', 'orders', 'helix', 'pharmacy-verify', 'forecast', 'capacity',
  'command', 'coding', 'appointments', 'surgical-suite', 'today', 'round', 'opd',
  'billing', 'payments', 'payer', 'procurement', 'frontdesk', 'housekeeping', 'nabh', 'abdm',
  'workforce', 'biomedical', 'dietary', 'pharmacy-store', 'lab',
]

afterEach(() => {
  resetClinicalSwitches()
  resetModuleFlags()
  resetFeatureFlags()
})

describe('the rail under the Orb Ward Record profile', () => {
  it.each(['admin', 'doctor', 'nurse', 'ward_clerk', 'pharmacist', 'surgeon', 'cashier', 'receptionist'])(
    '%s sees only Ward Record screens', role => {
      const rail = tabs(role, WARD_RECORD)
      for (const id of rail) expect(SOLD).toContain(id)
      for (const id of NEVER) expect(rail).not.toContain(id)
    })

  it('gives each Ward Record role its screens', () => {
    expect(tabs('nurse', WARD_RECORD)).toEqual(['shift', 'vigil'])
    expect(tabs('doctor', WARD_RECORD)).toEqual(['vigil'])
    expect(tabs('admin', WARD_RECORD)).toEqual(['vigil', 'adt', 'admin'])
    // The clerk's own Observations tab sits beside Beds (9 October 2026).
    expect(tabs('ward_clerk', WARD_RECORD)).toEqual(['adt', 'observations'])
    // Pharmacy, the Verify Queue and Sage are not part of it: nothing left.
    expect(tabs('pharmacist', WARD_RECORD)).toEqual([])
  })

  it('shows Beds only while ORB_ADT is on, and the clerk her Observations either way', () => {
    expect(tabs('admin', WARD_RECORD, ADT_OFF)).toEqual(['vigil', 'admin'])
    expect(tabs('ward_clerk', WARD_RECORD, ADT_OFF)).toEqual(['observations'])
  })

  it('a doctor whose home is Today lands on the ward list instead', () => {
    expect(tabs('doctor', WARD_RECORD)[0]).toBe('vigil')
  })

  it('offers no floating Sage', () => {
    expect(offersFloatingSage('doctor', WARD_RECORD)).toBe(false)
    expect(offersFloatingSage('doctor', DEFAULT_CLINICAL_SWITCHES)).toBe(true)
  })
})

describe('the rail with only the models switched off', () => {
  it('drops Sage, Scribe and Lens and keeps the rest', () => {
    const before = tabs('doctor', DEFAULT_CLINICAL_SWITCHES)
    const after = tabs('doctor', MODELS_OFF)
    expect(after).toEqual(before.filter(id => !['sage', 'scribe', 'lens'].includes(id)))
    expect(after).toContain('relay')
    expect(after).toContain('orders')
  })

  it('a site with every switch at its default keeps its whole rail', () => {
    expect(tabs('doctor', DEFAULT_CLINICAL_SWITCHES)).toContain('sage')
    expect(tabs('doctor', DEFAULT_CLINICAL_SWITCHES)).toContain('lens')
  })
})

describe('a direct visit to a screen the profile hides', () => {
  it('is named for the "Not part of Orb Ward Record" page', () => {
    setFeatureFlags({ registration: true })
    setClinicalSwitches({ scoring_enabled: false, models_enabled: false, ward_record: true })
    expect(switchedOffTab('doctor', undefined, 'sage')).toBe('sage')
    expect(switchedOffTab('doctor', undefined, 'orders')).toBe('orders')
    expect(switchedOffTab('nurse', undefined, 'relay')).toBe('relay')
  })

  it('is not named for a screen on the rail, a screen the role never had, or no screen', () => {
    setClinicalSwitches({ scoring_enabled: false, models_enabled: false, ward_record: true })
    expect(switchedOffTab('doctor', undefined, 'vigil')).toBeNull()
    expect(switchedOffTab('nurse', undefined, 'billing')).toBeNull()
    expect(switchedOffTab('doctor', undefined, 'not-a-tab')).toBeNull()
    expect(switchedOffTab('doctor', undefined, null)).toBeNull()
  })

  it('is never named on an ordinary site', () => {
    expect(switchedOffTab('doctor', undefined, 'sage')).toBeNull()
  })
})
