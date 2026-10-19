/**
 * The draft of the registration's privacy notice and consent step (opd-19),
 * and the rules that turn it into what the server takes. The fields are
 * RegistrationConsentFields.tsx; the server's own check is
 * registration_service._consent_values, and it refuses anything these allow
 * that it does not.
 */
import type { RegistrationConsentInput, RegistrationConsentOptions } from '../../../api/registration'

export type ConsentChoice = '' | 'now' | 'later'

export interface ConsentDraft {
  /** '' until the desk chooses; the registration waits for a choice. */
  choice: ConsentChoice
  noticeGiven: boolean
  treatmentConsent: boolean
  noticeVersion: string
  /** '' follows the preferred language when that is a notice language. */
  noticeLanguage: string
  method: string
  givenBy: string
  proxyName: string
  proxyRelationship: string
  evidenceRef: string
}

const NOTICE_VERSION_KEY = 'orb.registration.noticeVersion'

/** The notice version last used on this device, a convenience only. */
export function rememberedNoticeVersion(): string {
  try {
    return window.localStorage.getItem(NOTICE_VERSION_KEY) ?? ''
  } catch {
    return ''
  }
}

export function rememberNoticeVersion(version: string): void {
  try {
    window.localStorage.setItem(NOTICE_VERSION_KEY, version)
  } catch {
    /* Storage is a convenience: the next registration types it again. */
  }
}

export function emptyConsentDraft(): ConsentDraft {
  return {
    choice: '',
    noticeGiven: false,
    treatmentConsent: false,
    noticeVersion: rememberedNoticeVersion(),
    noticeLanguage: '',
    method: '',
    givenBy: 'patient',
    proxyName: '',
    proxyRelationship: '',
    evidenceRef: '',
  }
}

/** The listed notice language matching what was typed (case and spaces
 *  ignored), or ''. */
export function matchNoticeLanguage(options: RegistrationConsentOptions, typed: string): string {
  const wanted = typed.trim().replace(/\s+/g, ' ').toLowerCase()
  if (!wanted) return ''
  return options.notice_languages.find(l => l.toLowerCase() === wanted) ?? ''
}

export function effectiveNoticeLanguage(options: RegistrationConsentOptions, draft: ConsentDraft,
  preferredLanguage: string): string {
  return draft.noticeLanguage || matchNoticeLanguage(options, preferredLanguage)
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/

/** Is the patient younger than the server's adult age (options.adult_age)?
 *  true or false from a YYYY-MM-DD date of birth or a whole stated age, and
 *  null while neither is usable. Like the server
 *  (registration_service._younger_than), a date of birth is counted to the
 *  earlier of the local and the UTC date, so a birthday the two disagree
 *  about counts as the younger age. */
export function underAdultAge(options: RegistrationConsentOptions,
  stated: { dateOfBirth?: string; ageYears?: number }, now: Date = new Date()): boolean | null {
  const adult = options.adult_age
  if (stated.ageYears !== undefined) {
    return Number.isInteger(stated.ageYears) && stated.ageYears >= 0
      ? stated.ageYears < adult : null
  }
  const match = ISO_DATE.exec(stated.dateOfBirth ?? '')
  if (!match) return null
  const born = [Number(match[1]), Number(match[2]), Number(match[3])]
  const local = [now.getFullYear(), now.getMonth() + 1, now.getDate()]
  const utc = [now.getUTCFullYear(), now.getUTCMonth() + 1, now.getUTCDate()]
  const earlier = (a: number[], b: number[]) =>
    a[0] !== b[0] ? a[0] < b[0] : a[1] !== b[1] ? a[1] < b[1] : a[2] <= b[2]
  const today = earlier(local, utc) ? local : utc
  const beforeBirthday = today[1] < born[1] || (today[1] === born[1] && today[2] < born[2])
  return today[0] - born[0] - (beforeBirthday ? 1 : 0) < adult
}

/** Why this draft's "who gave consent" cannot be sent, in the desk's words,
 *  or '' when it can. The server refuses the same two contradictions
 *  (consent_given_by_mismatch, consent_guardian_required): a guardian method
 *  given by the patient, and a patient under the adult age (or of an age not
 *  yet known) consenting for themselves. */
export function givenByProblem(options: RegistrationConsentOptions, draft: ConsentDraft,
  minor: boolean | null): string {
  if (draft.givenBy !== 'patient') return ''
  if (options.guardian_methods.includes(draft.method)) {
    return 'This way of giving consent means a guardian or someone acting for the patient '
      + 'gave it. Choose who gave consent and record their name.'
  }
  if (minor === true) {
    return `This patient is under ${options.adult_age}, so a parent or guardian gives consent `
      + 'for them. Choose who gave consent and record their name.'
  }
  if (minor === null) return 'Record the date of birth or stated age first.'
  return ''
}

/** Whether the registration may be sent with this draft. `minor` is
 *  underAdultAge for what the form holds. */
export function consentReady(options: RegistrationConsentOptions, draft: ConsentDraft,
  preferredLanguage: string, minor: boolean | null): boolean {
  if (draft.choice === 'later') return true
  if (draft.choice !== 'now') return false
  const version = draft.noticeVersion.trim()
  return draft.noticeGiven && draft.treatmentConsent
    && version !== '' && version.length <= options.notice_version_max
    && effectiveNoticeLanguage(options, draft, preferredLanguage) !== ''
    && options.methods.some(m => m.value === draft.method)
    && options.given_by.includes(draft.givenBy)
    && givenByProblem(options, draft, minor) === ''
    && (draft.givenBy === 'patient'
      || (draft.proxyName.trim() !== '' && draft.proxyRelationship.trim() !== ''))
}

/** The step as the server takes it, or undefined for "not now". */
export function consentInput(options: RegistrationConsentOptions, draft: ConsentDraft,
  preferredLanguage: string): RegistrationConsentInput | undefined {
  if (draft.choice !== 'now') return undefined
  const input: RegistrationConsentInput = {
    notice_acknowledged: true,
    treatment_consent: true,
    notice_version: draft.noticeVersion.trim(),
    notice_language: effectiveNoticeLanguage(options, draft, preferredLanguage),
    method: draft.method,
    given_by: draft.givenBy,
  }
  if (draft.givenBy !== 'patient') {
    input.proxy_name = draft.proxyName.trim()
    input.proxy_relationship = draft.proxyRelationship.trim()
  }
  if (draft.evidenceRef.trim()) input.evidence_ref = draft.evidenceRef.trim()
  return input
}
