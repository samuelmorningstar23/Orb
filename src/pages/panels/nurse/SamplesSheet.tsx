/**
 * Samples: specimen collection at the bedside (lis-8).
 *
 * Two homes, one component:
 *   * 'ward', the nurse's Samples view: her own department's samples by bed
 *     (the server decides the department from the signed-in account);
 *   * 'laboratory', the phlebotomy list in the Laboratory panel: every ward,
 *     grouped by ward, for the phlebotomist and the laboratory.
 *
 * THE BEDSIDE STEPS, in the order the ward does them:
 *   1. Check identity. Type the UHID from the wristband and the name as the
 *      patient says it. Nothing prints until both match the record (ruling
 *      LIS-R6, strictest option: confirm on screen before labels print, at
 *      the point of collection, one patient at a time). A mismatch says which
 *      identifier did not match and never shows the stored value.
 *   2. Print labels, one per tube, on the ward's label printer. If the labels
 *      are already on the tubes (printed earlier), say so instead of
 *      reprinting; the identity is still checked when the sample is recorded.
 *   3. Draw the sample and mark each tube collected. The collection time is
 *      now: back-dating is switched off until the ruling is confirmed.
 *
 * A REJECTED SAMPLE (lis-9) shows as 'Recollection needed' with the
 * laboratory's reason until the laboratory decides. If it recollects, a NEW
 * tube appears here as an ordinary sample to collect, marked as the
 * recollection of the rejected one; if it cancels the tests, the row goes.
 *
 * A failed load never renders as an empty list, and an empty list names the
 * scope it covers. No polling: each read is audited, so the list refreshes
 * when it opens, after each step and when asked.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'

import { toLabError } from '../../../api/lab'
import {
  collectSpecimen,
  formatAccession,
  getCollectionList,
  identityChecks,
  printSpecimenLabels,
  priorityText,
  type CollectionIdentity,
  type CollectionList,
  type CollectionPatient,
  type CollectionSpecimen,
  type IdentityCheck,
  type SpecimenLabel,
} from '../../../api/lab/specimens'
import SpecimenLabelPrintView from '../../../components/print/SpecimenLabelPrintView'
import { formatClinicalTime } from '../../../lib/datetime'
import './SamplesSheet.css'

export interface SamplesSheetProps {
  /** 'ward' (default): by bed. 'laboratory': every ward, grouped by ward. */
  variant?: 'ward' | 'laboratory'
}

function rejectionWords(specimen: CollectionSpecimen): string {
  return [specimen.rejection_display, specimen.rejection_note].filter(Boolean).join(': ')
}

interface PatientGroup {
  patient: CollectionPatient
  specimens: CollectionSpecimen[]
  stat: boolean
}

const MODULE_STOPPED =
  'New collections have stopped: the laboratory module is switched off on this site.'

const UHID_WORDS: Record<IdentityCheck, string> = {
  match: '',
  no_match: 'The UHID does not match this patient\'s record.',
  not_given: 'Type the UHID from the wristband.',
  not_on_record: 'This patient has no UHID on record. Ask for the record to be corrected before collecting.',
}
const NAME_WORDS: Record<IdentityCheck, string> = {
  match: '',
  no_match: 'The name does not match this patient\'s record.',
  not_given: 'Type the patient\'s name.',
  not_on_record: 'This patient has no name on record.',
}

function failureMessage(err: unknown, fallback: string): string {
  const failure = toLabError(err)
  if (failure.isModuleDisabled) return MODULE_STOPPED
  if (failure.code === 'identity_not_confirmed') {
    const checks = identityChecks(failure.detail) ?? {}
    const lines = [
      checks.uhid ? UHID_WORDS[checks.uhid] : '',
      checks.name ? NAME_WORDS[checks.name] : '',
    ].filter(Boolean)
    return lines.length
      ? `${lines.join(' ')} Check the wristband and ask the patient again. Nothing was printed or recorded.`
      : failure.message
  }
  return failure.message || fallback
}

