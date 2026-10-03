/**
 * The Trust Center speaks to a CIO: install type, roles, permissions and audit
 * categories in words, and the CORS policy as installed.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const REPORT = {
  generated_at: '2026-10-03T05:00:00+00:00',
  overall_score: 52,
  controls: [{
    id: 'data_residency', name: 'Data residency / on-prem', status: 'fail',
    evidence: 'Orb keeps the full stack in-house, but it accepts browser requests from ANY web address.',
    remediation: 'Ask the person who installed Orb to run it in the production profile.',
    settings: ['ORB_CORS_ORIGINS'],
  }],
  mfa: {
    required_roles: ['front_desk'], by_role: { front_desk: { total: 2, enrolled: 1 } },
    total_users: 2, total_enrolled: 1, adoption_pct: 50, required_gap: ['front_desk'],
  },
  rbac_matrix: {
    roles: ['front_desk'], permissions: ['patient.read_all'],
    matrix: { front_desk: { 'patient.read_all': true } }, role_count: 1, permission_count: 1,
  },
  audit_chain: { verified: true, broken_at: null, count: 123456 },
  audit_coverage: { total_events: 123456, by_category: { phi_access: 120000 }, categories: 1 },
  encryption: { db_at_rest: true, uploads_at_rest: true, db_encryption_key_set: true, upload_encryption_key_set: true },
  residency: {
    host: '127.0.0.1', loopback_only: true, cors_origins: ['*'], cors_wildcard: true,
    deployment_profile: 'dev', on_prem: true,
  },
  models: { validated: 1, total: 7, unvalidated: 6 },
}

vi.mock('../../../api/client', () => ({
  apiFetch: vi.fn(() => Promise.resolve(REPORT)),
}))

import TrustCenter from './TrustCenter'

describe('the Trust Center', () => {
  it('prints the install type, roles, permissions and categories in words', async () => {
    render(<TrustCenter />)
    expect(await screen.findByText(/Development install/)).toBeTruthy()
    expect(screen.queryByText(/dev profile/)).toBeNull()
    expect(screen.getByText('PHI access')).toBeTruthy()
    expect(screen.getByText('1,23,456')).toBeTruthy()
    expect(screen.getAllByText('Front desk').length).toBeGreaterThan(0)
    expect(screen.getByText(/Accepts browser requests from any web address\./)).toBeTruthy()
    // The installer's setting names travel in the report, not on the screen.
    expect(screen.queryByText(/ORB_CORS_ORIGINS/)).toBeNull()
  })
})
