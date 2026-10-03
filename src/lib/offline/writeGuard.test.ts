/**
 * The read-only half of the contract. Offline, a write must fail in the
 * clinician's face — never queue quietly, never return something that looks
 * like a saved record.
 */
import { describe, it, expect, afterEach } from 'vitest'
import {
  assertOnlineForWrite,
  isOfflineWriteRefused,
  OfflineWriteRefusedError,
  offlineRefusalMessage,
} from './writeGuard'

function setOnline(value: boolean) {
  Object.defineProperty(window.navigator, 'onLine', { value, configurable: true })
}

afterEach(() => setOnline(true))

describe('writeGuard', () => {
  it('lets a write through while online', () => {
    setOnline(true)
    expect(() => assertOnlineForWrite('record observations')).not.toThrow()
  })

  it('refuses a write while offline', () => {
    setOnline(false)
    expect(() => assertOnlineForWrite('record observations')).toThrow(OfflineWriteRefusedError)
  })

  it('names the action and says plainly that nothing was saved', () => {
    setOnline(false)
    let caught: unknown
    try {
      assertOnlineForWrite('record observations')
    } catch (e) {
      caught = e
    }
    expect(isOfflineWriteRefused(caught)).toBe(true)
    const message = (caught as Error).message
    expect(message).toContain('record observations')
    expect(message.toLowerCase()).toContain('offline')
    expect(message.toLowerCase()).toContain('not been saved')
    // No hint of a queue: the founder chose read-only precisely so nothing
    // sits on the tablet pretending it will land later.
    expect(message.toLowerCase()).not.toContain('queue')
    expect(message.toLowerCase()).not.toContain('will sync')
  })

  it('offlineRefusalMessage is usable without throwing', () => {
    expect(offlineRefusalMessage('give a medication')).toContain('give a medication')
  })

  it('isOfflineWriteRefused does not claim unrelated errors', () => {
    expect(isOfflineWriteRefused(new Error('boom'))).toBe(false)
    expect(isOfflineWriteRefused(null)).toBe(false)
  })
})
