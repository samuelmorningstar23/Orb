/**
 * Orb, Beds and admissions API: the bed board, the awaiting and unlinked
 * lists, and available beds (adt-4a).
 *
 *   GET  /api/adt/board?ward_id=           every bed on the caller's wards
 *   GET  /api/adt/awaiting-bed?ward_id=    awaiting a bed / bed text not linked
 *   GET  /api/adt/beds/available?ward_id=  beds free now, for a picker
 *   POST /api/adt/master/reconciliation/link   link typed beds (bed.master)
 *
 * THE SERVER DERIVES EVERY STATUS AND WRITES EVERY WORD. A tile carries a
 * machine `status`, the sentence to print (`status_words`), a `tone` to style
 * by and `markers` (out of service, not linked, isolation). A screen prints
 * them as given and never maps a status to words of its own, so a status a
 * later increment adds (leave, reservations) shows without a client change.
 *
 * IDENTITY. `occupant` is present only for a patient inside the caller's
 * department; any other occupied tile has `occupant: null` and
 * `occupant_hidden: true`, and says 'Occupied' or 'Boarder'. The server
 * audits every read that names anybody.
 */
import { adtGet, adtPost } from './index'

/** How a tile is styled. Unknown tones are drawn neutral. */
export type BedTone = 'occupied' | 'attention' | 'ready' | 'progress' | 'held' | 'off' | 'neutral'

/** The occupant of a bed, present only inside the caller's department. */
export interface BedOccupant {
  admission_id: string
  patient_id: string
  name: string | null
  mrn: string | null
  department: string | null
  boarder: boolean
  /** The treating department, when it is not the ward's. */
  boarder_from: string | null
  presence_status: string | null
  /** False when the admission names this bed only as typed text. */
  linked: boolean
}

export interface BedTile {
  bed_id: string
  unit_code: string
  bed_label: string | null
  ward_id: string
  room_id: string | null
  room: string | null
  room_name: string | null
  bed_class: string | null
  isolation: boolean
  temporary: boolean
  /** occupied, occupied_on_leave, occupied_absconded, boarder, reserved,
   *  switched_off, out_of_service, needs_cleaning, cleaning, available,
   *  vacant_untracked, or a later status. */
  status: string
  status_words: string
  tone: BedTone | string
  markers: string[]
  /** A patient could be placed here now without skipping a clean. */
  free: boolean
  housekeeping_state: string | null
  /** When the bed entered its present status (UTC), when known. */
  since: string | null
  minutes_in_state: number | null
  occupant: BedOccupant | null
  occupant_hidden: boolean
}

export interface BoardWard {
  id: string
  code: string
  name: string | null
  department: string | null
  ward_type: string | null
  is_active: boolean
  cleaning_tracked: boolean
  register_complete: boolean
  /** beds, free, occupied_total, and one count per status present. */
  counts: Record<string, number>
  tiles: BedTile[]
}

export interface WardChoice {
  id: string
  code: string
  name: string | null
  department: string | null
  is_active: boolean
}

export interface BedBoard {
  generated_at: string
  department: string | null
  ward_id: string | null
  ward_choices: WardChoice[]
  wards: BoardWard[]
  /** status -> the server's words for it. */
  status_words: Record<string, string>
}

export function getBedBoard(wardId?: string | null): Promise<BedBoard> {
  return adtGet<BedBoard>('/board', { ward_id: wardId || undefined })
}

/** One admission on the awaiting or unlinked list. */
export interface AwaitingEntry {
  admission_id: string
  patient_id: string
  name: string | null
  mrn: string | null
  department: string | null
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
   *  doubled_bed or matches_bed. */
  reason: string | null
  reason_words: string | null
  since: string | null
}

export interface AwaitingBed {
  ward_id: string | null
  /** No bed text and no bed: nobody has said where they lie. */
  awaiting: AwaitingEntry[]
  /** Bed text typed, not linked to bed setup. Never merged with awaiting. */
  unlinked: AwaitingEntry[]
  counts: { awaiting: number; unlinked: number }
}

export function getAwaitingBed(wardId?: string | null): Promise<AwaitingBed> {
  return adtGet<AwaitingBed>('/awaiting-bed', { ward_id: wardId || undefined })
}

export interface AvailableBed {
  bed_id: string
  unit_code: string
  bed_label: string | null
  ward_id: string
  ward_code: string
  ward_department: string | null
  room: string | null
  bed_class: string | null
  isolation: boolean
  temporary: boolean
  status: string
  status_words: string
  markers: string[]
  terminal_clean: boolean
}

export interface AvailableBeds {
  ward_id: string | null
  beds: AvailableBed[]
  not_ready: AvailableBed[]
  status_words: Record<string, string>
}

export function getAvailableBeds(wardId?: string | null): Promise<AvailableBeds> {
  return adtGet<AvailableBeds>('/beds/available', { ward_id: wardId || undefined })
}

export interface LinkRequest {
  admission_ids: string[]
  boarder_acknowledged?: boolean
  out_of_service_reason?: string
  reason?: string
}

export interface LinkResult {
  linked: { admission_id: string; bed_id: string; ward_id: string; ward: string; bed: string;
            movement_id: string; boarder: boolean }[]
  skipped: { admission_id: string; code: string; message: string | null }[]
  count: number
}

/** Link typed beds to bed setup (bed.master). A placement that needs words
 *  (a boarder, a bed out of service) comes back in `skipped` with them. */
export function linkAdmissionsToBeds(request: LinkRequest): Promise<LinkResult> {
  return adtPost<LinkResult>('/master/reconciliation/link', request)
}
