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
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

vi.mock('./api/client', () => ({ getUser: () => null }))

import {
  CLINICAL_MODULE_IDS,
  DEFAULT_FEATURE_FLAGS,
  DEFAULT_MODULE_FLAGS,
  DEFAULT_PLATFORM_FLAGS,
  DIAGNOSTIC_MODULE_IDS,
  FEATURE_FLAG_IDS,
  FEATURE_TAB_FLAG,
  FEATURE_TAB_IDS,
  FEATURE_TAB_PANEL_PATH,
  HOSPITAL_MODULE_IDS,
  HOSPITAL_MODULE_PANEL_PATH,
  MONEY_MODULE_IDS,
  OPT_IN_MODULE_IDS,
  PHARMACY_DESK_MODULE_IDS,
  TABS,
  currentFeatureFlags,
  currentHealthProbe,
  currentModuleFlags,
  currentPlatformFlags,
  featurePanel,
  featureTabInstalled,
  getModuleOffNotice,
  isOptInModule,
  isRoleFeatureOn,
  offersFloatingSage,
  resetFeatureFlags,
  resetPlatformFlags,
  setFeatureFlags,
  setPlatformFlags,
  subscribeModuleFlags,
  type FeatureFlagId,
  type FeatureTabId,
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
import hi from './i18n/locales/hi.json'

/** Tabs that post-date the switch statement and are therefore not part of the
 *  behaviour-neutrality comparison: the doctor home, the hospital modules and
 *  the feature tabs (OPD), each registered only when its own panel module is in
 *  the build. */
const POST_REFACTOR = new Set<string>(['today', ...HOSPITAL_MODULE_IDS, ...FEATURE_TAB_IDS])

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
      .concat(getAccessibleTabs('clinician'))
      // The clerk's Observations tab needs no module, so it is on her rail here.
      .concat(getAccessibleTabs('ward_clerk', 'General Medicine')),
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

  /* AMENDED 3 October 2026 (HIS build plan, decision X1). The 2026-08-11
     ruling "visible by default" still holds for every module that existed
     then; Samuel departed from it for the three opt-in modules only (Beds,
     Pharmacy store, Laboratory), which default OFF on the server and here. */
  it('every non-opt-in module defaults ON, every opt-in module defaults OFF', () => {
    const flags = currentModuleFlags()
    expect(Object.keys(flags).sort()).toEqual([...HOSPITAL_MODULE_IDS].sort())
    for (const id of HOSPITAL_MODULE_IDS) {
      expect(flags[id], `'${id}' default`).toBe(!OPT_IN_MODULE_IDS.includes(id))
    }
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
    //
    // THE ONE PINNED EXCEPTION (GRN-R9, 3 October 2026): the pharmacist's rail
    // carries the Pharmacy store, PHARMACY_DESK_MODULE_IDS, and nothing else
    // from this list. Pinned by its own test below, next to the dietary one.
    for (const role of ['doctor', 'clinician', 'nurse', 'surgeon', 'pharmacist', '', null]) {
      const tabs = getAccessibleTabs(role, undefined, ALL_ON, ALL_INSTALLED)
      const exempt: readonly string[] = role === 'pharmacist' ? PHARMACY_DESK_MODULE_IDS : []
      for (const id of MONEY_MODULE_IDS) {
        if (exempt.includes(id)) continue
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
    // The laboratory is subtracted too, as a DIAGNOSTIC module (pinned below):
    // it is neither a money screen nor a clinician's tab.
    expect([...MONEY_MODULE_IDS].sort()).toEqual(
      HOSPITAL_MODULE_IDS.filter(id => id !== 'dietary' && id !== 'lab').slice().sort(),
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

  /* AMENDED 3 October 2026, as above: a failed probe keeps every non-opt-in
     module ON (a blip must not hide a module) and every opt-in module OFF (a
     tab the server never confirmed would open onto 404s). */
  it('an unreachable server leaves the defaults alone — a blip must not hide a module', () => {
    // THE FAILURE: a network error read as "no modules", and a hospital's
    // billing screen vanishing because a health probe timed out.
    const throwingFetch = (async () => { throw new Error('offline') }) as unknown as typeof fetch
    return refreshModuleFlags(throwingFetch).then(flags => {
      for (const id of HOSPITAL_MODULE_IDS) {
        expect(flags[id], `'${id}' after an unreachable server`).toBe(!OPT_IN_MODULE_IDS.includes(id))
      }
    })
  })

  it('a non-OK health response leaves the defaults alone', async () => {
    const badFetch = (async () => ({ ok: false, json: async () => ({}) })) as unknown as typeof fetch
    await refreshModuleFlags(badFetch)
    for (const id of HOSPITAL_MODULE_IDS) {
      expect(currentModuleFlags()[id], `'${id}' after a non-OK answer`).toBe(!OPT_IN_MODULE_IDS.includes(id))
    }
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

/* ── The HIS platform (build plan of 3 October 2026, wave 1) ──────────────── */

/** Fixtures for the HIS blocks below. Built from the id lists, never written
 *  out, so a fourth opt-in module is covered the day it lands. */
const MODULES_ALL_ON = Object.fromEntries(
  HOSPITAL_MODULE_IDS.map(id => [id, true]),
) as Record<HospitalModuleId, boolean>
const MODULES_ALL_INSTALLED = MODULES_ALL_ON
const MODULES_DEFAULT = { ...DEFAULT_MODULE_FLAGS }
const MODULES_NONE_INSTALLED = Object.fromEntries(
  HOSPITAL_MODULE_IDS.map(id => [id, false]),
) as Record<HospitalModuleId, boolean>
const FEATURES_ALL_ON = Object.fromEntries(
  FEATURE_FLAG_IDS.map(id => [id, true]),
) as Record<FeatureFlagId, boolean>
const FEATURES_OFF = { ...DEFAULT_FEATURE_FLAGS }
const FEATURE_TABS_INSTALLED = Object.fromEntries(
  FEATURE_TAB_IDS.map(id => [id, true]),
) as Record<FeatureTabId, boolean>
const FEATURE_TABS_ABSENT = Object.fromEntries(
  FEATURE_TAB_IDS.map(id => [id, false]),
) as Record<FeatureTabId, boolean>

const HIS_ROLES = ['receptionist', 'ward_clerk', 'lab_technician', 'pathologist', 'phlebotomist'] as const
const LAB_ROLES = ['lab_technician', 'pathologist', 'phlebotomist'] as const
const CLINICAL_ROLES = ['doctor', 'clinician', 'nurse', 'surgeon'] as const

describe('the opt-in modules default off and fail toward hidden', () => {
  afterEach(() => {
    delete window.__ORB_MODULES__
    resetModuleFlags()
    resetFeatureFlags()
  })

  it('are exactly Beds, the Pharmacy store and the Laboratory, and match the server keys', () => {
    // server/config.py OPT_IN_MODULES publishes these exact keys inside
    // 'modules' on /api/health. A rename on either side must fail here.
    expect([...OPT_IN_MODULE_IDS]).toEqual(['adt', 'pharmacy-store', 'lab'])
    for (const id of OPT_IN_MODULE_IDS) {
      expect(HOSPITAL_MODULE_IDS).toContain(id)
      expect(isOptInModule(id)).toBe(true)
    }
  })

  it('the default table is derived: every opt-in id off, every other id on', () => {
    expect(Object.keys(DEFAULT_MODULE_FLAGS).sort()).toEqual([...HOSPITAL_MODULE_IDS].sort())
    for (const id of HOSPITAL_MODULE_IDS) {
      expect(DEFAULT_MODULE_FLAGS[id]).toBe(!OPT_IN_MODULE_IDS.includes(id))
    }
  })

  it('their panels are the files the later waves build', () => {
    expect(HOSPITAL_MODULE_PANEL_PATH.adt).toBe('./pages/panels/BedBoardPanel.tsx')
    expect(HOSPITAL_MODULE_PANEL_PATH['pharmacy-store']).toBe('./pages/panels/PharmacyStorePanel.tsx')
    expect(HOSPITAL_MODULE_PANEL_PATH.lab).toBe('./pages/panels/LabPanel.tsx')
  })

  it('no opt-in tab reaches any rail on the default flags, however much is installed', () => {
    const everyRole = [...CLINICAL_ROLES, 'admin', 'pharmacist', 'cashier', ...HIS_ROLES, '', null]
    for (const role of everyRole) {
      const tabs = getAccessibleTabs(role, undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, FEATURES_ALL_ON)
      for (const id of OPT_IN_MODULE_IDS) {
        expect(tabs, `${role || '(no role)'} got '${id}' with its flag at the default`).not.toContain(id)
      }
    }
  })

  it('a flag table that does not mention an opt-in module leaves it off', () => {
    // An older server publishes only the eleven; a partial fixture likewise.
    // Every other module treats "not mentioned" as on; an opt-in module must not.
    const elevenOnly = Object.fromEntries(
      HOSPITAL_MODULE_IDS.filter(id => !isOptInModule(id)).map(id => [id, true]),
    ) as Record<HospitalModuleId, boolean>
    const tabs = getAccessibleTabs('admin', undefined, elevenOnly, MODULES_ALL_INSTALLED)
    for (const id of OPT_IN_MODULE_IDS) expect(tabs).not.toContain(id)
    expect(tabs).toContain('billing')
  })

  it('switched on by the server and installed, each appears for admin in the Hospital group', async () => {
    const fakeFetch = (async () => ({
      ok: true,
      json: async () => ({ modules: { adt: true, 'pharmacy-store': true, lab: true } }),
    })) as unknown as typeof fetch
    await refreshModuleFlags(fakeFetch)
    expect(currentModuleFlags().adt).toBe(true)
    const config = getRoleConfig('admin', undefined, currentModuleFlags(), MODULES_ALL_INSTALLED)
    for (const id of OPT_IN_MODULE_IDS) {
      const entry = config.tabs.find(tab => tab.id === id)
      expect(entry, `admin is missing '${id}'`).toBeTruthy()
      expect(entry!.group).toBe('hospital')
    }
    // ...and still nothing without its panel file.
    const none = getAccessibleTabs('admin', undefined, currentModuleFlags(), MODULES_NONE_INSTALLED)
    for (const id of OPT_IN_MODULE_IDS) expect(none).not.toContain(id)
  })

  it('a build list or a runtime patch can switch an opt-in module off, never on', () => {
    window.__ORB_MODULES__ = { adt: true, lab: true, billing: false }
    resetModuleFlags()
    expect(currentModuleFlags().adt).toBe(false)
    expect(currentModuleFlags().lab).toBe(false)
    expect(currentModuleFlags().billing).toBe(false)
    // The server can still switch it on afterwards...
    setModuleFlags({ adt: true })
    expect(currentModuleFlags().adt).toBe(true)
    // ...and an override can still take it back off.
    window.__ORB_MODULES__ = { adt: false }
    resetModuleFlags()
    expect(currentModuleFlags().adt).toBe(false)
  })

  /* The shell probes /api/health on every mount, so an override has to hold
     THROUGH the probe, not only until it answers. Before the fix the server's
     'true' was merged over the override and both modules came back. */
  it('a runtime off-override still holds after the health probe answers true', async () => {
    window.__ORB_MODULES__ = { procurement: false, adt: false }
    resetModuleFlags()
    expect(currentModuleFlags().procurement).toBe(false)
    expect(currentModuleFlags().adt).toBe(false)
    const fakeFetch = (async () => ({
      ok: true,
      json: async () => ({ modules: { procurement: true, adt: true, billing: true } }),
    })) as unknown as typeof fetch
    await refreshModuleFlags(fakeFetch)
    expect(currentHealthProbe()).toBe('answered')
    // A standard module and an opt-in module both stay off...
    expect(currentModuleFlags().procurement).toBe(false)
    expect(currentModuleFlags().adt).toBe(false)
    // ...and the rest of the server's answer is still adopted.
    expect(currentModuleFlags().billing).toBe(true)
  })

  it('a build allow-list still holds after the health probe answers true', async () => {
    vi.stubEnv('VITE_ORB_MODULES', 'billing,payments')
    try {
      resetModuleFlags()
      const fakeFetch = (async () => ({
        ok: true,
        json: async () => ({ modules: { billing: true, payments: true, procurement: true, adt: true } }),
      })) as unknown as typeof fetch
      await refreshModuleFlags(fakeFetch)
      expect(currentModuleFlags().billing).toBe(true)
      expect(currentModuleFlags().payments).toBe(true)
      // Not on the build list, so off whatever the server says.
      expect(currentModuleFlags().procurement).toBe(false)
      expect(currentModuleFlags().adt).toBe(false)
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('an override cannot switch an opt-in module on through the probe either', async () => {
    window.__ORB_MODULES__ = { lab: true }
    resetModuleFlags()
    const fakeFetch = (async () => ({
      ok: true,
      json: async () => ({ modules: { lab: false, adt: true } }),
    })) as unknown as typeof fetch
    await refreshModuleFlags(fakeFetch)
    // The server said off, and the override's 'on' was dropped.
    expect(currentModuleFlags().lab).toBe(false)
    // With no override naming it, the server alone switches an opt-in module on.
    expect(currentModuleFlags().adt).toBe(true)
  })

  it('this build installs no opt-in panel except those whose files exist', () => {
    for (const id of OPT_IN_MODULE_IDS) {
      expect(modulePanel(id) !== null).toBe(hospitalModuleInstalled[id])
    }
  })
})

describe('the pharmacy-desk exception (GRN-R9) is exactly one module and one role', () => {
  it('is pinned to the Pharmacy store alone', () => {
    // THE GUARD ON THE SECOND EXCEPTION, beside the dietary one. Adding an id
    // here would put another money screen on the pharmacist's rail.
    expect([...PHARMACY_DESK_MODULE_IDS]).toEqual(['pharmacy-store'])
    // It is NOT subtracted from the money list: for every other role the
    // Pharmacy store is still a money screen.
    expect(MONEY_MODULE_IDS).toContain('pharmacy-store')
  })

  it('the pharmacist gets it after the verify queue, only when on and installed', () => {
    expect(getAccessibleTabs('pharmacist', undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED))
      .toEqual(['helix', 'pharmacy-verify', 'pharmacy-store', 'sage'])
    expect(getAccessibleTabs('pharmacist', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED))
      .toEqual(['helix', 'pharmacy-verify', 'sage'])
    expect(getAccessibleTabs('pharmacist', undefined, MODULES_ALL_ON, MODULES_NONE_INSTALLED))
      .toEqual(['helix', 'pharmacy-verify', 'sage'])
  })

  it('no other non-admin role ever sees it', () => {
    for (const role of [...CLINICAL_ROLES, 'cashier', ...HIS_ROLES, '', null]) {
      expect(
        getAccessibleTabs(role, undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON),
        `${role || '(no role)'} must not see the Pharmacy store`,
      ).not.toContain('pharmacy-store')
    }
    expect(getAccessibleTabs('doctor', 'Surgery', MODULES_ALL_ON, MODULES_ALL_INSTALLED))
      .not.toContain('pharmacy-store')
  })

  it('the pharmacist receives no other money screen with everything on', () => {
    const tabs = getAccessibleTabs('pharmacist', undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON)
    const money = tabs.filter(id => (MONEY_MODULE_IDS as readonly string[]).includes(id))
    expect(money).toEqual([...PHARMACY_DESK_MODULE_IDS])
  })
})

describe('the laboratory is a diagnostic module: lab staff and admin only', () => {
  it('is the one diagnostic module', () => {
    expect([...DIAGNOSTIC_MODULE_IDS]).toEqual(['lab'])
    expect(MONEY_MODULE_IDS).not.toContain('lab')
    expect(CLINICAL_MODULE_IDS).not.toContain('lab')
  })

  it('never reaches a clinical rail or the pharmacist, even with everything on', () => {
    // Doctors and nurses reach lab work through Orders and the chart
    // (LIS design), never through this tab.
    for (const role of [...CLINICAL_ROLES, 'pharmacist', 'cashier', 'receptionist', 'ward_clerk', '', null]) {
      expect(
        getAccessibleTabs(role, undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON),
        `${role || '(no role)'} must not see the Laboratory`,
      ).not.toContain('lab')
    }
  })

  it.each(LAB_ROLES)('%s gets exactly the Laboratory, lands on it, and has no admit action', role => {
    expect(getAccessibleTabs(role, undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED)).toEqual(['lab'])
    expect(getRoleConfig(role, undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED).home).toBe('lab')
    expect(getRoleConfig(role).primaryAction).toBeNull()
    expect(getPrimaryAction(role)).toBeNull()
  })
})

describe('feature flags default off, and only the server can switch one on', () => {
  // Both sides: the module-level cache outlives a test, and the probe-state
  // case below must start from 'pending' whatever ran before it.
  beforeEach(() => {
    resetModuleFlags()
    resetFeatureFlags()
  })
  afterEach(() => {
    resetModuleFlags()
    resetFeatureFlags()
  })

  it('every feature defaults off', () => {
    expect(Object.keys(DEFAULT_FEATURE_FLAGS).sort()).toEqual([...FEATURE_FLAG_IDS].sort())
    expect(Object.values(DEFAULT_FEATURE_FLAGS).some(Boolean)).toBe(false)
    expect(Object.values(currentFeatureFlags()).some(Boolean)).toBe(false)
  })

  it('names the server features the shell and later waves read', () => {
    // server/config.py feature_flags(), published as 'features' on /api/health.
    for (const id of ['registration', 'opd'] as const) expect(FEATURE_FLAG_IDS).toContain(id)
  })

  it('refreshModuleFlags adopts the features from the same answer', async () => {
    const fakeFetch = (async () => ({
      ok: true,
      json: async () => ({ modules: {}, features: { registration: true, opd: true, not_a_feature: true } }),
    })) as unknown as typeof fetch
    await refreshModuleFlags(fakeFetch)
    expect(currentFeatureFlags().registration).toBe(true)
    expect(currentFeatureFlags().opd).toBe(true)
    expect(currentFeatureFlags().lab_portal_reports).toBe(false)
    expect(Object.keys(currentFeatureFlags())).not.toContain('not_a_feature')
  })

  it('only a strict boolean counts', () => {
    setFeatureFlags({ opd: 'yes' as unknown as boolean, registration: 1 as unknown as boolean })
    expect(currentFeatureFlags().opd).toBe(false)
    expect(currentFeatureFlags().registration).toBe(false)
  })

  it('a failed, non-OK or module-less answer leaves every feature off', async () => {
    const throwing = (async () => { throw new Error('offline') }) as unknown as typeof fetch
    await refreshModuleFlags(throwing)
    expect(Object.values(currentFeatureFlags()).some(Boolean)).toBe(false)
    const bad = (async () => ({ ok: false, json: async () => ({ features: { opd: true } }) })) as unknown as typeof fetch
    await refreshModuleFlags(bad)
    expect(currentFeatureFlags().opd).toBe(false)
    const noModules = (async () => ({ ok: true, json: async () => ({ features: { opd: true } }) })) as unknown as typeof fetch
    await refreshModuleFlags(noModules)
    expect(currentFeatureFlags().opd).toBe(false)
  })

  it('the probe state reads pending, then answered or failed', async () => {
    expect(currentHealthProbe()).toBe('pending')
    const ok = (async () => ({ ok: true, json: async () => ({ modules: {} }) })) as unknown as typeof fetch
    await refreshModuleFlags(ok)
    expect(currentHealthProbe()).toBe('answered')
    const bad = (async () => ({ ok: false, json: async () => ({}) })) as unknown as typeof fetch
    await refreshModuleFlags(bad)
    expect(currentHealthProbe()).toBe('failed')
    resetFeatureFlags()
    expect(currentHealthProbe()).toBe('pending')
    await refreshModuleFlags(null as unknown as typeof fetch)
    // No fetch at all is not "pending forever": nothing can be checked.
    expect(currentHealthProbe()).toBe('failed')
  })
})

describe('platform flags (multi-site) default off and come only from the probe', () => {
  beforeEach(() => {
    resetModuleFlags()
    resetFeatureFlags()
    resetPlatformFlags()
  })
  afterEach(() => {
    resetModuleFlags()
    resetFeatureFlags()
    resetPlatformFlags()
  })

  it('both default off, so a single-site install shows no facility label', () => {
    expect(DEFAULT_PLATFORM_FLAGS).toEqual({ multiSite: false, multiFacilityData: false })
    expect(currentPlatformFlags()).toEqual({ multiSite: false, multiFacilityData: false })
    expect(FEATURE_FLAG_IDS).toContain('multi_site')
    expect(currentFeatureFlags().multi_site).toBe(false)
  })

  it('refreshModuleFlags adopts the platform object from the same answer', async () => {
    const fakeFetch = (async () => ({
      ok: true,
      json: async () => ({
        modules: {},
        features: { multi_site: true },
        platform: { multi_site: true, multi_facility_data: true },
      }),
    })) as unknown as typeof fetch
    await refreshModuleFlags(fakeFetch)
    expect(currentPlatformFlags()).toEqual({ multiSite: true, multiFacilityData: true })
    expect(currentFeatureFlags().multi_site).toBe(true)
  })

  it('only a strict boolean counts, and a missing key leaves the flag as it was', () => {
    setPlatformFlags({ multi_site: 'yes', multi_facility_data: 1 })
    expect(currentPlatformFlags()).toEqual({ multiSite: false, multiFacilityData: false })
    setPlatformFlags({ multi_facility_data: true })
    expect(currentPlatformFlags()).toEqual({ multiSite: false, multiFacilityData: true })
    setPlatformFlags({})
    expect(currentPlatformFlags().multiFacilityData).toBe(true)
  })

  it('a failed or module-less answer leaves them off', async () => {
    const throwing = (async () => { throw new Error('offline') }) as unknown as typeof fetch
    await refreshModuleFlags(throwing)
    expect(currentPlatformFlags().multiFacilityData).toBe(false)
    const noModules = (async () => ({
      ok: true, json: async () => ({ platform: { multi_facility_data: true } }),
    })) as unknown as typeof fetch
    await refreshModuleFlags(noModules)
    expect(currentPlatformFlags().multiFacilityData).toBe(false)
  })

  it('every change notifies the one listener set', () => {
    const seen: number[] = []
    const off = subscribeModuleFlags(() => seen.push(1))
    setPlatformFlags({ multi_site: true })
    setPlatformFlags({ multi_site: true })
    resetPlatformFlags()
    off()
    expect(seen.length).toBe(2)
  })
})

describe('the OPD feature tab', () => {
  it('waits on the opd feature and its panel file', () => {
    expect([...FEATURE_TAB_IDS]).toEqual(['opd'])
    expect(FEATURE_TAB_FLAG.opd).toBe('opd')
    expect(FEATURE_TAB_PANEL_PATH.opd).toBe('./pages/panels/OpdPanel.tsx')
    expect(featurePanel('opd') !== null).toBe(featureTabInstalled.opd)
    expect(featurePanel('vigil')).toBeNull()
  })

  it('is hidden by default, and CLINICAL_MODULE_IDS is untouched', () => {
    expect(getAccessibleTabs('doctor')).not.toContain('opd')
    expect(getAccessibleTabs('doctor', undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_OFF, FEATURE_TABS_INSTALLED))
      .not.toContain('opd')
    expect([...CLINICAL_MODULE_IDS]).toEqual(['dietary'])
  })

  it('is hidden with the feature on while its panel is absent', () => {
    expect(getAccessibleTabs('doctor', undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON, FEATURE_TABS_ABSENT))
      .not.toContain('opd')
  })

  it('reaches the doctor, the clinician alias and a surgery doctor, after the round', () => {
    for (const [role, department] of [['doctor', undefined], ['clinician', undefined], ['doctor', 'Surgery']] as const) {
      const tabs = getAccessibleTabs(role, department, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON, FEATURE_TABS_INSTALLED)
      expect(tabs, `${role} in ${department ?? 'no department'}`).toContain('opd')
      expect(tabs.indexOf('opd')).toBe(tabs.indexOf('round') + 1)
    }
  })

  it('reaches nobody else', () => {
    for (const role of ['nurse', 'admin', 'pharmacist', 'surgeon', 'cashier', ...HIS_ROLES, '', null]) {
      expect(
        getAccessibleTabs(role, undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON, FEATURE_TABS_INSTALLED),
        `${role || '(no role)'} must not see OPD`,
      ).not.toContain('opd')
    }
  })

  it('has tab metadata and an English and Hindi label', () => {
    for (const id of FEATURE_TAB_IDS) {
      const def = tabDef(id)
      expect(def, `no TABS entry for '${id}'`).toBeTruthy()
      expect(def!.pillar).toBeTruthy()
      expect(def!.icon).toBeTruthy()
      expect((en.nav as Record<string, string>)[id]).toBeTruthy()
      expect((en.subtitle as Record<string, string>)[id]).toBeTruthy()
      expect((hi.nav as Record<string, string>)[id]).toBeTruthy()
      expect((hi.subtitle as Record<string, string>)[id]).toBeTruthy()
    }
  })
})

describe('the HIS roles: one screen each, live only while their feature is on', () => {
  afterEach(() => {
    resetModuleFlags()
    resetFeatureFlags()
  })

  it('the receptionist gets exactly Front Desk with registration on, nothing with it off', () => {
    const on = { ...FEATURES_OFF, registration: true }
    expect(getAccessibleTabs('receptionist', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, on)).toEqual(['frontdesk'])
    expect(getRoleConfig('receptionist', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, on).home).toBe('frontdesk')
    expect(getAccessibleTabs('receptionist', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, FEATURES_OFF)).toEqual([])
  })

  it('the receptionist never sees a money screen or a clinical tab', () => {
    const tabs = getAccessibleTabs('receptionist', undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON, FEATURE_TABS_INSTALLED)
    expect(tabs).toEqual(['frontdesk'])
    for (const id of ['billing', 'payments', 'payer', 'vigil', 'relay', 'sage']) expect(tabs).not.toContain(id)
  })

  it('the ward clerk gets Beds with the module on and built, and her Observations tab either way', () => {
    // Observations (9 October 2026) is a permission tab: it needs no module.
    expect(getAccessibleTabs('ward_clerk', 'Cardiology', MODULES_ALL_ON, MODULES_ALL_INSTALLED))
      .toEqual(['adt', 'observations'])
    expect(getRoleConfig('ward_clerk', 'Cardiology', MODULES_ALL_ON, MODULES_ALL_INSTALLED).home).toBe('adt')
    expect(getAccessibleTabs('ward_clerk', 'Cardiology', MODULES_DEFAULT, MODULES_ALL_INSTALLED)).toEqual(['observations'])
    expect(getRoleConfig('ward_clerk', 'Cardiology', MODULES_DEFAULT, MODULES_ALL_INSTALLED).home).toBe('observations')
    expect(getAccessibleTabs('ward_clerk', 'Cardiology', MODULES_ALL_ON, MODULES_NONE_INSTALLED)).toEqual(['observations'])
  })

  it('the Observations tab goes only to an account that holds vitals.transcribe', () => {
    for (const role of ['doctor', 'nurse', 'admin', 'pharmacist', 'receptionist', 'lab_technician', '', null]) {
      expect(getAccessibleTabs(role, undefined, MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_ALL_ON))
        .not.toContain('observations')
    }
  })

  it.each(HIS_ROLES)('%s never falls back to the clinical tabs it cannot open', role => {
    // The clerk keeps her own Observations tab, which needs no module.
    expect(getAccessibleTabs(role, undefined, MODULES_DEFAULT, MODULES_NONE_INSTALLED, FEATURES_OFF))
      .toEqual(role === 'ward_clerk' ? ['observations'] : [])
    expect(getRoleConfig(role).primaryAction).toBeNull()
    expect(getPrimaryAction(role)).toBeNull()
  })

  it.each(HIS_ROLES)('%s carries no floating Sage', role => {
    expect(offersFloatingSage(role)).toBe(false)
  })

  it('every older role keeps the floating Sage exactly as before', () => {
    for (const role of ['doctor', 'clinician', 'nurse', 'admin', 'pharmacist', 'cashier', 'surgeon', '', null]) {
      expect(offersFloatingSage(role)).toBe(true)
    }
  })

  it('isRoleFeatureOn reads modules for adt and lab, features for registration', () => {
    expect(isRoleFeatureOn('doctor', MODULES_DEFAULT, FEATURES_OFF)).toBe(true)
    expect(isRoleFeatureOn('cashier', MODULES_DEFAULT, FEATURES_OFF)).toBe(true)
    expect(isRoleFeatureOn('ward_clerk', MODULES_DEFAULT, FEATURES_ALL_ON)).toBe(false)
    expect(isRoleFeatureOn('ward_clerk', { ...MODULES_DEFAULT, adt: true }, FEATURES_OFF)).toBe(true)
    expect(isRoleFeatureOn('pathologist', { ...MODULES_DEFAULT, lab: true }, FEATURES_OFF)).toBe(true)
    expect(isRoleFeatureOn('pathologist', { ...MODULES_DEFAULT, adt: true }, FEATURES_ALL_ON)).toBe(false)
    expect(isRoleFeatureOn('receptionist', MODULES_ALL_ON, FEATURES_OFF)).toBe(false)
    expect(isRoleFeatureOn('Receptionist', MODULES_DEFAULT, { ...FEATURES_OFF, registration: true })).toBe(true)
  })

  it('every HIS role has an English and a Hindi role label', () => {
    for (const role of HIS_ROLES) {
      expect((en.role as Record<string, string>)[role], `en role.${role}`).toBeTruthy()
      expect((hi.role as Record<string, string>)[role], `hi role.${role}`).toBeTruthy()
    }
  })

  it('every opt-in module has an English and a Hindi nav label and subtitle', () => {
    for (const id of OPT_IN_MODULE_IDS) {
      expect((en.nav as Record<string, string>)[id]).toBeTruthy()
      expect((hi.nav as Record<string, string>)[id]).toBeTruthy()
      expect((en.subtitle as Record<string, string>)[id]).toBeTruthy()
      expect((hi.subtitle as Record<string, string>)[id]).toBeTruthy()
    }
    expect((en.nav as Record<string, string>).adt).toBe('Beds')
  })
})

describe('the switched-off notice names why a HIS role has no screen', () => {
  it('says checking before the server answers', () => {
    expect(getModuleOffNotice('lab_technician', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, FEATURES_OFF, 'pending'))
      .toEqual({ subject: 'lab', state: 'checking', home: 'lab' })
  })

  it('is never shown to the ward clerk, who keeps her Observations tab with Beds off', () => {
    for (const probe of ['pending', 'answered', 'failed'] as const) {
      expect(getModuleOffNotice('ward_clerk', 'Cardiology', MODULES_DEFAULT, MODULES_ALL_INSTALLED, FEATURES_OFF, probe))
        .toBeNull()
    }
    expect(getModuleOffNotice('ward_clerk', 'Cardiology', { ...MODULES_DEFAULT, adt: true }, MODULES_NONE_INSTALLED,
      FEATURES_OFF, 'answered')).toBeNull()
  })

  it('says off once the server answers off', () => {
    for (const role of LAB_ROLES) {
      expect(getModuleOffNotice(role, undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, FEATURES_OFF, 'answered'))
        .toEqual({ subject: 'lab', state: 'off', home: 'lab' })
    }
    expect(getModuleOffNotice('receptionist', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, FEATURES_OFF, 'answered'))
      .toEqual({ subject: 'registration', state: 'off', home: 'frontdesk' })
  })

  it('never claims off when the server could not be asked', () => {
    expect(getModuleOffNotice('lab_technician', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED, FEATURES_OFF, 'failed'))
      .toEqual({ subject: 'lab', state: 'unreachable', home: 'lab' })
  })

  it('says unavailable when the feature is on but the screen cannot open', () => {
    // ORB_LAB on, LabPanel.tsx not yet in the build.
    expect(getModuleOffNotice('pathologist', undefined, { ...MODULES_DEFAULT, lab: true }, MODULES_NONE_INSTALLED, FEATURES_OFF, 'answered'))
      .toEqual({ subject: 'lab', state: 'unavailable', home: 'lab' })
    // Registration on, the Front Desk module switched off.
    expect(getModuleOffNotice('receptionist', undefined, { ...MODULES_DEFAULT, frontdesk: false }, MODULES_ALL_INSTALLED,
      { ...FEATURES_OFF, registration: true }, 'answered'))
      .toEqual({ subject: 'registration', state: 'unavailable', home: 'frontdesk' })
  })

  it('is absent once the role has its screen', () => {
    expect(getModuleOffNotice('ward_clerk', 'Cardiology', MODULES_ALL_ON, MODULES_ALL_INSTALLED, FEATURES_OFF, 'answered')).toBeNull()
    expect(getModuleOffNotice('receptionist', undefined, MODULES_DEFAULT, MODULES_ALL_INSTALLED,
      { ...FEATURES_OFF, registration: true }, 'answered')).toBeNull()
  })

  it('is never shown to an older role, not even the cashier with Payments off', () => {
    // Behaviour for roles that existed before the HIS plan is unchanged.
    for (const role of ['doctor', 'clinician', 'nurse', 'admin', 'pharmacist', 'surgeon', '', null]) {
      expect(getModuleOffNotice(role, undefined, MODULES_DEFAULT, MODULES_NONE_INSTALLED, FEATURES_OFF, 'answered')).toBeNull()
    }
    expect(getModuleOffNotice('cashier', undefined, { ...MODULES_DEFAULT, payments: false }, MODULES_ALL_INSTALLED, FEATURES_OFF, 'answered'))
      .toBeNull()
  })

  it('has an English and a Hindi sentence for every state and subject', () => {
    const enMo = en.moduleOff as unknown as Record<string, unknown>
    const hiMo = hi.moduleOff as unknown as Record<string, unknown>
    for (const key of ['checking', 'off', 'offHint', 'unavailable', 'unavailableHint', 'unreachable', 'unreachableHint']) {
      expect(enMo[key], `en moduleOff.${key}`).toBeTruthy()
      expect(hiMo[key], `hi moduleOff.${key}`).toBeTruthy()
    }
    for (const subject of ['registration', 'adt', 'lab', 'opd']) {
      expect((enMo.name as Record<string, string>)[subject]).toBeTruthy()
      expect((hiMo.name as Record<string, string>)[subject]).toBeTruthy()
    }
    // House rule: no em dash anywhere in what this wave added.
    expect(JSON.stringify(en.moduleOff)).not.toContain('\u2014')
    expect(JSON.stringify(hi.moduleOff)).not.toContain('\u2014')
  })
})

/* ── Key parity with server/config.py ─────────────────────────────────────────
 *
 * "Keys match between server and frontend" is a property of two files, so it is
 * read from the server's own source rather than restated here. A renamed module
 * or feature on either side fails this the day it happens. */
describe('module and feature ids match server/config.py', () => {
  const config = readFileSync(resolve(__dirname, '../server/config.py'), 'utf8')
  const dictKeys = (name: string): string[] => {
    const block = config.match(new RegExp(`^${name} = \\{([\\s\\S]*?)^\\}`, 'm'))
    expect(block, `server/config.py has no ${name} block`).toBeTruthy()
    return Array.from(block![1].matchAll(/^\s+"([a-z_-]+)":/gm), m => m[1])
  }

  it('OPT_IN_MODULES are the opt-in module ids', () => {
    expect(dictKeys('OPT_IN_MODULES').sort()).toEqual([...OPT_IN_MODULE_IDS].sort())
  })

  it('HOSPITAL_MODULE_ENV names every module id (the opt-in ones spread in)', () => {
    const standard = dictKeys('HOSPITAL_MODULE_ENV')
    expect([...standard, ...OPT_IN_MODULE_IDS].sort()).toEqual([...HOSPITAL_MODULE_IDS].sort())
    expect(config).toMatch(/^HOSPITAL_MODULE_ENV = \{[\s\S]*?\*\*OPT_IN_MODULES,[\s\S]*?^\}/m)
  })

  it('FEATURE_FLAGS are the feature ids', () => {
    expect(dictKeys('FEATURE_FLAGS').sort()).toEqual([...FEATURE_FLAG_IDS].sort())
  })
})
