/**
 * The signal one tab gives this browser's other tabs when its sign-in moves to
 * another facility (multisite-19, W13-E). Every tab shares one sign-in cookie,
 * so the others reload on it (AppRoot).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { announceFacilitySwitch, isFacilitySwitchSignal } from './facilitySwitchSignal'

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('the facility switch signal', () => {
  it('writes a fresh value each time, so every switch reaches the other tabs', () => {
    announceFacilitySwitch()
    const first = localStorage.getItem('orb_facility_switch')
    announceFacilitySwitch()
    const second = localStorage.getItem('orb_facility_switch')
    expect(first).toBeTruthy()
    expect(second).toBeTruthy()
    expect(second).not.toBe(first)
  })

  it('keeps nothing about the person or the facility', () => {
    localStorage.setItem('orb_user', JSON.stringify({ id: 'd1', facility: { id: 9 } }))
    announceFacilitySwitch()
    expect(localStorage.getItem('orb_facility_switch')).toMatch(/^\d+\.[a-z0-9]+$/)
  })

  it('reads only its own key being written', () => {
    expect(isFacilitySwitchSignal(new StorageEvent('storage', { key: 'orb_facility_switch', newValue: '1.a' }))).toBe(true)
    // Removed, another key, or the whole storage cleared: not a switch.
    expect(isFacilitySwitchSignal(new StorageEvent('storage', { key: 'orb_facility_switch', newValue: null }))).toBe(false)
    expect(isFacilitySwitchSignal(new StorageEvent('storage', { key: 'orb_user', newValue: '{}' }))).toBe(false)
    expect(isFacilitySwitchSignal(new StorageEvent('storage', { key: null }))).toBe(false)
  })

  it('does not throw when storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError') })
    expect(() => announceFacilitySwitch()).not.toThrow()
  })
})
