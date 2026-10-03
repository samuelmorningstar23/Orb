/**
 * THE SHELL'S ROLE TABLE.
 *
 * Two jobs, and the first one is the important one:
 *
 * 1. BEHAVIOUR NEUTRALITY. `getAccessibleTabs` used to be a switch statement
 *    inside ClinicianShell.tsx. The arrays below are that switch, copied out of
 *    it verbatim before it was deleted, and every one of them is asserted
 *    exactly — order included, because the rail renders in this order and the
 *    landing tab used to be `accessibleTabs[0]`. If the extraction changed what
 *    any role can reach, one of these goes red.
 *
 * 2. THE PERMISSION IS THE GATE. The nurse and admin configs both DECLARE
 *    `primaryAction: 'admit'` — deliberately, so that the only thing standing
 *    between a role and the CTA is the permission mirror. A test that only
 *    checked "nurse has no admit CTA" would pass just as happily against a role
 *    list, which is the defect this whole seam exists to kill.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

vi.mock('./api/client', () => ({ getUser: () => null }))

import {
  CLINICAL_MODULE_IDS,
  HOSPITAL_MODULE_IDS,
  HOSPITAL_MODULE_PANEL_PATH,
  MONEY_MODULE_IDS,
  TABS,
  currentModuleFlags,
  doctorHomeInstalled,
  getAccessibleTabs,
  getLandingTab,
  getPrimaryAction,
  getRoleConfig,
  hospitalModuleInstalled,
  modulePanel,
  readDeploymentOverrides,
  refreshModuleFlags,
  resetModuleFlags,
  setModuleFlags,
  tabDef,
  type HospitalModuleId,
  type Tab,
} from './shellConfig'
import en from './i18n/locales/en.json'

/** Tabs that post-date the switch statement and are therefore not part of the
 *  behaviour-neutrality comparison: the doctor home and the five
 *  hospital-administration modules, each registered only when its own panel
 *  module is in the build. */
const POST_REFACTOR = new Set<string>(['today', ...HOSPITAL_MODULE_IDS])

/** getAccessibleTabs minus those. */
const legacyTabs = (role: string | null, department?: string) =>
  getAccessibleTabs(role, department).filter(id => !POST_REFACTOR.has(id))

/* The switch statement, verbatim, as it stood in ClinicianShell.tsx:183-206. */
const LEGACY: Array<[string, string | undefined, Tab[]]> = [
  ['surgeon', undefined, ['vigil', 'round', 'relay', 'sage', 'scribe', 'lens', 'forecast', 'surgical-suite']],
  ['doctor', 'General Medicine', ['vigil', 'round', 'orders', 'relay', 'sage', 'scribe', 'lens', 'forecast', 'command', 'capacity', 'coding', 'appointments']],
  ['doctor', 'Surgery', ['vigil', 'round', 'orders', 'relay', 'sage', 'scribe', 'lens', 'forecast', 'command', 'capacity', 'coding', 'surgical-suite']],
  ['doctor', 'General Surgery', ['vigil', 'round', 'orders', 'relay', 'sage', 'scribe', 'lens', 'forecast', 'command', 'capacity', 'coding', 'surgical-suite']],
  ['nurse', undefined, ['shift', 'vigil', 'orders', 'relay', 'sage', 'scribe', 'lens', 'forecast', 'coding', 'appointments']],
  ['admin', undefined, ['vigil', 'relay', 'sage', 'forecast', 'command', 'capacity', 'coding', 'appointments', 'admin']],
  ['pharmacist', undefined, ['helix', 'pharmacy-verify', 'sage']],
  ['radiographer', undefined, ['vigil', 'relay']],
  ['', undefined, ['vigil', 'relay']],
]

