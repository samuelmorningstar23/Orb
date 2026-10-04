import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { devAllowedHosts } from './vite.hosts'

// The same cases as server/tests/test_host_gate.py's derivation tests: the dev
// server and the backend must answer to the same names.

function list(result: true | string[]): string[] {
  if (result === true) throw new Error('expected a list of names, got true (check off)')
  return result
}

describe('devAllowedHosts', () => {
  it('derives the names from the settings an install already has', () => {
    const names = devAllowedHosts(
      {
        ORB_CORS_ORIGINS:
          'http://localhost:5173, https://ORB.Hospital.Local:8000,http://192.168.1.10:8000,*',
        ORB_PUBLIC_APP_BASE_URL: 'https://discharge.hospital.org/',
        ORB_HOST: '0.0.0.0',
      },
      'Orb-Box.local',
    )
    expect(names).toEqual([
      'discharge.hospital.org', 'localhost', 'orb-box', 'orb-box.local', 'orb.hospital.local',
    ])
  })

  it('gives a bare machine name its mDNS .local form', () => {
    expect(devAllowedHosts({}, 'orbbox')).toEqual(['localhost', 'orbbox', 'orbbox.local'])
  })

  it('counts ORB_HOST only when it is a name', () => {
    expect(devAllowedHosts({ ORB_HOST: 'orb.hospital.local' }, 'x')).toContain('orb.hospital.local')
    expect(devAllowedHosts({ ORB_HOST: '0.0.0.0' }, 'x')).toEqual(devAllowedHosts({}, 'x'))
  })

  it('adds ORB_ALLOWED_HOSTS in any spelling and drops junk', () => {
    const names = list(devAllowedHosts(
      { ORB_ALLOWED_HOSTS: 'Extra.Example:8443, https://other.example/, bare, not a name!, 10.0.0.5' },
      'x',
    ))
    expect(names).toEqual(expect.arrayContaining(['extra.example', 'other.example', 'bare']))
    expect(names).not.toContain('10.0.0.5') // Vite admits every IP anyway
    expect(names.some((n) => n.includes(' ') || n.includes('!'))).toBe(false)
  })

  it('turns the check off only for * in ORB_ALLOWED_HOSTS, not in ORB_CORS_ORIGINS', () => {
    expect(devAllowedHosts({ ORB_ALLOWED_HOSTS: '*' }, 'x')).toBe(true)
    expect(devAllowedHosts({ ORB_CORS_ORIGINS: '*' }, 'x')).toEqual(['localhost', 'x', 'x.local'])
  })
})

describe('vite.config.ts', () => {
  it('never switches the dev server host check off', () => {
    // allowedHosts: true let any DNS-rebinding page read the app and call /api
    // through the proxy, where the backend only ever sees Host localhost:8000.
    const source = readFileSync(resolve(__dirname, 'vite.config.ts'), 'utf8')
    expect(source).not.toMatch(/allowedHosts:\s*true/)
    expect(source).toMatch(/allowedHosts:\s*devAllowedHosts\(/)
  })
})