function groupByPatient(specimens: CollectionSpecimen[]): PatientGroup[] {
  const groups = new Map<string, PatientGroup>()
  for (const specimen of specimens) {
    const existing = groups.get(specimen.patient.id)
    if (existing) {
      existing.specimens.push(specimen)
      existing.stat = existing.stat || specimen.priority === 'stat'
    } else {
      groups.set(specimen.patient.id, {
        patient: specimen.patient,
        specimens: [specimen],
        stat: specimen.priority === 'stat',
      })
    }
  }
  return [...groups.values()]
}

function groupByWard(groups: PatientGroup[]): { ward: string; patients: PatientGroup[] }[] {
  const wards = new Map<string, PatientGroup[]>()
  for (const group of groups) {
    const ward = group.patient.ward || group.patient.department || 'Ward not recorded'
    wards.set(ward, [...(wards.get(ward) ?? []), group])
  }
  return [...wards.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([ward, patients]) => ({ ward, patients }))
}

function labelStatus(specimen: CollectionSpecimen): string {
  if (specimen.label_print_count < 1) return 'Label not printed'
  const at = formatClinicalTime(specimen.label_printed_at)
  const times = specimen.label_print_count === 1 ? '' : `, ${specimen.label_print_count} prints`
  return `Label printed${at ? ` at ${at}` : ''}${times}`
}

interface PatientCardProps {
  group: PatientGroup
  showWard: boolean
  onPrinted: (labels: SpecimenLabel[]) => void
  onChanged: (message: string) => void
}