describe('getAccessibleTabs reproduces the pre-refactor switch exactly', () => {
  it.each(LEGACY)('%s in %s', (role, department, expected) => {
    expect(legacyTabs(role, department)).toEqual(expected)
  })

  it('an absent role still gets the two-tab fallback, never a blank shell', () => {
    expect(legacyTabs(null)).toEqual(['vigil', 'relay'])
    expect(legacyTabs(undefined as unknown as string)).toEqual(['vigil', 'relay'])
  })

  it('role matching is case-insensitive, as the server lower-cases too', () => {
    expect(legacyTabs('NURSE')).toEqual(legacyTabs('nurse'))
    expect(legacyTabs(' Doctor ')).toEqual(legacyTabs('doctor'))
  })

  /* The one deliberate change to reachability. The server aliases
     ROLE_PERMISSIONS["clinician"] = ROLE_PERMISSIONS["doctor"], and
     src/lib/roles.ts mirrors it, but the shell's switch had no 'clinician'
     case — so an account holding every doctor permission fell through to the
     unknown-role default and saw two tabs. Same drift family as the admit CTA. */
  it('clinician is the doctor alias the server says it is, not an unknown role', () => {
    expect(getAccessibleTabs('clinician')).toEqual(getAccessibleTabs('doctor'))
    expect(getLandingTab('clinician')).toBe(getLandingTab('doctor'))
  })
})

describe('the doctor home is registered, and only when its panel exists', () => {
  it("'today' is a tab for doctor and clinician iff its module is installed", () => {
    expect(getAccessibleTabs('doctor').includes('today')).toBe(doctorHomeInstalled)
    expect(getAccessibleTabs('clinician').includes('today')).toBe(doctorHomeInstalled)
  })

  it('is never offered to a role whose experience is not the doctor one', () => {
    for (const role of ['nurse', 'admin', 'pharmacist', 'surgeon', 'patient', null]) {
      expect(getAccessibleTabs(role).includes('today')).toBe(false)
    }
  })

  it('lands the doctor on it when installed, and on the roster when not', () => {
    expect(getLandingTab('doctor')).toBe(doctorHomeInstalled ? 'today' : 'vigil')
  })

  /* A tab whose panel is missing must not become a blank landing screen: the
     config's `home` only wins if it survived into the tab list. */
  it('never names a landing tab the role cannot reach', () => {
    for (const [role, department] of LEGACY) {
      const config = getRoleConfig(role, department)
      expect(config.tabs.map(entry => entry.id)).toContain(config.home)
      expect(getAccessibleTabs(role, department)).toContain(getLandingTab(role, department))
    }
  })
})

describe('per-deployment landing override', () => {
  afterEach(() => { delete window.__ORB_SHELL__ })

  it('puts the doctor back on the roster without a code change', () => {
    expect(getLandingTab('doctor', undefined, { home: { doctor: 'vigil' } })).toBe('vigil')
  })

  it('is read from the runtime global a deployment can inject', () => {
    window.__ORB_SHELL__ = { home: { doctor: 'round' } }
    expect(getLandingTab('doctor', undefined, readDeploymentOverrides())).toBe('round')
  })

  /* A typo'd or stale override must not be able to land a clinician on a tab
     their role cannot open — or on nothing at all. */
  it('ignores an override naming a tab the role cannot reach', () => {
    expect(getLandingTab('nurse', undefined, { home: { nurse: 'admin' } })).toBe('shift')
    expect(getLandingTab('doctor', undefined, { home: { doctor: 'not-a-tab' } }))
      .toBe(getLandingTab('doctor'))
  })

  it('leaves roles it does not name alone', () => {
    expect(getLandingTab('nurse', undefined, { home: { doctor: 'vigil' } })).toBe('shift')
  })
})

describe('the primary CTA is gated on the permission, never on a role list', () => {
  beforeEach(() => { localStorage.clear() })

  /* patient.write is granted to doctor and its clinician alias and to nobody
     else (server/permissions.py ROLE_PERMISSIONS). */
  it.each(['doctor', 'clinician'])('%s holds patient.write, so admit is offered', role => {
    expect(getPrimaryAction(role)).toBe('admit')
  })

  it.each(['nurse', 'admin', 'pharmacist', 'surgeon', 'patient', 'radiographer'])(
    '%s does not hold patient.write, so admit is withheld',
    role => { expect(getPrimaryAction(role)).toBeNull() },
  )

  it('withholds it from a signed-out session', () => {
    expect(getPrimaryAction(null)).toBeNull()
  })

  /* The teeth of this file. The nurse and admin experiences DO declare the
     admit action; the only reason the button does not render for them is the
     permission mirror. Delete the permission check in getPrimaryAction and this
     is the assertion that goes red — the "does not render for nurse" tests
     above would go red too, but a role list would satisfy them. */
  it('the nurse and admin configs still declare the action — the mirror is what stops it', () => {
    expect(getRoleConfig('nurse').primaryAction).toBe('admit')
    expect(getRoleConfig('admin').primaryAction).toBe('admit')
    expect(getPrimaryAction('nurse')).toBeNull()
    expect(getPrimaryAction('admin')).toBeNull()
  })
})

