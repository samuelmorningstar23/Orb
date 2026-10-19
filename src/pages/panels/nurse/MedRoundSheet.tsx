import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import AdministerModal from '../../../components/vigil/AdministerModal'
import OfflineBanner from '../../../components/OfflineBanner'
import { getAdministrations, givenAcrossAllergy, type AdministrationRecord } from '../../../api/client'
import type { MedTask, NursePatientCard } from '../../../api/nurse'
import { allergyBlocked } from './allergyBlock'
import type { OfflineSnapshot } from '../../../hooks/useOfflineSnapshot'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { dueLabel, missedSlotsCount } from './time'
import { useFeatureFlags } from '../../../shellConfig'

/**
 * The drug round for one patient.
 *
 * This screen deliberately owns NO administration logic. Tapping a med opens
 * the existing components/vigil/AdministerModal, which runs the five-rights
 * summary and the type-the-drug-name barcode-style interlock, and posts to
 * /api/emar — where emar_service enforces active-order, pharmacist-verification
 * and mandatory-reason guards. Adding a second, "faster" administration path
 * for nurses would mean adding a path around those guards, which is the exact
 * thing a ward eMAR exists to prevent.
 *
 * What this screen adds is ORDER and CONTEXT: which doses are late, which are
 * coming, which are as-required, and the allergies visible while deciding.
 */

interface Props {
  patient: NursePatientCard
  /** Focus the round on one med (tapped straight from the task list). */
  focusMedicationId?: number | null
  /**
   * The shift board's snapshot, because THIS PATIENT'S DRUG CHART IS THAT BOARD.
   * The sheet is a full-screen portal on a tablet, so it covers the banner on
   * the panel underneath — and a nurse at the bedside deciding whether a dose is
   * late is looking at nothing else. "Last given 06:00" read as current, from a
   * copy saved before someone else gave the 14:00 dose, is how a drug is given
   * twice. Passed in rather than re-derived so there is exactly one answer to
   * how old this data is.
   */
  offline: OfflineSnapshot<unknown>
  onClose: () => void
  onAdministered: (record: AdministrationRecord) => void
}

interface Group {
  id: string
  title: string
  hint: string
  meds: MedTask[]
}

/* A stat order is one dose. Once it is charted it is not due, not late and not
   "later": it is done, and the round must not offer it again. It used to fall
   through to "Later this shift" with a live Administer button, and the server
   only WARNS on the repeat, so a second nurse could chart it twice. */
const isStatGiven = (m: MedTask) => m.schedule_kind === 'stat' && m.due_basis === 'stat_given'

/* medorders-11 (ruling MO-R1): the bedside will refuse this dose until a
   pharmacist has checked the order. The AI-extracted line already says so for
   its own orders, so this one is for every other linked order. */
const waitingForPharmacist = (m: MedTask) =>
  !m.unverified && m.pharmacist_check?.state === 'waiting'

/* A first stat or urgent dose the bedside will let go before the pharmacist's
   check, and mark as such. */
const firstDoseBeforeCheck = (m: MedTask) =>
  m.pharmacist_check?.state === 'first_dose_before_check'

/* medorders-10: every dose the bedside will refuse until a pharmacist checks
   the order, whichever rule says so (an AI-charted drug, or ruling MO-R1's
   check before the first dose). With the order store on these get their own
   group, so a nurse walking the round does not stop at each one. */
const awaitingPharmacist = (m: MedTask) => m.unverified || waitingForPharmacist(m)

/** The record of a dose the server marked as given before the pharmacist's
 *  check (medorders-11). The key is absent on every other record. */
type MarkedRecord = AdministrationRecord & { given_before_pharmacist_check?: boolean }
const givenBeforeCheck = (r: AdministrationRecord | undefined) =>
  (r as MarkedRecord | undefined)?.given_before_pharmacist_check === true

/** Where a set due time came from, in the round's words (medorders-19). */
function setTimeWords(m: MedTask): string | null {
  if (m.due_source === 'prescriber') return "time set by the prescriber"
  if (m.due_source === 'site_times') return "hospital's set time"
  return null
}

