import { useCallback, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  administerMedication,
  type AdministerPayload,
  type AdministrationRecord,
  type DoseCheckSummary,
} from '../../api/client'
import { assertOnlineForWrite } from '../../lib/offline/writeGuard'
import { hasPermission, MEDICATION_PRESCRIBE } from '../../lib/roles'
import {
  allergenMatchesDrug,
  doseWarnings,
  overrideAudience,
  severityLabel,
  type ServerAllergyScreen,
} from './administerSafety'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { newId } from '../../lib/id'
import './AdministerModal.css'

/**
 * eMAR bedside administration modal.
 *
 * Shows the five-rights summary (patient, drug, dose, route, time = now) and a
 * barcode-style type-to-confirm field: "Given" stays disabled until the user
 * types the drug name exactly (case-insensitive), so the physical med in hand
 * is checked against the chart row before a dose is charted. "Not given"
 * requires a structured reason (Refused / Held / NPO / Unavailable / Other).
 * Guard failures from the backend (inactive order, unverified AI med) surface
 * as an inline 422 message.
 *
 * THE ALLERGY OVERRIDE (2026-08-15) is the second half of the interlock, and it
 * only ever appears AFTER the server has refused the dose — never before, and
 * never to someone who cannot use it. See the block above `overrideAudience`
 * in administerSafety.ts.
 */
interface AdministerMed {
  id: number
  drug_name: string
  dosage: string
  route: string
  frequency?: string
}

/** One charted allergy, exactly as `patient_allergies` holds it. */
export interface AdministerAllergy {
  substance: string
  reaction?: string | null
  severity?: string | null
}

interface Props {
  med: AdministerMed
  patientName: string
  patientBed?: string
  /**
   * THE PATIENT'S CHARTED ALLERGIES, REPEATED AT THE MOMENT OF ADMINISTRATION.
   *
   * `null` means NOT KNOWN — the list could not be read, or the caller has not
   * got one. It does NOT mean "no allergies", and the modal renders those two
   * states as opposite things. A caller that has no allergy list must pass
   * `null` rather than `[]`.
   */
  allergies: AdministerAllergy[] | null
  /**
   * The bedside allergy screen as the shift board already ran it for THIS
   * dose (allergy_interlock.summarize). A 'block' with no override opens the
   * modal in the danger state, because the server will refuse the dose: the
   * name-overlap rule below cannot see a class or cross-reactivity match, and
   * without this the screen reassured in amber about a dose Orb would stop.
   * Advisory only. The server screens again when Given is pressed.
   */
  allergyPreview?: ServerAllergyScreen | null
  onClose: () => void
  onRecorded: (record: AdministrationRecord) => void
}

const NOT_GIVEN_REASONS = ['Refused', 'Held (clinical decision)', 'NPO', 'Unavailable', 'Other']

/** How the drug matched the charted allergy, in words. The screen printed the
 *  server's key, so a nurse read "CROSS_REACTIVITY MATCH". */
const MATCH_LABEL: Record<string, string> = {
  direct: 'direct match',
  class: 'drug class match',
  cross_reactivity: 'cross-reactivity match',
}

function matchLabel(match: string): string {
  return MATCH_LABEL[match] ?? `${match.replace(/_/g, ' ')} match`
}

/** Pull the interlock's payload off a rejected administration, if it is one.
 *  api/client puts the structured 428 body on `error.detail`. It rides on the
 *  403 refusal too (`allergy_override_not_permitted`), so a caller who may not
 *  clear the block still learns what the block IS. */
function allergyScreenFrom(e: unknown): ServerAllergyScreen | null {
  const detail = (e as { detail?: unknown } | null)?.detail
  if (!detail || typeof detail !== 'object') return null
  const screen = (detail as { allergy?: unknown }).allergy
  if (!screen || typeof screen !== 'object') return null
  return screen as ServerAllergyScreen
}

