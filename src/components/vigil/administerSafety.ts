import type { AdministrationRecord } from '../../api/client'
import { MEDICATION_PRESCRIBE } from '../../lib/roles'

/* The bedside rules behind AdministerModal: which charted allergy to lift
   to the top, who may clear a refusal, and what a charted dose came back
   with. Pure, and kept apart from AdministerModal.tsx so that file exports
   only components (fast refresh) and the tests can pin these directly. */

/* ─── Naming the allergy that belongs to THIS drug ───
   Suresh Reddy, GM-104, seen on the ward 2026-08-14: a charted SEVERE
   ceftriaxone anaphylaxis (urticaria, angio-oedema, hypotension, 2023) and an
   active Ceftriaxone 2g IV OD order 5h17m late on the due-now list. The
   medicine-round sheet carried a red allergy banner. THIS modal — the last
   screen before the drug goes in — did not repeat it.

   Two rules govern what follows, because this screen must never become a second
   opinion about a clinical gate:

     1. EVERY charted allergy is shown, always, whatever this drug is. That
        needs no matching at all and so cannot be got wrong.
     2. The matcher below only decides ORDER AND EMPHASIS — which allergy is
        lifted to the top and shouted. It is a deliberately NARROWER rule than
        the server's (name overlap only; the interlock also matches drug class
        and documented cross-reactivity, and grades severity from the reaction
        text). Narrower in this direction is safe: it can fail to shout, in
        which case the allergy is still on screen right below, but it can never
        quieten one the server would stop.

   The AUTHORITY is server-side and unchanged: services/allergy_interlock.py
   screens every 'given' dose against the live list inside the same transaction
   as the write and refuses a severe match with a 428. Nothing here weakens,
   duplicates or pre-empts that verdict — this only stops the nurse meeting it
   for the first time after she has already pressed Given. */
const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase()
const wordsOf = (s: string): string[] => s.match(/[a-z0-9][a-z0-9'-]*/g) ?? []

/** Mirrors drug_safety_service.names_overlap: equality → whole-token
 *  containment → substring, the last only when both sides are ≥4 characters
 *  (a 2–3 character allergen substring-matches an arbitrary slice of any
 *  formulary, which is the documented alert-fatigue hazard). */
export function allergenMatchesDrug(substance: string, drugName: string): boolean {
  const a = norm(substance)
  const b = norm(drugName)
  if (!a || !b) return false
  if (a === b) return true
  if (wordsOf(b).includes(a) || wordsOf(a).includes(b)) return true
  if (a.length >= 4 && b.length >= 4) return a.includes(b) || b.includes(a)
  return false
}

/** Severity as CHARTED, never re-graded here. 'unknown'/absent is displayed as
 *  unknown — the server treats an ungraded allergy as severe unless the
 *  reaction text reads as a plain intolerance, and inventing a grade on this
 *  screen would be exactly the second opinion it must not offer. */
export function severityLabel(severity?: string | null): string {
  const s = norm(severity || '')
  if (s === 'severe' || s === 'moderate' || s === 'mild') return s
  return 'severity not recorded'
}

/** The shape allergy_interlock.summarize() puts in a 428 `detail.allergy`. */
export interface ServerAllergyAlert {
  substance?: string
  reaction?: string | null
  match?: string
  decision?: string
  effective_severity?: string
  message?: string
}
export interface ServerAllergyScreen {
  decision?: string
  screen_failed?: boolean
  alerts?: ServerAllergyAlert[]
  /** The permission that clears this block, named by the SERVER. */
  override_requires_permission?: string | null
  /** Set when a prescriber already authorised this order across this allergy. */
  override?: { reason?: string } | null
  message?: string | null
}

/* ─── Who, if anyone, may clear THIS refusal ───
   Read off the server's own answer rather than decided here. The server sends
   `override_requires_permission` on every screen it summarises; this function
   turns it into an audience and never invents one.

   'prescriber' — a CHARTED allergy. Giving the drug anyway is a prescribing
     decision (`medication.prescribe`): a de-labelled allergy, a desensitisation
     step, a risk/benefit call in an emergency. The nurse at the bedside is the
     last independent check on that decision, so she is shown the block and who
     can authorise it, and is never shown a control.
   'anyone'     — the screen FAILED (the allergy list could not be read). Nothing
     charted is being overruled, only an outage, and refusing this to a nurse
     would let an infrastructure fault stop her giving adrenaline in a
     peri-arrest. The server takes the same view.
   'none'       — a requirement this build does not recognise. Offer nothing: an
     unknown gate is not an open one.

   FAIL CLOSED ON A MISSING FIELD: a charted-allergy block from a server that did
   not send the field is treated as prescriber-only, which is what it has always
   meant. The alternative default would hand the control to everyone the first
   time a client ran ahead of a server. */
export type OverrideAudience = 'prescriber' | 'anyone' | 'none'

export function overrideAudience(screen: ServerAllergyScreen | null): OverrideAudience {
  if (!screen) return 'none'
  if (screen.screen_failed) return 'anyone'
  const required = screen.override_requires_permission
  if (required === undefined || required === null || required === MEDICATION_PRESCRIBE) {
    return 'prescriber'
  }
  return 'none'
}

/* ─── The dose guard's verdict on a dose it LET THROUGH ───
   A WARN (a repeat inside the order's window, a dose the guard could not
   check) does not refuse the dose: the server charts it and answers 200 with
   the verdict on `administration.dose_check`. This modal used to close on that
   200 without reading it, so the one warning written for exactly this moment
   (a stat dose charted twice) reached nobody. The shape is
   drug_safety_service.summarize_dose_check. */

/** The warnings a charted dose came back with, in the server's words. */
export function doseWarnings(record: AdministrationRecord | null | undefined): string[] {
  const check = record?.dose_check
  if (!check || record?.status !== 'given') return []
  const out = (check.findings ?? [])
    .filter(f => f?.decision === 'warn' && f.message?.trim())
    .map(f => f.message!.trim())
  // An unchecked dose is said out loud too: silence would read as "checked, fine".
  if (check.unavailable) {
    out.push(check.unavailable_reason?.trim()
      ? `The dose check ${check.unavailable_reason.trim()}.`
      : 'This dose has not been fully checked.')
  }
  return out
}