describe('every configured tab is renderable', () => {
  const everyConfiguredId = [...new Set(
    LEGACY.flatMap(([role, department]) => getAccessibleTabs(role, department))
      .concat(getAccessibleTabs('clinician')),
  )]

  it('has metadata in TABS (icon, pillar, label)', () => {
    for (const id of everyConfiguredId) {
      const def = tabDef(id)
      expect(def, `no TABS entry for '${id}'`).toBeTruthy()
      expect(def!.pillar).toBeTruthy()
      expect(def!.icon).toBeTruthy()
    }
  })

  /* A missing key renders the raw key string in the rail — "nav.today" in the
     navigation of a shipping build. */
  it('has an English nav label and page subtitle', () => {
    const nav = en.nav as Record<string, string>
    const subtitle = en.subtitle as Record<string, string>
    for (const id of everyConfiguredId) {
      expect(nav[id], `missing nav.${id}`).toBeTruthy()
      expect(subtitle[id], `missing subtitle.${id}`).toBeTruthy()
    }
  })

  it('has no TABS entry that no role can reach', () => {
    const reachable = new Set(everyConfiguredId)
    // 'today' and the five hospital-module tabs are absent from the reachable
    // set when their panel modules are not installed in this build; every other
    // id in TABS must belong to somebody.
    const orphans = TABS.map(t => t.id).filter(id => !reachable.has(id) && !POST_REFACTOR.has(id))
    expect(orphans).toEqual([])
  })
})

/* ── The hospital-administration modules ──────────────────────────────────── */

