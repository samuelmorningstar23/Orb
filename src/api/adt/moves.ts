/**
 * Orb, Beds and admissions API: placing a patient in a bed (adt-5).
 *
 *   POST /api/adt/admissions/{id}/bed   a bed for an admission awaiting one,
 *                                       or a correction of a bed recorded
 *                                       wrongly (adt.bed or patient.write)
 *
 * The admission itself is POST /api/admissions/admit (the admission form's
 * own route, not under /api/adt). With ORB_ADT on, GET /api/admissions/options
 * adds `master`: the wards and beds of bed setup, each bed with the status
 * the bed board derives and what placing a patient there needs. The types for
 * that answer, and the pure rules the bed picker uses, live here so the
 * picker and the admission form share one reading of them.
 *
 * THE SERVER DECIDES. Every rule the picker shows (a reason for a bed not
 * recorded clean, an authoriser for a terminal clean, the boarder words, an
 * extra bed beside an occupied one) is enforced by the server, which answers
 * a refusal with a stable `error` code and its own words. The picker only
 * asks for what the server will ask for, before the round trip.
 *
 * AUTHORISING IS AN ACT (ruling ADT-S2 (c), round 3). A bed awaiting a
 * terminal clean is authorised by the person sending the form, when their
 * role allows (`self_authoriser`), or by someone who types their own signing
 * PIN into this form there and then. A name alone is refused. The PIN is
 * sent once and never kept: the form clears it after every send.
 */
import { adtPost } from './index'

/** What placing a patient in a bed needs, besides choosing it. */
export type BedNeed = 'out_of_service_reason' | 'not_clean_reason' | 'terminal_clean_authorisation'

/** One bed of bed setup, as the admission form's picker sees it. No patient
 *  data: an occupied bed says 'Occupied' and nothing about who. */
export interface MasterBedChoice {
  bed_id: string
  bed_label: string | null
  unit_code: string
  room: string | null
  bed_class: string | null
  isolation: boolean
  temporary: boolean
  status: string
  status_words: string
  tone: string
  markers: string[]
  /** A patient lies here; an extra bed may be added beside it. */
  occupied: boolean
  /** May be chosen (with what `needs` asks for). */
  selectable: boolean
  needs: BedNeed | null
  /** Its next clean is terminal: an isolation room, or an isolation patient
   *  left it since it was last recorded clean. */
  terminal_clean: boolean
  /** Housekeeping records it out of service, so its reason is needed too,
   *  even when `needs` asks for the terminal-clean authorisation. */
  out_of_service?: boolean
}

export interface MasterWardChoice {
  id: string
  code: string
  name: string | null
  department: string | null
  ward_type: string | null
  cleaning_tracked: boolean
  register_complete: boolean
  beds: MasterBedChoice[]
}

/** Who may authorise a bed awaiting a terminal clean. */
export interface BedAuthoriser {
  id: string
  name: string
  role: string
  department: string | null
}

/** The `master` block of GET /api/admissions/options, present with ORB_ADT on. */
export interface AdmissionMaster {
  wards: MasterWardChoice[]
  /** Others who can confirm with their own signing PIN (only those with one). */
  authorisers: BedAuthoriser[]
  /** The person signed in, when their role lets them authorise it themself. */
  self_authoriser?: BedAuthoriser | null
  /** How far back an admission time may be entered (ruling ADT-P3). */
  backtime_hours: number
}

/** `BedChoice.wardId` for a ward that is not in bed setup, typed instead. */
export const TYPED_WARD = 'typed'

/** The choice the bed picker holds. */
export interface BedChoice {
  /** A ward of bed setup by id, TYPED_WARD for a ward typed because it is not
   *  in bed setup, or '' while nothing is chosen. */
  wardId: string
  /** The bed chosen; with `extraBed`, the occupied bed the extra goes beside. */
  bedId: string
  /** 'No bed yet': admitted to the ward, awaiting a bed. */
  awaiting: boolean
  /** Add a temporary bed with its own label beside the occupied `bedId`. */
  extraBed: boolean
  /** A ward or bed not in bed setup, as typed (kept unlinked, with a warning). */
  typedWard: string
  typedBed: string
  outOfServiceReason: string
  notCleanReason: string
  terminalAuthorisedBy: string
  /** The signing PIN of the person authorising, typed by them; empty when the
   *  sender authorises it themself. Cleared after every send. */
  terminalAuthoriserPin: string
  terminalReason: string
  boarderAcknowledged: boolean
}

export const EMPTY_BED_CHOICE: BedChoice = {
  wardId: '',
  bedId: '',
  awaiting: false,
  extraBed: false,
  typedWard: '',
  typedBed: '',
  outOfServiceReason: '',
  notCleanReason: '',
  terminalAuthorisedBy: '',
  terminalAuthoriserPin: '',
  terminalReason: '',
  boarderAcknowledged: false,
}

