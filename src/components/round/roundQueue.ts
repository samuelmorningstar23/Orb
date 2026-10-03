/**
 * Ward Round — the queue.
 *
 * A round is a SEQUENCE Orb drives, not a list the doctor scrolls. This module
 * owns every rule about that sequence and holds no React, no fetching and no
 * clinical text, so the rules can be tested exhaustively (see roundQueue.test.ts).
 *
 * Three things here are load-bearing:
 *
 *   BED ORDER IS THE WALK. Beds are sorted the way a person walks a corridor
 *   ("GM-9" then "GM-10"), not the way a computer sorts strings ("GM-10" then
 *   "GM-9"). Getting this wrong sends the doctor back down the ward.
 *
 *   SKIP MUST BE USABLE. "Next" prefers beds never reached, and only comes back
 *   to skipped ones when nothing else is left — so skipping the patient who is
 *   in radiology moves the round on instead of stalling it.
 *
 *   RESUME IS THE DEFAULT, BUT THE TABLET IS SHARED. Progress is persisted so an
 *   interrupted round is picked up where it stopped. What is persisted is
 *   deliberately the thinnest thing that works — patient IDs and a seen/skipped
 *   mark, no names, no beds, no clinical text — and any round belonging to
 *   another clinician or another day is ERASED from the device on mount, not
 *   merely ignored. Which patients a named doctor visited is itself disclosure.
 */

import type { NursePatientCard } from '../../api/nurse'
import type { Patient } from '../../api/client'

/** A patient in the round queue. Merged from the two department-scoped rosters
 *  the server already exposes — the ward board (`/nurse/board`, which carries
 *  NEWS2, medications and allergies) and the patient board (`/vigil/patients`,
 *  which carries the admission date and risk). Both derive their scope from the
 *  authenticated user, never from anything this client sends. */
export interface RoundPatient {
  id: string
  name: string
  bed: string
  ward: string
  mrn: string
  age: number
  sex: string
  diagnosis: string
  admission_date: string
  risk_score: number | null
  risk_level: string | null
  risk_drivers: string[]
  attending_doctor_name: string | null
}

/** 'pending' = not yet reached. 'skipped' = deliberately deferred, still owed.
 *  Only 'seen' completes a patient. */
export type RoundStatus = 'pending' | 'seen' | 'skipped'

export interface RoundState {
  userId: string
  dateKey: string
  /** Bed the clinician is standing at. */
  cursor: string | null
  statuses: Record<string, RoundStatus>
}

export interface RoundProgress {
  total: number
  seen: number
  skipped: number
  pending: number
  /** Patients still owed a visit — skipped + never reached. */
  remaining: number
  complete: boolean
}

export const ROUND_STATE_PREFIX = 'orb_round_v1:'

export function statusOf(
  statuses: Record<string, RoundStatus>,
  id: string,
): RoundStatus {
  return statuses[id] ?? 'pending'
}

// ── Bed ordering ──

/** Split "GM-10" into ["gm-", 10] so digit runs compare as numbers. */
function bedTokens(bed: string): (string | number)[] {
  const out: (string | number)[] = []
  for (const chunk of bed.toLowerCase().match(/\d+|\D+/g) ?? []) {
    out.push(/^\d/.test(chunk) ? parseInt(chunk, 10) : chunk)
  }
  return out
}

/**
 * Natural-order bed comparison. Beds with nothing recorded sort LAST: an empty
 * string wins a plain sort, which would open the round on a patient who has no
 * bed to walk to.
 */
export function compareBeds(a: string, b: string): number {
  const aEmpty = !a || !a.trim()
  const bEmpty = !b || !b.trim()
  if (aEmpty || bEmpty) return aEmpty && bEmpty ? 0 : aEmpty ? 1 : -1

  const ta = bedTokens(a)
  const tb = bedTokens(b)
  for (let i = 0; i < Math.max(ta.length, tb.length); i++) {
    const x = ta[i]
    const y = tb[i]
    if (x === undefined) return -1
    if (y === undefined) return 1
    if (typeof x === 'number' && typeof y === 'number') {
      if (x !== y) return x - y
    } else if (typeof x === 'number') {
      return -1 // numbers before letters, so "4" precedes "4a"
    } else if (typeof y === 'number') {
      return 1
    } else if (x !== y) {
      return x < y ? -1 : 1
    }
  }
  return 0
}

