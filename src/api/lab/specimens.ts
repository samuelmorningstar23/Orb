/**
 * Orb, Laboratory specimens API (lis-8): the collection list, labels and
 * collection.
 *
 * The server decides the scope: a ward account (nurse or doctor) sees its own
 * department's samples, the phlebotomist and the laboratory every ward.
 *
 * TWO IDENTIFIERS (ruling LIS-R6, strictest option until it is confirmed).
 * Labels print, and a sample is recorded as collected, only when the UHID and
 * the name typed at the bedside both match the record. The server answers 409
 * `identity_not_confirmed` with one outcome per identifier and never echoes
 * the stored value. Labels print one patient at a time.
 *
 * TIME. The collection time is now. Back-dating is refused while the site
 * limit (`backdate_minutes` on the list) is 0, which is how Orb ships.
 */
import { labGet, labPost } from './index'

export type LabPriority = 'stat' | 'urgent' | 'routine'

export interface CollectionTest {
  item_id: number
  order_id: string
  test_id: string
  status: string
  name: string
  short_name: string
}

export interface CollectionPatient {
  id: string
  name: string
  mrn: string | null
  bed: string | null
  ward: string | null
  department: string | null
}

/** One sample waiting to be collected (or, with recollection_needed, one the
 *  laboratory rejected and wants drawn again). */
export interface CollectionSpecimen {
  /** The accession number, which is also the specimen's id. */
  id: string
  patient: CollectionPatient
  specimen_type_code: string
  specimen_display: string
  container: string | null
  container_colour_name: string | null
  priority: LabPriority
  status: string
  planned_at: string | null
  label_print_count: number
  label_printed_at: string | null
  recollection_needed: boolean
  rejection_code: string | null
  rejection_note: string | null
  tests: CollectionTest[]
}

export interface CollectionList {
  specimens: CollectionSpecimen[]
  scope: { department: string | null; hospital_wide: boolean }
  /** How far back a collection time may be set here; 0 refuses any. */
  backdate_minutes: number
}

/** What the person at the bedside typed. Compared with the record, never stored. */
export interface CollectionIdentity {
  uhid: string
  name: string
}

/** One outcome per identifier. */
export type IdentityCheck = 'match' | 'no_match' | 'not_given' | 'not_on_record'

export interface SpecimenLabel {
  specimen_id: string
  patient: { name: string; mrn: string | null; bed: string | null; ward: string | null }
  specimen_display: string
  container: string | null
  container_colour_name: string | null
  priority: LabPriority
  /** Short test names, as printed. */
  tests: string[]
  print_number: number
}

export interface LabelPrint {
  labels: SpecimenLabel[]
  printed_at: string
}

export interface CollectResult {
  specimen: { id: string; status: string; collected_at: string | null; [key: string]: unknown }
  items_collected: number
  orders: { id: string; status: string; conflict: boolean }[]
}

export function getCollectionList(): Promise<CollectionList> {
  return labGet<CollectionList>('/collection')
}

export function printSpecimenLabels(
  specimenIds: string[],
  identity: CollectionIdentity,
): Promise<LabelPrint> {
  return labPost<LabelPrint>('/collection/labels', { specimen_ids: specimenIds, identity })
}

export function collectSpecimen(
  specimenId: string,
  identity: CollectionIdentity,
  extra: { collection_location?: string } = {},
): Promise<CollectResult> {
  return labPost<CollectResult>(`/specimens/${encodeURIComponent(specimenId)}/collect`, {
    identity,
    ...extra,
  })
}

/** The identity outcomes from a 409 `identity_not_confirmed`, or null. */
export function identityChecks(detail: unknown): Partial<Record<'uhid' | 'name', IdentityCheck>> | null {
  if (!detail || typeof detail !== 'object') return null
  const checks = (detail as { checks?: unknown }).checks
  return checks && typeof checks === 'object'
    ? (checks as Partial<Record<'uhid' | 'name', IdentityCheck>>)
    : null
}

/**
 * An accession number as a person reads it: the 12 digits in groups of six,
 * five and one (day, sequence, check digit), after any site prefix. Anything
 * that is not a 12-digit accession is returned unchanged.
 */
export function formatAccession(accession: string): string {
  const match = /^([A-Z]{0,4})(\d{6})(\d{5})(\d)$/.exec(accession.trim().toUpperCase())
  if (!match) return accession
  const [, prefix, day, sequence, check] = match
  return `${prefix ? `${prefix} ` : ''}${day} ${sequence} ${check}`
}

/** The ward's words for a priority. Routine is said, never left blank. */
export function priorityText(priority: string): string {
  if (priority === 'stat') return 'STAT'
  if (priority === 'urgent') return 'Urgent'
  return 'Routine'
}
