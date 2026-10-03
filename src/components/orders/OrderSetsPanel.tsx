/**
 * Order sets / protocols — apply a cited protocol and watch the safety gates.
 *
 * WHY THIS FILE EXISTS. The backend for this has been complete and enforced for
 * a while: list/get/preview/apply/applications, an `order_set_applications`
 * table, audit hooks, and `server/site_config/order_sets.json` full of cited,
 * India-appropriate protocols. There was NO frontend — `grep -rni "order.set"
 * src/` returned nothing — so a listed demo step ("apply an order set, see the
 * safety gates fire") could not be performed at all.
 *
 * THE ONE RULE THIS SCREEN ENFORCES BY ITS SHAPE: a set is applied WHOLE OR NOT
 * AT ALL. Preview runs the identical gates apply runs, so the two can never
 * disagree; a blocked item makes Apply refuse and say why, per item; and there
 * is deliberately NO control anywhere on this screen that skips a gate. An
 * override, when one is legitimate, belongs on the individual order where the
 * per-dose acknowledgement machinery and its audit row live.
 */
import { Fragment, useCallback, useEffect, useState } from 'react'
import {
  listOrderSets,
  previewOrderSet,
  applyOrderSet,
  listOrderSetApplications,
  type OrderSetSummary,
  type OrderSetPreview,
  type OrderSetVerdict,
  type OrderSetApplication,
} from '../../api/orderSets'
import PatientPicker from '../common/PatientPicker'
import { formatUTCDateTime } from '../../lib/datetime'
import { MED_NOT_SENT, isMedicationOrder } from './medicationRouting'
import './OrderSets.css'

interface Props {
  /** When hosted inside a patient chart, the patient is fixed. */
  patientId?: string
  /** Controlled picker. The Orders tab shares its patient with this screen, so
   *  the picker stays visible (a hidden picker is how a set came to be applied
   *  to a patient the screen never named) and a change here moves both. */
  selectedPatientId?: string
  onSelectPatient?: (patientId: string) => void
  /** After a successful apply, so the host can refresh its order list. */
  onApplied?: () => void
}

/** Who the gates ran for, returned WITH the verdicts by the preview. */
interface PatientBanner {
  id: string
  name: string | null
  mrn: string | null
  bed: string | null
  ward: string | null
  /** null means the list could not be read, which is not the same as none. */
  allergies: { substance: string; reaction?: string | null; severity?: string | null }[] | null
}

/** The latest application of the same set to the same patient, if recent. */
interface RecentApplication {
  application_id: number
  applied_at: string
  applied_by: string | null
  applied_by_name: string | null
  order_count: number | null
  minutes_ago: number | null
}

type Preview = OrderSetPreview & {
  patient?: PatientBanner
  recent_application?: RecentApplication | null
}

/** The dose guard's verdict on one medication item (order_sets_service._dose_screen). */
interface DoseCheck {
  decision?: string
  findings?: { decision?: string; message?: string }[]
  unavailable?: boolean
  unavailable_reason?: string | null
}

const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many)

/** Server reasons are sometimes clause-shaped ("the dose guard could not run"). */
const sentence = (s: string) => {
  const t = s.trim()
  if (!t) return t
  const capped = t[0].toUpperCase() + t.slice(1)
  return /[.!?]$/.test(capped) ? capped : `${capped}.`
}

function patientLabel(p: PatientBanner | undefined): string | null {
  if (!p?.name) return null
  return p.bed ? `${p.name} (${p.bed})` : p.name
}

function agoText(minutes: number | null): string {
  if (minutes === null) return 'recently'
  if (minutes < 1) return 'less than a minute ago'
  if (minutes < 60) return `${minutes} ${plural(minutes, 'minute')} ago`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return `${h} ${plural(h, 'hour')}${m ? ` ${m} min` : ''} ago`
}