/** The dose guard's refusal (428 `dose_acknowledgement_required`, body
 *  `detail.dose`), if this rejection is one. It used to fall through to a
 *  generic error line with the findings thrown away and Given still armed. */
function doseScreenFrom(e: unknown): DoseCheckSummary | null {
  const detail = (e as { detail?: unknown } | null)?.detail
  if (!detail || typeof detail !== 'object') return null
  const dose = (detail as { dose?: unknown }).dose
  if (!dose || typeof dose !== 'object') return null
  return dose as DoseCheckSummary
}

/** What the dose guard found, in its own words, refusals first. */
function doseFindingLines(check: DoseCheckSummary | null): string[] {
  if (!check) return []
  const findings = check.findings ?? []
  const lines = [
    ...findings.filter(f => f?.decision === 'block'),
    ...findings.filter(f => f?.decision !== 'block'),
  ].map(f => f.message?.trim() ?? '').filter(Boolean)
  if (lines.length === 0 && check.message?.trim()) lines.push(check.message.trim())
  return lines
}

/** One charted allergy, rendered. Allergen, severity and reaction AS CHARTED —
 *  the three things a nurse needs to recognise the hazard she is holding. */
function AllergyRow({ allergy }: { allergy: AdministerAllergy }) {
  return (
    <li className="administer-modal__allergy-row">
      <span className="administer-modal__allergen">{allergy.substance}</span>
      <span className="administer-modal__allergy-severity">{severityLabel(allergy.severity)}</span>
      <span className="administer-modal__allergy-reaction">
        {allergy.reaction?.trim()
          ? `Reaction as charted: ${allergy.reaction.trim()}`
          : 'No reaction recorded'}
      </span>
    </li>
  )
}

