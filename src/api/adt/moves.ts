/**
 * Orb, Beds and admissions API: placing a patient in a bed (adt-5), and
 * moving one (adt-6a).
 *
 *   POST /api/adt/admissions/{id}/bed   a bed for an admission awaiting one,
 *                                       or a correction of a bed recorded
 *                                       wrongly (adt.bed or patient.write)
 *   GET  /api/adt/move-options          the Move screen's wards, beds,
 *                                       authorisers and covered departments
 *   GET  /api/adt/admissions/{id}/location
 *                                       where the admission lies now
 *   POST /api/adt/admissions/{id}/bed-change
 *                                       another bed, same treating department
 *                                       (adt.bed or patient.write)
 *   POST /api/adt/transfers             another treating department
 *                                       (adt.transfer or patient.write)
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
import { adtGet, adtPost, toAdtError } from './index'

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
  /** adt-16 (ORB_ADT_STRICT_LOCATIONS): the server refuses a ward or bed not
   *  in bed setup, so the picker offers no typed place. Absent means off. */
  strict_locations?: boolean
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

// ── Moves (adt-6a) ───────────────────────────────────────────────────────────

/** GET /api/adt/move-options: no patient data. */
export interface MoveOptions {
  master: AdmissionMaster
  /** Departments with clinical cover: where a transfer may go (rule C). */
  departments: string[]
}

export function getMoveOptions(): Promise<MoveOptions> {
  return adtGet<MoveOptions>('/move-options')
}

/** What keeping the current place on a transfer resolves to in bed setup,
 *  read the way the server's writer resolves it: a typed, unlinked ward that
 *  matches a ward of bed setup is that ward. */
export interface KeptPlace {
  /** The ward's code in bed setup, or null when the place matches none. */
  ward: string | null
  ward_department: string | null
  bed: string | null
  /** Housekeeping records the bed out of service: keeping it needs a reason. */
  out_of_service: boolean
}

/** One side of a recorded move: where the patient was or went. */
export interface MovePlace {
  department: string | null
  ward: string | null
  bed: string | null
}

/** The stay's latest move, and whether it can be undone as recorded in
 *  error (adt-6b). Only a bed assignment, a bed change or a transfer can be,
 *  only the latest, only once, and only while the patient is still where it
 *  put them; `not_correctable` says why not in the server's words. */
export interface LastMove {
  id: string
  movement_type: string
  moved_at: string | null
  recorded_at?: string | null
  moved_by_name?: string | null
  reason?: string | null
  from: MovePlace
  to: MovePlace
  /** Undoing it gives the patient back to another treating department. */
  changes_department: boolean
  correctable: boolean
  not_correctable?: { code: string; message: string } | null
}

/** Where an admission lies now (GET /api/adt/admissions/{id}/location). */
export interface AdmissionLocation {
  admission_id: string
  department: string | null
  ward: string | null
  bed: string | null
  ward_id: string | null
  bed_id: string | null
  ward_department: string | null
  linked: boolean
  awaiting_bed: boolean
  boarder: boolean
  kept_place?: KeptPlace | null
  /** adt-6b: the latest move of this stay (absent from an older server). */
  last_move?: LastMove | null
}

export function getAdmissionLocation(admissionId: string): Promise<AdmissionLocation> {
  return adtGet<AdmissionLocation>(`/admissions/${encodeURIComponent(admissionId)}/location`)
}

/** The body of a bed change; a transfer adds its own fields. */
export interface MoveRequest {
  bed_id?: string
  ward_id?: string
  ward?: string
  bed?: string
  extra_bed?: boolean
  /** The bed the screen showed (null for none). Always sent. */
  expected_bed_id: string | null
  reason: string
  moved_at?: string
  notes?: string
  out_of_service_reason?: string
  boarder_acknowledged?: boolean
  not_clean_reason?: string
  terminal_clean_authorised_by?: string
  terminal_clean_reason?: string
  terminal_clean_authoriser_pin?: string
}

