/**
 * Orb, Beds and admissions API: bed setup (adt-4b).
 *
 *   GET   /api/adt/master                        adt.read    wards, rooms, beds, bed classes
 *   POST  /api/adt/master/wards                  bed.master  set up a ward
 *   PATCH /api/adt/master/wards/{id}             bed.master  change a ward
 *   POST  /api/adt/master/rooms                  bed.master  set up a room on a ward
 *   PATCH /api/adt/master/rooms/{id}             bed.master  change a room
 *   POST  /api/adt/master/beds                   bed.master  register beds (a list or a range)
 *   PATCH /api/adt/master/beds/{id}              bed.master  change a bed
 *   POST  /api/adt/master/beds/{id}/retire       bed.master  retire a bed nobody lies in
 *   POST  /api/adt/master/bed-classes            bed.master  set up a bed class
 *   PATCH /api/adt/master/bed-classes/{code}     bed.master  change a bed class
 *   POST  /api/adt/master/import-site-config     bed.master  wards from the site configuration
 *   GET   /api/adt/master/reconciliation         bed.master  active admissions against bed setup
 *   POST  /api/adt/master/wards/{id}/register-complete      the ward's register is complete
 *
 * Linking a matched admission to its bed is POST /master/reconciliation/link,
 * already in ./board.ts (linkAdmissionsToBeds); the Setup view uses that one.
 *
 * THE SERVER DECIDES AND WRITES THE WORDS. A ward code or a bed label cannot
 * change once anything refers to it, a bed with a patient in it cannot be
 * retired, and a register cannot be marked complete while an admission on the
 * ward is not linked or a bed is claimed twice. Each refusal comes back with a
 * stable `error` code on AdtApiError.code and the sentence to print as its
 * message. Every registration is one transaction on the server: a clash on one
 * bed saves none of them.
 *
 * NO PATIENT DATA in bed setup. The reconciliation is the exception: it lists
 * admission and patient ids against typed places (never a name or MRN), and
 * the server audits every read of it.
 */
import { adtGet, adtPost, adtRequest } from './index'

/** A ward of bed setup, as GET /master sends it. */
export interface MasterWard {
  id: string
  facility_code: string | null
  code: string
  name: string | null
  department: string | null
  ward_type: string | null
  default_bed_class: string | null
  cleaning_tracked: boolean
  register_complete: boolean
  register_complete_at: string | null
  register_complete_by: string | null
  ledger_complete_from: string | null
  is_active: boolean
  /** A doctor or nurse covers the ward's department. */
  has_cover: boolean
  /** The server's words when nobody covers the department, else null. */
  cover_note: string | null
  /** Beds in use and retired beds on the ward. */
  beds?: number
  retired_beds?: number
  rooms?: number
  created_at?: string | null
  updated_at?: string | null
}

export interface MasterRoom {
  id: string
  ward_id: string
  code: string
  name: string | null
  room_type: string
  is_isolation: boolean
  is_active: boolean
}

/** A bed of bed setup: a housekeeping unit with a ward. */
export interface MasterBed {
  id: string
  unit_code: string
  ward_id: string
  ward: string | null
  room_id: string | null
  room: string | null
  bed_label: string | null
  bed_class: string | null
  is_isolation_room: boolean
  /** An extra bed added beside an occupied one. */
  is_temporary: boolean
  is_active: boolean
  retired_at: string | null
  /** The housekeeping state: occupied, vacant_dirty, cleaning_in_progress,
   *  vacant_ready or out_of_service. */
  state: string | null
  state_since: string | null
  notes: string | null
}

export interface BedClass {
  code: string
  name: string
  bed_day_service_code: string | null
  is_critical_care: boolean
  counts_in_census: boolean
  is_active: boolean
  /** Whether the bed-day charge code is on the price list (null: no code). */
  bed_day_charge_listed: boolean | null
}

/** A bed typed into the housekeeping register that bed setup does not hold. */
export interface HousekeepingBedNotInSetup {
  id: string
  unit_code: string
  ward: string | null
  room: string | null
  bed_label: string | null
  state: string | null
}

export interface BedMaster {
  facility_code: string
  wards: MasterWard[]
  rooms: MasterRoom[]
  beds: MasterBed[]
  bed_classes: BedClass[]
  housekeeping_beds_not_in_setup: HousekeepingBedNotInSetup[]
  /** Departments a ward may belong to: covered by a doctor or nurse, or
   *  declared in the site configuration. */
  departments: { covering: string[]; declared: string[] }
  ward_types: string[]
  room_types: string[]
  max_beds_per_registration: number
}

export function getBedMaster(wardId?: string | null): Promise<BedMaster> {
  return adtGet<BedMaster>('/master', { ward_id: wardId || undefined })
}

function adtPatch<T>(path: string, body: unknown): Promise<T> {
  return adtRequest<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) })
}