describe('hospital modules are registered, and only when they can render', () => {
  afterEach(() => { resetModuleFlags() })

  /* WHY THESE FIXTURES EXIST. Whether a module's panel file is in the build is a
     property of the BUILD, not of this config. A test that reads it from the
     build asserts nothing tonight (the five panels are written by other agents,
     after this file) and asserts something different next week — a test whose
     meaning silently changes, which is the failure class this project keeps
     getting caught by. So the two halves of the gate are supplied explicitly:
     the rule is tested, not the current state of the directory. */
  const ALL_INSTALLED = Object.fromEntries(
    HOSPITAL_MODULE_IDS.map(id => [id, true]),
  ) as Record<HospitalModuleId, boolean>
  const NONE_INSTALLED = Object.fromEntries(
    HOSPITAL_MODULE_IDS.map(id => [id, false]),
  ) as Record<HospitalModuleId, boolean>
  const ALL_ON = Object.fromEntries(
    HOSPITAL_MODULE_IDS.map(id => [id, true]),
  ) as Record<HospitalModuleId, boolean>

  it('every module id has tab metadata, a panel path and an i18n label', () => {
    const nav = en.nav as Record<string, string>
    const subtitle = en.subtitle as Record<string, string>
    for (const id of HOSPITAL_MODULE_IDS) {
      const def = tabDef(id)
      expect(def, `no TABS entry for '${id}'`).toBeTruthy()
      expect(def!.pillar).toBeTruthy()
      expect(def!.icon).toBeTruthy()
      expect(HOSPITAL_MODULE_PANEL_PATH[id]).toMatch(/^\.\/pages\/panels\/.+\.tsx$/)
      expect(nav[id], `missing nav.${id}`).toBeTruthy()
      expect(subtitle[id], `missing subtitle.${id}`).toBeTruthy()
    }
  })

  it('a tab with no panel is not a tab, however the flag is set', () => {
    // THE INVARIANT that lets this nav ship before the five panel files exist,
    // and that stops a slipping module from putting a blank screen in the rail.
    const tabs = getAccessibleTabs('admin', undefined, ALL_ON, NONE_INSTALLED)
    for (const id of HOSPITAL_MODULE_IDS) expect(tabs).not.toContain(id)
  })

  it('every module appears for admin, and the money ones sit in the Hospital group', () => {
    const config = getRoleConfig('admin', undefined, ALL_ON, ALL_INSTALLED)
    const byId = new Map(config.tabs.map(entry => [entry.id, entry]))
    for (const id of HOSPITAL_MODULE_IDS) {
      expect(byId.has(id), `admin is missing '${id}'`).toBe(true)
    }
    // GROUPING IS ASSERTED ONLY FOR THE MONEY MODULES. A clinical-content module
    // (dietary) is grouped 'clinical' on purpose — it belongs beside the ward
    // screens, not beside the invoices. See CLINICAL_MODULE_IDS in shellConfig.
    for (const id of MONEY_MODULE_IDS) {
      expect(byId.get(id)!.group, `'${id}' must be in the Hospital group`).toBe('hospital')
    }
  })

  it('a module tab is absent when the deployment switched it off', () => {
    // Asserted against ALL_INSTALLED so the flag is the ONLY thing under test —
    // otherwise this passes for the wrong reason while the panels are absent.
    for (const id of HOSPITAL_MODULE_IDS) {
      const off = { ...ALL_ON, [id]: false }
      const tabs = getAccessibleTabs('admin', undefined, off, ALL_INSTALLED)
      expect(tabs, `'${id}' survived its own flag being off`).not.toContain(id)
      for (const other of HOSPITAL_MODULE_IDS) {
        if (other !== id) expect(tabs, `'${other}' vanished with '${id}'`).toContain(other)
      }
    }
  })

  it('the flag cache actually drives the rail', () => {
    setModuleFlags({ procurement: false })
    expect(getAccessibleTabs('admin', undefined, currentModuleFlags(), ALL_INSTALLED))
      .not.toContain('procurement')
    resetModuleFlags()
    expect(getAccessibleTabs('admin', undefined, currentModuleFlags(), ALL_INSTALLED))
      .toContain('procurement')
  })

  it('flags default ON — the ruling is "visible by default"', () => {
    const flags = currentModuleFlags()
    expect(Object.keys(flags).sort()).toEqual([...HOSPITAL_MODULE_IDS].sort())
    expect(Object.values(flags).every(Boolean)).toBe(true)
  })

  it('NO clinical role gets a money screen, even with every module on and built', () => {
    // The pilot's premise is that a financial rule never touches a clinical
    // action. Adjacency is part of that: "raise an invoice" must not sit one tap
    // from "administer a dose". Asserted with everything installed and every
    // flag on, so it is the ROLE TABLE being tested and nothing else.
    //
    // ITERATES MONEY_MODULE_IDS, NOT HOSPITAL_MODULE_IDS. Until the dietary
    // module landed those were the same list, and this test asserted the second
    // while its name and its reason meant the first. They are no longer the
    // same, and conflating them would have forced a clinical screen — the one
    // that says who is NIL BY MOUTH — onto an admin-only rail. The exception set
    // is pinned by its own test below, so widening it is not a quiet edit.
    for (const role of ['doctor', 'clinician', 'nurse', 'surgeon', 'pharmacist', '', null]) {
      const tabs = getAccessibleTabs(role, undefined, ALL_ON, ALL_INSTALLED)
      for (const id of MONEY_MODULE_IDS) {
        expect(tabs, `${role || '(no role)'} must not see '${id}'`).not.toContain(id)
      }
    }
    // A surgeon by department is the same doctor experience — check that path too.
    const surgeonTabs = getAccessibleTabs('doctor', 'Surgery', ALL_ON, ALL_INSTALLED)
    for (const id of MONEY_MODULE_IDS) expect(surgeonTabs).not.toContain(id)
  })

  it('the clinical-module exception is exactly one module, and it is dietary', () => {
    // THE GUARD ON THE GUARD. `MONEY_MODULE_IDS` is derived by subtracting this
    // list, so anything added to it silently leaves the money-adjacency test
    // above. Pinning the exact set means putting 'billing' on a nurse's rail
    // requires editing THIS line, in a test whose name says what that means.
    expect([...CLINICAL_MODULE_IDS]).toEqual(['dietary'])
    expect([...MONEY_MODULE_IDS].sort()).toEqual(
      HOSPITAL_MODULE_IDS.filter(id => id !== 'dietary').slice().sort(),
    )
  })

  it('the ward can see who is nil by mouth — dietary reaches doctors and nurses', () => {
    // The reason the exception exists at all. A diet order is a clinical order
    // (the server gates the write on `order.write`, which only doctors and
    // nurses hold), and the person who most needs to see who cannot eat is the
    // nurse walking the ward at 07:00 with a trolley. An admin-only Diet tab
    // puts that screen in an office.
    for (const role of ['doctor', 'clinician', 'nurse', 'admin']) {
      expect(
        getAccessibleTabs(role, undefined, ALL_ON, ALL_INSTALLED),
        `${role} must be able to reach the diet screen`,
      ).toContain('dietary')
    }
    // And it still obeys both halves of the module gate.
    expect(getAccessibleTabs('nurse', undefined, ALL_ON, NONE_INSTALLED)).not.toContain('dietary')
    expect(getAccessibleTabs('nurse', undefined, { ...ALL_ON, dietary: false }, ALL_INSTALLED))
      .not.toContain('dietary')
  })

  it('the panel lookup matches the installed set', () => {
    for (const id of HOSPITAL_MODULE_IDS) {
      expect(modulePanel(id) !== null).toBe(hospitalModuleInstalled[id])
    }
    // A non-module tab never resolves to a module panel.
    expect(modulePanel('vigil')).toBeNull()
  })

  it('a runtime override can hide a module without a rebuild', () => {
    window.__ORB_MODULES__ = { procurement: false }
    resetModuleFlags()
    expect(currentModuleFlags().procurement).toBe(false)
    expect(currentModuleFlags().billing).toBe(true)
    delete window.__ORB_MODULES__
    resetModuleFlags()
    expect(currentModuleFlags().procurement).toBe(true)
  })

  it('refreshModuleFlags adopts the server answer', async () => {
    const fakeFetch = (async () => ({
      ok: true,
      json: async () => ({ modules: { billing: true, payments: false, payer: true, procurement: false, frontdesk: true } }),
    })) as unknown as typeof fetch
    await refreshModuleFlags(fakeFetch)
    expect(currentModuleFlags().payments).toBe(false)
    expect(currentModuleFlags().procurement).toBe(false)
    expect(currentModuleFlags().billing).toBe(true)
  })

  it('an unreachable server leaves the defaults alone — a blip must not hide a module', () => {
    // THE FAILURE: a network error read as "no modules", and a hospital's
    // billing screen vanishing because a health probe timed out.
    const throwingFetch = (async () => { throw new Error('offline') }) as unknown as typeof fetch
    return refreshModuleFlags(throwingFetch).then(flags => {
      expect(Object.values(flags).every(Boolean)).toBe(true)
    })
  })

  it('a non-OK health response leaves the defaults alone', async () => {
    const badFetch = (async () => ({ ok: false, json: async () => ({}) })) as unknown as typeof fetch
    await refreshModuleFlags(badFetch)
    expect(Object.values(currentModuleFlags()).every(Boolean)).toBe(true)
  })
})

