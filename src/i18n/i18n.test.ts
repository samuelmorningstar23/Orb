import { describe, it, expect, beforeEach } from 'vitest'
import i18n, { LANGUAGE_STORAGE_KEY } from './index'

describe('i18n', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en')
  })

  it('loads the English bundle by default', () => {
    expect(i18n.t('nav.vigil')).toBe('Patients')
    expect(i18n.t('login.signIn')).toBe('Sign In')
  })

  it('loads the Hindi bundle and switches keys', async () => {
    await i18n.changeLanguage('hi')
    expect(i18n.language).toBe('hi')
    expect(i18n.t('nav.vigil')).toBe('मरीज़')
    expect(i18n.t('login.signIn')).toBe('साइन इन')
  })

  it('interpolates the pending count in the offline status string', () => {
    expect(i18n.t('status.offlinePending', { count: 3 })).toBe('Offline · 3 pending')
  })

  it('falls back to English for a key missing from a bundle', async () => {
    await i18n.changeLanguage('hi')
    // Every en key should exist in hi for the translated slice, but the fallback
    // guarantees a real string (never a raw key) if one is ever missing.
    expect(i18n.t('shell.signOut')).not.toBe('shell.signOut')
  })

  it('the lines shown with scoring off name no NEWS2, score or handover', () => {
    for (const key of ['subtitleScoringOff.vigil', 'subtitleWardRecord.shift']) {
      const line = i18n.t(key)
      expect(line).not.toBe(key)
      expect(line).not.toMatch(/NEWS|score|band|handover/i)
    }
  })

  it('persists the chosen language to localStorage', async () => {
    await i18n.changeLanguage('hi')
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe('hi')
  })
})