const seg = (value: string) => encodeURIComponent(value)

// ── Wards ────────────────────────────────────────────────────────────────────

export interface NewWard {
  code: string
  department: string
  name?: string
  ward_type?: string
  default_bed_class?: string | null
  cleaning_tracked?: boolean
  reason?: string
}

export type WardChange = Partial<{
  code: string
  name: string
  department: string
  ward_type: string
  default_bed_class: string | null
  cleaning_tracked: boolean
  is_active: boolean
  reason: string
}>

export function createWard(ward: NewWard): Promise<MasterWard> {
  return adtPost<MasterWard>('/master/wards', ward)
}

/** Only the fields sent are changed. A code the ward is known by is refused
 *  with the server's words (code_in_use); a display name always changes. */
export function updateWard(wardId: string, change: WardChange): Promise<MasterWard> {
  return adtPatch<MasterWard>(`/master/wards/${seg(wardId)}`, change)
}

// ── Rooms ────────────────────────────────────────────────────────────────────

export interface NewRoom {
  ward_id: string
  code: string
  name?: string
  room_type?: string
  is_isolation?: boolean
  reason?: string
}

export type RoomChange = Partial<{
  code: string
  name: string
  room_type: string
  is_isolation: boolean
  is_active: boolean
  reason: string
}>

export function createRoom(room: NewRoom): Promise<MasterRoom> {
  return adtPost<MasterRoom>('/master/rooms', room)
}

export function updateRoom(roomId: string, change: RoomChange): Promise<MasterRoom> {
  return adtPatch<MasterRoom>(`/master/rooms/${seg(roomId)}`, change)
}

// ── Beds ─────────────────────────────────────────────────────────────────────

/** A numbered range: prefix '' from 1 to 24 with padding 2 gives 01 to 24. */
export interface BedLabelRange {
  prefix: string
  start: number
  end: number
  pad: number
}

export interface RegisterBedsRequest {
  ward_id: string
  labels?: string[]
  range?: BedLabelRange
  room_id?: string | null
  bed_class?: string | null
  is_isolation_room?: boolean | null
  notes?: string
  /** Link beds already typed into the housekeeping register on this ward with
   *  the same label, keeping their cleaning state, instead of refusing. */
  adopt_existing?: boolean
  reason?: string
}

export interface RegisteredBeds {
  ward_id: string
  ward_code: string
  registered: MasterBed[]
  adopted: MasterBed[]
  count: number
}

/** Every bed in one transaction: a clash on one bed saves none. */
export function registerBeds(request: RegisterBedsRequest): Promise<RegisteredBeds> {
  return adtPost<RegisteredBeds>('/master/beds', request)
}

export type BedChange = Partial<{
  bed_label: string
  room_id: string | null
  bed_class: string | null
  is_isolation_room: boolean
  notes: string
  reason: string
}>

/** A label the bed is known by is refused with the server's words
 *  (label_in_use). */
export function updateBed(bedId: string, change: BedChange): Promise<MasterBed> {
  return adtPatch<MasterBed>(`/master/beds/${seg(bedId)}`, change)
}

/** Refused while a patient lies in the bed or a reservation holds it. */
export function retireBed(bedId: string, reason?: string): Promise<MasterBed> {
  return adtPost<MasterBed>(`/master/beds/${seg(bedId)}/retire`, reason ? { reason } : {})
}

// ── Bed classes ──────────────────────────────────────────────────────────────

export interface NewBedClass {
  code: string
  name: string
  bed_day_service_code?: string | null
  is_critical_care?: boolean
  counts_in_census?: boolean
  reason?: string
}

export type BedClassChange = Partial<{
  name: string
  bed_day_service_code: string | null
  is_critical_care: boolean
  counts_in_census: boolean
  is_active: boolean
  reason: string
}>

export function createBedClass(bedClass: NewBedClass): Promise<BedClass> {
  return adtPost<BedClass>('/master/bed-classes', bedClass)
}

export function updateBedClass(code: string, change: BedClassChange): Promise<BedClass> {
  return adtPatch<BedClass>(`/master/bed-classes/${seg(code)}`, change)
}

// ── Site configuration ───────────────────────────────────────────────────────

export interface SiteConfigImport {
  created: MasterWard[]
  existing: MasterWard[]
  conflicts: { code: string; ward_id: string; department_in_setup: string | null;
               department_in_file: string | null }[]
  skipped: { code: string; department: string; reason: string }[]
  file_present: boolean
  /** The server's sentence when nothing could be set up, else null. */
  message: string | null
}

/** One ward per bed prefix the site configuration declares. Safe to repeat:
 *  wards already set up are reported, never changed. */
export function importSiteConfigWards(): Promise<SiteConfigImport> {
  return adtPost<SiteConfigImport>('/master/import-site-config', {})
}