/* ── The cashier ──────────────────────────────────────────────────────────── */

describe('the cashier lands on Payments and sees nothing else', () => {
  /* THE FAILURE: the server staffs cashiers (server/roles.py, permissions.py
     "cashier": payments.read, .record, .close_counter) but this table had no
     cashier row, so the account fell through to the fallback: Vigil and Relay,
     both 403 for it, and no Payments tab at all. Fixtures, not the build, for
     the reason given in the hospital-modules block above. */
  const ALL = Object.fromEntries(HOSPITAL_MODULE_IDS.map(id => [id, true])) as Record<HospitalModuleId, boolean>

  it('has exactly one tab, Payments, and lands on it', () => {
    expect(getAccessibleTabs('cashier', undefined, ALL, ALL)).toEqual(['payments'])
    const config = getRoleConfig('cashier', undefined, ALL, ALL)
    expect(config.home).toBe('payments')
    expect(config.tabs.map(entry => entry.group)).toEqual(['hospital'])
  })

  it('never falls back to the clinical tabs it cannot open', () => {
    expect(getAccessibleTabs('cashier', undefined, ALL, ALL)).not.toEqual(getAccessibleTabs(null))
    const paymentsOff = { ...ALL, payments: false }
    expect(getAccessibleTabs('cashier', undefined, paymentsOff, ALL)).toEqual([])
  })

  it('is offered no admit action, by the config and by the permission mirror', () => {
    expect(getRoleConfig('cashier').primaryAction).toBeNull()
    expect(getPrimaryAction('cashier')).toBeNull()
  })
})