/** Order the roster into the physical walk. */
export function buildQueue(patients: RoundPatient[]): RoundPatient[] {
  return [...patients].sort((a, b) => compareBeds(a.bed, b.bed) || a.id.localeCompare(b.id))
}

/**
 * Fuse the ward board and the patient board into one queue.
 *
 * Either source may be missing — each is a separate request and one can fail
 * while the other succeeds. A patient present in only ONE of them still gets a
 * bed on the round: dropping them because the other request failed would mean a
 * network blip silently removes a patient from the doctor's round, which is the
 * worst possible failure mode for this screen.
 */
export function mergeRoster(
  cards: NursePatientCard[],
  vigil: Patient[],
): RoundPatient[] {
  const byId = new Map<string, RoundPatient>()

  for (const c of cards) {
    byId.set(c.patient_id, {
      id: c.patient_id,
      name: c.name,
      bed: c.bed ?? '',
      ward: c.ward ?? '',
      mrn: c.mrn ?? '',
      age: c.age ?? 0,
      sex: c.sex ?? '',
      diagnosis: c.diagnosis ?? '',
      admission_date: '',
      risk_score: null,
      risk_level: null,
      risk_drivers: [],
      attending_doctor_name: c.attending_doctor_name ?? null,
    })
  }

  for (const p of vigil) {
    const existing = byId.get(p.id)
    if (existing) {
      // The ward board wins on clinical fields; the patient board supplies the
      // two it does not carry.
      existing.admission_date = p.admission_date ?? ''
      existing.risk_score = p.risk_score ?? null
      existing.risk_level = p.risk_level ?? null
      existing.risk_drivers = p.risk_drivers ?? []
      if (!existing.bed) existing.bed = p.bed ?? ''
      if (!existing.diagnosis) existing.diagnosis = p.diagnosis ?? ''
    } else {
      byId.set(p.id, {
        id: p.id,
        name: p.name,
        bed: p.bed ?? '',
        ward: p.ward ?? '',
        mrn: p.mrn ?? '',
        age: p.age ?? 0,
        sex: p.sex ?? '',
        diagnosis: p.diagnosis ?? '',
        admission_date: p.admission_date ?? '',
        risk_score: p.risk_score ?? null,
        risk_level: p.risk_level ?? null,
        risk_drivers: p.risk_drivers ?? [],
        attending_doctor_name: p.attending_doctor_name ?? null,
      })
    }
  }

  return buildQueue([...byId.values()])
}

// ── Movement ──

/** Circular index walk starting just after `fromId` (or at 0 when unset). */
function* walkFrom(queue: RoundPatient[], fromId: string | null): Generator<RoundPatient> {
  if (queue.length === 0) return
  const start = fromId ? queue.findIndex(p => p.id === fromId) : -1
  for (let step = 1; step <= queue.length; step++) {
    yield queue[(start + step + queue.length * 2) % queue.length]
  }
}

/**
 * The next bed to walk to, or null when the round is finished.
 *
 * Two passes on purpose. The first looks only for beds never reached, so
 * skipping a patient moves the round ON. The second returns to skipped beds
 * once nothing else is pending — that is what stops a skip from quietly
 * becoming a patient nobody saw.
 */
export function nextIncomplete(
  queue: RoundPatient[],
  statuses: Record<string, RoundStatus>,
  fromId: string | null,
): string | null {
  for (const p of walkFrom(queue, fromId)) {
    if (statusOf(statuses, p.id) === 'pending') return p.id
  }
  for (const p of walkFrom(queue, fromId)) {
    if (p.id !== fromId && statusOf(statuses, p.id) === 'skipped') return p.id
  }
  return null
}

/** Step back through the physical order. A correction, so status is ignored. */
export function previousInQueue(queue: RoundPatient[], fromId: string | null): string | null {
  if (queue.length === 0) return null
  const i = queue.findIndex(p => p.id === fromId)
  if (i < 0) return queue[queue.length - 1].id
  return queue[(i - 1 + queue.length) % queue.length].id
}

