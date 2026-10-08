/**
 * The client permission mirror is only worth having if it is TRUE. A mirror
 * that has drifted from server/permissions.py is worse than no mirror: it hides
 * a control the user is entitled to, or offers one the server will refuse, and
 * it does either silently.
 *
 * So this file asserts the table against the server's grants role by role, and
 * pins the two properties the call sites depend on: an unknown role holds
 * nothing, and a permission check never falls back to storage when a role was
 * passed explicitly.
 */
import { describe, expect, it, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import {
  ADT_BED,
  ADT_PRESENCE,
  ADT_READ,
  ADT_TRANSFER,
  BED_MASTER,
  canSeeWardList,
  currentRole,
  DISCHARGE_GENERATE,
  DISCHARGE_PUBLISH,
  HIS_PERMISSIONS,
  VITALS_TRANSCRIBE,
  VITALS_WRITE,
  hasPermission,
  INGEST_RECONCILE,
  LAB_AUTHORISE,
  LAB_COLLECT,
  LAB_CRITICAL_ACK,
  LAB_CRITICAL_LOG,
  LAB_INTERFACE,
  LAB_MASTER,
  LAB_READ,
  LAB_RECEIVE,
  LAB_RESULT,
  MEDICATION_PRESCRIBE,
  OPD_CONSULT,
  OUTCOME_RECORD,
  PATIENT_READ_ALL,
  PATIENT_WRITE,
  PHARMACY_STORE_APPROVE,
  PHARMACY_STORE_NDPS,
  PHARMACY_STORE_READ,
  PHARMACY_STORE_WRITE,
  REGISTRATION_READ,
  REGISTRATION_WRITE,
  RELAY_EXECUTE_ACTION,
  ROLE_FEATURE,
  ROLE_FEATURE_ALSO,
  SCHEDULE_MANAGE,
  WARD_INDENT_RAISE,
  WARD_RETURN_RECORD,
  WARD_STOCK_READ,
  WARD_STOCK_RECEIVE,
  type Permission,
} from './roles'

/** server/permissions.py:ROLE_PERMISSIONS, restricted to the mirrored set.
 *
 *  RELAY_EXECUTE_ACTION is the doctor's alone (permissions.py:129). It gates
 *  resolving Sage's action cards AND — since the drafted alert assessment
 *  shipped — recording the approve/decline verdict on Sage's clinical
 *  reasoning. A nurse holds RELAY_READ and reads every word of a draft; the
 *  verdict is a doctor's.
 *
 *  MEDICATION_PRESCRIBE is likewise the doctor's alone (permissions.py, added
 *  2026-08-15). It gates the allergy override at administration. The NURSE row
 *  is the load-bearing one: she holds `medication.write` on the server and gives
 *  every dose on the ward, and if this table ever grants her
 *  MEDICATION_PRESCRIBE the modal starts offering her an override control the
 *  server will refuse — and, worse, tells her the refusal is a bug. */
/*
 * THE HIS GRANTS (3 October 2026), checked against server/permissions.py
 * ROLE_PERMISSIONS as W1-A built it. The rows that carry a safety ruling:
 *   - the NURSE gets no ADT grant at all: her read-only bed board waits on
 *     ADT-S7, and until then she gets nothing new on the bed side;
 *   - the WARD CLERK holds adt.read and adt.bed only: never adt.transfer
 *     (ADT-S8, a treating-department change is a scope change) and never
 *     adt.presence (ADT-S7);
 *   - ADMIN never holds registration.write (OPD-N3), never pharmacy_store.write
 *     (GRN-R7) and never ingest.reconcile (LIS-R17);
 *   - no department-scoped role (doctor, nurse, ward clerk) holds
 *     ingest.reconcile, because the unmatched-results queue shows identifiers
 *     from every ward.
 */
const DOCTOR_HIS: Permission[] = [REGISTRATION_READ, REGISTRATION_WRITE, OPD_CONSULT,
                                  ADT_READ, LAB_COLLECT, LAB_CRITICAL_ACK]
const TECHNICIAN: Permission[] = [LAB_READ, LAB_COLLECT, LAB_RECEIVE, LAB_RESULT, LAB_CRITICAL_LOG, INGEST_RECONCILE]
const SERVER_GRANTS: Record<string, Permission[]> = {
  doctor: [PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
           MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH, VITALS_WRITE, ...DOCTOR_HIS],
  clinician: [PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
              MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH, VITALS_WRITE, ...DOCTOR_HIS],
  nurse: [PATIENT_READ_ALL, OUTCOME_RECORD, VITALS_WRITE,
          LAB_COLLECT, LAB_CRITICAL_ACK,
          WARD_STOCK_READ, WARD_STOCK_RECEIVE, WARD_INDENT_RAISE, WARD_RETURN_RECORD],
  admin: [PATIENT_READ_ALL, OUTCOME_RECORD,
          REGISTRATION_READ, SCHEDULE_MANAGE,
          ADT_READ, ADT_BED, ADT_TRANSFER, ADT_PRESENCE, BED_MASTER,
          PHARMACY_STORE_READ, PHARMACY_STORE_APPROVE,
          LAB_READ, LAB_INTERFACE],
  pharmacist: [PHARMACY_STORE_READ, PHARMACY_STORE_WRITE, PHARMACY_STORE_NDPS],
  patient: [],
  cashier: [],
  receptionist: [REGISTRATION_READ, REGISTRATION_WRITE],
  ward_clerk: [ADT_READ, ADT_BED, VITALS_TRANSCRIBE],
  phlebotomist: [LAB_READ, LAB_COLLECT],
  lab_technician: TECHNICIAN,
  pathologist: [...TECHNICIAN, LAB_AUTHORISE, LAB_MASTER],
}
const ALL: Permission[] = [
  PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
  MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH,
  VITALS_WRITE, VITALS_TRANSCRIBE,
  ...HIS_PERMISSIONS,
]

beforeEach(() => localStorage.clear())

describe('permission mirror vs server/permissions.py', () => {
  for (const [role, granted] of Object.entries(SERVER_GRANTS)) {
    it(`${role} holds exactly ${granted.length ? granted.join(', ') : 'nothing'}`, () => {
      for (const p of ALL) {
        expect(hasPermission(p, role)).toBe(granted.includes(p))
      }
    })
  }

  it('a role the server does not know holds nothing', () => {
    // 'surgeon' is a real one: the shell keeps a nav safety net for the raw
    // role, but ROLE_PERMISSIONS has no such key, so the server refuses it.
    for (const role of ['surgeon', 'porter', '']) {
      for (const p of ALL) expect(hasPermission(p, role)).toBe(false)
    }
  })

  it('a null or absent role holds nothing and does NOT fall back to storage', () => {
    localStorage.setItem('orb_user', JSON.stringify({ id: 'u1', name: 'Dr X', role: 'doctor' }))
    expect(hasPermission(PATIENT_WRITE, null)).toBe(false)
    // …while the stored session is still what an unqualified check reads.
    expect(hasPermission(PATIENT_WRITE)).toBe(true)
  })

  it('role matching is case-insensitive, as the server lower-cases too', () => {
    expect(hasPermission(PATIENT_WRITE, 'Doctor')).toBe(true)
    expect(hasPermission(OUTCOME_RECORD, 'NURSE')).toBe(true)
  })
})

describe('canSeeWardList (GET /api/vigil/patients)', () => {
  const signIn = (role: string) =>
    localStorage.setItem('orb_user', JSON.stringify({ id: 'u1', name: 'X', role }))

  it.each(['doctor', 'clinician', 'nurse', 'admin'])('%s may list ward patients', role => {
    signIn(role)
    expect(currentRole()).toBe(role)
    expect(canSeeWardList()).toBe(true)
  })

  // The pharmacist case this function was written for: SAGE_QUERY and the Helix
  // permissions and nothing else, so the ward list 403s.
  it.each(['pharmacist', 'patient', 'surgeon'])('%s may not', role => {
    signIn(role)
    expect(canSeeWardList()).toBe(false)
  })

  it('is false with nobody signed in', () => {
    expect(canSeeWardList()).toBe(false)
  })
})

/* ── Parity with the server's own source ─────────────────────────────────────
 *
 * The table above is hand-kept, so it can only be as true as the last person
 * who checked it. These read server/permissions.py and server/roles.py
 * directly, so a renamed permission string or a moved role feature fails HERE,
 * in the frontend suite, the day it happens. */
const serverSource = (path: string) => readFileSync(resolve(__dirname, '../../server', path), 'utf8')

describe('the HIS mirror matches the server source', () => {
  it('every HIS permission string is defined, with the same spelling, in server/permissions.py', () => {
    const permissions = serverSource('permissions.py')
    for (const perm of HIS_PERMISSIONS) {
      expect(permissions, `server/permissions.py has no constant for '${perm}'`).toMatch(
        new RegExp(`^[A-Z_]+ = "${perm.replace('.', '\\.')}"`, 'm'),
      )
    }
  })

  it('ROLE_FEATURE is server/roles.py ROLE_FEATURE, entry for entry', () => {
    const roles = serverSource('roles.py')
    const block = roles.match(/^ROLE_FEATURE: dict\[str, str\] = \{([\s\S]*?)^\}/m)
    expect(block, 'server/roles.py has no ROLE_FEATURE block').toBeTruthy()
    const server = Object.fromEntries(
      Array.from(block![1].matchAll(/"([a-z_]+)":\s*"([a-z_-]+)"/g), m => [m[1], m[2]]),
    )
    expect(server).toEqual({ ...ROLE_FEATURE })
  })

  it('ROLE_FEATURE_ALSO is server/roles.py ROLE_FEATURE_ALSO, entry for entry', () => {
    const roles = serverSource('roles.py')
    const block = roles.match(/^ROLE_FEATURE_ALSO: dict\[str, str\] = \{([\s\S]*?)^\}/m)
    expect(block, 'server/roles.py has no ROLE_FEATURE_ALSO block').toBeTruthy()
    const server = Object.fromEntries(
      Array.from(block![1].matchAll(/"([a-z_]+)":\s*"([a-z_-]+)"/g), m => [m[1], m[2]]),
    )
    expect(server).toEqual({ ...ROLE_FEATURE_ALSO })
  })

  it('every HIS role is a stored role on the server', () => {
    const roles = serverSource('roles.py')
    for (const role of Object.keys(ROLE_FEATURE)) {
      expect(roles).toMatch(new RegExp(`^\\s+"${role}",`, 'm'))
    }
  })
})

describe('the HIS safety rows hold', () => {
  it('no role holding a department scope holds ingest.reconcile', () => {
    for (const role of ['doctor', 'clinician', 'nurse', 'ward_clerk', 'admin']) {
      expect(hasPermission(INGEST_RECONCILE, role), role).toBe(false)
    }
  })

  it('the ward clerk can neither transfer nor record leave', () => {
    expect(hasPermission(ADT_TRANSFER, 'ward_clerk')).toBe(false)
    expect(hasPermission(ADT_PRESENCE, 'ward_clerk')).toBe(false)
    expect(hasPermission(PATIENT_READ_ALL, 'ward_clerk')).toBe(false)
  })

  it('the nurse holds no bed-board grant until ADT-S7 is ruled', () => {
    for (const p of [ADT_READ, ADT_BED, ADT_TRANSFER, ADT_PRESENCE, BED_MASTER]) {
      expect(hasPermission(p, 'nurse'), p).toBe(false)
    }
  })

  it('admin registers nobody, writes no pharmacy stock and reconciles no result', () => {
    expect(hasPermission(REGISTRATION_WRITE, 'admin')).toBe(false)
    expect(hasPermission(PHARMACY_STORE_WRITE, 'admin')).toBe(false)
    expect(hasPermission(OPD_CONSULT, 'admin')).toBe(false)
  })

  it('the lab roles and the desk roles hold none of the clinical set', () => {
    for (const role of ['receptionist', 'ward_clerk', 'lab_technician', 'pathologist', 'phlebotomist']) {
      for (const p of [PATIENT_READ_ALL, PATIENT_WRITE, RELAY_EXECUTE_ACTION, MEDICATION_PRESCRIBE]) {
        expect(hasPermission(p, role), `${role} ${p}`).toBe(false)
      }
    }
  })

  it('only the pathologist authorises a result or edits the lab master', () => {
    for (const role of Object.keys(SERVER_GRANTS)) {
      expect(hasPermission(LAB_AUTHORISE, role), role).toBe(role === 'pathologist')
      expect(hasPermission(LAB_MASTER, role), role).toBe(role === 'pathologist')
    }
  })
})