/** Name, bed, MRN and allergies of the patient the verdicts below were run for. */
function PatientBannerView({ p }: { p: PatientBanner }) {
  const where = [p.bed ? `Bed ${p.bed}` : null, p.ward].filter(Boolean).join(', ')
  const meta = [where, p.mrn ? `MRN ${p.mrn}` : null].filter(Boolean).join(' · ')
  return (
    <div className="order-sets__patient" aria-label="Patient these orders are for">
      <div className="order-sets__patient-who">
        <span className="order-sets__patient-name">{p.name || 'Name not recorded'}</span>
        {meta && <span className="order-sets__patient-meta">{meta}</span>}
      </div>
      {p.allergies === null ? (
        <span className="order-sets__patient-allergy-unknown" role="alert">
          The allergy list could not be read. Check the chart before applying.
        </span>
      ) : p.allergies.length === 0 ? (
        <span className="order-sets__patient-allergies">No allergies recorded</span>
      ) : (
        <span className="order-sets__patient-allergies order-sets__patient-allergies--some">
          Allergies:{' '}
          {p.allergies
            .map(a => {
              const name = a.substance ? a.substance[0].toUpperCase() + a.substance.slice(1) : ''
              return a.reaction ? `${name} (${a.reaction})` : name
            })
            .join(', ')}
        </span>
      )}
    </div>
  )
}

/**
 * "Nothing to show yet, and that is not a fault."
 *
 * Deliberately NOT shaped like .order-sets__error: on a clinical screen an
 * empty region and a failed region must never be confusable, because one means
 * "no findings" and the other means "we could not look".
 */
function Placeholder({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="order-sets__placeholder">
      <span className="order-sets__placeholder-icon" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
          <rect x="9" y="3" width="6" height="4" rx="1" />
          <path d="M9 13h6" />
          <path d="M9 17h4" />
        </svg>
      </span>
      <p className="order-sets__placeholder-title">{title}</p>
      <p className="order-sets__placeholder-detail">{detail}</p>
    </div>
  )
}

/** The structured body of an `order_set_blocked` refusal. */
interface BlockedDetail {
  error?: string
  message?: string
  blocked?: OrderSetVerdict[]
  items?: OrderSetVerdict[]
}

/** Everything the gates said about an unblocked item WITHOUT stopping it, or
 *  null when no drug gate applies (a lab or nursing item has no safety chip).
 *
 *  "Cleared" is only honest when every gate ran and said nothing. This used to
 *  read the allergy and interaction alerts alone, so a drug outside the
 *  interaction table (interaction_screened false), a dose-guard warning and a
 *  dose guard that could not run all rendered "Cleared"; interaction alerts
 *  were labelled "Allergy alert"; and lab items were "Cleared" by a drug screen
 *  that never looked at them. */
function findingsFor(v: OrderSetVerdict): { chip: string; why: string }[] | null {
  if (!isMedicationOrder(v.order_type)) return null
  const out: { chip: string; why: string }[] = []
  const s = v.medication_safety
  if (!s) {
    out.push({ chip: 'Not screened', why: 'The allergy and interaction screen did not run for this item.' })
  } else {
    const allergy = s.allergy_alerts || []
    const interaction = s.interaction_alerts || []
    if (allergy.length) out.push({ chip: 'Allergy alert', why: allergy.map(a => a.message).join(' ') })
    if (interaction.length) {
      out.push({ chip: 'Interaction alert', why: interaction.map(a => a.message).join(' ') })
    }
    if (s.interaction_screened === false) {
      out.push({
        chip: 'Not screened for interactions',
        why: s.coverage_note
          || 'This drug is outside the interaction table, so it was not checked against the other medicines on this chart.',
      })
    } else if (s.coverage_note) {
      out.push({ chip: 'Partly screened for interactions', why: s.coverage_note })
    }
  }
  const d = v.dose_check as DoseCheck | null
  if (!d || d.unavailable) {
    out.push({
      chip: 'Dose not checked',
      why: sentence(d?.unavailable_reason || 'the dose guard did not run for this item'),
    })
  } else if (d.decision === 'warn') {
    const msgs = (d.findings || []).filter(f => f.decision === 'warn' && f.message).map(f => f.message)
    out.push({ chip: 'Dose warning', why: msgs.join(' ') || 'The dose guard raised a warning on this dose.' })
  }
  return out
}