// ── Progress and hand-on ──

export function progressOf(
  queue: RoundPatient[],
  statuses: Record<string, RoundStatus>,
): RoundProgress {
  let seen = 0
  let skipped = 0
  let pending = 0
  for (const p of queue) {
    const s = statusOf(statuses, p.id)
    if (s === 'seen') seen++
    else if (s === 'skipped') skipped++
    else pending++
  }
  return {
    total: queue.length,
    seen,
    skipped,
    pending,
    remaining: skipped + pending,
    complete: queue.length > 0 && seen === queue.length,
  }
}

/** Everyone the round did NOT finish, in walk order — the list handed on. */
export function handoffList(
  queue: RoundPatient[],
  statuses: Record<string, RoundStatus>,
): { patient: RoundPatient; status: RoundStatus }[] {
  return queue
    .filter(p => statusOf(statuses, p.id) !== 'seen')
    .map(p => ({ patient: p, status: statusOf(statuses, p.id) }))
}

// ── Resume ──

/** LOCAL calendar date. A round belongs to a shift, and the tablet's clock is
 *  the shift's clock — deriving this from UTC rolls the key over mid-round in
 *  India (IST is +05:30, so a 04:00 ward round is still "yesterday" in UTC). */
export function todayKey(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

export function roundStateKey(userId: string, dateKey: string): string {
  return `${ROUND_STATE_PREFIX}${userId}:${dateKey}`
}

export function saveRoundState(state: RoundState): void {
  try {
    localStorage.setItem(
      roundStateKey(state.userId, state.dateKey),
      // Explicit field list, not a spread: this is the guarantee that no
      // clinical text can ever be written to the device by accident.
      JSON.stringify({
        userId: state.userId,
        dateKey: state.dateKey,
        cursor: state.cursor,
        statuses: state.statuses,
      }),
    )
  } catch {
    // Storage full / disabled (private mode). The round still works in memory;
    // only resume-after-reload is lost, which is better than a crash mid-round.
  }
}

export function loadRoundState(userId: string, dateKey: string): RoundState | null {
  try {
    const raw = localStorage.getItem(roundStateKey(userId, dateKey))
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<RoundState>
    if (!parsed || typeof parsed !== 'object') return null
    if (parsed.userId !== userId || parsed.dateKey !== dateKey) return null
    const statuses: Record<string, RoundStatus> = {}
    for (const [id, s] of Object.entries(parsed.statuses ?? {})) {
      if (s === 'seen' || s === 'skipped' || s === 'pending') statuses[id] = s
    }
    return {
      userId,
      dateKey,
      cursor: typeof parsed.cursor === 'string' ? parsed.cursor : null,
      statuses,
    }
  } catch {
    // Corrupt payload — start a fresh round rather than throwing on mount.
    return null
  }
}

/**
 * Erase every stored round except this clinician's round for today.
 *
 * A ward tablet passes between doctors. Leaving yesterday's or a colleague's
 * round on disk discloses which patients a named clinician visited, so it is
 * removed rather than merely skipped at read time.
 */
export function purgeForeignRoundState(userId: string, dateKey: string): void {
  try {
    const keep = roundStateKey(userId, dateKey)
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith(ROUND_STATE_PREFIX) && key !== keep) doomed.push(key)
    }
    for (const key of doomed) localStorage.removeItem(key)
  } catch {
    // Storage unavailable — nothing was persisted, so nothing to purge.
  }
}

/**
 * Re-key stored marks against the roster as it is NOW.
 *
 * Patients are discharged and admitted mid round. A mark for someone no longer
 * on the ward is dropped (so they stop counting toward "remaining"), and a
 * patient admitted mid round simply has no mark, which reads as not-yet-seen.
 */
export function reconcileStatuses(
  queue: RoundPatient[],
  statuses: Record<string, RoundStatus>,
): Record<string, RoundStatus> {
  const live = new Set(queue.map(p => p.id))
  const out: Record<string, RoundStatus> = {}
  for (const [id, s] of Object.entries(statuses)) {
    if (live.has(id)) out[id] = s
  }
  return out
}
