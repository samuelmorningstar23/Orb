import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './locales/en.json'
import hi from './locales/hi.json'

/**
 * Orb internationalization.
 *
 * This is a proven first slice: navigation + shell chrome + login + the
 * high-visibility panel headings are translated into English (en) and Hindi
 * (hi) for the India market on ward tablets. The rest of the app remains English
 * and can be migrated incrementally by adding keys to the locale bundles.
 *
 * Language is persisted to localStorage under `orb_lang` so a ward tablet keeps
 * its chosen language across reloads. Fallback is English.
 */
export const LANGUAGE_STORAGE_KEY = 'orb_lang'
export const SUPPORTED_LANGUAGES = ['en', 'hi'] as const
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number]

function detectInitialLanguage(): SupportedLanguage {
  try {
    const stored = localStorage.getItem(LANGUAGE_STORAGE_KEY)
    if (stored && (SUPPORTED_LANGUAGES as readonly string[]).includes(stored)) {
      return stored as SupportedLanguage
    }
  } catch {
    // localStorage unavailable (private mode / SSR) — fall through to default.
  }
  return 'en'
}

i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    hi: { translation: hi },
  },
  lng: detectInitialLanguage(),
  fallbackLng: 'en',
  interpolation: {
    // React already escapes rendered values.
    escapeValue: false,
  },
})

// Persist language changes so the choice survives reloads.
i18n.on('languageChanged', (lng) => {
  try {
    localStorage.setItem(LANGUAGE_STORAGE_KEY, lng)
  } catch {
    // Ignore write failures (private mode).
  }
})

export default i18n