/** The ward of bed setup the choice names, if any. */
export function chosenWard(master: AdmissionMaster | null | undefined,
  choice: BedChoice): MasterWardChoice | null {
  if (!master || !choice.wardId) return null
  return master.wards.find((w) => w.id === choice.wardId) ?? null
}

/** The bed of bed setup the choice names, if any. */
export function chosenBed(master: AdmissionMaster | null | undefined,
  choice: BedChoice): MasterBedChoice | null {
  const ward = chosenWard(master, choice)
  if (!ward || !choice.bedId) return null
  return ward.beds.find((b) => b.bed_id === choice.bedId) ?? null
}

const same = (a: string | null | undefined, b: string | null | undefined) =>
  (a ?? '').trim().toLowerCase() === (b ?? '').trim().toLowerCase()

/** True when the chosen ward belongs to another department than the treating
 *  one: the patient would lie there as a boarder. */
export function isBoarder(ward: MasterWardChoice | null, department: string): boolean {
  return !!ward?.department && !!department.trim() && !same(ward.department, department)
}

/** The server's words for a boarder placement (services/adt/core.boarder_words). */
export function boarderWords(ward: MasterWardChoice, department: string): string {
  return `Nurses on ${ward.code} (${ward.department}) will not be able to open this chart. `
    + `The ${department || 'treating'} team keeps it.`
}

/** True when placing a patient in the bed needs its out-of-service reason. */
export function needsOutOfServiceReason(bed: MasterBedChoice | null): boolean {
  return !!bed && (bed.needs === 'out_of_service_reason'
    || (bed.needs === 'terminal_clean_authorisation' && bed.out_of_service === true))
}

/** True when the person chosen to authorise is the one sending the form. */
export function authorisesThemself(master: AdmissionMaster | null | undefined,
  choice: BedChoice): boolean {
  return !!choice.terminalAuthorisedBy
    && choice.terminalAuthorisedBy === (master?.self_authoriser?.id ?? null)
}

/** Who may be offered to authorise a bed on `ward` for a patient of
 *  `department`: the sender first (when their role allows), then those who
 *  can confirm with a signing PIN, each working in the ward's department or
 *  the treating one, or in none. The server applies the same rule. */
export function authorisersFor(master: AdmissionMaster, ward: MasterWardChoice | null,
  department: string): BedAuthoriser[] {
  const fits = (a: BedAuthoriser) => !a.department?.trim()
    || same(a.department, ward?.department) || same(a.department, department)
  const self = master.self_authoriser && fits(master.self_authoriser) ? [master.self_authoriser] : []
  return [...self, ...master.authorisers.filter((a) => a.id !== self[0]?.id && fits(a))]
}

/** What is still missing before the choice can be sent, in words, or null. */
export function bedChoiceMissing(master: AdmissionMaster | null | undefined,
  choice: BedChoice, department: string): string | null {
  if (choice.wardId === TYPED_WARD) {
    return choice.typedWard.trim() ? null : 'Type the ward.'
  }
  const ward = chosenWard(master, choice)
  if (!ward) return choice.wardId ? 'Choose a ward from the list.' : 'Choose a ward.'
  if (isBoarder(ward, department) && !choice.boarderAcknowledged) {
    return 'Confirm you have read who will be able to open this chart.'
  }
  if (choice.awaiting) return null
  const bed = chosenBed(master, choice)
  if (!bed) return 'Choose a bed, or No bed yet.'
  if (choice.extraBed) return bed.occupied ? null : 'An extra bed is added only beside an occupied bed.'
  if (!bed.selectable) return 'That bed cannot be chosen. Choose another bed.'
  if (needsOutOfServiceReason(bed) && !choice.outOfServiceReason.trim()) {
    return 'Type the reason the patient is placed in a bed out of service.'
  }
  if (bed.needs === 'not_clean_reason' && !choice.notCleanReason.trim()) {
    return 'Type the reason the patient is placed in a bed not yet recorded clean.'
  }
  if (bed.needs === 'terminal_clean_authorisation') {
    if (!choice.terminalAuthorisedBy || !choice.terminalReason.trim()) {
      return 'Choose who authorises this bed before its terminal clean, and why.'
    }
    if (!authorisesThemself(master, choice) && !choice.terminalAuthoriserPin) {
      return 'The person authorising types their own signing PIN.'
    }
  }
  return null
}

/** The admission request's placement fields for the choice (adt-5). ward and
 *  bed stay text, as the request has always carried them. */
export interface AdmissionPlacement {
  ward: string
  bed: string
  ward_id?: string
  bed_id?: string
  awaiting_bed?: boolean
  extra_bed?: boolean
  out_of_service_reason?: string
  not_clean_reason?: string
  terminal_clean_authorised_by?: string
  terminal_clean_reason?: string
  terminal_clean_authoriser_pin?: string
  boarder_acknowledged?: boolean
}

