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

import {
  canSeeWardList,
  currentRole,
  DISCHARGE_GENERATE,
  DISCHARGE_PUBLISH,
  hasPermission,
  MEDICATION_PRESCRIBE,
  OUTCOME_RECORD,
  PATIENT_READ_ALL,
  PATIENT_WRITE,
  RELAY_EXECUTE_ACTION,
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
const SERVER_GRANTS: Record<string, Permission[]> = {
  doctor: [PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
           MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH],
  clinician: [PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
              MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH],
  nurse: [PATIENT_READ_ALL, OUTCOME_RECORD],
  admin: [PATIENT_READ_ALL, OUTCOME_RECORD],
  pharmacist: [],
  patient: [],
}
const ALL: Permission[] = [
  PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
  MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH,
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