export default function OrderSetsPanel({
  patientId: patientIdProp, selectedPatientId, onSelectPatient, onApplied,
}: Props) {
  const [sets, setSets] = useState<OrderSetSummary[]>([])
  const [configNote, setConfigNote] = useState<string | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [loadingSets, setLoadingSets] = useState(true)

  /* Derived, not copied into state: a copy is adjusted one render late, and
     everything downstream of `patientId` here (preview, apply gates,
     application history) is patient-specific. Fixed (chart-hosted) wins, then
     the host's controlled selection, then this screen's own picker. */
  const [localPatientId, setLocalPatientId] = useState('')
  const controlled = !patientIdProp && onSelectPatient !== undefined
  const patientId = patientIdProp || (controlled ? selectedPatientId || '' : localPatientId)
  const choosePatient = controlled ? onSelectPatient : setLocalPatientId
  const [selectedSet, setSelectedSet] = useState<OrderSetSummary | null>(null)

  const [previewState, setPreview] = useState<Preview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState<
    { orders: number; medications: number; patient: string | null } | null
  >(null)
  const [applyBlocked, setApplyBlocked] = useState<BlockedDetail | null>(null)
  const [applyError, setApplyError] = useState<string | null>(null)
  // Second press needed when the same set went on this patient recently.
  const [repeatArmed, setRepeatArmed] = useState(false)

  const [applications, setApplications] = useState<OrderSetApplication[]>([])

  // A preview is only ever shown beside the patient and protocol it was run
  // for. The effect below clears it on change, but only after a commit; this
  // makes the frame in between show nothing rather than the last patient's
  // verdicts under the new name.
  const preview = previewState
    && previewState.patient_id === patientId
    && previewState.set?.id === selectedSet?.id
    ? previewState : null

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setLoadingSets(true)
    listOrderSets()
      .then(res => {
        setSets(res.sets || [])
        setListError(null)
        if (res.config) {
          // Provenance, stated on the screen: nobody can change these
          // protocols through this UI. That is a claim a CIO will test. The
          // server's editable_by names a config file, which is not something
          // a clinician should be told to go and find.
          setConfigNote(
            `Protocol library version ${res.config.config_version}. Changes are made by your Orb administrator.`,
          )
        }
      })
      .catch(e => setListError(e instanceof Error ? e.message : 'Order sets could not be loaded.'))
      .finally(() => setLoadingSets(false))
  }, [])

  const loadApplications = useCallback(() => {
    if (!patientId) { setApplications([]); return }
    listOrderSetApplications(patientId)
      .then(r => setApplications(r.applications || []))
      // A missing history list must not look like a failed apply.
      .catch(() => setApplications([]))
  }, [patientId])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { loadApplications() }, [loadApplications])

  // Changing either input invalidates everything downstream of it — and then
  // re-runs the gates for the new pair.
  //
  // This used to be two separate things: an effect that CLEARED the preview on
  // any change, and a runPreview() called only from the protocol card's
  // onClick. So the gates ran only if you picked the protocol last. Pick the
  // protocol first and the patient second — which is the order the screen reads
  // in, big list on the left, small picker top-right, and the order the demo
  // script uses — and the effect wiped the preview that the click had just
  // fetched and nothing ever refetched it. The panel sat on a title, a citation
  // and no items, with no Apply button and nothing saying why.
  //
  // Owning both halves here means the invalidation and the refetch cannot drift
  // apart again: there is exactly one place that decides what is on screen for
  // a given (patient, protocol) pair. `live` drops any response that arrives
  // after its inputs changed, so a slow preview can never paint onto a patient
  // it was not run for.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setPreview(null); setPreviewError(null)
    setApplyResult(null); setApplyBlocked(null); setApplyError(null)
    setRepeatArmed(false)

    if (!patientId || !selectedSet) { setPreviewing(false); return }

    let live = true
    setPreviewing(true)
    previewOrderSet(selectedSet.id, patientId)
      .then(p => { if (live) setPreview(p) })
      .catch(e => {
        if (!live) return
        setPreview(null)
        setPreviewError(e instanceof Error ? e.message : 'Preview failed.')
      })
      .finally(() => { if (live) setPreviewing(false) })

    return () => { live = false }
  }, [patientId, selectedSet])

  // Explicit re-run, for Retry and for refreshing the gates after an apply.
  // The effect above covers every *input* change; this covers the cases where
  // the inputs are unchanged but the answer may not be.
  const runPreview = async (s: OrderSetSummary) => {
    if (!patientId) return
    setPreviewing(true)
    setPreviewError(null)
    try {
      setPreview(await previewOrderSet(s.id, patientId))
    } catch (e) {
      setPreview(null)
      setPreviewError(e instanceof Error ? e.message : 'Preview failed.')
    } finally {
      setPreviewing(false)
    }
  }

  const runApply = async () => {
    if (!patientId || !selectedSet) return
    setApplying(true)
    setRepeatArmed(false)
    setApplyBlocked(null); setApplyError(null)
    try {
      const res = await applyOrderSet(selectedSet.id, patientId)
      const orders: { order_type?: string }[] = res.orders || []
      setApplyResult({
        orders: orders.length,
        medications: orders.filter(o => isMedicationOrder(o.order_type)).length,
        patient: patientLabel(preview?.patient),
      })
      loadApplications()
      onApplied?.()
      void runPreview(selectedSet)
    } catch (e) {
      const err = e as Error & { detail?: BlockedDetail; status?: number }
      if (err.detail && typeof err.detail === 'object' && err.detail.error === 'order_set_blocked') {
        setApplyBlocked(err.detail)
      } else {
        setApplyError(err.message || 'The order set could not be applied.')
      }
    } finally {
      setApplying(false)
    }
  }

  const blockedNow = preview?.blocked ?? []
  const recent = preview?.recent_application ?? null
  const forWhom = patientLabel(preview?.patient)
  const itemCount = preview?.items.length ?? 0
  const applyLabel = (() => {
    const what = `${itemCount} ${plural(itemCount, 'order')}${forWhom ? ` for ${forWhom}` : ''}`
    if (applying) return 'Applying…'
    if (recent && repeatArmed) return `Confirm: apply again, ${what}`
    if (recent) return `Apply again: ${what}`
    return `Apply ${what}`
  })()
  const onApplyClick = () => {
    // Applying the same set twice is two sets of STAT drafts. Not blocked (a
    // protocol can legitimately be re-run) but never a single unread click.
    if (recent && !repeatArmed) { setRepeatArmed(true); return }
    void runApply()
  }

  return (
    <div className="order-sets">
      <header className="order-sets__head">
        <div>
          <h2 className="order-sets__title">Order Sets</h2>
          <p className="order-sets__sub">
            Cited protocols. Every item still goes through the allergy check, the dose
            check and the code check, and a set is applied whole or not at all.
          </p>
        </div>
        {!patientIdProp && (
          <PatientPicker
            value={patientId}
            onChange={choosePatient}
            label="Apply to"
            hint="Choose the patient this protocol is for."
          />
        )}
      </header>

      {configNote && <p className="order-sets__config">{configNote}</p>}

      {listError && (
        <div className="order-sets__error" role="alert">
          Order sets could not be loaded: {listError}
        </div>
      )}

      <div className="order-sets__body">
        <section className="order-sets__list" aria-label="Available protocols">
          {loadingSets && <p className="order-sets__muted">Loading protocols…</p>}
          {!loadingSets && sets.length === 0 && !listError && (
            <p className="order-sets__muted">No protocols are configured for this site.</p>
          )}
          {sets.map(s => (
            <button
              key={s.id}
              type="button"
              className={`order-sets__card${selectedSet?.id === s.id ? ' order-sets__card--on' : ''}`}
              onClick={() => setSelectedSet(s)}
              aria-pressed={selectedSet?.id === s.id}
            >
              <span className="order-sets__card-name">{s.name}</span>
              <span className="order-sets__card-ind">{s.indication}</span>
              <span className="order-sets__card-meta">
                {s.item_count} item{s.item_count === 1 ? '' : 's'}
                {s.guideline?.name ? ` · ${s.guideline.name}` : ''}
              </span>
            </button>
          ))}
        </section>

        <section className="order-sets__detail" aria-label="Protocol detail">
          {!selectedSet ? (
            <Placeholder
              title="No protocol selected"
              detail="Pick one on the left and this panel lists every order it would place, with the safety gates already run against it."
            />
          ) : !patientId ? (
            <Placeholder
              title="No patient selected"
              detail="The safety gates are patient-specific (allergies, weight, renal function) so there is nothing honest to show until Orb knows whose chart this is. Choose a patient above."
            />
          ) : (
            <>
              {/* Above the protocol, from the preview itself: the name here is
                  the patient the verdicts below were run for. */}
              {preview?.patient && <PatientBannerView p={preview.patient} />}
              <h3 className="order-sets__detail-title">{selectedSet.name}</h3>
              {selectedSet.guideline?.citation && (
                <p className="order-sets__cite">{selectedSet.guideline.citation}</p>
              )}
              {(selectedSet.guideline?.reviewed_by || '').startsWith('SITE TO COMPLETE') && (
                <p className="order-sets__warn" role="note">
                  This protocol has not yet been signed off by a named consultant at this site.
                </p>
              )}

              {previewing && <p className="order-sets__muted">Running the safety gates…</p>}
              {previewError && (
                <div className="order-sets__error" role="alert">
                  Preview failed: {previewError}
                  <button type="button" className="order-sets__link" onClick={() => void runPreview(selectedSet)}>
                    Retry
                  </button>
                </div>
              )}

              {preview && (
                <>
                  {preview.unknown_codes?.length > 0 && (
                    <p className="order-sets__warn" role="note">
                      {preview.unknown_codes.length}{' '}
                      {plural(preview.unknown_codes.length, 'code')} in this protocol{' '}
                      {plural(preview.unknown_codes.length, 'is', 'are')} not in this
                      hospital's code list:{' '}
                      {preview.unknown_codes.map(c => `${c.code_system} ${c.code}`).join(', ')}.
                    </p>
                  )}

                  <ul className="order-sets__items">
                    {preview.items.map(v => {
                      const findings = v.blocked ? null : findingsFor(v)
                      return (
                        <li
                          key={v.index}
                          className={`order-sets__item${v.blocked ? ' order-sets__item--blocked' : ''}`}
                        >
                          <div className="order-sets__item-main">
                            <span className="order-sets__item-desc">{v.description}</span>
                            <span className="order-sets__item-meta">
                              {[v.order_type, v.priority,
                                [v.dose, v.route, v.frequency].filter(Boolean).join(' '),
                                v.code ? `${v.code_system} ${v.code}` : null]
                                .filter(Boolean).join(' · ')}
                            </span>
                            {v.note && <span className="order-sets__item-note">{v.note}</span>}
                            {isMedicationOrder(v.order_type) && (
                              <span className="order-sets__item-route">{MED_NOT_SENT}</span>
                            )}
                          </div>
                          {v.blocked ? (
                            <div className="order-sets__block" role="alert">
                              <span className="order-sets__block-chip">STOPPED</span>
                              <span className="order-sets__block-why">
                                {v.block_reason || v.block_kind || 'A safety check stopped this item.'}
                              </span>
                            </div>
                          ) : findings === null ? null : findings.length > 0 ? (
                            /* Non-blocking, but NOT clean: a gate raised something
                               below its stop threshold, or could not look at all.
                               The set still applies; the prescriber applies it
                               having read this. */
                            <div className="order-sets__caution" role="note">
                              {findings.map(f => (
                                <Fragment key={f.chip}>
                                  <span className="order-sets__caution-chip">{f.chip}</span>
                                  <span className="order-sets__caution-why">{f.why}</span>
                                </Fragment>
                              ))}
                            </div>
                          ) : (
                            <span className="order-sets__ok-chip">Cleared</span>
                          )}
                        </li>
                      )
                    })}
                  </ul>

                  {recent && (
                    <p className="order-sets__warn" role="alert">
                      {`${preview.set.name} was applied to ${preview.patient?.name || 'this patient'} `
                        + `${agoText(recent.minutes_ago)} by `
                        + `${recent.applied_by_name || recent.applied_by || 'another user'}`
                        + (typeof recent.order_count === 'number'
                          ? ` (${recent.order_count} ${plural(recent.order_count, 'order')})` : '')
                        + '. Applying it again places a second, separate set of orders.'}
                    </p>
                  )}

                  <div className="order-sets__actions">
                    <button
                      type="button"
                      className="order-sets__apply"
                      onClick={onApplyClick}
                      disabled={applying || previewing || !preview.would_apply}
                    >
                      {applyLabel}
                    </button>
                    {repeatArmed && !applying && (
                      <button
                        type="button"
                        className="order-sets__secondary"
                        onClick={() => setRepeatArmed(false)}
                      >
                        Do not apply again
                      </button>
                    )}
                    {!preview.would_apply && (
                      <span className="order-sets__actions-note">
                        {blockedNow.length} {plural(blockedNow.length, 'item is', 'items are')} stopped
                        by a safety check. A set is applied whole or not at all. Resolve these on the
                        individual orders, where the override is recorded against the dose it
                        applies to.
                      </span>
                    )}
                  </div>
                </>
              )}

              {applyResult && (
                <div className="order-sets__ok" role="status">
                  Applied{applyResult.patient ? ` for ${applyResult.patient}` : ''}:{' '}
                  {applyResult.orders} draft {plural(applyResult.orders, 'order')} created and
                  audited.
                </div>
              )}
              {applyResult && applyResult.medications > 0 && (
                <div className="order-sets__warn" role="alert">
                  The {applyResult.medications} medication{' '}
                  {plural(applyResult.medications, 'order is', 'orders are')} not sent to the ward
                  or pharmacy. Prescribe {plural(applyResult.medications, 'it', 'them')} from the
                  patient chart (Medications) so nurses can give{' '}
                  {plural(applyResult.medications, 'it', 'them')}.
                </div>
              )}

              {applyBlocked && (
                <div className="order-sets__error" role="alert">
                  <strong>Nothing was ordered.</strong>{' '}
                  {applyBlocked.message
                    || 'A safety check stopped at least one item, and a set is applied whole or not at all.'}
                  <ul className="order-sets__blocked-list">
                    {(applyBlocked.blocked || []).map(b => (
                      <li key={b.index}>
                        <strong>{b.description}</strong>: {b.block_reason || b.block_kind}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {applyError && (
                <div className="order-sets__error" role="alert">{applyError}</div>
              )}
            </>
          )}
        </section>
      </div>

      {patientId && applications.length > 0 && (
        <section className="order-sets__history" aria-label="Protocols already applied">
          <h3 className="order-sets__detail-title">Already applied to this patient</h3>
          <ul className="order-sets__history-list">
            {applications.map((a, i) => (
              <li key={String(a.id ?? i)}>
                <strong>{a.set_name || a.set_id}</strong>
                {a.applied_at ? ` · ${formatUTCDateTime(String(a.applied_at))}` : ''}
                {a.applied_by ? ` · ${a.applied_by}` : ''}
                {typeof a.order_count === 'number'
                  ? ` · ${a.order_count} ${plural(a.order_count, 'order')}` : ''}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
