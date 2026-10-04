/**
 * Orb, Laboratory catalogue and master API (lis-3; the setup screens of lis-4
 * build on it). Every call goes through ./index, so a refusal arrives as a
 * LabApiError with the server's code.
 *
 * No reference range and no critical limit ships (rulings LIS-R1 and LIS-R2):
 * a pathologist approves each one here, with an approval note, and a critical
 * limit also names the clinicians it was agreed with. Values travel as the
 * strings a person typed, and must be exact at the test's decimal places.
 */
import { labGet, labPatch, labPost, labPut } from './index'

export type LabCodeSystem = 'LOINC' | 'LOCAL'
export type LabTestKind = 'analyte' | 'panel'
export type LabPerformedAt = 'lab' | 'ward_poct'
export type LabResultType = 'numeric' | 'text' | 'coded'
export type LabSex = 'any' | 'male' | 'female'

/** One orderable test as the order screen's typeahead shows it. */
export interface LabCatalogueEntry {
  id: string
  code: string
  code_system: LabCodeSystem
  name: string
  short_name: string
  kind: LabTestKind
  performed_at: LabPerformedAt
  unit: string | null
  result_type: LabResultType
  section_id: string | null
  section_name: string | null
  specimen_type_code: string | null
  specimen_display: string | null
  container: string | null
  container_colour_name: string | null
  components?: { test_id: string; name: string | null; short_name: string | null }[]
}

export interface LabTest {
  id: string
  code: string
  code_system: LabCodeSystem
  name: string
  short_name: string
  kind: LabTestKind
  performed_at: LabPerformedAt
  section_id: string | null
  specimen_type_code: string | null
  result_type: LabResultType
  unit: string | null
  decimal_places: number
  orderable: boolean
  active: boolean
  version: number
  updated_by: string | null
  updated_at: string
  components?: { test_id: string; name: string | null; short_name: string | null; sort_order: number }[]
  reference_ranges?: LabReferenceRange[]
  critical_limits?: LabCriticalLimit[]
  [key: string]: unknown
}

interface VersionedBand {
  id: number
  test_id: string
  sex: LabSex
  age_min_days: number
  age_max_days: number | null
  display_text: string
  effective_from: string
  effective_to: string | null
  approved_by: string
  approved_at: string
  method_note: string
}

export interface LabReferenceRange extends VersionedBand {
  low_milli: number | null
  high_milli: number | null
}

export interface LabCriticalLimit extends VersionedBand {
  critical_low_milli: number | null
  critical_high_milli: number | null
  agreed_with: string
}

/** The loud counts: tests nobody has configured yet are never shown as zero. */
export interface LabConfigurationGaps {
  numeric_tests: number
  without_reference_range: number
  without_critical_limit: number
}

export interface RangeApproval {
  test_id: string
  sex?: LabSex
  age_min_days?: number
  age_max_days?: number | null
  low?: string | null
  high?: string | null
  method_note: string
}

export interface CriticalLimitApproval {
  test_id: string
  sex?: LabSex
  age_min_days?: number
  age_max_days?: number | null
  critical_low?: string | null
  critical_high?: string | null
  agreed_with: string
  method_note: string
}

export function searchLabCatalogue(q: string, limit = 25): Promise<{ tests: LabCatalogueEntry[] }> {
  return labGet('/catalogue', { q, limit })
}

export function listLabTests(params: { include_inactive?: boolean; kind?: LabTestKind } = {}):
  Promise<{ tests: LabTest[]; gaps: LabConfigurationGaps }> {
  return labGet('/master/tests', params)
}

export function getLabTest(testId: string): Promise<LabTest> {
  return labGet(`/master/tests/${encodeURIComponent(testId)}`)
}

export function createLabTest(body: Partial<LabTest> & {
  code_system: LabCodeSystem; code: string; name: string; short_name: string; specimen_type_code: string
}): Promise<LabTest> {
  return labPost('/master/tests', body)
}

export function updateLabTest(testId: string, changes: Partial<LabTest>): Promise<LabTest> {
  return labPatch(`/master/tests/${encodeURIComponent(testId)}`, changes)
}

export function setLabPanelComponents(panelId: string, componentTestIds: string[]): Promise<LabTest> {
  return labPut(`/master/panels/${encodeURIComponent(panelId)}/components`,
    { component_test_ids: componentTestIds })
}

export function listLabReferenceRanges(params: { test_id?: string; include_retired?: boolean } = {}):
  Promise<{ ranges: LabReferenceRange[] }> {
  return labGet('/master/ranges', params)
}

export function approveLabReferenceRange(body: RangeApproval): Promise<LabReferenceRange> {
  return labPost('/master/ranges', body)
}

export function retireLabReferenceRange(rangeId: number, reason: string): Promise<LabReferenceRange> {
  return labPost(`/master/ranges/${rangeId}/retire`, { reason })
}

export function listLabCriticalLimits(params: { test_id?: string; include_retired?: boolean } = {}):
  Promise<{ critical_limits: LabCriticalLimit[] }> {
  return labGet('/master/critical-limits', params)
}

export function approveLabCriticalLimit(body: CriticalLimitApproval): Promise<LabCriticalLimit> {
  return labPost('/master/critical-limits', body)
}

export function retireLabCriticalLimit(limitId: number, reason: string): Promise<LabCriticalLimit> {
  return labPost(`/master/critical-limits/${limitId}/retire`, { reason })
}