export interface TransferRequest extends MoveRequest {
  admission_id: string
  to_department: string
  /** Moved off the old bed, waiting for a bed on the receiving ward. */
  awaiting_bed?: boolean
  /** The department changes; the patient stays where they lie. */
  keep_location?: boolean
}

/** What a move answers. A replay carries only `replayed`, `admission_id`
 *  and the movement's id, type and time: the chart may already have been
 *  handed over to another department. */
export interface MoveResult {
  replayed: boolean
  admission_id: string
  movement: { id: string; movement_type: string; moved_at?: string | null; [key: string]: unknown }
  patient_id?: string
  current_department?: string | null
  current_ward?: string | null
  current_bed?: string | null
  ward_id?: string | null
  bed_id?: string | null
  linked?: boolean
  awaiting_bed?: boolean
  boarder?: boolean
  department_changed?: boolean
}

/** The placement fields of a move for the picker's choice. 'No bed yet' is
 *  only for a transfer; a bed change always names a bed. */
export function movePlacement(master: AdmissionMaster | null | undefined, choice: BedChoice,
  { allowAwaiting }: { allowAwaiting: boolean }): Partial<TransferRequest> {
  const placed = bedChoicePayload(master, choice)
  const out: Partial<TransferRequest> = {}
  if (placed.ward_id) out.ward_id = placed.ward_id
  else if (placed.ward) out.ward = placed.ward
  if (placed.bed_id) out.bed_id = placed.bed_id
  else if (placed.bed) out.bed = placed.bed
  if (placed.awaiting_bed && allowAwaiting) out.awaiting_bed = true
  if (placed.extra_bed) out.extra_bed = true
  if (placed.boarder_acknowledged) out.boarder_acknowledged = true
  if (placed.out_of_service_reason) out.out_of_service_reason = placed.out_of_service_reason
  if (placed.not_clean_reason) out.not_clean_reason = placed.not_clean_reason
  if (placed.terminal_clean_authorised_by) {
    out.terminal_clean_authorised_by = placed.terminal_clean_authorised_by
    out.terminal_clean_reason = placed.terminal_clean_reason
    if (placed.terminal_clean_authoriser_pin) {
      out.terminal_clean_authoriser_pin = placed.terminal_clean_authoriser_pin
    }
  }
  return out
}

/** Move an admitted patient to another bed, keeping the treating department.
 *  Throws AdtApiError; branch on its `code`. */
export function changeBed(admissionId: string, request: MoveRequest,
  key: string): Promise<MoveResult> {
  return adtPost<MoveResult>(`/admissions/${encodeURIComponent(admissionId)}/bed-change`,
    request, key)
}

/** Transfer an admitted patient to another treating department. Throws
 *  AdtApiError; branch on its `code`. */
export function transferAdmission(request: TransferRequest, key: string): Promise<MoveResult> {
  return adtPost<MoveResult>('/transfers', request, key)
}

/** The words for a failed move: the server's own words when it gave them. */
export function moveFailureWords(err: unknown): string {
  const failure = toAdtError(err)
  if (failure.isUnreachable) {
    return 'Could not reach the server, so it is not known whether the move was recorded. '
      + 'Send it again: the same move is never recorded twice.'
  }
  // A 403 that carries its own refusal (a signing PIN that is not right) is
  // printed in its words; only a refused permission reads as the role.
  if (failure.isForbidden && (failure.code === 'permission_denied' || failure.code === 'http_403')) {
    return 'Your role cannot make this move.'
  }
  if (failure.code === 'state_changed') {
    return 'Where this patient lies changed since this screen opened. Close it and open it again.'
  }
  if (failure.code === 'idempotency_key_reused') {
    return 'This screen was already used for another move. Close it and open it again.'
  }
  if (failure.isNotFound) return 'This patient is not on your list, so they cannot be moved from here.'
  return failure.message || 'The move was not recorded. Nothing was changed.'
}

// ── Swap and a move recorded in error (adt-6b) ───────────────────────────────