export default function MedRoundSheet({ patient, focusMedicationId, offline, onClose, onAdministered }: Props) {
  // Never straight into the modal for a stat dose already given: the row below
  // offers no Administer button, and a deep link must not be a way around that.
  const [administering, setAdministering] = useState<MedTask | null>(
    () => patient.medications.find(m => m.medication_id === focusMedicationId && !isStatGiven(m)
      && !waitingForPharmacist(m)) ?? null,
  )
  // Meds charted during this sheet's lifetime, so a nurse working down the list
  // sees what they have already given without a full board refresh.
  const [justGiven, setJustGiven] = useState<Record<number, AdministrationRecord>>({})
  // Who gave each stat dose already charted. The board carries the time but not
  // the name, and "given at 09:10 by S. Nair" is what lets a nurse check with a
  // colleague instead of guessing. Optional context: the row stands without it.
  const [statGivenBy, setStatGivenBy] = useState<Record<number, AdministrationRecord>>({})
  const hasStatGiven = patient.medications.some(isStatGiven)

  useEffect(() => {
    if (!hasStatGiven) return
    let cancelled = false
    getAdministrations(patient.patient_id)
      .then(rows => {
        if (cancelled) return
        const latest: Record<number, AdministrationRecord> = {}
        // Newest first from the server, so the first 'given' row per order wins.
        for (const r of rows) {
          if (r.status === 'given' && !(r.medication_id in latest)) latest[r.medication_id] = r
        }
        setStatGivenBy(latest)
      })
      .catch(() => { /* offline or refused: the row still shows when it was given */ })
    return () => { cancelled = true }
  }, [hasStatGiven, patient.patient_id])

  const close = useCallback(() => { if (!administering) onClose() }, [administering, onClose])
  const modalRef = useFocusTrap<HTMLDivElement>(close)
  /* The 'Awaiting pharmacist' group is drawn only while the server's order
     store is on (medorders-10); with it off the round is grouped as before. */
  const storeOn = useFeatureFlags().med_order_store === true

  const groups: Group[] = useMemo(() => {
    const overdue: MedTask[] = []
    const soon: MedTask[] = []
    const later: MedTask[] = []
    const pharmacist: MedTask[] = []
    const prn: MedTask[] = []
    const continuous: MedTask[] = []
    const given: MedTask[] = []
    for (const m of patient.medications) {
      if (isStatGiven(m)) given.push(m)
      else if (storeOn && awaitingPharmacist(m)) pharmacist.push(m)
      else if (m.schedule_kind === 'prn') prn.push(m)
      else if (m.schedule_kind === 'continuous') continuous.push(m)
      else if (m.overdue) overdue.push(m)
      else if (m.due_soon) soon.push(m)
      else later.push(m)
    }
    const byDue = (a: MedTask, b: MedTask) =>
      (a.minutes_until_due ?? 1e9) - (b.minutes_until_due ?? 1e9)
    // Set times (medorders-19) are not derived, so the hint stops saying so
    // once any late dose runs on them.
    const overdueHint = overdue.some(m => m.due_source)
      ? 'Past the due time by more than 15 minutes'
      : 'Past the derived due time by more than 15 minutes'
    return [
      { id: 'overdue', title: 'Overdue', hint: overdueHint, meds: overdue.sort(byDue) },
      { id: 'soon', title: 'Due now', hint: 'Due within the next 30 minutes', meds: soon.sort(byDue) },
      { id: 'later', title: 'Later this shift', hint: '', meds: later.sort(byDue) },
      { id: 'pharmacist', title: 'Awaiting pharmacist', hint: 'A pharmacist must check these orders before a dose can be given.', meds: pharmacist.sort(byDue) },
      { id: 'prn', title: 'As required (PRN)', hint: 'Available when indicated, never due, never late', meds: prn },
      // The hint used to say "do not chart" above a row that offers Administer.
      // What it meant is that these carry no due time, so it now says that.
      { id: 'continuous', title: 'Continuous', hint: 'Running infusions and feeds. No due time, so never shown as late.', meds: continuous },
      { id: 'given', title: 'Already given', hint: 'Stat doses already charted. Nothing more is due on these orders.', meds: given },
    ].filter(g => g.meds.length > 0)
  }, [patient.medications, storeOn])

  const severeAllergies = patient.allergies.filter(a => a.severity === 'severe')

  return createPortal(
    <div className="nurse-sheet-backdrop" onClick={close}>
      <div
        className="nurse-sheet nurse-sheet--wide glass-strong"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="med-round-title"
        onClick={e => e.stopPropagation()}
      >
        <header className="nurse-sheet__header">
          <div>
            <h2 className="nurse-sheet__title" id="med-round-title">Medication round</h2>
            <p className="nurse-sheet__subject">
              <strong>{patient.name}</strong>
              {patient.bed ? <> · Bed {patient.bed}</> : null}
              {patient.mrn ? <> · {patient.mrn}</> : null}
            </p>
          </div>
          <button type="button" className="nurse-sheet__close" onClick={close} aria-label="Close medication round">
            &times;
          </button>
        </header>

        <div className="nurse-sheet__body">
          {/* Above the allergies on purpose. Allergies say what this patient
              must not be given; this says whether anything on the screen can be
              believed at all, which has to be read first. */}
          <OfflineBanner snapshot={offline} what="drug chart" />

          {patient.allergies.length > 0 ? (
            <div
              className={`nurse-note ${severeAllergies.length ? 'nurse-note--danger' : 'nurse-note--warn'}`}
              role={severeAllergies.length ? 'alert' : 'note'}
            >
              <strong>Allergies:</strong>{' '}
              {patient.allergies
                .map(a => `${a.substance}${a.reaction ? ` (${a.reaction})` : ''}${a.severity === 'severe' ? ', severe' : ''}`)
                .join(' · ')}
            </div>
          ) : (
            <div className="nurse-note nurse-note--muted" role="note">
              No allergies recorded. Confirm with the patient.
            </div>
          )}

          {groups.length === 0 && (
            <p className="nurse-empty">No active medications charted for this patient.</p>
          )}

          {groups.map(group => (
            <section className="med-group" key={group.id}>
              <h3 className="med-group__title">
                {group.title}
                <span className="med-group__count">{group.meds.length}</span>
              </h3>
              {group.hint && <p className="med-group__hint">{group.hint}</p>}
              <ul className="med-list">
                {group.meds.map(med => {
                  const given = justGiven[med.medication_id]
                  const statDone = isStatGiven(med)
                  const statRecord = statGivenBy[med.medication_id]
                  // Not given is a recorded decision, not a completed dose, so it
                  // never takes the green rule a given dose earns.
                  const outcome = given
                    ? (given.status === 'given' ? 'med-row--done' : 'med-row--held')
                    : statDone ? 'med-row--done' : ''
                  return (
                    <li
                      className={`med-row ${med.overdue ? 'med-row--overdue' : ''} ${outcome}`}
                      key={med.medication_id}
                    >
                      <div className="med-row__main">
                        <span className="med-row__name">{med.drug_name}</span>
                        <span className="med-row__detail">
                          {[med.dosage, med.route, med.frequency].filter(Boolean).join(' · ')}
                        </span>
                        <span className="med-row__timing">
                          {statDone ? (
                            <>
                              Given {formatClinicalDateTime(med.last_given_at)}
                              {statRecord?.administered_by_name && <> by {statRecord.administered_by_name}</>}
                            </>
                          ) : (
                            <>
                              {dueLabel(med)}
                              {setTimeWords(med) && <> ({setTimeWords(med)})</>}
                              {med.last_given_at && <> · last given {formatClinicalDateTime(med.last_given_at)}</>}
                            </>
                          )}
                          {med.missed_slots > 0 && (
                            <> · <span className="med-row__missed">{missedSlotsCount(med.missed_slots)} earlier dose{med.missed_slots === 1 ? '' : 's'} not charted</span></>
                          )}
                        </span>
                        {med.unverified && (
                          <span className="med-row__blocked">
                            AI-extracted order: a pharmacist must verify it before it can be given.
                          </span>
                        )}
                        {/* medorders-11: the bedside's pharmacist check, said
                            before the walk to the bed. */}
                        {!statDone && !given && waitingForPharmacist(med) && (
                          <span className="med-row__blocked">
                            Waiting for a pharmacist to check this order. It cannot be given yet.
                          </span>
                        )}
                        {!statDone && !given && firstDoseBeforeCheck(med) && (
                          <span className="med-row__blocked">
                            {med.pharmacist_check?.priority === 'urgent' ? 'Urgent' : 'Stat'} order:
                            the first dose can be given before the pharmacist checks it. It will be
                            marked as given before the check, and the next dose waits for the check.
                          </span>
                        )}
                        {/* The board ran the bedside allergy screen already. A
                            dose it will refuse is said here, before the walk to
                            the bed, not first at the bedside. */}
                        {!statDone && !given && allergyBlocked(med) && (
                          <span className="med-row__blocked med-row__blocked--danger">
                            Allergy block: needs prescriber authorisation
                          </span>
                        )}
                      </div>
                      <div className="med-row__action">
                        {statDone && !given ? (
                          <>
                            {/* Status, not a control: there is no second dose to give. */}
                            <span className="med-row__result med-row__result--given">Given</span>
                            {statRecord && givenAcrossAllergy(statRecord) && (
                              <span
                                className="med-row__override"
                                title={statRecord.allergy_check?.override?.reason ?? undefined}
                              >
                                Across charted allergy
                              </span>
                            )}
                            {givenBeforeCheck(statRecord) && (
                              <span className="med-row__blocked">Given before pharmacist check</span>
                            )}
                          </>
                        ) : given ? (
                          <>
                            <span className={`med-row__result med-row__result--${given.status}`}>
                              {given.status === 'given' ? 'Given' : 'Not given'}
                            </span>
                            {/* NEVER SILENT. A dose given across a charted
                                allergy says so on the round sheet immediately,
                                for the nurse who takes over this list mid-round
                                as much as for the reviewer six months later. */}
                            {givenAcrossAllergy(given) && (
                              <span
                                className="med-row__override"
                                title={given.allergy_check?.override?.reason ?? undefined}
                              >
                                Across charted allergy
                              </span>
                            )}
                            {/* medorders-11: never silent either. A first
                                stat or urgent dose given before the
                                pharmacist's check says so on the round. */}
                            {givenBeforeCheck(given) && (
                              <span className="med-row__blocked">Given before pharmacist check</span>
                            )}
                          </>
                        ) : (
                          <button
                            type="button"
                            className="nurse-btn nurse-btn--secondary nurse-btn--compact"
                            onClick={() => setAdministering(med)}
                            disabled={med.unverified || waitingForPharmacist(med)}
                            title={med.unverified || waitingForPharmacist(med)
                              ? 'Blocked: awaiting pharmacist verification' : undefined}
                          >
                            Administer
                          </button>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            </section>
          ))}

          {patient.medications.some(m => m.due_source) ? (
            <p className="nurse-footnote">
              Due times marked as set come from the prescriber or the hospital's signed dose times.
              The others are derived from each order's frequency and the last charted dose. Treat them
              as a working list, and check the prescription.
            </p>
          ) : (
            <p className="nurse-footnote">
              Due times are derived from each order's frequency and the last charted dose. Orb holds no
              prescriber-authored administration schedule. Treat them as a working list, not a prescription.
            </p>
          )}
        </div>

        <footer className="nurse-sheet__actions">
          <button type="button" className="nurse-btn nurse-btn--ghost" onClick={close}>Done</button>
        </footer>
      </div>

      {administering && (
        <AdministerModal
          med={{
            id: administering.medication_id,
            drug_name: administering.drug_name,
            dosage: administering.dosage ?? '',
            route: administering.route ?? '',
            frequency: administering.frequency ?? undefined,
          }}
          patientName={patient.name}
          patientBed={patient.bed ?? undefined}
          /* The SAME list the red banner above is drawn from, handed to the
             modal so the allergy is repeated on the last screen before the
             drug goes in. The banner is on a screen the nurse has left by
             then. Always an array here — the shift board carries the list on
             every patient card — so the modal never renders its
             "list unavailable" state on this path. */
          allergies={patient.allergies}
          allergyPreview={administering.allergy ?? null}
          onClose={() => setAdministering(null)}
          onRecorded={record => {
            setJustGiven(prev => ({ ...prev, [administering.medication_id]: record }))
            setAdministering(null)
            onAdministered(record)
          }}
        />
      )}
    </div>,
    document.body,
  )
}
