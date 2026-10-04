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

// ── The setup screen's other lists (lis-4) ──────────────────────────────────
// A save answers with the stored row, whose flags may come back as 0 or 1
// rather than false or true; read them with Boolean().

export interface LabSpecimenType {
  code: string
  display: string
  container: string | null
  container_colour_name: string | null
  min_volume_ul: number | null
  stability_minutes: number | null
  handling_note: string | null
  active: boolean | number
}

export interface LabSection {
  id: string
  name: string
  site_id: string | null
  active: boolean | number
  sort_order: number
}

export interface LabRejectionReason {
  code: string
  display: string
  incident_prompt: boolean | number
  active: boolean | number
}

export type LabSpecimenTypeBody = Omit<LabSpecimenType, 'code' | 'active'> & { active: boolean }
export type LabSectionBody = { name: string; sort_order: number; active: boolean }
export type LabRejectionReasonBody = { display: string; incident_prompt: boolean; active: boolean }

export function listLabPanels(): Promise<{ panels: LabTest[] }> {
  return labGet('/master/panels')
}

export function listLabSpecimenTypes(params: { include_inactive?: boolean } = {}):
  Promise<{ specimen_types: LabSpecimenType[] }> {
  return labGet('/master/specimen-types', params)
}

export function saveLabSpecimenType(code: string, body: LabSpecimenTypeBody): Promise<LabSpecimenType> {
  return labPut(`/master/specimen-types/${encodeURIComponent(code)}`, body)
}

export function listLabSections(params: { include_inactive?: boolean } = {}):
  Promise<{ sections: LabSection[] }> {
  return labGet('/master/sections', params)
}

export function saveLabSection(sectionId: string, body: LabSectionBody): Promise<LabSection> {
  return labPut(`/master/sections/${encodeURIComponent(sectionId)}`, body)
}

export function listLabRejectionReasons(params: { include_inactive?: boolean } = {}):
  Promise<{ rejection_reasons: LabRejectionReason[] }> {
  return labGet('/master/rejection-reasons', params)
}

export function saveLabRejectionReason(code: string, body: LabRejectionReasonBody):
  Promise<LabRejectionReason> {
  return labPut(`/master/rejection-reasons/${encodeURIComponent(code)}`, body)
}