export default function AdministerModal({
  med, patientName, patientBed, allergies, allergyPreview, onClose, onRecorded,
}: Props) {
  const [confirmText, setConfirmText] = useState('')
  const [showNotGiven, setShowNotGiven] = useState(false)
  const [reason, setReason] = useState('')
  const [reasonDetail, setReasonDetail] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  /* The interlock's OWN verdict, lifted off a 428 and shown verbatim. This is
     the "surface what it already knows" half: whatever the client-side
     emphasis rule above did or did not spot, once the server has spoken it is
     the server's alerts, severities and reasoning on the screen. */
  const [serverScreen, setServerScreen] = useState<ServerAllergyScreen | null>(null)
  /* The written clinical justification, once the server has refused the dose
     and this user is one of the people who can authorise it.

     NOT cleared when a LATER request fails. The dose guard can refuse the same
     dose after the allergy is cleared, and wiping a paragraph a prescriber has
     just written — because a second, different gate fired — is how a clinician
     learns to type the shortest thing that will pass. */
  const [overrideReason, setOverrideReason] = useState('')
  /* Why the REASON was refused — as distinct from why the DOSE was.
     Found in a browser check, 2026-08-15: the server refuses a junk reason with
     a precise sentence ("an allergy override requires a clinical reason…"), and
     the block below suppresses `error` whenever a serverScreen exists, which is
     always true by the time the override panel is on screen. So a prescriber
     who typed "per doctor" saw the confirm field silently clear and nothing
     else — the one refusal in this flow whose remedy is entirely in the user's
     hands was the one the UI would not state. Kept separate from `error` so the
     suppression rule (don't reprint the interlock verdict) still holds. */
  const [overrideError, setOverrideError] = useState('')
  /* A dose that is ALREADY CHARTED but came back with dose-guard warnings. Held
     here so the nurse reads them before the modal goes; every way out of the
     modal from this state reports the record, because the dose is on the chart
     whether or not she presses the button. */
  const [recordedWithWarnings, setRecordedWithWarnings] = useState<AdministrationRecord | null>(null)
  /* The dose guard's refusal, kept like the allergy verdict: the hazard stays
     true whatever fails next. No override box is offered from this screen
     (whether anyone at the bedside may type one is ruling R011), so the next
     step is the prescriber or Not given, and Given stays off. */
  const [doseRefusal, setDoseRefusal] = useState<DoseCheckSummary | null>(null)

  const close = useCallback(() => {
    if (recordedWithWarnings) onRecorded(recordedWithWarnings)
    else if (!busy) onClose()
  }, [busy, onClose, onRecorded, recordedWithWarnings])
  const modalRef = useFocusTrap<HTMLDivElement>(close)

  const confirmed = confirmText.trim().toLowerCase() === med.drug_name.trim().toLowerCase()
  const notGivenValid = reason !== '' && (reason !== 'Other' || reasonDetail.trim().length > 0)

  /* ── The override, and who is being offered it ──
     `audience` comes from the server's refusal; the permission check is the
     client mirror of server/permissions.py. Hiding the control is NOT the
     control — the server refuses a nurse's override with a 403 whatever this
     renders — but offering a control that is always refused teaches a clinician
     at 03:00 that the software is broken, and that is its own hazard. */
  const audience = overrideAudience(serverScreen)
  const mayOverride =
    audience === 'anyone' || (audience === 'prescriber' && hasPermission(MEDICATION_PRESCRIBE))
  const overrideOffered = serverScreen !== null && mayOverride
  /* Mirrors allergy_interlock.MIN_OVERRIDE_REASON_CHARS. A PRE-CHECK, not a
     rule: the server owns the floor and the junk-reason list, and it is the one
     that will refuse "ok". This only stops the button offering to send
     something already known to fail. */
  const OVERRIDE_MIN_CHARS = 10
  const overrideReasonReady = overrideReason.trim().length >= OVERRIDE_MIN_CHARS
  /* Both, always: the written reason AND the drug name typed again. The second
     one is the ceremony this screen already uses to chart any dose, reused
     rather than replaced — an override is not a lighter act than an ordinary
     administration, it is a heavier one. */
  const overrideReady = !overrideOffered || (overrideReasonReady && confirmed)

  // `null` = the list is NOT KNOWN. `[]` = a list was read and it was empty.
  // Two different sentences on screen, because they are opposite findings.
  const charted = Array.isArray(allergies)
    ? allergies.filter(a => (a?.substance || '').trim().length > 0)
    : null
  const matched = charted?.filter(a => allergenMatchesDrug(a.substance, med.drug_name)) ?? []
  const otherAllergies = charted?.filter(a => !allergenMatchesDrug(a.substance, med.drug_name)) ?? []
  // The board's screen says the server will refuse this dose. Shown before the
  // press, in the danger state, whatever the name-overlap rule above found.
  const previewBlocks = !!allergyPreview
    && allergyPreview.decision === 'block'
    && !allergyPreview.screen_failed
    && !allergyPreview.override
  const previewAlerts = previewBlocks ? (allergyPreview?.alerts ?? []) : []

  /* ONE DOSE, ONE KEY.
   *
   * The `busy` flag plus the button's `disabled` attribute do stop a double-tap
   * (React flushes a click's state update before the next click is dispatched).
   * What they do NOT stop is the retry: a Wi-Fi drop at the bedside — the exact
   * scenario this endpoint's offline support exists for — can lose the RESPONSE
   * to a request the server already applied. The nurse sees "Failed to record
   * administration", taps Given again, and a freshly minted Idempotency-Key
   * made that a second charted dose.
   *
   * So the key is minted once per (modal, intended action) and reused by every
   * retry, which is what makes the server's dedupe reachable. Given and
   * Not-given are different mutations, so they get different keys.
   */
  const keysRef = useRef<Partial<Record<'given' | 'not_given', string>>>({})
  const keyFor = (status: 'given' | 'not_given') =>
    (keysRef.current[status] ??= newId())

  const submit = async (status: 'given' | 'not_given') => {
    setBusy(true)
    setError('')
    setOverrideError('')
    // Whether THIS attempt carried a written authorisation decides how its
    // failure is read: with a reason on board, a refusal is about the reason.
    const carriedOverride = status === 'given' && overrideOffered && overrideReason.trim().length > 0
    try {
      // REFUSE BEFORE THE QUEUE IS REACHABLE.
      //
      // api/client.administerMedication routes through offlineMutate: with no
      // connection it queues the request and returns an optimistic
      // `status: 'queued_offline'` record, which the round sheet renders as
      // "Given". On a drug round that is the worst available outcome — the
      // nurse sees her own confirmation, moves on, and whether the dose is ever
      // charted depends on a replay that can fail permanently (the order goes
      // inactive, the tablet is reset) long after she has stopped watching.
      // Better she is told plainly that nothing was recorded.
      assertOnlineForWrite('chart this dose')
      const payload: AdministerPayload =
        status === 'given'
          ? {
              status,
              dose_given: med.dosage,
              route_given: med.route,
              confirmation_method: 'typed',
              // Sent ONLY when the server has already refused this dose and
              // this user may clear it. On a first press the field is absent,
              // so the interlock is met head-on rather than pre-emptively
              // waved past — a client that always sent a reason would silently
              // turn the block into the default path.
              ...(overrideOffered && overrideReason.trim()
                ? { allergy_override_reason: overrideReason.trim() }
                : {}),
            }
          : {
              status,
              not_given_reason: reason === 'Other' ? reasonDetail.trim() : reason,
              confirmation_method: 'typed',
              notes: reason !== 'Other' && reasonDetail.trim() ? reasonDetail.trim() : undefined,
            }
      const res = await administerMedication(med.id, payload, keyFor(status))
      if (doseWarnings(res.administration).length > 0) {
        setRecordedWithWarnings(res.administration)
        setBusy(false)
        return
      }
      onRecorded(res.administration)
    } catch (e) {
      const refusal = allergyScreenFrom(e)
      /* Kept, not replaced by null, when a LATER failure carries no allergy
         payload: the interlock's verdict is still the truth about this dose,
         and blanking it would hide the hazard behind whatever failed second. */
      if (refusal) setServerScreen(refusal)
      /* THE CONFIRMATION IS SPENT. The drug name was typed to confirm "give
         this dose"; what is on offer now is a different and heavier act — give
         it ACROSS a charted allergy — so the barcode-style check is re-armed
         and has to be satisfied again for the new decision. Cheap to redo,
         impossible to do by momentum. */
      if (refusal) setConfirmText('')
      // The dose guard's refusal: same spent confirmation, findings on screen.
      const doseRefused = doseScreenFrom(e)
      if (doseRefused) {
        setDoseRefusal(doseRefused)
        setConfirmText('')
      }
      const message = e instanceof Error ? e.message : 'Failed to record administration'
      if (doseRefused) {
        // Stated in full by the dose panel. It is not a refusal of the written
        // allergy reason either, so the override panel stays quiet about it.
        setError('')
      } else {
        if (carriedOverride) setOverrideError(message)
        setError(message)
      }
      setBusy(false)
    }
  }

  return createPortal(
    <div className="administer-modal-backdrop" onClick={close}>
      <div
        className="administer-modal glass-strong"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="administer-modal-title"
        onClick={e => e.stopPropagation()}
      >
        <div className="administer-modal__header">
          <h2 className="administer-modal__title" id="administer-modal-title">
            {recordedWithWarnings ? 'Dose recorded, with a warning' : 'Administer medication'}
          </h2>
          <button type="button" className="administer-modal__close" onClick={close} aria-label="Close" disabled={busy}>
            &times;
          </button>
        </div>

        {recordedWithWarnings ? (
          <div className="administer-modal__body">
            {/* The dose IS charted. This is not a gate and offers no undo: it
                makes sure the warning is read before the round moves on. */}
            <section className="administer-modal__allergy administer-modal__allergy--warn" role="alert">
              <h3 className="administer-modal__allergy-title">
                {med.drug_name} is now on the chart. Orb flagged this dose:
              </h3>
              <ul className="administer-modal__allergy-list">
                {doseWarnings(recordedWithWarnings).map((w, i) => (
                  <li className="administer-modal__allergy-row" key={`w${i}`}>
                    <span className="administer-modal__allergy-reaction">{w}</span>
                  </li>
                ))}
              </ul>
              <p className="administer-modal__allergy-note">
                If this may be a second dose, tell the prescriber now. The warning is kept with this
                dose on the record.
              </p>
            </section>
            <div className="administer-modal__actions">
              <button type="button" className="administer-btn administer-btn--primary" onClick={close}>
                I have read this
              </button>
            </div>
          </div>
        ) : (
          <div className="administer-modal__body">
            {/* ── THE ALLERGY, REPEATED HERE, ABOVE EVERYTHING ──
                   First in the body and first in the DOM. The round sheet's red
                   banner is on a screen the nurse has already left; this is the
                   last one before the drug goes in. */}
            {charted === null ? (
              <section className="administer-modal__allergy administer-modal__allergy--unknown" role="alert">
                <h3 className="administer-modal__allergy-title">Allergy list unavailable</h3>
                <p className="administer-modal__allergy-note">
                  This screen could not read {patientName}'s allergies. That is <strong>not</strong> the
                  same as “no known allergies”. Check the chart before giving {med.drug_name}.
                </p>
              </section>
            ) : previewBlocks ? (
              <section className="administer-modal__allergy administer-modal__allergy--danger" role="alert">
                <h3 className="administer-modal__allergy-title">
                  Allergy block: needs prescriber authorisation
                </h3>
                {previewAlerts.length > 0 && (
                  <ul className="administer-modal__allergy-list">
                    {previewAlerts.map((a, i) => (
                      <li className="administer-modal__allergy-row" key={`p${i}`}>
                        <span className="administer-modal__allergen">{a.substance}</span>
                        <span className="administer-modal__allergy-severity">
                          {a.effective_severity || 'severity not recorded'}
                          {a.match ? ` · ${matchLabel(a.match)}` : ''}
                        </span>
                        <span className="administer-modal__allergy-reaction">
                          {a.message || (a.reaction ? `Reaction as charted: ${a.reaction}` : '')}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                {/* Stated BEFORE the confirmation field, not after the press. */}
                <p className="administer-modal__allergy-note">
                  {hasPermission(MEDICATION_PRESCRIBE)
                    ? `Orb will refuse ${med.drug_name} when Given is pressed. You will then be asked for the clinical reason to give it across the allergy.`
                    : `Orb will refuse ${med.drug_name} when Given is pressed. Only a prescriber can authorise it, in Orb, with a written reason. Contact the prescriber, or record it as Not given.`}
                </p>
                {charted.length > 0 && (
                  <>
                    <h4 className="administer-modal__allergy-subtitle">Charted allergies</h4>
                    <ul className="administer-modal__allergy-list">
                      {charted.map((a, i) => <AllergyRow key={`c${i}`} allergy={a} />)}
                    </ul>
                  </>
                )}
              </section>
            ) : matched.length > 0 ? (
              <section className="administer-modal__allergy administer-modal__allergy--danger" role="alert">
                <h3 className="administer-modal__allergy-title">
                  Charted allergy to {med.drug_name}
                </h3>
                <ul className="administer-modal__allergy-list">
                  {matched.map((a, i) => <AllergyRow key={`m${i}`} allergy={a} />)}
                </ul>
                {/* Stated BEFORE the confirmation field, not after the press. */}
                <p className="administer-modal__allergy-note">
                  Orb screens this dose against the live allergy list at the moment Given is pressed.
                  A severe match is refused and needs a prescriber's recorded authorisation, which
                  this screen cannot give.
                </p>
                {otherAllergies.length > 0 && (
                  <>
                    <h4 className="administer-modal__allergy-subtitle">Also charted</h4>
                    <ul className="administer-modal__allergy-list">
                      {otherAllergies.map((a, i) => <AllergyRow key={`o${i}`} allergy={a} />)}
                    </ul>
                  </>
                )}
              </section>
            ) : charted.length > 0 ? (
              <section className="administer-modal__allergy administer-modal__allergy--warn" role="note">
                <h3 className="administer-modal__allergy-title">Charted allergies</h3>
                <ul className="administer-modal__allergy-list">
                  {charted.map((a, i) => <AllergyRow key={`c${i}`} allergy={a} />)}
                </ul>
                <p className="administer-modal__allergy-note">
                  No charted allergen is named “{med.drug_name}”. Orb still screens this dose against
                  the whole list (including drug class and cross-reactivity) when Given is pressed.
                </p>
              </section>
            ) : (
              <section className="administer-modal__allergy administer-modal__allergy--none" role="note">
                {/* Never "NKDA": that is a clinician's assertion, and an empty
                    list is also what a patient nobody asked looks like. */}
                <h3 className="administer-modal__allergy-title">No allergies recorded</h3>
                <p className="administer-modal__allergy-note">
                  Confirm with the patient. An empty list can mean the patient was never asked.
                </p>
              </section>
            )}

            {/* The interlock's own verdict, once it has spoken. */}
            {serverScreen && (
              <section className="administer-modal__allergy administer-modal__allergy--danger" role="alert">
                <h3 className="administer-modal__allergy-title">
                  {serverScreen.screen_failed
                    ? 'This dose was NOT screened'
                    : 'Orb refused this dose'}
                </h3>
                {serverScreen.message && (
                  <p className="administer-modal__allergy-note">{serverScreen.message}</p>
                )}
                {(serverScreen.alerts?.length ?? 0) > 0 && (
                  <ul className="administer-modal__allergy-list">
                    {serverScreen.alerts!.map((a, i) => (
                      <li className="administer-modal__allergy-row" key={`s${i}`}>
                        <span className="administer-modal__allergen">{a.substance}</span>
                        <span className="administer-modal__allergy-severity">
                          {a.effective_severity || 'severity not recorded'}
                          {a.match ? ` · ${matchLabel(a.match)}` : ''}
                        </span>
                        <span className="administer-modal__allergy-reaction">
                          {a.message || (a.reaction ? `Reaction as charted: ${a.reaction}` : '')}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {/* ── WHO CAN CLEAR THIS ──
                       Said plainly to the person who cannot, instead of leaving
                       her at a dead end with a red box. The nurse's next action is
                       a real one — bleep the prescriber, chart it as not given —
                       and naming it is what stops the workaround (giving the drug
                       on a verbal instruction and charting it as something else).
                       She keeps "Not given", which is one press away below. */}
                {!mayOverride && (
                  <p className="administer-modal__allergy-note">
                    {audience === 'none'
                      ? 'This dose cannot be authorised from this screen. Contact the prescriber.'
                      : <>
                          <strong>Only the prescriber can authorise this dose.</strong> You are the last
                          check on that decision, so Orb will not take the authorisation from you,
                          and it will not accept one typed here on someone else's behalf. Ask the
                          prescriber to record the reason in Orb themselves. If the dose is not given
                          now, record it as <strong>Not given</strong> so the omission is on the chart.
                        </>}
                  </p>
                )}
              </section>
            )}

            {/* The dose guard's verdict, once it has spoken: its findings in its
                own words, and the next step named, never a dead end. */}
            {doseRefusal && (
              <section className="administer-modal__allergy administer-modal__allergy--danger" role="alert">
                <h3 className="administer-modal__allergy-title">The dose check stopped this dose</h3>
                {doseFindingLines(doseRefusal).length > 0 && (
                  <ul className="administer-modal__allergy-list">
                    {doseFindingLines(doseRefusal).map((line, i) => (
                      <li className="administer-modal__allergy-row" key={`d${i}`}>
                        <span className="administer-modal__allergy-reaction">{line}</span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="administer-modal__allergy-note">
                  <strong>Nothing was recorded.</strong> Contact the prescriber, or record it
                  as <strong>Not given</strong> so the omission is on the chart.
                </p>
              </section>
            )}

            {/* ── THE OVERRIDE ──
                   A separate, heavier panel below the refusal, and reachable only
                   after the server has spoken. It restates the hazard AT THE
                   MOMENT OF OVERRIDING rather than relying on the reader
                   remembering the box above: allergen, severity as charted, and
                   the charted reaction, one row each. The reason is asked for as
                   what it is — a clinical justification a colleague may read in an
                   incident review — not as a formality to be cleared. */}
            {overrideOffered && (
              <section className="administer-modal__override" role="group" aria-labelledby="administer-override-title">
                <h3 className="administer-modal__override-title" id="administer-override-title">
                  {serverScreen?.screen_failed
                    ? 'Give without a completed allergy screen'
                    : `Authorise ${med.drug_name} despite the charted allergy`}
                </h3>

                {serverScreen?.screen_failed ? (
                  <p className="administer-modal__override-note">
                    This dose has <strong>not</strong> been screened against {patientName}'s
                    allergies. The list could not be read. Proceeding is a judgement about an
                    unknown, not about a known-safe drug.
                  </p>
                ) : (
                  <>
                    <p className="administer-modal__override-note">
                      You are about to give <strong>{med.drug_name}</strong> to a patient with this
                      charted allergy:
                    </p>
                    <ul className="administer-modal__allergy-list">
                      {(serverScreen?.alerts ?? []).map((a, i) => (
                        <li className="administer-modal__allergy-row" key={`ov${i}`}>
                          <span className="administer-modal__allergen">{a.substance}</span>
                          <span className="administer-modal__allergy-severity">
                            {a.effective_severity || 'severity not recorded'} as charted
                          </span>
                          <span className="administer-modal__allergy-reaction">
                            {a.reaction ? `Reaction as charted: ${a.reaction}` : 'No reaction recorded'}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}

                <label className="administer-modal__label" htmlFor="administer-override-reason">
                  Why is it safe to give this drug to this patient?
                </label>
                <textarea
                  id="administer-override-reason"
                  className="administer-modal__override-reason"
                  rows={3}
                  value={overrideReason}
                  onChange={e => setOverrideReason(e.target.value)}
                  placeholder="e.g. Allergy de-labelled by immunology 12/08 after negative skin test and graded oral challenge; giving under 1:1 observation."
                  disabled={busy}
                />
                {overrideError && (
                  <div className="administer-modal__error" role="alert">
                    {overrideError}
                  </div>
                )}
                <p className="administer-modal__hint">
                  Recorded against your name, this order and this allergy, and kept on the patient's
                  record. Write it for the colleague who reads it in a review, not for this box,
                  “per doctor”, “ok” and anything under {OVERRIDE_MIN_CHARS} characters are refused.
                </p>
              </section>
            )}

            {/* Non-allergy failures only — an interlock refusal is already stated
                in full above, and printing its sentence twice would train a nurse
                to skim the region that matters. */}
            {error && !serverScreen && (
              <div className="administer-modal__error" role="alert">
                {error}
              </div>
            )}

            {/* Five rights — verify each line against the med in hand. */}
            <dl className="administer-modal__rights">
              <div className="administer-modal__right">
                <dt>Patient</dt>
                <dd>
                  {patientName}
                  {patientBed ? ` · Bed ${patientBed}` : ''}
                </dd>
              </div>
              <div className="administer-modal__right">
                <dt>Drug</dt>
                <dd className="administer-modal__drug-name">{med.drug_name}</dd>
              </div>
              <div className="administer-modal__right">
                <dt>Dose</dt>
                <dd>{med.dosage || '—'}</dd>
              </div>
              <div className="administer-modal__right">
                <dt>Route</dt>
                <dd>{med.route || '—'}</dd>
              </div>
              <div className="administer-modal__right">
                <dt>Time</dt>
                <dd>{new Date().toLocaleString([], { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' })} (now)</dd>
              </div>
            </dl>

            <label className="administer-modal__label" htmlFor="administer-confirm">
              {overrideOffered ? 'Re-confirm drug name to authorise' : 'Confirm drug name'}
            </label>
            <input
              id="administer-confirm"
              className={`administer-modal__confirm ${confirmed ? 'administer-modal__confirm--ok' : ''}`}
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder={`Type "${med.drug_name}" to confirm`}
              value={confirmText}
              onChange={e => setConfirmText(e.target.value)}
              disabled={busy}
              autoFocus
            />
            <p className="administer-modal__hint">
              {overrideOffered
                ? `Your earlier confirmation was for an ordinary dose. Type “${med.drug_name}” again to confirm you are giving it across the charted allergy.`
                : 'Name check: type the drug name exactly as charted (capital letters do not matter) to enable “Given”.'}
            </p>

            {showNotGiven && (
              <div className="administer-modal__not-given">
                <label className="administer-modal__label" htmlFor="administer-reason">
                  Reason not given
                </label>
                <select
                  id="administer-reason"
                  className="administer-modal__select"
                  value={reason}
                  onChange={e => setReason(e.target.value)}
                  disabled={busy}
                >
                  <option value="">Select a reason…</option>
                  {NOT_GIVEN_REASONS.map(r => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <input
                  className="administer-modal__detail"
                  type="text"
                  placeholder={reason === 'Other' ? 'Describe the reason (required)' : 'Additional detail (optional)'}
                  value={reasonDetail}
                  onChange={e => setReasonDetail(e.target.value)}
                  disabled={busy}
                />
              </div>
            )}

            <div className="administer-modal__actions">
              <button type="button" className="administer-btn administer-btn--ghost" onClick={close} disabled={busy}>
                Cancel
              </button>
              {showNotGiven ? (
                <button
                  type="button"
                  className="administer-btn administer-btn--danger"
                  onClick={() => submit('not_given')}
                  disabled={busy || !notGivenValid}
                >
                  {busy ? 'Recording…' : 'Record not given'}
                </button>
              ) : (
                <button
                  type="button"
                  className="administer-btn administer-btn--secondary"
                  onClick={() => setShowNotGiven(true)}
                  disabled={busy}
                >
                  Not given
                </button>
              )}
              {/* The SAME button, relabelled and repainted, rather than a second
                  "override" button beside it. Two buttons would let a press land
                  on the wrong one, and would make the override look like an
                  alternative route to the same outcome. It is not: it is the same
                  act, now carrying a written justification. */}
              <button
                type="button"
                className={`administer-btn ${overrideOffered ? 'administer-btn--danger' : 'administer-btn--primary'}`}
                onClick={() => submit('given')}
                disabled={busy || !confirmed || !overrideReady || doseRefusal !== null}
                title={
                  doseRefusal
                    ? 'Orb refused this dose. Contact the prescriber, or record it as Not given.'
                    : confirmed && overrideReady
                    ? undefined
                    : overrideOffered
                      ? 'Write the clinical reason and re-type the drug name to authorise this dose'
                      : 'Type the drug name to confirm before recording'
                }
              >
                {busy
                  ? 'Recording…'
                  : overrideOffered
                    ? `Give ${med.drug_name} despite the allergy`
                    : 'Given'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