function PatientCard({ group, showWard, onPrinted, onChanged }: PatientCardProps) {
  const { patient } = group
  const planned = group.specimens.filter(s => !s.recollection_needed)
  const allLabelled = planned.length > 0 && planned.every(s => s.label_print_count > 0)

  const [checking, setChecking] = useState(false)
  const [uhid, setUhid] = useState('')
  const [name, setName] = useState('')
  // The identity the next step sends. Kept for this card only, never stored.
  const [identity, setIdentity] = useState<CollectionIdentity | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Tubes whose label this card just printed, so "Mark collected" is ready
  // before the refreshed list arrives.
  const [printedHere, setPrintedHere] = useState<ReadonlySet<string>>(new Set())
  const labelled = (s: CollectionSpecimen) => s.label_print_count > 0 || printedHere.has(s.id)

  const typed: CollectionIdentity = { uhid: uhid.trim(), name: name.trim() }
  const canSubmit = typed.uhid !== '' && typed.name !== '' && busy === null

  const print = async (who: CollectionIdentity) => {
    setBusy('print')
    setError(null)
    try {
      const result = await printSpecimenLabels(planned.map(s => s.id), who)
      setPrintedHere(new Set(result.labels.map(l => l.specimen_id)))
      setIdentity(who)
      setChecking(false)
      onPrinted(result.labels)
    } catch (err) {
      setError(failureMessage(err, 'The labels could not be printed.'))
    } finally {
      setBusy(null)
    }
  }

  const collect = async (specimen: CollectionSpecimen) => {
    if (!identity) return
    setBusy(specimen.id)
    setError(null)
    try {
      await collectSpecimen(specimen.id, identity)
      onChanged(`Sample ${formatAccession(specimen.id)} recorded as collected for ${patient.name}.`)
    } catch (err) {
      const failure = toLabError(err)
      if (failure.code === 'identity_not_confirmed') setIdentity(null)
      setError(failureMessage(err, 'The sample could not be recorded as collected.'))
    } finally {
      setBusy(null)
    }
  }

  const where = [showWard ? patient.ward : null, patient.bed ? `Bed ${patient.bed}` : null]
    .filter(Boolean).join(' · ')

  return (
    <li className={`samples__patient${group.stat ? ' samples__patient--stat' : ''}`}>
      <div className="samples__who">
        <span className="samples__name">{patient.name}</span>
        <span className="samples__meta">
          {[where, patient.mrn ? `UHID ${patient.mrn}` : 'UHID not recorded'].filter(Boolean).join(' · ')}
        </span>
      </div>

      <ul className="samples__tubes" aria-label={`Samples for ${patient.name}`}>
        {group.specimens.map(specimen => (
          <li key={specimen.id} className="samples__tube">
            <span className="samples__tube-body">
              <span>
                {specimen.priority !== 'routine' && (
                  <span className={`samples__priority${specimen.priority === 'urgent' ? ' samples__priority--urgent' : ''}`}>
                    {priorityText(specimen.priority)}{' '}
                  </span>
                )}
                {[specimen.container, specimen.specimen_display].filter(Boolean).join(', ')}
              </span>
              <span className="samples__tube-line">
                {specimen.tests.map(t => t.short_name).join(', ')}
              </span>
              <span className="samples__tube-line">
                {formatAccession(specimen.id)}
                {specimen.recollection_needed ? null : ` · ${labelStatus(specimen)}`}
              </span>
              {specimen.recollect_of && !specimen.recollection_needed && (
                <span className="samples__tube-line">
                  Recollection of {formatAccession(specimen.recollect_of)},
                  which the laboratory rejected
                </span>
              )}
              {specimen.recollection_needed && (
                <span className="samples__recollect">
                  Recollection needed: the laboratory rejected this sample
                  {rejectionWords(specimen) ? ` (${rejectionWords(specimen)})` : ''}. The laboratory
                  decides whether to recollect it; if it does, a new tube to collect appears here.
                </span>
              )}
            </span>
            {!specimen.recollection_needed && identity && (
              <button
                type="button"
                className="samples__btn samples__btn--primary"
                disabled={busy !== null || !labelled(specimen)}
                onClick={() => void collect(specimen)}
              >
                {busy === specimen.id ? 'Recording…' : 'Mark collected'}
              </button>
            )}
          </li>
        ))}
      </ul>

      {planned.length > 0 && !identity && !checking && (
        <div className="samples__actions">
          <button
            type="button"
            className="samples__btn samples__btn--primary"
            onClick={() => { setChecking(true); setError(null) }}
          >
            Check identity and print labels
          </button>
        </div>
      )}

      {checking && (
        <form
          className="samples__identity"
          aria-label={`Check identity for ${patient.name}`}
          onSubmit={e => { e.preventDefault(); if (canSubmit) void print(typed) }}
        >
          <p className="samples__identity-hint">
            Read the UHID from the wristband and ask the patient to say their name. Both must
            match before labels print.
          </p>
          <div className="samples__fields">
            <label className="samples__field">
              <span className="samples__label">UHID from the wristband</span>
              <input
                className="samples__input"
                value={uhid}
                onChange={e => setUhid(e.target.value)}
                autoComplete="off"
                autoCapitalize="characters"
                spellCheck={false}
              />
            </label>
            <label className="samples__field">
              <span className="samples__label">Name as the patient says it</span>
              <input
                className="samples__input"
                value={name}
                onChange={e => setName(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
          </div>
          <div className="samples__actions">
            <button type="submit" className="samples__btn samples__btn--primary" disabled={!canSubmit}>
              {busy === 'print' ? 'Printing…' : `Print ${planned.length === 1 ? 'label' : `${planned.length} labels`}`}
            </button>
            {allLabelled && (
              <button
                type="button"
                className="samples__btn"
                disabled={!canSubmit}
                onClick={() => { setIdentity(typed); setChecking(false); setError(null) }}
              >
                Labels are already on the tubes
              </button>
            )}
            <button type="button" className="samples__btn" onClick={() => { setChecking(false); setError(null) }}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {identity && planned.length > 0 && (
        <div className="samples__actions">
          <button
            type="button"
            className="samples__btn"
            disabled={busy !== null}
            onClick={() => void print(identity)}
          >
            Print labels again
          </button>
          <button type="button" className="samples__btn" onClick={() => setIdentity(null)}>
            Done with this patient
          </button>
        </div>
      )}

      {error && <p className="samples__note samples__note--error" role="alert">{error}</p>}
    </li>
  )
}

export default function SamplesSheet({ variant = 'ward' }: SamplesSheetProps) {
  const [list, setList] = useState<CollectionList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [moduleOff, setModuleOff] = useState(false)
  const [labels, setLabels] = useState<SpecimenLabel[] | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const next = await getCollectionList()
      setList(next)
      setError(null)
      setModuleOff(false)
      setCheckedAt(new Date())
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) setModuleOff(true)
      else setError(failure.message || 'The samples list could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  const groups = useMemo(() => groupByPatient(list?.specimens ?? []), [list])
  const wards = useMemo(() => (variant === 'laboratory' ? groupByWard(groups) : null), [groups, variant])

  const onPrinted = (printed: SpecimenLabel[]) => {
    setLabels(printed)
    setNotice(`${printed.length === 1 ? 'Label' : `${printed.length} labels`} sent to the printer for ${printed[0]?.patient.name ?? 'the patient'}. Mark each tube collected once the sample is drawn.`)
    void load()
  }

  const onChanged = (message: string) => {
    setNotice(message)
    void load()
  }

  const scopeWords = list?.scope.hospital_wide
    ? 'every ward'
    : (list?.scope.department || 'your ward')

  const renderPatients = (patients: PatientGroup[]) => (
    <ul className="samples__list">
      {patients.map(group => (
        <PatientCard
          key={group.patient.id}
          group={group}
          showWard={variant === 'laboratory'}
          onPrinted={onPrinted}
          onChanged={onChanged}
        />
      ))}
    </ul>
  )

  return (
    <div className="samples">
      <div className="samples__head">
        <div>
          <h2 className="samples__title">
            {variant === 'laboratory' ? 'Samples to collect, all wards' : 'Samples to collect'}
          </h2>
          {list && (
            <p className="samples__scope">
              {list.specimens.length} {list.specimens.length === 1 ? 'sample' : 'samples'} on {scopeWords}
              {checkedAt ? `, checked at ${formatClinicalTime(checkedAt)}` : ''}.
              {' '}Collection time is recorded as now.
            </p>
          )}
        </div>
        <button type="button" className="samples__btn" onClick={() => void load()} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {notice && (
        <p className="samples__note samples__note--ok" role="status">{notice}</p>
      )}

      {moduleOff && (
        <p className="samples__note samples__note--warn" role="status">
          The laboratory module is switched off on this site, so there are no samples to collect here.
        </p>
      )}

      {error && (
        <div className="samples__note samples__note--error" role="alert">
          <p style={{ margin: 0 }}>Couldn't load the samples list. {error}</p>
          <p style={{ margin: '4px 0 0' }}>
            This is a display failure, not an empty list. Do not read it as nothing to collect.
          </p>
        </div>
      )}

      {loading && !list && !error && <p className="samples__scope" role="status">Loading the samples list</p>}

      {list && !error && list.specimens.length === 0 && (
        <p className="samples__empty">
          No samples waiting to be collected on {scopeWords}. Laboratory orders placed for these
          patients appear here once the laboratory has planned their tubes.
        </p>
      )}

      {list && !error && list.specimens.length > 0 && (
        wards
          ? wards.map(w => (
              <section key={w.ward} aria-label={w.ward}>
                <h3 className="samples__ward-title">{w.ward}</h3>
                {renderPatients(w.patients)}
              </section>
            ))
          : renderPatients(groups)
      )}

      {labels && createPortal(
        <SpecimenLabelPrintView labels={labels} onAfterPrint={() => setLabels(null)} />,
        document.body,
      )}
    </div>
  )
}
