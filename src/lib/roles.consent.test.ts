/**
 * consent.record in the client mirror (opd-19). The registration step offers
 * the privacy notice and consent questions only to an account that holds it,
 * so the mirror must say exactly what server/permissions.py says: the doctor
 * (and the clinician alias) only. Ruling OPD-N3 keeps it from the receptionist
 * until counsel agrees; the day it is granted, this test and the server's
 * tests/test_role_batch.py change together.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { CONSENT_RECORD, hasPermission } from './roles'

const permissionsSource = readFileSync(resolve(__dirname, '../../server/permissions.py'), 'utf8')

/** The constants named in one role's set in server/permissions.py. */
function serverGrantNames(role: string): string[] {
  const block = permissionsSource.match(new RegExp(`^    "${role}": \\{([\\s\\S]*?)^    \\},`, 'm'))
  if (!block) throw new Error(`server/permissions.py has no "${role}" set`)
  const code = block[1].split('\n').map(line => line.replace(/#.*$/, '')).join('\n')
  return Array.from(code.matchAll(/\b([A-Z][A-Z_]+)\b/g), m => m[1])
}

describe('consent.record, client and server', () => {
  it('is the server string exactly', () => {
    expect(permissionsSource).toMatch(/^CONSENT_RECORD = "consent\.record"$/m)
    expect(CONSENT_RECORD).toBe('consent.record')
  })

  it('the doctor and the clinician hold it; no other role does', () => {
    for (const role of ['doctor', 'clinician']) expect(hasPermission(CONSENT_RECORD, role), role).toBe(true)
    for (const role of ['nurse', 'admin', 'pharmacist', 'patient', 'cashier', 'receptionist',
      'ward_clerk', 'phlebotomist', 'lab_technician', 'pathologist']) {
      expect(hasPermission(CONSENT_RECORD, role), role).toBe(false)
    }
  })

  it('matches the server table for the doctor and the receptionist (ruling OPD-N3)', () => {
    expect(serverGrantNames('doctor')).toContain('CONSENT_RECORD')
    expect(serverGrantNames('receptionist')).not.toContain('CONSENT_RECORD')
    expect(serverGrantNames('receptionist')).toEqual(
      expect.arrayContaining(['REGISTRATION_READ', 'REGISTRATION_WRITE']))
  })
})