/** The body of POST /api/adt/bed-swaps. Both expected bed ids are always
 *  sent: the beds the screen showed. A treating department never changes. */
export interface SwapRequest {
  first_admission_id: string
  second_admission_id: string
  expected_first_bed_id: string | null
  expected_second_bed_id: string | null
  reason: string
  moved_at?: string
  notes?: string
  out_of_service_reason?: string
  boarder_acknowledged?: boolean
  /** Ruling ADT-S2 (c): neither bed is cleaned between the two patients. */
  not_clean_reason?: string
  terminal_clean_authorised_by?: string
  terminal_clean_reason?: string
  terminal_clean_authoriser_pin?: string
}

/** What a swap answers: each patient's move. A replay is the first answer. */
export interface SwapResult {
  replayed: boolean
  swap_key?: string
  first: MoveResult
  second: MoveResult
}

/** Swap two admitted patients' beds. One Idempotency-Key covers the pair.
 *  Throws AdtApiError; branch on its `code`. */
export function swapBeds(request: SwapRequest, key: string): Promise<SwapResult> {
  return adtPost<SwapResult>('/bed-swaps', request, key)
}

/** The body of POST /api/adt/movements/{id}/correct. */
export interface CorrectionRequest {
  /** The bed the screen showed (null for none). Always sent. */
  expected_bed_id: string | null
  reason: string
  notes?: string
  out_of_service_reason?: string
  boarder_acknowledged?: boolean
  not_clean_reason?: string
  terminal_clean_authorised_by?: string
  terminal_clean_reason?: string
  terminal_clean_authoriser_pin?: string
}

export interface CorrectionResult extends MoveResult {
  corrects_movement_id: string
}

/** Undo the stay's latest move as recorded in error. Throws AdtApiError. */
export function correctMovement(movementId: string, request: CorrectionRequest,
  key: string): Promise<CorrectionResult> {
  return adtPost<CorrectionResult>(`/movements/${encodeURIComponent(movementId)}/correct`,
    request, key)
}

/** The kinds of move, in words, for a line about the last one. */
export const MOVE_TYPE_WORDS: Record<string, string> = {
  admission: 'Admission',
  bed_assigned: 'Bed assigned',
  bed_change: 'Bed change',
  transfer: 'Transfer',
  bed_swap: 'Bed swap',
  location_linked: 'Linked to bed setup',
  correction: 'Correction',
  discharge: 'End of stay',
}

/** The words for a failed swap: the server's own words when it gave them. */
export function swapFailureWords(err: unknown): string {
  const failure = toAdtError(err)
  if (failure.isUnreachable) {
    return 'Could not reach the server, so it is not known whether the swap was recorded. '
      + 'Send it again: the same swap is never recorded twice.'
  }
  if (failure.isForbidden && (failure.code === 'permission_denied' || failure.code === 'http_403')) {
    return 'Your role cannot swap beds.'
  }
  if (failure.code === 'state_changed') {
    return 'Where one of these patients lies changed since this screen opened. Close it and open it again.'
  }
  if (failure.code === 'idempotency_key_reused') {
    return 'This screen was already used for another swap. Close it and open it again.'
  }
  if (failure.isNotFound) return 'One of these patients is not on your list, so the beds cannot be swapped from here.'
  return failure.message || 'The swap was not recorded. Nothing was changed.'
}

/** The words for a failed correction. */
export function correctionFailureWords(err: unknown): string {
  const failure = toAdtError(err)
  if (failure.isUnreachable) {
    return 'Could not reach the server, so it is not known whether the correction was recorded. '
      + 'Send it again: the same correction is never recorded twice.'
  }
  if (failure.isForbidden && (failure.code === 'permission_denied' || failure.code === 'http_403')) {
    return 'Your role cannot undo this move.'
  }
  if (failure.code === 'idempotency_key_reused') {
    return 'This screen was already used for another change. Close it and open it again.'
  }
  if (failure.isNotFound) return 'This patient is not on your list, so the move cannot be undone from here.'
  return failure.message || 'The correction was not recorded. Nothing was changed.'
}
