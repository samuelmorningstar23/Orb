/**
 * Orb — the ABDM / ABHA desk.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ THIS SCREEN SENDS NOTHING TO ABDM, AND SAYS SO, ALWAYS.                ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * Orb has no connection to the ABDM gateway: no HIP callback, no HIU
 * registration, no consent-manager subscription, no sandbox credential. Every
 * button on this screen records what a person did somewhere else — on the
 * ABDM/NHA portal, in the patient's ABHA app, or on the HIP console of a system
 * that IS onboarded. The notice is rendered before the first network round trip,
 * is not dismissible, and repeats on every form that could otherwise be mistaken
 * for a transmission.
 *
 * That is not decoration. The failure it prevents is silent and lands on the
 * patient: a clerk who believes Orb linked the care context does not link it,
 * and the discharge summary never appears in the patient's health account —
 * with no error, no empty screen, and nobody the wiser for months.
 *
 * WHY THE QUEUE IS THE DEFAULT VIEW. Everything else here is a record of work
 * already done. The queue is the work NOT done, which is the only part a desk
 * can act on. An ABDM story built on "we have the fields" with no queue is a
 * story about a database, not about a hospital that links its episodes.
 *
 * UNVERIFIED READS AS UNVERIFIED. An ABHA number keyed at a counter renders with
 * the words "Not verified" beside it, in a warn-toned chip, on every surface it
 * appears on — never as a bare number (which reads as fine) and never as a tick.
 * The one thing this screen must never do is make a national health id that
 * nobody confirmed look confirmed.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  CONSENT_LABEL, checkAbha, consentTone, getCareContexts, getConsents, getIdentities,
  getMeta, getQueue, getSummary, linkCareContext, queueTone, recordAbha,
  recordVerification, revokeConsent, unlinkCareContext, verificationTone,
  type AbdmMeta, type AbdmSummary, type AbhaIdentity, type CareContext,
  type ConsentArtefact, type FormatCheck, type Queue, type QueueEntry,
} from '../../api/abdm'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { formatClinicalDateTime } from '../../lib/datetime'
import './AbdmPanel.css'

/* ── The notice, duplicated on purpose ──────────────────────────────────────
 * The server sends this same sentence on every payload and the panel prefers
 * the server's copy once it has one. This literal exists so the words are on
 * screen in the FIRST PAINT — before any fetch resolves, and still there if
 * every fetch fails. The one message that must never be missing cannot be one
 * that depends on the network being up.
 */
const NOTICE =
  'Orb is not connected to the ABDM gateway. '
  + 'Nothing on this screen is transmitted to ABDM, to a health information '
  + 'exchange or to a consent manager. '
  + 'Recording an ABHA, a verification, a care-context link or a consent '
  + "artefact here records what a person did elsewhere, on the ABDM portal, in "
  + "the ABHA app, or on another system's HIP console."

const NOTICE_SHORT = 'Recorded in Orb only: nothing was sent to ABDM.'

type View = 'queue' | 'identities' | 'links' | 'consents'

/** ABDM's health-information types are CamelCase codes ("OPConsultation");
 *  the desk reads them as the record they name. The code still goes on the
 *  wire, exactly as the national stack spells it. */
const HI_TYPE_LABEL: Record<string, string> = {
  OPConsultation: 'Outpatient consultation',
  DischargeSummary: 'Discharge summary',
  Prescription: 'Prescription',
  DiagnosticReport: 'Diagnostic report',
  ImmunizationRecord: 'Immunisation record',
  HealthDocumentRecord: 'Health document',
  WellnessRecord: 'Wellness record',
}

function hiTypeLabel(code: string): string {
  return HI_TYPE_LABEL[code] ?? code
}

const VIEWS: { id: View; label: string }[] = [
  { id: 'queue', label: 'Linking queue' },
  { id: 'identities', label: 'ABHA records' },
  { id: 'links', label: 'Care contexts' },
  { id: 'consents', label: 'Consent artefacts' },
]

/* ── Small presentational pieces ─────────────────────────────────────────── */

/** The verification state, in words, always. A bare ABHA number reads as fine;
 *  that is the whole reason this component exists and is used everywhere. */
function VerificationChip({
  status, label,
}: { status: AbhaIdentity['verification_status'] | null; label: string }) {
  return (
    <span className={`abdm-chip abdm-chip--${verificationTone(status)}`}>{label}</span>
  )
}

