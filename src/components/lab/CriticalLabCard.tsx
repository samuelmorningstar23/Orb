import { useCallback, useEffect, useState, useSyncExternalStore, type FormEvent } from 'react'
import { getUser } from '../../api/client'
import { toLabError } from '../../api/lab'
import {
  acknowledgeWardCritical,
  criticalCardOn,
  getWardCriticals,
  type WardAckMethod,
  type WardCritical,
} from '../../api/lab/wardCriticals'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { formatClinicalDateTime } from '../../lib/datetime'
import { hasPermission, LAB_CRITICAL_ACK } from '../../lib/roles'
import { currentFeatureFlags, currentModuleFlags, subscribeModuleFlags } from '../../shellConfig'
import './CriticalLabCard.css'

/* ═══════════════════════════════════════════════════════════════════════════
   CRITICAL LAB RESULTS: the ward's card (lis-16)

   On the nurse home and the doctor home: the open critical laboratory results
   of the patients on this ward, and the ward's read-back acknowledgement.

   IT IS THE LABORATORY TALKING, NOT ORB. The card shows the laboratory's
   value, its own flag and the limit its pathologist agreed (for a result that
   came over the interface from another laboratory, that laboratory's flag and
   the reference range it sent). Orb adds no severity and no score of its own,
   and nothing here is coloured beyond the existing status tokens: the danger
   rule and the count chip carry the tint, the words carry the meaning.

   WHO SEES IT. Only an account holding lab.critical_ack (a ward nurse or
   doctor), and only while the laboratory module is on or draining or the
   interfaced critical switch is on (/api/health). With both off there is no
   card and no request at all, so the homes are exactly what they were. The
   ward is decided on the server from the signed-in account.

   A CARD THAT FAILED IS NEVER EMPTY. "No critical result" renders nothing; a
   failed read with no list to show (the first load, or a refresh after an
   empty load) renders an error block that says it is a display failure, not
   an all-clear; a failed refresh under a list keeps the list and says it may
   be out of date.

   ACKNOWLEDGING needs the name of the person who was told and the read-back
   ticked; the request names the value the card showed, and a value that
   changed meanwhile is refused by the server and the card reloads.
   ═══════════════════════════════════════════════════════════════════════════ */

const REFRESH_MS = 60_000

export default function CriticalLabCard() {
  const modules = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)
  const features = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  // Denial is absence, never a control that exists only to 403.
  if (!criticalCardOn(modules, features) || !hasPermission(LAB_CRITICAL_ACK)) return null
  return <CriticalLabCardBody />
}

