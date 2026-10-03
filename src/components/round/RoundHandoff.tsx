/**
 * Ward Round — what did NOT get finished.
 *
 * The reason the queue tracks "skipped" separately from "not yet reached" is
 * this screen. At the end of a round the useful artefact is not "12 patients
 * seen", it is the short list of people who still need a doctor, and whether
 * anyone was deliberately deferred or simply never reached.
 *
 * Unsaved dictation is listed here too. A plan that was spoken but never saved
 * is invisible to everyone except the person holding the tablet, so it is the
 * one thing on this screen that must not be closed over silently.
 */
import type { RoundPatient, RoundProgress, RoundStatus } from './roundQueue'

interface Props {
  incomplete: { patient: RoundPatient; status: RoundStatus }[]
  unsavedDrafts: RoundPatient[]
  progress: RoundProgress
  onResume: (patientId: string) => void
  onClose: () => void
}

export default function RoundHandoff({
  incomplete, unsavedDrafts, progress, onResume, onClose,
}: Props) {
  return (
    <section className="round-handoff" aria-label="Round hand-on list">
      <header className="round-handoff__head">
        <h2 className="round-handoff__title">
          {progress.complete ? 'Round complete' : 'Round so far'}
        </h2>
        <p className="round-handoff__sub">
          {progress.seen} of {progress.total} seen
          {progress.skipped > 0 && ` · ${progress.skipped} skipped`}
          {progress.pending > 0 && ` · ${progress.pending} not reached`}
        </p>
      </header>

      {unsavedDrafts.length > 0 && (
        <div className="round-handoff__unsaved" role="alert">
          <h3 className="round-handoff__section-title">Dictated but not saved</h3>
          <p className="round-handoff__warn">
            These plans exist only on this tablet. They are not in the chart and
            nobody else can see them. Reloading this page will lose them.
          </p>
          <ul className="round-handoff__list">
            {unsavedDrafts.map(p => (
              <li key={p.id}>
                <button type="button" className="round-handoff__row" onClick={() => onResume(p.id)}>
                  <span className="round-handoff__bed">{p.bed || '—'}</span>
                  <span className="round-handoff__name">{p.name}</span>
                  <span className="round-handoff__tag round-handoff__tag--danger">Unsaved plan</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <h3 className="round-handoff__section-title">Still to be seen</h3>
      {incomplete.length === 0 ? (
        <p className="round-handoff__done">Every patient on this ward was seen.</p>
      ) : (
        <ul className="round-handoff__list">
          {incomplete.map(({ patient, status }) => (
            <li key={patient.id}>
              <button type="button" className="round-handoff__row" onClick={() => onResume(patient.id)}>
                <span className="round-handoff__bed">{patient.bed || '—'}</span>
                <span className="round-handoff__name">{patient.name}</span>
                <span className={`round-handoff__tag round-handoff__tag--${status === 'skipped' ? 'warn' : 'info'}`}>
                  {status === 'skipped' ? 'Skipped' : 'Not reached'}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="round-handoff__actions">
        <button type="button" className="round-btn round-btn--primary" onClick={onClose}>
          Back to the round
        </button>
      </div>
    </section>
  )
}