function ConsentChip({ artefact }: { artefact: ConsentArtefact }) {
  /* The EFFECTIVE status, not the stored one: a granted artefact whose expiry
     passed must never render as live permission. Both are shown when they
     disagree, so nobody has to wonder whether the screen is stale. */
  const effective = artefact.effective_status
  const drifted = effective !== artefact.status
  return (
    <span className="abdm-chip-group">
      <span className={`abdm-chip abdm-chip--${consentTone(effective)}`}>
        {CONSENT_LABEL[effective]}
      </span>
      {drifted && (
        <span className="abdm-chip abdm-chip--muted" title="What the stored row still says">
          stored: {CONSENT_LABEL[artefact.status]}
        </span>
      )}
    </span>
  )
}

/** An ABHA number, in card grouping, with the verification state welded to it. */
function AbhaValue({ identity }: { identity: AbhaIdentity }) {
  return (
    <span className="abdm-abha">
      {identity.abha_number_formatted ? (
        <span className="abdm-abha__number tabular-nums">{identity.abha_number_formatted}</span>
      ) : (
        <span className="abdm-abha__none">No ABHA number</span>
      )}
      {identity.abha_address && (
        <span className="abdm-abha__address">{identity.abha_address}</span>
      )}
      <VerificationChip
        status={identity.verification_status}
        label={identity.verification_label}
      />
    </span>
  )
}

function Field({ label, hint, children }: {
  label: string; hint?: string; children: React.ReactNode
}) {
  return (
    <label className="abdm-field">
      <span className="abdm-field__label">{label}</span>
      {children}
      {hint && <span className="abdm-field__hint">{hint}</span>}
    </label>
  )
}

function Age({ days, stale }: { days: number | null; stale: boolean }) {
  if (days === null) return <span className="abdm-age abdm-age--unknown">Age unknown</span>
  return (
    <span className={`abdm-age${stale ? ' abdm-age--stale' : ''}`}>
      <span className="abdm-age__n tabular-nums">{days}</span>
      <span className="abdm-age__unit">{days === 1 ? 'day' : 'days'} since admission</span>
    </span>
  )
}

/** "Kavitha Menon, MRN-CC002, CCU". Every dialog that records against a
 *  national health id names the patient the way the ward does, never by the
 *  internal id alone: a desk working down a queue is exactly where the wrong
 *  row gets clicked. The id is the last resort, for a row with no name. */
function patientLine(p: {
  patient_id: string; name?: string | null; mrn?: string | null; ward?: string | null
}): string {
  return [p.name || `Patient ${p.patient_id}`, p.mrn, p.ward].filter(Boolean).join(', ')
}

/* ── Modal shell: portalled to document.body ─────────────────────────────── */

function Modal({ title, subtitle, onClose, children }: {
  title: string; subtitle?: string; onClose: () => void; children: React.ReactNode
}) {
  // The shared trap: focus moves in, Tab stays in, Escape closes, focus goes
  // back to the button that opened it.
  const ref = useFocusTrap<HTMLElement>(onClose)

  return createPortal(
    <div className="abdm-modal__scrim" role="presentation" onClick={onClose}>
      <section
        ref={ref}
        className="abdm-modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={e => e.stopPropagation()}
      >
        <header className="abdm-modal__head">
          <div>
            <h2 className="abdm-modal__title">{title}</h2>
            {subtitle && <p className="abdm-modal__sub">{subtitle}</p>}
          </div>
          <button type="button" className="abdm-btn abdm-btn--quiet" onClick={onClose}>
            Close
          </button>
        </header>
        {/* Repeated inside every action form, not only at the top of the page:
            this is the moment a clerk is most likely to believe a button sends
            something. */}
        <p className="abdm-notice abdm-notice--inline">{NOTICE_SHORT}</p>
        <div className="abdm-modal__body">{children}</div>
      </section>
    </div>,
    document.body,
  )
}

/* ── Record an ABHA ──────────────────────────────────────────────────────── */

