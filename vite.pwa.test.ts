import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ORB_NAVIGATE_FALLBACK, ORB_ONE_PAGE_KEY, ORB_PAGE_CACHING, isOrbPageLoad } from './vite.pwa'

// The startup flash (Samuel's ruling, 9 October 2026): the page carries this
// computer's switches, so the service worker must not answer a page load from
// the copy of index.html it saved on the day it installed.

const page = (path: string, mode = 'navigate') => ({
  request: { mode } as unknown as Request,
  url: new URL(path, 'http://localhost:8000'),
})

describe('how the service worker answers a page load', () => {
  it('never from the install copy without asking the server', () => {
    expect(ORB_NAVIGATE_FALLBACK).toBeNull()
    expect(ORB_PAGE_CACHING.handler).toBe('NetworkFirst')
    expect(ORB_PAGE_CACHING.urlPattern).toBe(isOrbPageLoad)
  })

  it('falls back to a saved copy of the page when the server does not answer', () => {
    const options = ORB_PAGE_CACHING.options ?? {}
    expect(options.networkTimeoutSeconds).toBeGreaterThan(0)
    expect(options.precacheFallback).toEqual({ fallbackURL: 'index.html' })
  })

  it('keeps one saved page, the one the server served last, for every address', async () => {
    expect(ORB_PAGE_CACHING.options?.plugins).toContain(ORB_ONE_PAGE_KEY)
    const key = (path: string) => ORB_ONE_PAGE_KEY.cacheKeyWillBeUsed({ request: { url: `http://orb.local:8000${path}` } as Request })
    expect(await key('/dashboard/vigil')).toBe('http://orb.local:8000/')
    expect(await key('/downtime/P001')).toBe('http://orb.local:8000/')
    // Copied into the worker as source: it must read nothing outside itself.
    const copied = new Function(`return (${ORB_ONE_PAGE_KEY.cacheKeyWillBeUsed.toString()})`)() as typeof ORB_ONE_PAGE_KEY.cacheKeyWillBeUsed
    expect(await copied({ request: { url: 'http://orb.local:8000/handoff/P002' } as Request })).toBe('http://orb.local:8000/')
  })

  it('counts the screens as page loads, and not the API, uploads or the socket', () => {
    for (const path of ['/', '/dashboard', '/dashboard/vigil', '/downtime/P001', '/handoff/P001', '/login/staff']) {
      expect(isOrbPageLoad(page(path))).toBe(true)
    }
    for (const path of ['/api/health', '/uploads/a.png', '/socket.io/']) {
      expect(isOrbPageLoad(page(path))).toBe(false)
    }
    expect(isOrbPageLoad(page('/dashboard', 'cors'))).toBe(false)
  })

  it('the match reads nothing outside itself, as Workbox copies its source into the worker', () => {
    const copied = new Function(`return (${isOrbPageLoad.toString()})`)() as typeof isOrbPageLoad
    expect(copied(page('/dashboard'))).toBe(true)
    expect(copied(page('/api/health'))).toBe(false)
  })

  it('vite.config.ts builds the worker with these settings', () => {
    const config = readFileSync(resolve(__dirname, 'vite.config.ts'), 'utf8')
    expect(config).toMatch(/navigateFallback:\s*ORB_NAVIGATE_FALLBACK\b/)
    expect(config).toMatch(/runtimeCaching:\s*\[ORB_PAGE_CACHING\]/)
  })
})