function CriticalLabCardBody() {
  const [items, setItems] = useState<WardCritical[] | null>(null)
  const [error, setError] = useState('')
  const [switchedOff, setSwitchedOff] = useState(false)
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState('')
  const [formFor, setFormFor] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const body = await getWardCriticals()
      setItems(body.open)
      setError('')
      setSwitchedOff(false)
    } catch (e) {
      const err = toLabError(e)
      if (err.isModuleDisabled) {
        // The switch went off after the shell read /api/health.
        setSwitchedOff(true)
        setItems([])
        setError('')
      } else {
        setError(err.message)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])
  // Paused while an acknowledgement is being filled in: re-rendering the list
  // under a nurse mid-entry would be hostile, and the server refuses a stale
  // value anyway.
  useVisiblePolling(() => void load(), REFRESH_MS, formFor === null)

  if (switchedOff) return null

  // The full error block shows whenever the latest read failed and there is
  // no list on screen to fall back on: a first load that failed, and also an
  // empty ward whose refresh then failed. The server can only fail its ward
  // read (the audit row, the views) once there is a patient to return, so a
  // failed refresh after an empty load is exactly when a new critical may
  // have arrived; rendering nothing there would look like an all-clear.
  if (error && (items === null || items.length === 0)) {
    return (
      <section className="critical-lab-card" aria-labelledby="critical-lab-card-title">
        <h2 id="critical-lab-card-title" className="orb-section-header__label">
          Critical lab results
        </h2>
        {notice && (
          <p className="critical-lab-card__notice" role="status">
            {notice}
            {' '}
            <button type="button" className="critical-lab-card__link" onClick={() => setNotice('')}>
              Dismiss
            </button>
          </p>
        )}
        <div className="orb-state orb-state--error" role="alert">
          <div className="orb-state__content">
            <p className="orb-state__title">Couldn't load critical lab results</p>
            <p className="orb-state__detail">{error}</p>
            <p className="orb-state__warn">
              This is a display failure, not an all-clear. Phone the laboratory if you are
              waiting for a result.
            </p>
            <div>
              <button type="button" className="orb-home__refresh" onClick={() => void load()}
                disabled={loading}>
                {loading ? 'Retrying…' : 'Retry'}
              </button>
            </div>
          </div>
        </div>
      </section>
    )
  }

  if (!items || (items.length === 0 && !notice)) return null

  const done = (message: string, reload = true) => {
    setFormFor(null)
    setNotice(message)
    if (reload) void load()
  }

  return (
    <section className="critical-lab-card" aria-labelledby="critical-lab-card-title">
      <div className="orb-section-header critical-lab-card__header">
        <h2 id="critical-lab-card-title" className="orb-section-header__label">
          Critical lab results
        </h2>
        {items.length > 0 && (
          <span className="orb-count-chip orb-count-chip--danger"
            aria-label={`${items.length} to acknowledge`}>
            {items.length}
          </span>
        )}
      </div>
      {notice && (
        <p className="critical-lab-card__notice" role="status">
          {notice}
          {' '}
          <button type="button" className="critical-lab-card__link" onClick={() => setNotice('')}>
            Dismiss
          </button>
        </p>
      )}
      {error && (
        <p className="critical-lab-card__error" role="alert">
          Could not refresh: {error} The list below may be out of date.
        </p>
      )}
      {items.length > 0 && (
        <ul className="critical-lab-card__list">
          {items.map(item => (
            <CriticalItem
              key={item.id}
              item={item}
              formOpen={formFor === item.id}
              onOpen={() => { setNotice(''); setFormFor(item.id) }}
              onCancel={() => setFormFor(null)}
              onDone={done}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function locationWords(item: WardCritical): string {
  if (item.patient.discharged) return 'Discharged'
  return item.patient.bed ? `Bed ${item.patient.bed}` : 'No bed recorded'
}

function headline(item: WardCritical): string {
  return item.withdrawn ? `${item.test_name}: withdrawn` : `${item.test_name} ${item.value_told}`
}

interface ItemProps {
  item: WardCritical
  formOpen: boolean
  onOpen: () => void
  onCancel: () => void
  onDone: (message: string, reload?: boolean) => void
}

function CriticalItem({ item, formOpen, onOpen, onCancel, onDone }: ItemProps) {
  const interfaced = item.source_lane === 'interfaced'
  const titleId = `critical-lab-${item.id}`
  return (
    <li className="critical-lab-card__item" aria-labelledby={titleId}>
      <div className="critical-lab-card__head">
        {/* The tag never stands alone: the words beside it say the same thing. */}
        <span className="orb-tag orb-tag--ack" aria-hidden="true">Critical</span>
        <span id={titleId} className="critical-lab-card__test">
          <span className="sr-only">Critical result: </span>{headline(item)}
        </span>
        {item.preliminary && (
          <span className="critical-lab-card__state">Preliminary, not yet authorised</span>
        )}
      </div>
      <p className="critical-lab-card__patient">
        {item.patient.name ?? 'Unnamed patient'}
        {item.patient.mrn && <> · UHID {item.patient.mrn}</>}
        {' · '}{locationWords(item)}
      </p>
      <dl className="critical-lab-card__facts">
        {item.lab_flag?.text && (
          <>
            <dt>{interfaced ? "Sending laboratory's flag" : "Laboratory's flag"}</dt>
            <dd>{item.lab_flag.text}</dd>
          </>
        )}
        {item.critical_limit_text && (
          <>
            <dt>Agreed critical limit</dt>
            <dd>{item.critical_limit_text}</dd>
          </>
        )}
        {item.reference_range && (
          <>
            <dt>Reference range sent</dt>
            <dd>{item.reference_range}</dd>
          </>
        )}
        {item.state_text && !item.preliminary && (
          <>
            <dt>Status</dt>
            <dd>{item.state_text}</dd>
          </>
        )}
        <dt>Reported</dt>
        <dd>
          {formatClinicalDateTime(item.raised_at)}
          {interfaced ? ' by the sending laboratory' : ' by the laboratory'}
        </dd>
      </dl>
      {item.change_text && <p className="critical-lab-card__change">{item.change_text}</p>}
      {formOpen ? (
        <AcknowledgeForm item={item} onCancel={onCancel} onDone={onDone} />
      ) : (
        <div className="critical-lab-card__actions">
          <button type="button" className="critical-lab-card__btn critical-lab-card__btn--primary"
            onClick={onOpen} aria-describedby={titleId}>
            Acknowledge
          </button>
        </div>
      )}
    </li>
  )
}

interface FormProps {
  item: WardCritical
  onCancel: () => void
  onDone: (message: string, reload?: boolean) => void
}

function AcknowledgeForm({ item, onCancel, onDone }: FormProps) {
  const [name, setName] = useState(() => getUser()?.name ?? '')
  const [method, setMethod] = useState<WardAckMethod>('in_app')
  const [caller, setCaller] = useState('')
  const [readBack, setReadBack] = useState(false)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState('')
  const base = `critical-ack-${item.id}`
  const ready = readBack && name.trim().length >= 2 && !busy

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!ready) return
    setBusy(true)
    setFormError('')
    try {
      await acknowledgeWardCritical(item.id, {
        recipient_name: name.trim(),
        method,
        caller_name: method === 'phone' && caller.trim() ? caller.trim() : null,
        read_back: true,
        value_told: item.value_told,
      })
      onDone(`Acknowledged: ${headline(item)} for ${item.patient.name ?? 'the patient'}. `
        + 'The laboratory can see it was read back on the ward.')
    } catch (err) {
      const lab = toLabError(err)
      if (lab.code === 'value_changed') {
        onDone(`${lab.message}`)
      } else if (lab.code === 'already_closed') {
        onDone(lab.message)
      } else if (lab.isNotFound) {
        onDone('This critical result is no longer on your ward. Nothing was recorded.')
      } else {
        setFormError(lab.message)
        setBusy(false)
      }
    }
  }

  return (
    <form className="critical-lab-card__form" onSubmit={e => void submit(e)}
      aria-label={`Acknowledge ${headline(item)}`}>
      <label className="critical-lab-card__field" htmlFor={`${base}-name`}>
        <span>Name of the person who was told</span>
        <input id={`${base}-name`} className="critical-lab-card__input" value={name}
          autoComplete="off" maxLength={120} onChange={e => setName(e.target.value)} />
      </label>
      <fieldset className="critical-lab-card__choices">
        <legend>How were you told?</legend>
        <label className="critical-lab-card__check">
          <input type="radio" name={`${base}-method`} value="in_app" checked={method === 'in_app'}
            onChange={() => setMethod('in_app')} />
          Read on the ward screen
        </label>
        <label className="critical-lab-card__check">
          <input type="radio" name={`${base}-method`} value="phone" checked={method === 'phone'}
            onChange={() => setMethod('phone')} />
          Phone call from the laboratory
        </label>
      </fieldset>
      {method === 'phone' && (
        <label className="critical-lab-card__field" htmlFor={`${base}-caller`}>
          <span>Who phoned from the laboratory (optional)</span>
          <input id={`${base}-caller`} className="critical-lab-card__input" value={caller}
            autoComplete="off" maxLength={120} onChange={e => setCaller(e.target.value)} />
        </label>
      )}
      <label className="critical-lab-card__check critical-lab-card__check--readback">
        <input type="checkbox" checked={readBack} onChange={e => setReadBack(e.target.checked)} />
        {item.withdrawn
          ? `Read back: ${item.test_name} was withdrawn and must not be acted on`
          : `Read back: ${item.test_name} ${item.value_told}`}
      </label>
      <div className="critical-lab-card__actions">
        <button type="submit" className="critical-lab-card__btn critical-lab-card__btn--primary"
          disabled={!ready}>
          {busy ? 'Recording…' : 'Acknowledge'}
        </button>
        <button type="button" className="critical-lab-card__btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
      {formError && <p className="critical-lab-card__error" role="alert">{formError}</p>}
    </form>
  )
}
