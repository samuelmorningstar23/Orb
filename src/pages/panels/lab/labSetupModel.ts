/**
 * The laboratory Setup screen's words and checks (lis-4), kept apart from the
 * components so they can be tested on their own and so the component files
 * export components only.
 *
 * Nothing here decides a clinical value. The server is the authority on every
 * check (a range must be exact at the test's reporting precision, bands must
 * not overlap, only a pathologist may write); these checks only stop a form
 * from being sent when it would certainly be refused, and say why in words.
 */
import type {
  LabConfigurationGaps,
  LabPerformedAt,
  LabResultType,
  LabSex,
  LabTest,
  LabTestKind,
} from '../../../api/lab/master'

export const SEX_LABEL: Record<LabSex, string> = {
  any: 'Any sex',
  male: 'Male',
  female: 'Female',
}

export const PERFORMED_AT_LABEL: Record<LabPerformedAt, string> = {
  lab: 'Laboratory',
  ward_poct: 'At the bedside',
}

export const RESULT_TYPE_LABEL: Record<LabResultType, string> = {
  numeric: 'Number',
  text: 'Text',
  coded: 'Choice from a list',
}

export const KIND_LABEL: Record<LabTestKind, string> = {
  analyte: 'Test',
  panel: 'Panel',
}

const NUMBER = /^-?\d+(?:\.\d+)?$/

/**
 * Why a typed value cannot be a limit for a test reported at `decimalPlaces`,
 * or null when it can (blank is allowed here; the caller asks for at least one
 * value). Trailing zeros are fine: "5.10" is exact at one decimal place, as the
 * server reads it.
 */
export function precisionProblem(raw: string, decimalPlaces: number): string | null {
  const text = raw.trim()
  if (!text) return null
  if (!NUMBER.test(text)) return 'Type a number, like 4.5.'
  const fraction = text.includes('.') ? text.split('.')[1].replace(/0+$/, '') : ''
  if (fraction.length > decimalPlaces) {
    return decimalPlaces === 0
      ? 'This test reports whole numbers only.'
      : `This test reports ${decimalPlaces} decimal place${decimalPlaces === 1 ? '' : 's'}.`
  }
  return null
}

/** A whole number of days typed in a form: the number, null for blank, or a
 *  sentence saying what is wrong. */
export function parseDays(raw: string): number | null | string {
  const text = raw.trim()
  if (!text) return null
  if (!/^\d+$/.test(text)) return 'Ages are whole numbers of days.'
  return Number(text)
}

/** "about 18 years" for a day count, so a pathologist entering 6570 days sees
 *  what that means. Days below 60 are said as days. */
export function aboutAge(days: number): string {
  if (days < 60) return `${days} day${days === 1 ? '' : 's'}`
  if (days < 730) return `about ${Math.round(days / 30.4)} months`
  const years = Math.round((days / 365.25) * 10) / 10
  return `about ${Number.isInteger(years) ? years.toFixed(0) : years.toFixed(1)} years`
}

/** "Any sex, every age", "Male, 0 to 28 days", "Female, 6570 days and older
 *  (about 18 years)". Ages are in days at collection, upper bound excluded. */
export function bandText(sex: LabSex, ageMinDays: number, ageMaxDays: number | null): string {
  const who = SEX_LABEL[sex] ?? sex
  if (ageMinDays === 0 && ageMaxDays === null) return `${who}, every age`
  if (ageMaxDays === null) return `${who}, ${ageMinDays} days and older (${aboutAge(ageMinDays)})`
  return `${who}, ${ageMinDays} to under ${ageMaxDays} days (up to ${aboutAge(ageMaxDays)})`
}

/** A test whose results a reference range and a critical limit apply to. */
export function takesRanges(test: Pick<LabTest, 'kind' | 'result_type'>): boolean {
  return test.kind === 'analyte' && test.result_type === 'numeric'
}

/** A test the loud counts include (the server's configuration_gaps rule). */
export function countedInGaps(test: Pick<LabTest, 'kind' | 'result_type' | 'performed_at' | 'active'>): boolean {
  return takesRanges(test) && test.performed_at === 'lab' && Boolean(test.active)
}

export interface GapLine {
  key: 'range' | 'limit' | 'empty'
  /** True when the line reports something not configured. */
  warn: boolean
  sentence: string
}

/** The loud counts, in words. Not configured is never shown as zero or as a
 *  blank: each line says how many tests lack an approved value and what that
 *  means for a result. An empty catalogue is said as empty, never as "every
 *  test is configured". */
export function gapLines(gaps: LabConfigurationGaps): GapLine[] {
  const total = gaps.numeric_tests
  if (!total) {
    return [{
      key: 'empty',
      warn: true,
      sentence: 'There are no numeric laboratory tests in the catalogue yet, so no result can carry a flag or raise a critical.',
    }]
  }
  const tests = (n: number) => `${n} of ${total} numeric laboratory test${total === 1 ? '' : 's'}`
  return [
    {
      key: 'range',
      warn: gaps.without_reference_range > 0,
      sentence: gaps.without_reference_range > 0
        ? `${tests(gaps.without_reference_range)} ${gaps.without_reference_range === 1 ? 'has' : 'have'} no approved reference range. Their results will say "Reference range not configured" and carry no high or low flag.`
        : `Every numeric laboratory test has an approved reference range.`,
    },
    {
      key: 'limit',
      warn: gaps.without_critical_limit > 0,
      sentence: gaps.without_critical_limit > 0
        ? `${tests(gaps.without_critical_limit)} ${gaps.without_critical_limit === 1 ? 'has' : 'have'} no approved critical limit. No critical result will be raised for them.`
        : `Every numeric laboratory test has an approved critical limit.`,
    },
  ]
}

/** Split a typed list of coded values ("Positive, Negative") into words. */
export function codedValuesFromText(raw: string): string[] {
  return raw.split(/[,\n]/).map((v) => v.trim()).filter(Boolean)
}

/** The stored coded values (a JSON list) as the text a form shows. */
export function codedValuesToText(stored: unknown): string {
  if (Array.isArray(stored)) return stored.join(', ')
  if (typeof stored !== 'string' || !stored.trim()) return ''
  try {
    const parsed: unknown = JSON.parse(stored)
    return Array.isArray(parsed) ? parsed.map(String).join(', ') : ''
  } catch {
    return ''
  }
}
