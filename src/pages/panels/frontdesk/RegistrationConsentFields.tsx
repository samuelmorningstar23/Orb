/**
 * The privacy notice and consent step of a new registration (opd-19).
 *
 * Offered only to an account that holds consent.record (the doctor today; the
 * receptionist once counsel agrees, ruling OPD-N3) and only when the server
 * says what the step may record (GET /registration/config, "consent"). The
 * server checks the permission again and refuses the step with 403 otherwise.
 *
 * The desk chooses one of two answers before the patient is registered:
 *   - record now: the notice version and language, both facts ticked (the
 *     patient was given the notice, and agrees to treatment), how consent was
 *     given, and by whom. One consent row is written with the registration.
 *   - not now: nothing is recorded; consent can be recorded later.
 * The language defaults to the patient's preferred language when the notice
 * may be given in it (English or an Eighth Schedule language, DPDP s.5(3)).
 * The retention line says registration details are part of the medical record.
 * Who gave consent must fit how it was given and the patient's age: a line
 * under the choice says what to change (a guardian method given by the
 * patient, or a patient under the adult age consenting for themselves).
 * The draft and its rules are ./registrationConsent.ts.
 */
import { useId } from 'react'

import type { RegistrationConsentOptions } from '../../../api/registration'
import { effectiveNoticeLanguage, givenByProblem, type ConsentDraft } from './registrationConsent'
import './RegistrationConsentFields.css'

const GIVEN_BY_LABEL: Record<string, string> = {
  patient: 'The patient',
  guardian: 'A guardian',
  proxy: 'Someone acting for them',
}

export default function RegistrationConsentFields({
  options,
  draft,
  preferredLanguage,
  minor,
  disabled,
  onChange,
}: {
  options: RegistrationConsentOptions
  draft: ConsentDraft
  preferredLanguage: string
  /** underAdultAge for the form's date of birth or stated age (null: none yet). */
  minor: boolean | null
  disabled?: boolean
  onChange: (next: ConsentDraft) => void
}) {
  const uid = useId()
  const set = (patch: Partial<ConsentDraft>) => onChange({ ...draft, ...patch })
  const language = effectiveNoticeLanguage(options, draft, preferredLanguage)
  const problem = givenByProblem(options, draft, minor)

  return (
    <fieldset className="rcf" aria-label="Privacy notice and consent">
      <legend className="rcf__legend">Privacy notice and consent</legend>
      <p className="rpv-hint">{options.retention_text}</p>
      <div className="rpv-choice">
        <label>
          <input type="radio" name={`${uid}-choice`} checked={draft.choice === 'now'}
            disabled={disabled} onChange={() => set({ choice: 'now' })} />
          Record the notice and consent now
        </label>
        <label>
          <input type="radio" name={`${uid}-choice`} checked={draft.choice === 'later'}
            disabled={disabled} onChange={() => set({ choice: 'later' })} />
          Not now
        </label>
      </div>
      {draft.choice === 'later' && (
        <p className="rpv-hint">Nothing about consent is recorded with this registration.</p>
      )}

      {draft.choice === 'now' && (
        <>
          <div className="rpv-field-row">
            <div className="rpv-field">
              <label htmlFor={`${uid}-version`}>Notice version (printed on the notice)</label>
              <input id={`${uid}-version`} type="text" value={draft.noticeVersion}
                maxLength={options.notice_version_max} autoComplete="off" disabled={disabled}
                onChange={e => set({ noticeVersion: e.target.value })} />
            </div>
            <div className="rpv-field">
              <label htmlFor={`${uid}-language`}>Language of the notice</label>
              <select id={`${uid}-language`} value={language} disabled={disabled}
                onChange={e => set({ noticeLanguage: e.target.value })}>
                <option value="">Choose…</option>
                {options.notice_languages.map(l => <option key={l} value={l}>{l}</option>)}
              </select>
            </div>
          </div>

          <label className="rcf-check">
            <input type="checkbox" checked={draft.noticeGiven} disabled={disabled}
              onChange={e => set({ noticeGiven: e.target.checked })} />
            <span>The patient was given the privacy notice{language ? ` in ${language}` : ''}.</span>
          </label>
          <label className="rcf-check">
            <input type="checkbox" checked={draft.treatmentConsent} disabled={disabled}
              onChange={e => set({ treatmentConsent: e.target.checked })} />
            <span>The patient agrees to: {options.purpose_label.toLowerCase()}.</span>
          </label>

          <div className="rpv-field-row">
            <div className="rpv-field">
              <label htmlFor={`${uid}-method`}>How consent was given</label>
              <select id={`${uid}-method`} value={draft.method} disabled={disabled}
                onChange={e => set({ method: e.target.value })}>
                <option value="">Choose…</option>
                {options.methods.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div className="rpv-field">
              <label htmlFor={`${uid}-evidence`}>Form or reference number (optional)</label>
              <input id={`${uid}-evidence`} type="text" value={draft.evidenceRef} maxLength={120}
                autoComplete="off" disabled={disabled}
                onChange={e => set({ evidenceRef: e.target.value })} />
            </div>
          </div>

          <fieldset className="rpv-field">
            <legend>Who gave consent</legend>
            <div className="rpv-choice">
              {options.given_by.map(g => (
                <label key={g}>
                  <input type="radio" name={`${uid}-given-by`} checked={draft.givenBy === g}
                    disabled={disabled} onChange={() => set({ givenBy: g })} />
                  {GIVEN_BY_LABEL[g] ?? g}
                </label>
              ))}
            </div>
            {problem && <p className="rpv-hint" role="status">{problem}</p>}
          </fieldset>
          {draft.givenBy !== 'patient' && (
            <div className="rpv-field-row">
              <div className="rpv-field">
                <label htmlFor={`${uid}-proxy-name`}>Their name</label>
                <input id={`${uid}-proxy-name`} type="text" value={draft.proxyName}
                  maxLength={200} autoComplete="off" disabled={disabled}
                  onChange={e => set({ proxyName: e.target.value })} />
              </div>
              <div className="rpv-field">
                <label htmlFor={`${uid}-proxy-relationship`}>Relationship to the patient</label>
                <input id={`${uid}-proxy-relationship`} type="text" value={draft.proxyRelationship}
                  maxLength={80} autoComplete="off" disabled={disabled}
                  onChange={e => set({ proxyRelationship: e.target.value })} />
              </div>
            </div>
          )}
        </>
      )}
    </fieldset>
  )
}