export function bedChoicePayload(master: AdmissionMaster | null | undefined,
  choice: BedChoice): AdmissionPlacement {
  const ward = chosenWard(master, choice)
  if (!ward) {
    const typed = choice.wardId === TYPED_WARD
    const bed = typed && !choice.awaiting ? choice.typedBed.trim() : ''
    return { ward: typed ? choice.typedWard.trim() : '', bed,
      ...(bed ? {} : { awaiting_bed: true }) }
  }
  const out: AdmissionPlacement = { ward: ward.code, bed: '', ward_id: ward.id }
  if (choice.boarderAcknowledged) out.boarder_acknowledged = true
  if (choice.awaiting) return { ...out, awaiting_bed: true }
  const bed = chosenBed(master, choice)
  if (!bed) return out
  out.bed = bed.bed_label ?? ''
  out.bed_id = bed.bed_id
  if (choice.extraBed) return { ...out, extra_bed: true }
  if (needsOutOfServiceReason(bed)) out.out_of_service_reason = choice.outOfServiceReason.trim()
  if (bed.needs === 'not_clean_reason') out.not_clean_reason = choice.notCleanReason.trim()
  if (bed.needs === 'terminal_clean_authorisation') {
    out.terminal_clean_authorised_by = choice.terminalAuthorisedBy
    out.terminal_clean_reason = choice.terminalReason.trim()
    if (!authorisesThemself(master, choice) && choice.terminalAuthoriserPin) {
      out.terminal_clean_authoriser_pin = choice.terminalAuthoriserPin
    }
  }
  return out
}

/** A short line saying where the choice puts the patient. */
export function bedChoiceSummary(master: AdmissionMaster | null | undefined,
  choice: BedChoice): string {
  const ward = chosenWard(master, choice)
  if (!ward) {
    if (choice.wardId !== TYPED_WARD || !choice.typedWard.trim()) return 'No ward chosen'
    const bed = choice.awaiting ? '' : choice.typedBed.trim()
    return `${choice.typedWard.trim()}, ${bed ? `bed ${bed}` : 'no bed yet'} (not in bed setup)`
  }
  if (choice.awaiting) return `${ward.code}, no bed yet`
  const bed = chosenBed(master, choice)
  if (!bed) return `${ward.code}, no bed chosen`
  if (choice.extraBed) return `${ward.code}, an extra bed beside bed ${bed.bed_label}`
  return `${ward.code}, bed ${bed.bed_label}`
}

/** Refusal codes the bed picker can act on (the server's `error`). */
export const BED_REFUSAL_CODES = new Set([
  'bed_occupied', 'bed_retired', 'bed_inactive', 'ward_inactive', 'bed_reserved',
  'out_of_service_reason_required', 'bed_not_clean_reason_required',
  'terminal_clean_authorisation_required', 'terminal_clean_authoriser_invalid',
  'terminal_clean_pin_required', 'terminal_clean_pin_wrong', 'terminal_clean_authoriser_locked',
  'terminal_clean_authoriser_no_pin', 'terminal_clean_authoriser_out_of_department',
  'boarder_acknowledgement_required', 'extra_bed_not_needed', 'extra_bed_refused',
  'extra_bed_labels_exhausted', 'bed_not_found', 'ward_not_found', 'bed_not_on_ward',
  'awaiting_bed_with_bed',
])

/** A new Idempotency-Key: one per form, so a retried send is never recorded
 *  twice. */
export function newIdempotencyKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (c?.randomUUID) return c.randomUUID()
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

/** The body of POST /api/adt/admissions/{id}/bed. */
export interface AssignBedRequest {
  bed_id?: string
  ward_id?: string
  ward?: string
  bed?: string
  extra_bed?: boolean
  correction?: boolean
  /** Sent (even as null) with a correction: the bed the screen showed. */
  expected_bed_id?: string | null
  reason?: string
  moved_at?: string
  out_of_service_reason?: string
  boarder_acknowledged?: boolean
  not_clean_reason?: string
  terminal_clean_authorised_by?: string
  terminal_clean_reason?: string
  /** The signing PIN of the person authorising, when it is not the sender. */
  terminal_clean_authoriser_pin?: string
}

export interface PlacedAdmission {
  replayed: boolean
  admission_id: string
  patient_id: string
  ward_id?: string | null
  bed_id?: string | null
  ward?: string | null
  bed?: string | null
  linked?: boolean
  boarder?: boolean
  movement: { id: string; movement_type: string; [key: string]: unknown }
}

/** Give an admission awaiting a bed its bed, or correct a bed recorded
 *  wrongly. Throws AdtApiError; branch on its `code`. */
export function assignBed(admissionId: string, request: AssignBedRequest,
  key: string): Promise<PlacedAdmission> {
  return adtPost<PlacedAdmission>(`/admissions/${encodeURIComponent(admissionId)}/bed`,
    request, key)
}