function RecordAbhaModal({ entry, meta, onClose, onSaved }: {
  entry: QueueEntry
  meta: AbdmMeta | null
  onClose: () => void
  onSaved: (identity: AbhaIdentity) => void
}) {
  const [number, setNumber] = useState('')
  const [address, setAddress] = useState('')
  const [source, setSource] = useState('counter_keyed')
  const [notes, setNotes] = useState('')
  const [check, setCheck] = useState<{ number?: FormatCheck; address?: FormatCheck } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const runCheck = useCallback(async () => {
    if (!number.trim() && !address.trim()) { setCheck(null); return }
    try {
      const reply = await checkAbha({
        ...(number.trim() ? { abha_number: number.trim() } : {}),
        ...(address.trim() ? { abha_address: address.trim() } : {}),
      })
      setCheck({ number: reply.number, address: reply.address })
    } catch {
      /* The structural check is a courtesy; its failure must not block the
         clerk from recording what the patient handed them. */
      setCheck(null)
    }
  }, [number, address])

  const save = async () => {
    setBusy(true); setError(null)
    try {
      const reply = await recordAbha({
        patient_id: entry.patient_id,
        abha_number: number.trim() || null,
        abha_address: address.trim() || null,
        source,
        notes: notes.trim() || null,
      })
      onSaved(reply.identity)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not record this ABHA.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Record an ABHA"
      subtitle={patientLine({
        patient_id: entry.patient_id, name: entry.patient_name,
        mrn: entry.patient_mrn, ward: entry.ward,
      })}
      onClose={onClose}
    >
      <p className="abdm-callout">
        What you record here is stored <strong>unverified</strong>. It stays
        unverified until someone records a verification they performed on the
        ABDM portal or in the patient&apos;s ABHA app.
      </p>
      <Field label="ABHA number" hint="14 digits. Hyphens and spaces are fine.">
        <input
          className="abdm-input tabular-nums"
          inputMode="numeric"
          value={number}
          placeholder="12-3456-7890-1234"
          onChange={e => setNumber(e.target.value)}
          onBlur={runCheck}
        />
      </Field>
      {check?.number && (
        <p className={`abdm-check abdm-check--${check.number.valid ? 'ok' : 'bad'}`}>
          {check.number.valid
            ? `Reads as ${check.number.formatted}. ${check.number.note}`
            : check.number.reason}
        </p>
      )}
      <Field label="ABHA address" hint="For example hina.patel@abdm">
        <input
          className="abdm-input"
          value={address}
          placeholder="username@abdm"
          onChange={e => setAddress(e.target.value)}
          onBlur={runCheck}
        />
      </Field>
      {check?.address && (
        <p className={`abdm-check abdm-check--${check.address.valid ? 'ok' : 'bad'}`}>
          {check.address.valid
            ? `Reads as ${check.address.normalised}. ${check.address.note}`
            : check.address.reason}
        </p>
      )}
      <Field label="Where it came from">
        <select className="abdm-input" value={source} onChange={e => setSource(e.target.value)}>
          {(meta?.identity_sources ?? [{ code: 'counter_keyed', label: 'Keyed at the counter' }])
            .map(s => <option key={s.code} value={s.code}>{s.label}</option>)}
        </select>
      </Field>
      <Field label="Note (optional)">
        <input className="abdm-input" value={notes} onChange={e => setNotes(e.target.value)} />
      </Field>
      {error && <p className="abdm-error">{error}</p>}
      <div className="abdm-actions">
        <button
          type="button"
          className="abdm-btn"
          disabled={busy || (!number.trim() && !address.trim())}
          onClick={save}
        >
          {busy ? 'Recording…' : 'Record as unverified'}
        </button>
      </div>
    </Modal>
  )
}

/* ── Record a verification ───────────────────────────────────────────────── */

function VerificationModal({ identity, meta, onClose, onSaved }: {
  identity: AbhaIdentity
  meta: AbdmMeta | null
  onClose: () => void
  onSaved: (identity: AbhaIdentity) => void
}) {
  const [method, setMethod] = useState('aadhaar_otp')
  const [outcome, setOutcome] = useState<'verified' | 'failed' | 'inconclusive'>('verified')
  const [performedBy, setPerformedBy] = useState('')
  const [performedWhere, setPerformedWhere] = useState('abdm_portal')
  const [performedAt, setPerformedAt] = useState('')
  const [reference, setReference] = useState('')
  const [failureReason, setFailureReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setBusy(true); setError(null)
    try {
      const reply = await recordVerification(identity.id, {
        method,
        outcome,
        performed_by: performedBy.trim(),
        // The picker gives local wall-clock time; the server reads a bare
        // timestamp as UTC, so convert here or the record is 5h30 out.
        performed_at: performedAt ? new Date(performedAt).toISOString() : null,
        performed_where: performedWhere,
        reference: reference.trim() || null,
        failure_reason: failureReason.trim() || null,
      })
      onSaved(reply.identity)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not record this verification.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Record a verification"
      subtitle={`${patientLine({
        patient_id: identity.patient_id, name: identity.patient_name,
        mrn: identity.patient_mrn, ward: identity.patient_ward,
      })} · ABHA ${identity.abha_number_formatted ?? identity.abha_address ?? `record #${identity.id}`}`}
      onClose={onClose}
    >
      <p className="abdm-callout">
        This records a verification <strong>someone already performed elsewhere</strong>.
        Orb cannot verify an ABHA. It has no connection to ABDM. Enter who did
        it, when, and by what method, exactly as it happened.
      </p>
      <Field label="Method">
        <select className="abdm-input" value={method} onChange={e => setMethod(e.target.value)}>
          {(meta?.verification_methods ?? []).map(m => (
            <option key={m.code} value={m.code}>{m.label}</option>
          ))}
        </select>
      </Field>
      <Field label="Outcome">
        <select
          className="abdm-input"
          value={outcome}
          onChange={e => setOutcome(e.target.value as typeof outcome)}
        >
          <option value="verified">Verified</option>
          <option value="failed">Failed</option>
          <option value="inconclusive">Inconclusive, proved nothing</option>
        </select>
      </Field>
      <Field label="Who performed it" hint="The person, not the software.">
        <input
          className="abdm-input"
          value={performedBy}
          onChange={e => setPerformedBy(e.target.value)}
          placeholder="Employee id or name"
        />
      </Field>
      <Field label="Where" hint="Every one of these is outside Orb.">
        <select
          className="abdm-input"
          value={performedWhere}
          onChange={e => setPerformedWhere(e.target.value)}
        >
          {(meta?.verification_places ?? []).map(p => (
            <option key={p.code} value={p.code}>{p.label}</option>
          ))}
        </select>
      </Field>
      <Field label="When (optional)" hint="Leave blank if it was just now.">
        <input
          className="abdm-input"
          type="datetime-local"
          value={performedAt}
          onChange={e => setPerformedAt(e.target.value)}
        />
      </Field>
      <Field label="Reference from the portal (optional)">
        <input
          className="abdm-input"
          value={reference}
          onChange={e => setReference(e.target.value)}
          placeholder="Transaction id you copied"
        />
      </Field>
      {outcome === 'failed' && (
        <Field label="Why it failed" hint="Required: a failure with no reason explains nothing.">
          <input
            className="abdm-input"
            value={failureReason}
            onChange={e => setFailureReason(e.target.value)}
          />
        </Field>
      )}
      {error && <p className="abdm-error">{error}</p>}
      <div className="abdm-actions">
        <button
          type="button"
          className="abdm-btn"
          disabled={busy || !performedBy.trim()}
          onClick={save}
        >
          {busy ? 'Recording…' : 'Record this verification'}
        </button>
      </div>
    </Modal>
  )
}

/* ── Record a care-context link ──────────────────────────────────────────── */

function LinkModal({ entry, identity, meta, onClose, onSaved }: {
  entry: QueueEntry
  identity: AbhaIdentity | null
  meta: AbdmMeta | null
  onClose: () => void
  onSaved: () => void
}) {
  const [hiType, setHiType] = useState('OPConsultation')
  const [linkedBy, setLinkedBy] = useState('')
  const [linkMethod, setLinkMethod] = useState('abdm_portal')
  const [reference, setReference] = useState('')
  const [display, setDisplay] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    if (!identity) return
    setBusy(true); setError(null)
    try {
      await linkCareContext({
        identity_id: identity.id,
        care_context_reference: entry.encounter_ref,
        hi_type: hiType,
        display: display.trim() || null,
        encounter_type: 'admission',
        encounter_ref: entry.encounter_ref,
        linked_by: linkedBy.trim(),
        link_method: linkMethod,
        link_reference: reference.trim() || null,
      })
      onSaved()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not record this link.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Record a care-context link"
      subtitle={`${patientLine({
        patient_id: entry.patient_id, name: entry.patient_name,
        mrn: entry.patient_mrn, ward: entry.ward,
      })} · ${entry.encounter_ref}`}
      onClose={onClose}
    >
      <p className="abdm-callout">
        Record this <strong>after</strong> you have linked the episode on the
        portal or HIP console. Orb does not perform the link.
      </p>
      {identity && !identity.is_verified && (
        <p className="abdm-callout abdm-callout--warn">
          This ABHA is {identity.verification_label.toLowerCase()}. Record the
          verification first. Linking a mistyped number puts this patient&apos;s
          record into somebody else&apos;s health account, and that cannot be
          undone.
        </p>
      )}
      <Field label="Health-information type">
        <select className="abdm-input" value={hiType} onChange={e => setHiType(e.target.value)}>
          {(meta?.hi_types ?? ['OPConsultation']).map(t => (
            <option key={t} value={t}>{hiTypeLabel(t)}</option>
          ))}
        </select>
      </Field>
      <Field label="Label the patient will see (optional)">
        <input className="abdm-input" value={display} onChange={e => setDisplay(e.target.value)} />
      </Field>
      <Field label="Who performed the link">
        <input
          className="abdm-input"
          value={linkedBy}
          onChange={e => setLinkedBy(e.target.value)}
          placeholder="Employee id or name"
        />
      </Field>
      <Field label="Where">
        <select
          className="abdm-input"
          value={linkMethod}
          onChange={e => setLinkMethod(e.target.value)}
        >
          {(meta?.link_methods ?? []).map(m => (
            <option key={m.code} value={m.code}>{m.label}</option>
          ))}
        </select>
      </Field>
      <Field label="Reference from the portal (optional)">
        <input
          className="abdm-input"
          value={reference}
          onChange={e => setReference(e.target.value)}
        />
      </Field>
      {error && <p className="abdm-error">{error}</p>}
      <div className="abdm-actions">
        <button
          type="button"
          className="abdm-btn"
          disabled={busy || !identity || !identity.is_verified || !linkedBy.trim()}
          onClick={save}
        >
          {busy ? 'Recording…' : 'Record this link'}
        </button>
      </div>
    </Modal>
  )
}

/* ── Reason-required modal (unlink / revoke) ─────────────────────────────── */

function ReasonModal({ title, subtitle, warning, confirmLabel, onClose, onConfirm }: {
  title: string
  subtitle: string
  warning: string
  confirmLabel: string
  onClose: () => void
  onConfirm: (reason: string) => Promise<void>
}) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setBusy(true); setError(null)
    try {
      await onConfirm(reason.trim())
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not record this.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={title} subtitle={subtitle} onClose={onClose}>
      <p className="abdm-callout abdm-callout--warn">{warning}</p>
      <Field
        label="Reason"
        hint="Required. This is the only thing that explains this row to whoever reads it next."
      >
        <textarea
          className="abdm-input abdm-input--area"
          rows={3}
          value={reason}
          onChange={e => setReason(e.target.value)}
        />
      </Field>
      {error && <p className="abdm-error">{error}</p>}
      <div className="abdm-actions">
        <button
          type="button"
          className="abdm-btn abdm-btn--danger"
          disabled={busy || reason.trim().length < 3}
          onClick={run}
        >
          {busy ? 'Recording…' : confirmLabel}
        </button>
      </div>
    </Modal>
  )
}

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function AbdmPanel() {
  const [view, setView] = useState<View>('queue')
  const [meta, setMeta] = useState<AbdmMeta | null>(null)
  const [summary, setSummary] = useState<AbdmSummary | null>(null)
  const [queue, setQueue] = useState<Queue | null>(null)
  const [identities, setIdentities] = useState<AbhaIdentity[]>([])
  const [contexts, setContexts] = useState<CareContext[]>([])
  const [consents, setConsents] = useState<ConsentArtefact[]>([])
  const [bucket, setBucket] = useState<QueueEntry['bucket'] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  /* Server copy of the notice, once we have one. The literal above is the
     first-paint fallback and the offline fallback; they are the same words. */
  const notice = queue?.notice ?? summary?.notice ?? meta?.notice ?? NOTICE

  // The whole queue row, not just its patient id, so the dialog can name the
  // patient by name, MRN and ward.
  const [recordFor, setRecordFor] = useState<QueueEntry | null>(null)
  const [verifyFor, setVerifyFor] = useState<AbhaIdentity | null>(null)
  const [linkFor, setLinkFor] = useState<QueueEntry | null>(null)
  const [unlinkFor, setUnlinkFor] = useState<CareContext | null>(null)
  const [revokeFor, setRevokeFor] = useState<ConsentArtefact | null>(null)

  /* Deliberately touches NO state before its first await — same rule as
     PayerPanel. The mount effect calls this, and a setState in an effect BODY is
     a cascading render (and, if the panel unmounted mid-flight, a write into a
     dead tree). `loading` already starts true, so the mount path needs no
     pre-flight flag; the Refresh button raises it itself, from an event handler,
     where it is free to. */
  const refresh = useCallback(async () => {
    try {
      const [q, s, ids, ccs, cons] = await Promise.all([
        getQueue({ limit: 200 }),
        getSummary(),
        getIdentities({ limit: 200 }),
        getCareContexts({ limit: 200 }),
        getConsents({ limit: 200 }),
      ])
      setQueue(q); setSummary(s)
      setIdentities(ids.identities)
      setContexts(ccs.care_contexts)
      setConsents(cons.consents)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the ABDM desk.')
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => { getMeta().then(setMeta).catch(() => setMeta(null)) }, [])

  const identityFor = useCallback(
    (id: number | null) => identities.find(i => i.id === id) ?? null,
    [identities],
  )

  const visibleQueue = useMemo(
    () => (queue?.encounters ?? []).filter(e => !bucket || e.bucket === bucket),
    [queue, bucket],
  )

  return (
    <div className="abdm-panel">
      <header className="abdm-head">
        <div>
          <h1 className="abdm-title">ABDM / ABHA</h1>
          <p className="abdm-sub">
            The hospital&apos;s record of ABDM work. Orb records; people do the work
            on the national stack.
          </p>
        </div>
        <button
          type="button"
          className="abdm-btn abdm-btn--quiet"
          /* The spinner is raised HERE, in the event handler, not inside
             refresh() — see the note on refresh(). */
          onClick={() => { setLoading(true); void refresh() }}
        >
          Refresh
        </button>
      </header>

      {/* Undismissable, above everything, rendered before any fetch resolves. */}
      <p className="abdm-notice" role="note">{notice}</p>

      {summary && (
        <div className="abdm-metrics">
          <div className="abdm-metric">
            <span className="abdm-metric__value tabular-nums">
              {summary.identities.verified_of_total}
            </span>
            <span className="abdm-metric__label">ABHA records verified</span>
          </div>
          <div className="abdm-metric">
            {/* A tone only when the count is not zero: an amber "0" read as a
                warning about nothing. */}
            <span className={`abdm-metric__value${summary.identities.unverified > 0 ? ' abdm-metric__value--warn' : ''} tabular-nums`}>
              {summary.identities.unverified}
            </span>
            <span className="abdm-metric__label">Unverified</span>
          </div>
          <div className="abdm-metric">
            <span className="abdm-metric__value tabular-nums">
              {summary.care_contexts.linked}
            </span>
            <span className="abdm-metric__label">Care contexts linked</span>
          </div>
          <div className="abdm-metric">
            <span className={`abdm-metric__value${summary.queue.total > 0 ? ' abdm-metric__value--danger' : ''} tabular-nums`}>
              {summary.queue.total}
            </span>
            <span className="abdm-metric__label">Encounters not linked</span>
          </div>
        </div>
      )}

      <nav className="abdm-views" aria-label="ABDM views">
        {VIEWS.map(v => (
          <button
            key={v.id}
            type="button"
            className={`abdm-view${view === v.id ? ' abdm-view--on' : ''}`}
            aria-pressed={view === v.id}
            onClick={() => setView(v.id)}
          >
            {v.label}
          </button>
        ))}
      </nav>

      {error && <p className="abdm-error">{error}</p>}
      {loading && <p className="abdm-empty">Loading…</p>}

      {view === 'queue' && queue && (
        <section className="abdm-section">
          <div className="abdm-buckets">
            <button
              type="button"
              className={`abdm-bucket${bucket === null ? ' abdm-bucket--on' : ''}`}
              onClick={() => setBucket(null)}
            >
              <span className="abdm-bucket__n tabular-nums">{queue.total}</span>
              <span className="abdm-bucket__label">All unlinked</span>
            </button>
            {queue.buckets.map(b => (
              <button
                key={b.id}
                type="button"
                className={
                  `abdm-bucket abdm-bucket--${queueTone(b.id)}`
                  + (bucket === b.id ? ' abdm-bucket--on' : '')
                }
                onClick={() => setBucket(b.id)}
              >
                <span className="abdm-bucket__n tabular-nums">{b.count}</span>
                <span className="abdm-bucket__label">{b.label}</span>
              </button>
            ))}
          </div>

          {visibleQueue.length === 0 && (
            <p className="abdm-empty">Every encounter in view has a live care-context link.</p>
          )}

          <ul className="abdm-list">
            {visibleQueue.map(entry => (
              <li key={entry.encounter_ref} className="abdm-row">
                <div className="abdm-row__main">
                  <span className="abdm-row__name">{entry.patient_name ?? entry.patient_id}</span>
                  <span className="abdm-row__meta">
                    {entry.patient_mrn ?? '—'} · {entry.ward ?? '—'} · {entry.encounter_ref}
                  </span>
                </div>
                <div className="abdm-row__state">
                  <span className={`abdm-chip abdm-chip--${queueTone(entry.bucket)}`}>
                    {entry.bucket_label}
                  </span>
                  {entry.abha_number_formatted && (
                    <span className="abdm-row__abha tabular-nums">
                      {entry.abha_number_formatted}
                    </span>
                  )}
                  <Age days={entry.age_days} stale={entry.is_stale} />
                </div>
                <div className="abdm-row__actions">
                  {entry.bucket === 'no_abha' && (
                    <button
                      type="button"
                      className="abdm-btn abdm-btn--quiet"
                      onClick={() => setRecordFor(entry)}
                    >
                      Record ABHA
                    </button>
                  )}
                  {entry.bucket === 'abha_unverified' && entry.abha_identity_id && (
                    <button
                      type="button"
                      className="abdm-btn abdm-btn--quiet"
                      onClick={() => setVerifyFor(identityFor(entry.abha_identity_id))}
                      disabled={!identityFor(entry.abha_identity_id)}
                    >
                      Record verification
                    </button>
                  )}
                  {entry.bucket === 'ready_to_link' && (
                    <button
                      type="button"
                      className="abdm-btn"
                      onClick={() => setLinkFor(entry)}
                    >
                      Record link
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {view === 'identities' && (
        <section className="abdm-section">
          {identities.length === 0 && <p className="abdm-empty">No ABHA records yet.</p>}
          <ul className="abdm-list">
            {identities.map(identity => (
              <li key={identity.id} className="abdm-row">
                <div className="abdm-row__main">
                  <span className="abdm-row__name">
                    {identity.patient_name ?? identity.patient_id}
                  </span>
                  <span className="abdm-row__meta">
                    {identity.patient_mrn ?? '—'} · recorded {identity.created_at ? formatClinicalDateTime(identity.created_at) : '—'}
                  </span>
                </div>
                <div className="abdm-row__state"><AbhaValue identity={identity} /></div>
                <div className="abdm-row__actions">
                  <button
                    type="button"
                    className="abdm-btn abdm-btn--quiet"
                    onClick={() => setVerifyFor(identity)}
                  >
                    Record verification
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {view === 'links' && (
        <section className="abdm-section">
          {contexts.length === 0 && <p className="abdm-empty">No care contexts recorded yet.</p>}
          <ul className="abdm-list">
            {contexts.map(cc => (
              <li key={cc.id} className="abdm-row">
                <div className="abdm-row__main">
                  <span className="abdm-row__name">
                    {cc.display || cc.care_context_reference}
                  </span>
                  <span className="abdm-row__meta">
                    {hiTypeLabel(cc.hi_type)} · {cc.patient_name ?? cc.patient_id} · {cc.link_method_label}
                  </span>
                </div>
                <div className="abdm-row__state">
                  <span className={`abdm-chip abdm-chip--${cc.is_linked ? 'ok' : 'muted'}`}>
                    {cc.is_linked ? 'Linked' : 'Unlinked'}
                  </span>
                  {/* An unlink is never a deletion, so the row keeps saying who
                      unlinked it and why. */}
                  {!cc.is_linked && (
                    <span className="abdm-row__reason">
                      {cc.unlinked_by} · {cc.unlink_reason}
                    </span>
                  )}
                </div>
                <div className="abdm-row__actions">
                  {cc.is_linked && (
                    <button
                      type="button"
                      className="abdm-btn abdm-btn--danger"
                      onClick={() => setUnlinkFor(cc)}
                    >
                      Record unlink
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {view === 'consents' && (
        <section className="abdm-section">
          {consents.length === 0 && <p className="abdm-empty">No consent artefacts recorded yet.</p>}
          <ul className="abdm-list">
            {consents.map(artefact => (
              <li key={artefact.id} className="abdm-row">
                <div className="abdm-row__main">
                  <span className="abdm-row__name">{artefact.requester_name}</span>
                  <span className="abdm-row__meta">
                    {artefact.purpose_label} · {artefact.patient_name ?? artefact.patient_id}
                    {artefact.hi_types_recorded
                      ? ` · ${artefact.hi_types.map(hiTypeLabel).join(', ')}`
                      : ' · scope not recorded'}
                  </span>
                </div>
                <div className="abdm-row__state">
                  <ConsentChip artefact={artefact} />
                  {/* expires_at_display is the UTC clock with no zone; render the
                      instant in the viewer's time instead. */}
                  <span className="abdm-row__meta">
                    {artefact.expires_at
                      ? `Expires ${formatClinicalDateTime(artefact.expires_at)}`
                      : artefact.expires_at_display}
                  </span>
                  {artefact.is_revoked && (
                    <span className="abdm-row__reason">
                      Revoked by {artefact.revoked_by} · {artefact.revocation_reason}
                    </span>
                  )}
                </div>
                <div className="abdm-row__actions">
                  {artefact.status === 'granted' && (
                    <button
                      type="button"
                      className="abdm-btn abdm-btn--danger"
                      onClick={() => setRevokeFor(artefact)}
                    >
                      Record revocation
                    </button>
                  )}
                  {/* No "reinstate" button exists, deliberately: a revoked
                      artefact is replaced by a NEW one from the consent
                      manager, never edited back to granted. The desk has no
                      control to record a new artefact yet, so the note does
                      not tell anyone to. */}
                  {artefact.is_revoked && (
                    <span className="abdm-row__note">
                      Revoked consents cannot be reinstated. A fresh consent is a
                      new artefact from the consent manager, and this screen
                      cannot record one yet.
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {recordFor && (
        <RecordAbhaModal
          entry={recordFor}
          meta={meta}
          onClose={() => setRecordFor(null)}
          onSaved={() => { void refresh() }}
        />
      )}
      {verifyFor && (
        <VerificationModal
          identity={verifyFor}
          meta={meta}
          onClose={() => setVerifyFor(null)}
          onSaved={() => { void refresh() }}
        />
      )}
      {linkFor && (
        <LinkModal
          entry={linkFor}
          identity={identityFor(linkFor.abha_identity_id)}
          meta={meta}
          onClose={() => setLinkFor(null)}
          onSaved={() => { void refresh() }}
        />
      )}
      {unlinkFor && (
        <ReasonModal
          title="Record an unlink"
          subtitle={unlinkFor.display || unlinkFor.care_context_reference}
          warning={
            'The record is kept, not deleted. This care context will still show '
            + 'that it was linked, by whom, and that you unlinked it.'
          }
          confirmLabel="Record this unlink"
          onClose={() => setUnlinkFor(null)}
          onConfirm={async reason => {
            await unlinkCareContext(unlinkFor.id, reason)
            await refresh()
          }}
        />
      )}
      {revokeFor && (
        <ReasonModal
          title="Record a revocation"
          subtitle={`${revokeFor.requester_name} · ${revokeFor.purpose_label}`}
          warning={
            'This cannot be undone. A revoked consent can never be granted again '
            + 'in Orb. If the patient consents afresh, the consent manager issues '
            + 'a NEW artefact, and this screen cannot record one yet.'
          }
          confirmLabel="Record this revocation"
          onClose={() => setRevokeFor(null)}
          onConfirm={async reason => {
            await revokeConsent(revokeFor.id, reason)
            await refresh()
          }}
        />
      )}
    </div>
  )
}