// ── Reconciliation and register complete ─────────────────────────────────────

/** One active admission set against bed setup. Ids only, never a name. */
export interface ReconciliationEntry {
  admission_id: string
  patient_id: string
  department: string | null
  /** The typed ward and bed the admission carries. */
  ward: string | null
  bed: string | null
  ward_id: string | null
  bed_id: string | null
  master_ward_id: string | null
  master_ward_code: string | null
  master_bed_id: string | null
  master_bed_label: string | null
  master_unit_code: string | null
  /** awaiting_bed, ward_not_in_setup, bed_not_in_setup, bed_retired,
   *  doubled_bed, or null. */
  reason: string | null
}

export interface DoubledBed {
  bed_id: string
  unit_code: string | null
  bed_label: string | null
  ward_id: string | null
  admissions: ReconciliationEntry[]
}

export interface Reconciliation {
  ward_id: string | null
  ward_code: string | null
  linked: ReconciliationEntry[]
  /** The typed bed names exactly one bed in setup nobody else claims: these
   *  can be linked. */
  matched: ReconciliationEntry[]
  /** One bed claimed by two or more admissions: never linked from here. */
  doubled: DoubledBed[]
  unlinked: ReconciliationEntry[]
  awaiting: ReconciliationEntry[]
  mirror_mismatches: ReconciliationEntry[]
  counts: Record<string, number>
}

export function getReconciliation(wardId?: string | null): Promise<Reconciliation> {
  return adtGet<Reconciliation>('/master/reconciliation', { ward_id: wardId || undefined })
}

/** Refused (register_incomplete, 409) while an admission on the ward is not
 *  linked or a bed is claimed twice; the refusal names them by id. */
export function markRegisterComplete(wardId: string, reason?: string): Promise<MasterWard> {
  return adtPost<MasterWard>(`/master/wards/${seg(wardId)}/register-complete`,
    reason ? { reason } : {})
}

// ── Bulk registration preview ────────────────────────────────────────────────

/** What a registration will save, worked out before it is sent: the labels
 *  and their unit codes, or the reason it cannot be sent. The server applies
 *  the same rules and has the last word (a label already on the ward, a unit
 *  code already on the housekeeping register). */
export interface BedPreview {
  labels: string[]
  unitCodes: string[]
  problem: string | null
}

/** A ward code, room code or bed label as the server stores it: spaces
 *  tidied. */
export function tidyCode(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(' ')
}

/** '<ward code>/<bed label>', as the server codes every bed. */
export function masterUnitCode(wardCode: string, label: string): string {
  return `${wardCode}/${label}`
}

/** Labels typed one per line or separated by commas. */
export function splitLabels(text: string): string[] {
  return text.split(/[\n,]+/).map(tidyCode).filter(Boolean)
}

function wholeNumber(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null
  return Number(value.trim())
}

/** The labels a range gives, as the server expands it. */
export function rangeLabels(range: BedLabelRange): string[] {
  const prefix = tidyCode(range.prefix)
  const out: string[] = []
  for (let n = range.start; n <= range.end; n += 1) {
    out.push(`${prefix}${String(n).padStart(range.pad, '0')}`)
  }
  return out
}

export function previewBeds(input: {
  wardCode: string
  mode: 'labels' | 'range'
  labelsText: string
  prefix: string
  start: string
  end: string
  pad: string
  max: number
}): BedPreview {
  const none = (problem: string): BedPreview => ({ labels: [], unitCodes: [], problem })
  let labels: string[]
  if (input.mode === 'range') {
    const start = wholeNumber(input.start)
    const end = wholeNumber(input.end)
    const pad = input.pad.trim() ? wholeNumber(input.pad) : 0
    if (start === null || end === null) return none('A range needs a whole-number start and end.')
    if (end < start) return none('A range must run from a start up to an end at or above it.')
    if (pad === null || pad > 4) return none('Padding is between 0 and 4 digits.')
    if (end - start + 1 > input.max) return none(`At most ${input.max} beds can be registered at once.`)
    labels = rangeLabels({ prefix: input.prefix, start, end, pad })
  } else {
    labels = splitLabels(input.labelsText)
    if (!labels.length) return none('Give at least one bed label.')
    if (labels.length > input.max) return none(`At most ${input.max} beds can be registered at once.`)
  }
  if (labels.some((label) => label.includes('/'))) {
    return none("A bed label cannot contain '/'. A bed is shown as ward/bed.")
  }
  const seen = new Set<string>()
  const repeated: string[] = []
  for (const label of labels) {
    const key = label.toLowerCase()
    if (seen.has(key)) repeated.push(label)
    seen.add(key)
  }
  if (repeated.length) return none(`These bed labels appear more than once: ${repeated.join(', ')}.`)
  return { labels, unitCodes: labels.map((label) => masterUnitCode(input.wardCode, label)), problem: null }
}
