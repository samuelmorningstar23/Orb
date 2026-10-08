/**
 * Beds, Bed setup view (adt-4b): the bed register a bed manager keeps.
 *
 * Only a bed manager (bed.master) sees this view. What it holds:
 *
 *   Wards        code, display name, department, ward type and whether
 *                housekeeping tracks the ward's cleaning. The department is
 *                one a doctor or nurse covers, or one the site configuration
 *                declares; a ward in a department nobody covers is flagged in
 *                the server's words (admissions are refused there until
 *                somebody covers it). 'Import wards from the site
 *                configuration' sets up the wards the file declares and is
 *                safe to repeat.
 *   Rooms        bays and rooms on a ward; an isolation room makes its beds
 *                isolation beds, which is what sends a terminal clean.
 *   Beds         registered in bulk, from a list of labels or a numbered
 *                range. The preview shows every unit code ('<ward>/<bed>')
 *                BEFORE anything is saved, and the save is one transaction on
 *                the server: a clash on one bed saves none of them.
 *   Bed classes  general, private, ICU and so on, with the bed-day charge code.
 *   Reconciling  the active admissions on a ward set against its beds: a
 *                typed bed that names exactly one bed in setup can be linked;
 *                a bed claimed by two admissions is listed with no link,
 *                because only the ward can say who lies where.
 *   Register complete  refused while an admission on the ward is not linked
 *                or a bed is claimed twice; the refusal is shown with the
 *                admissions and beds it names.
 *
 * EDITS. A display name can always change. A ward code, room code or bed
 * label cannot change once anything refers to it: the server refuses and its
 * words are shown as sent. Nothing on this screen names a patient; the
 * reconciliation lists admission and patient ids only, and is read only when
 * asked for because the server audits every read of it.
 */
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'

import { toAdtError, type AdtApiError } from '../../../api/adt'
import { linkAdmissionsToBeds } from '../../../api/adt/board'
import {
  createBedClass,
  createRoom,
  createWard,
  getBedMaster,
  getReconciliation,
  importSiteConfigWards,
  markRegisterComplete,
  previewBeds,
  registerBeds,
  retireBed,
  updateBed,
  updateBedClass,
  updateRoom,
  updateWard,
  type BedMaster,
  type BedPreview,
  type MasterBed,
  type MasterWard,
  type Reconciliation,
  type ReconciliationEntry,
  type SiteConfigImport,
  type WardChange,
} from '../../../api/adt/master'
import { STATE_LABEL, type UnitState } from '../../../api/housekeeping'
import { formatClinicalDateTime } from '../../../lib/datetime'
import type { AdtViewProps } from './viewRegistry'
import './SetupView.css'

export const viewId = 'setup'
export const viewLabel = 'Bed setup'
export const viewOrder = 90
export const viewPermission = 'bed.master'

// ── Words ────────────────────────────────────────────────────────────────────

const WARD_TYPE_WORDS: Record<string, string> = {
  general: 'General', icu: 'ICU', hdu: 'HDU', nicu: 'NICU', picu: 'PICU',
  maternity: 'Maternity', daycare: 'Day care', emergency: 'Emergency',
  isolation: 'Isolation', other: 'Other',
}

const ROOM_TYPE_WORDS: Record<string, string> = {
  bay: 'Bay', private: 'Private room', semi_private: 'Semi-private room',
  isolation: 'Isolation room', icu_cubicle: 'ICU cubicle', other: 'Other',
}

const RECONCILE_REASON_WORDS: Record<string, string> = {
  awaiting_bed: 'No bed recorded',
  ward_not_in_setup: 'The typed ward is not in bed setup',
  bed_not_in_setup: 'The typed bed is not in bed setup',
  bed_retired: 'The typed bed is retired',
  doubled_bed: 'Another admission claims the same bed',
}

function plainWords(map: Record<string, string>, value: string | null | undefined): string {
  if (!value) return 'Not set'
  const known = map[value]
  if (known) return known
  const text = value.replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function cleaningWords(state: string | null): string {
  if (!state) return 'Cleaning not recorded'
  return STATE_LABEL[state as UnitState] ?? plainWords({}, state)
}

/** The server's refusal as a sentence, or null when the module went off (the
 *  panel then shows its own notice). */
function refusalOf(err: unknown, onModuleOff: () => void): AdtApiError | null {
  const failure = toAdtError(err)
  if (failure.isModuleDisabled) {
    onModuleOff()
    return null
  }
  return failure
}

/** A refusal of a write the server makes in one transaction: when the server
 *  answered (a 4xx), nothing was saved, and the words say so. */
function nothingSaved(failure: AdtApiError): string {
  const answered = failure.status >= 400 && failure.status < 500
  if (!answered || /nothing was (saved|changed)/i.test(failure.message)) return failure.message
  return `${failure.message.replace(/\.?\s*$/, '.')} Nothing was saved.`
}

/** '1 bed', '2 beds'. */
function counted(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

function departmentChoices(master: BedMaster): { name: string; covered: boolean }[] {
  const out = master.departments.covering.map((name) => ({ name, covered: true }))
  for (const name of master.departments.declared) {
    if (!out.some((o) => o.name.toLowerCase() === name.toLowerCase())) {
      out.push({ name, covered: false })
    }
  }
  return out
}

function DepartmentOptions({ master }: { master: BedMaster }) {
  return (
    <>
      {departmentChoices(master).map((d) => (
        <option key={d.name} value={d.name}>
          {d.covered ? d.name : `${d.name} (no doctor or nurse covers it yet)`}
        </option>
      ))}
    </>
  )
}

function Refusal({ text }: { text: string | null }) {
  if (!text) return null
  return <p className="bs-refusal" role="alert">{text}</p>
}

// ── The view ─────────────────────────────────────────────────────────────────

type Load =
  | { state: 'loading' }
  | { state: 'ready'; master: BedMaster }
  | { state: 'error'; message: string }

export default function SetupView({ onModuleOff }: AdtViewProps) {
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [wardId, setWardId] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<string | null>(null)

  // State changes only in the reply's callbacks, never synchronously.
  const refresh = useCallback((): Promise<void> => getBedMaster().then(
    (master) => { setLoad({ state: 'ready', master }) },
    (err: unknown) => {
      const failure = refusalOf(err, onModuleOff)
      if (!failure) return
      setLoad({
        state: 'error',
        message: failure.isForbidden ? 'Your role cannot open bed setup.' : failure.message,
      })
    },
  ), [onModuleOff])

  useEffect(() => { void refresh() }, [refresh])

  const done = useCallback(async (words: string) => {
    setOutcome(words)
    await refresh()
  }, [refresh])

  if (load.state === 'loading') {
    return <p className="bb-status" role="status">Loading bed setup</p>
  }
  if (load.state === 'error') {
    return (
      <section className="bb-notice bb-notice--error" role="alert">
        <p className="bb-notice__body">{load.message}</p>
        <button type="button" className="bb-btn" onClick={() => { void refresh() }}>Try again</button>
      </section>
    )
  }

  const { master } = load
  const ward = master.wards.find((w) => w.id === wardId) ?? null

  return (
    <div className="bs">
      {outcome && <p className="bb-note bs-outcome" role="status">{outcome}</p>}

      <WardsSection
        master={master}
        openWard={(id) => { setWardId(id); setOutcome(null) }}
        openedId={ward?.id ?? null}
        onModuleOff={onModuleOff}
        onDone={done}
      />

      {ward && (
        <WardDetail
          key={ward.id}
          master={master}
          ward={ward}
          onModuleOff={onModuleOff}
          onDone={done}
          onClose={() => setWardId(null)}
        />
      )}

      <BedClassesSection master={master} onModuleOff={onModuleOff} onDone={done} />

      {master.housekeeping_beds_not_in_setup.length > 0 && (
        <section className="bb-list" aria-labelledby="bs-legacy-title">
          <h2 id="bs-legacy-title" className="bb-list__title">
            Beds on the housekeeping register, not in bed setup{' '}
            <span className="bb-list__n">{master.housekeeping_beds_not_in_setup.length}</span>
          </h2>
          <p className="bb-note">
            To bring one into bed setup, register a bed with the same label on its ward and
            choose to adopt it. It keeps its cleaning record.
          </p>
          <ul className="bb-rows">
            {master.housekeeping_beds_not_in_setup.map((u) => (
              <li key={u.id} className="bb-row">
                <div className="bb-row__main">
                  <span className="bb-row__name">{u.unit_code}</span>
                  <span className="bb-row__dept">
                    {[u.ward, u.bed_label ? `bed ${u.bed_label}` : null].filter(Boolean).join(', ')}
                  </span>
                </div>
                <div className="bb-row__meta"><span>{cleaningWords(u.state)}</span></div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

interface SectionProps {
  master: BedMaster
  onModuleOff: () => void
  onDone: (words: string) => Promise<void>
}

// ── Wards ────────────────────────────────────────────────────────────────────

function WardsSection({ master, openWard, openedId, onModuleOff, onDone }: SectionProps & {
  openWard: (id: string) => void
  openedId: string | null
}) {
  const [adding, setAdding] = useState(false)
  const [importing, setImporting] = useState(false)
  const [imported, setImported] = useState<SiteConfigImport | null>(null)
  const [importRefusal, setImportRefusal] = useState<string | null>(null)

  const runImport = async () => {
    setImporting(true)
    setImportRefusal(null)
    try {
      const result = await importSiteConfigWards()
      setImported(result)
      await onDone(result.message
        ?? `${counted(result.created.length, 'ward')} set up from the site configuration.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setImportRefusal(nothingSaved(failure))
    } finally {
      setImporting(false)
    }
  }

  return (
    <section className="bb-list" aria-labelledby="bs-wards-title">
      <div className="bs-head">
        <h2 id="bs-wards-title" className="bb-list__title">
          Wards <span className="bb-list__n">{master.wards.length}</span>
        </h2>
        <div className="bs-actions">
          <button type="button" className="bb-btn bb-btn--small" disabled={importing}
            onClick={() => { void runImport() }}>
            {importing ? 'Importing' : 'Import wards from the site configuration'}
          </button>
          <button type="button" className="bb-btn bb-btn--small" aria-expanded={adding}
            onClick={() => setAdding((on) => !on)}>
            Set up a ward
          </button>
        </div>
      </div>

      <Refusal text={importRefusal} />
      {imported && <ImportResult result={imported} />}

      {adding && (
        <NewWardForm
          master={master}
          onModuleOff={onModuleOff}
          onCreated={async (ward) => {
            setAdding(false)
            await onDone(`Ward ${ward.code} set up in ${ward.department}.`)
            openWard(ward.id)
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      {master.wards.length === 0 ? (
        <p className="bb-note">No ward is set up yet. Set one up, or import the wards the site
          configuration declares.</p>
      ) : (
        <ul className="bb-rows">
          {master.wards.map((w) => (
            <li key={w.id} className={`bb-row${w.id === openedId ? ' bs-row--open' : ''}`}>
              <div className="bb-row__main">
                <span className="bb-row__name">{w.code}</span>
                {w.name && w.name !== w.code && <span className="bb-row__dept">{w.name}</span>}
                <span className="bb-row__dept">{w.department}</span>
              </div>
              <div className="bb-row__meta">
                <span>{plainWords(WARD_TYPE_WORDS, w.ward_type)}</span>
                <span>{counted(w.beds ?? 0, 'bed')}</span>
                {(w.retired_beds ?? 0) > 0 && <span>{w.retired_beds} retired</span>}
                <span>{counted(w.rooms ?? 0, 'room')}</span>
                <span>{w.cleaning_tracked ? 'Cleaning tracked' : 'Cleaning not tracked'}</span>
                <span>{w.register_complete ? 'Register complete' : 'Register not complete'}</span>
                {!w.is_active && <span>Switched off</span>}
              </div>
              {w.cover_note && <p className="bb-note bb-note--warn">{w.cover_note}</p>}
              <div className="bs-row-actions">
                <button type="button" className="bb-btn bb-btn--small"
                  aria-label={`Open ward ${w.code}`} onClick={() => openWard(w.id)}>
                  Open
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function ImportResult({ result }: { result: SiteConfigImport }) {
  return (
    <div className="bb-note bs-box" role="status">
      {result.message ? (
        <p className="bs-line">{result.message}</p>
      ) : (
        <p className="bs-line">
          {counted(result.created.length, 'ward')} set up, {result.existing.length} already set up.
        </p>
      )}
      {result.conflicts.map((c) => (
        <p key={c.ward_id} className="bs-line">
          Ward {c.code} is set up under {c.department_in_setup ?? 'no department'}, but the site
          configuration puts it under {c.department_in_file ?? 'no department'}. It was left as it is.
        </p>
      ))}
      {result.skipped.map((s) => (
        <p key={`${s.department}-${s.code}`} className="bs-line">
          {s.code} ({s.department}) was not set up: {s.reason}
        </p>
      ))}
    </div>
  )
}

function NewWardForm({ master, onModuleOff, onCreated, onCancel }: {
  master: BedMaster
  onModuleOff: () => void
  onCreated: (ward: MasterWard) => Promise<void>
  onCancel: () => void
}) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [department, setDepartment] = useState('')
  const [wardType, setWardType] = useState('general')
  const [bedClass, setBedClass] = useState('')
  const [tracked, setTracked] = useState(true)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setRefusal(null)
    try {
      const ward = await createWard({
        code: code.trim(), department, name: name.trim() || undefined, ward_type: wardType,
        default_bed_class: bedClass || undefined, cleaning_tracked: tracked,
      })
      await onCreated(ward)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal(nothingSaved(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="bs-form" aria-label="Set up a ward" onSubmit={(e) => { void submit(e) }}>
      <label className="bb-field">
        <span className="bb-field__label">Ward code</span>
        <input className="bb-input" value={code} onChange={(e) => setCode(e.target.value)} />
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Display name</span>
        <input className="bb-input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Department</span>
        <select className="bb-select" value={department} onChange={(e) => setDepartment(e.target.value)}>
          <option value="">Choose...</option>
          <DepartmentOptions master={master} />
        </select>
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Ward type</span>
        <select className="bb-select" value={wardType} onChange={(e) => setWardType(e.target.value)}>
          {master.ward_types.map((t) => <option key={t} value={t}>{plainWords(WARD_TYPE_WORDS, t)}</option>)}
        </select>
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Default bed class</span>
        <select className="bb-select" value={bedClass} onChange={(e) => setBedClass(e.target.value)}>
          <option value="">None</option>
          {master.bed_classes.filter((c) => c.is_active).map((c) => (
            <option key={c.code} value={c.code}>{c.name} ({c.code})</option>
          ))}
        </select>
      </label>
      <label className="bs-check">
        <input type="checkbox" checked={tracked} onChange={(e) => setTracked(e.target.checked)} />
        <span>Housekeeping tracks this ward's cleaning</span>
      </label>
      <p className="bb-note bs-wide">
        The ward code is what every screen prints for the ward, and it cannot change once a bed,
        a room or a patient refers to it. The display name can always change.
      </p>
      <Refusal text={refusal} />
      <div className="bs-form-actions">
        <button type="submit" className="bb-btn" disabled={busy || !code.trim() || !department}>
          Save the ward
        </button>
        <button type="button" className="bb-btn bb-btn--quiet" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  )
}

// ── One ward ─────────────────────────────────────────────────────────────────

function WardDetail({ master, ward, onModuleOff, onDone, onClose }: SectionProps & {
  ward: MasterWard
  onClose: () => void
}) {
  const beds = master.beds.filter((b) => b.ward_id === ward.id)
  const rooms = master.rooms.filter((r) => r.ward_id === ward.id)
  return (
    <section className="bb-list bs-ward" aria-labelledby="bs-ward-title">
      <div className="bs-head">
        <h2 id="bs-ward-title" className="bb-list__title">Ward {ward.code}</h2>
        <button type="button" className="bb-btn bb-btn--small bb-btn--quiet" onClick={onClose}>
          Close ward {ward.code}
        </button>
      </div>
      {ward.cover_note && <p className="bb-note bb-note--warn">{ward.cover_note}</p>}
      <WardDetailsForm master={master} ward={ward} onModuleOff={onModuleOff} onDone={onDone} />
      <RoomsBlock master={master} ward={ward} onModuleOff={onModuleOff} onDone={onDone} />
      <BedsBlock master={master} ward={ward} beds={beds} rooms={rooms}
        onModuleOff={onModuleOff} onDone={onDone} />
      <RegisterBedsForm master={master} ward={ward} onModuleOff={onModuleOff} onDone={onDone} />
      <ReconcileBlock ward={ward} onModuleOff={onModuleOff} onDone={onDone} />
      <RegisterCompleteBlock master={master} ward={ward} onModuleOff={onModuleOff} onDone={onDone} />
    </section>
  )
}

function WardDetailsForm({ master, ward, onModuleOff, onDone }: SectionProps & { ward: MasterWard }) {
  const [name, setName] = useState(ward.name ?? '')
  const [code, setCode] = useState(ward.code)
  const [department, setDepartment] = useState(ward.department ?? '')
  const [wardType, setWardType] = useState(ward.ward_type ?? 'general')
  const [tracked, setTracked] = useState(ward.cleaning_tracked)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  const change: WardChange = {}
  if (name.trim() !== (ward.name ?? '')) change.name = name.trim()
  if (code.trim() !== ward.code) change.code = code.trim()
  if (department !== (ward.department ?? '')) change.department = department
  if (wardType !== (ward.ward_type ?? 'general')) change.ward_type = wardType
  if (tracked !== ward.cleaning_tracked) change.cleaning_tracked = tracked
  const changed = Object.keys(change).length > 0

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setRefusal(null)
    try {
      const saved = await updateWard(ward.id, change)
      await onDone(`Ward ${saved.code} saved.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal(failure.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="bs-form" aria-label={`Ward ${ward.code} details`} onSubmit={(e) => { void submit(e) }}>
      <h3 className="bs-sub">Details</h3>
      <label className="bb-field">
        <span className="bb-field__label">Display name</span>
        <input className="bb-input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Ward code</span>
        <input className="bb-input" value={code} onChange={(e) => setCode(e.target.value)} />
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Department</span>
        <select className="bb-select" value={department} onChange={(e) => setDepartment(e.target.value)}>
          {!departmentChoices(master).some((d) => d.name === department) && department && (
            <option value={department}>{department}</option>
          )}
          <DepartmentOptions master={master} />
        </select>
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Ward type</span>
        <select className="bb-select" value={wardType} onChange={(e) => setWardType(e.target.value)}>
          {master.ward_types.map((t) => <option key={t} value={t}>{plainWords(WARD_TYPE_WORDS, t)}</option>)}
        </select>
      </label>
      <label className="bs-check">
        <input type="checkbox" checked={tracked} onChange={(e) => setTracked(e.target.checked)} />
        <span>Housekeeping tracks this ward's cleaning</span>
      </label>
      <Refusal text={refusal} />
      <div className="bs-form-actions">
        <button type="submit" className="bb-btn" disabled={busy || !changed}>Save changes</button>
      </div>
    </form>
  )
}

/** A value with a Change button that opens a one-field form. `onSave`
 *  answers null when saved, else the server's words. */
function InlineEdit({ label, value, onSave, children }: {
  label: string
  value: string
  onSave: (next: string) => Promise<string | null>
  children?: ReactNode
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  if (!editing) {
    return (
      <span className="bs-inline">
        {children}
        <button type="button" className="bb-btn bb-btn--small bb-btn--quiet"
          onClick={() => { setDraft(value); setRefusal(null); setEditing(true) }}>
          Change {label}
        </button>
      </span>
    )
  }
  return (
    <span className="bs-inline">
      <label className="bb-field">
        <span className="bb-field__label">{label.charAt(0).toUpperCase() + label.slice(1)}</span>
        <input className="bb-input" value={draft} onChange={(e) => setDraft(e.target.value)} />
      </label>
      <button
        type="button"
        className="bb-btn bb-btn--small"
        disabled={busy || !draft.trim() || draft.trim() === value}
        onClick={() => {
          setBusy(true)
          void onSave(draft.trim()).then((words) => {
            setBusy(false)
            if (words) setRefusal(words)
            else setEditing(false)
          })
        }}
      >
        Save {label}
      </button>
      <button type="button" className="bb-btn bb-btn--small bb-btn--quiet"
        onClick={() => setEditing(false)}>
        Cancel
      </button>
      <Refusal text={refusal} />
    </span>
  )
}

function RoomsBlock({ master, ward, onModuleOff, onDone }: SectionProps & { ward: MasterWard }) {
  const rooms = master.rooms.filter((r) => r.ward_id === ward.id)
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [roomType, setRoomType] = useState('bay')
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  const save = (roomId: string, field: 'code' | 'name') => async (next: string) => {
    try {
      const room = await updateRoom(roomId, { [field]: next })
      await onDone(`Room ${room.code} saved.`)
      return null
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      return failure ? failure.message : null
    }
  }

  const add = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setRefusal(null)
    try {
      const room = await createRoom({ ward_id: ward.id, code: code.trim(),
        name: name.trim() || undefined, room_type: roomType })
      setCode('')
      setName('')
      await onDone(`Room ${room.code} set up on ${ward.code}.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal(nothingSaved(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bs-block">
      <h3 className="bs-sub">Rooms <span className="bb-list__n">{rooms.length}</span></h3>
      {rooms.length > 0 && (
        <ul className="bb-rows">
          {rooms.map((r) => (
            <li key={r.id} className="bb-row">
              <div className="bb-row__main">
                <InlineEdit label={`code of room ${r.code}`} value={r.code} onSave={save(r.id, 'code')}>
                  <span className="bb-row__name">Room {r.code}</span>
                </InlineEdit>
                <InlineEdit label={`name of room ${r.code}`} value={r.name ?? r.code} onSave={save(r.id, 'name')}>
                  <span className="bb-row__dept">{r.name ?? r.code}</span>
                </InlineEdit>
              </div>
              <div className="bb-row__meta">
                <span>{plainWords(ROOM_TYPE_WORDS, r.room_type)}</span>
                {r.is_isolation && <span>Isolation: a terminal clean after each patient</span>}
                {!r.is_active && <span>Switched off</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
      <form className="bs-form" aria-label={`Add a room to ${ward.code}`} onSubmit={(e) => { void add(e) }}>
        <label className="bb-field">
          <span className="bb-field__label">Room code</span>
          <input className="bb-input" value={code} onChange={(e) => setCode(e.target.value)} />
        </label>
        <label className="bb-field">
          <span className="bb-field__label">Room name</span>
          <input className="bb-input" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="bb-field">
          <span className="bb-field__label">Room type</span>
          <select className="bb-select" value={roomType} onChange={(e) => setRoomType(e.target.value)}>
            {master.room_types.map((t) => <option key={t} value={t}>{plainWords(ROOM_TYPE_WORDS, t)}</option>)}
          </select>
        </label>
        <Refusal text={refusal} />
        <div className="bs-form-actions">
          <button type="submit" className="bb-btn bb-btn--small" disabled={busy || !code.trim()}>
            Add the room
          </button>
        </div>
      </form>
    </div>
  )
}

function BedsBlock({ ward, beds, rooms, onModuleOff, onDone }: SectionProps & {
  ward: MasterWard
  beds: MasterBed[]
  rooms: BedMaster['rooms']
}) {
  const [retiring, setRetiring] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<{ bedId: string; text: string } | null>(null)
  const inUse = beds.filter((b) => !b.retired_at)
  const retired = beds.filter((b) => b.retired_at)

  const relabel = (bed: MasterBed) => async (next: string) => {
    try {
      const saved = await updateBed(bed.id, { bed_label: next })
      await onDone(`Bed ${bed.bed_label} on ${ward.code} is now bed ${saved.bed_label}.`)
      return null
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      return failure ? failure.message : null
    }
  }

  const retire = async (bed: MasterBed) => {
    setBusy(true)
    setRefusal(null)
    try {
      await retireBed(bed.id, reason.trim() || undefined)
      setRetiring(null)
      setReason('')
      await onDone(`Bed ${bed.bed_label} on ${ward.code} is retired.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal({ bedId: bed.id, text: failure.message })
    } finally {
      setBusy(false)
    }
  }

  const roomOf = (bed: MasterBed) => rooms.find((r) => r.id === bed.room_id)

  return (
    <div className="bs-block">
      <h3 className="bs-sub">Beds <span className="bb-list__n">{inUse.length}</span></h3>
      {inUse.length === 0 ? (
        <p className="bb-note">No beds are registered on {ward.code} yet.</p>
      ) : (
        <ul className="bb-rows" aria-label={`Beds on ${ward.code}`}>
          {inUse.map((b) => (
            <li key={b.id} className="bb-row">
              <div className="bb-row__main">
                <InlineEdit label={`label of bed ${b.bed_label}`} value={b.bed_label ?? ''}
                  onSave={relabel(b)}>
                  <span className="bb-row__name">Bed {b.bed_label}</span>
                  <span className="bb-row__dept">{b.unit_code}</span>
                </InlineEdit>
              </div>
              <div className="bb-row__meta">
                {roomOf(b) && <span>Room {roomOf(b)?.code}</span>}
                {b.bed_class && <span>Class {b.bed_class}</span>}
                {b.is_isolation_room && <span>Isolation</span>}
                {b.is_temporary && <span>Extra bed</span>}
                {!b.is_active && <span>Switched off</span>}
                <span>{cleaningWords(b.state)}</span>
              </div>
              {retiring === b.id ? (
                <div className="bb-confirm" role="group" aria-label={`Retire bed ${b.bed_label}`}>
                  <p className="bb-confirm__words">
                    A retired bed leaves the board and every picker. It keeps its record and its
                    label, so a new bed on this ward takes a new label.
                  </p>
                  <label className="bb-field">
                    <span className="bb-field__label">Reason</span>
                    <input className="bb-input" value={reason} onChange={(e) => setReason(e.target.value)} />
                  </label>
                  <button type="button" className="bb-btn bb-btn--small" disabled={busy}
                    onClick={() => { void retire(b) }}>
                    Retire bed {b.bed_label}
                  </button>
                  <button type="button" className="bb-btn bb-btn--small bb-btn--quiet"
                    onClick={() => { setRetiring(null); setReason(''); setRefusal(null) }}>
                    Cancel
                  </button>
                </div>
              ) : (
                <div className="bs-row-actions">
                  <button type="button" className="bb-btn bb-btn--small bb-btn--quiet"
                    aria-label={`Retire bed ${b.bed_label}`}
                    onClick={() => { setRetiring(b.id); setReason(''); setRefusal(null) }}>
                    Retire
                  </button>
                </div>
              )}
              {refusal?.bedId === b.id && <Refusal text={refusal.text} />}
            </li>
          ))}
        </ul>
      )}
      {retired.length > 0 && (
        <p className="bb-note">
          Retired: {retired.map((b) => b.bed_label).join(', ')}. Their labels stay taken on this ward.
        </p>
      )}
    </div>
  )
}

function RegisterBedsForm({ master, ward, onModuleOff, onDone }: SectionProps & { ward: MasterWard }) {
  const rooms = master.rooms.filter((r) => r.ward_id === ward.id && r.is_active)
  const legacyHere = master.housekeeping_beds_not_in_setup.filter(
    (u) => (u.ward ?? '').trim().toLowerCase() === ward.code.trim().toLowerCase())
  const [mode, setMode] = useState<'labels' | 'range'>('range')
  const [labelsText, setLabelsText] = useState('')
  const [prefix, setPrefix] = useState('')
  const [start, setStart] = useState('1')
  const [end, setEnd] = useState('')
  const [pad, setPad] = useState('0')
  const [roomId, setRoomId] = useState('')
  const [bedClass, setBedClass] = useState('')
  const [isolation, setIsolation] = useState(false)
  const [adopt, setAdopt] = useState(false)
  const [preview, setPreview] = useState<BedPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  /** Any change to what would be registered clears the preview, so what is
   *  saved is always what was last previewed. */
  const edit = <T,>(setter: (v: T) => void) => (value: T) => {
    setter(value)
    setPreview(null)
    setRefusal(null)
  }

  const showPreview = () => {
    setRefusal(null)
    setPreview(previewBeds({
      wardCode: ward.code, mode, labelsText, prefix, start, end, pad,
      max: master.max_beds_per_registration,
    }))
  }

  const save = async () => {
    if (!preview || preview.problem) return
    setBusy(true)
    setRefusal(null)
    try {
      const result = await registerBeds({
        ward_id: ward.id,
        labels: preview.labels,
        room_id: roomId || undefined,
        bed_class: bedClass || undefined,
        ...(isolation ? { is_isolation_room: true } : {}),
        ...(adopt ? { adopt_existing: true } : {}),
      })
      setPreview(null)
      setLabelsText('')
      setEnd('')
      await onDone(`${counted(result.registered.length, 'bed')} registered`
        + `${result.adopted.length ? ` and ${result.adopted.length} adopted` : ''} on ${result.ward_code}.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal(nothingSaved(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="bs-form" aria-label={`Register beds on ${ward.code}`}
      onSubmit={(e) => { e.preventDefault(); showPreview() }}>
      <h3 className="bs-sub bs-wide">Register beds</h3>
      <div className="bs-wide bs-choice" role="radiogroup" aria-label="How the beds are named">
        <label className="bs-check">
          <input type="radio" name={`bs-mode-${ward.id}`} checked={mode === 'range'}
            onChange={() => edit(setMode)('range')} />
          <span>A numbered range</span>
        </label>
        <label className="bs-check">
          <input type="radio" name={`bs-mode-${ward.id}`} checked={mode === 'labels'}
            onChange={() => edit(setMode)('labels')} />
          <span>A list of labels</span>
        </label>
      </div>
      {mode === 'range' ? (
        <>
          <label className="bb-field">
            <span className="bb-field__label">Label prefix</span>
            <input className="bb-input" value={prefix} onChange={(e) => edit(setPrefix)(e.target.value)} />
          </label>
          <label className="bb-field">
            <span className="bb-field__label">From</span>
            <input className="bb-input" inputMode="numeric" value={start}
              onChange={(e) => edit(setStart)(e.target.value)} />
          </label>
          <label className="bb-field">
            <span className="bb-field__label">To</span>
            <input className="bb-input" inputMode="numeric" value={end}
              onChange={(e) => edit(setEnd)(e.target.value)} />
          </label>
          <label className="bb-field">
            <span className="bb-field__label">Digits (leading zeros)</span>
            <input className="bb-input" inputMode="numeric" value={pad}
              onChange={(e) => edit(setPad)(e.target.value)} />
          </label>
        </>
      ) : (
        <label className="bb-field bs-wide">
          <span className="bb-field__label">Bed labels, one per line or separated by commas</span>
          <textarea className="bb-input bs-textarea" value={labelsText}
            onChange={(e) => edit(setLabelsText)(e.target.value)} />
        </label>
      )}
      <label className="bb-field">
        <span className="bb-field__label">Room</span>
        <select className="bb-select" value={roomId} onChange={(e) => edit(setRoomId)(e.target.value)}>
          <option value="">No room</option>
          {rooms.map((r) => <option key={r.id} value={r.id}>{r.code}{r.name && r.name !== r.code ? ` (${r.name})` : ''}</option>)}
        </select>
      </label>
      <label className="bb-field">
        <span className="bb-field__label">Bed class</span>
        <select className="bb-select" value={bedClass} onChange={(e) => edit(setBedClass)(e.target.value)}>
          <option value="">{ward.default_bed_class ? `The ward's default (${ward.default_bed_class})` : 'None'}</option>
          {master.bed_classes.filter((c) => c.is_active).map((c) => (
            <option key={c.code} value={c.code}>{c.name} ({c.code})</option>
          ))}
        </select>
      </label>
      {!roomId && (
        <label className="bs-check">
          <input type="checkbox" checked={isolation} onChange={(e) => edit(setIsolation)(e.target.checked)} />
          <span>Isolation beds (a terminal clean after each patient)</span>
        </label>
      )}
      {legacyHere.length > 0 && (
        <label className="bs-check bs-wide">
          <input type="checkbox" checked={adopt} onChange={(e) => edit(setAdopt)(e.target.checked)} />
          <span>
            Adopt beds already on the housekeeping register with the same label
            ({legacyHere.map((u) => u.unit_code).join(', ')}), keeping their cleaning record
          </span>
        </label>
      )}
      <div className="bs-form-actions">
        <button type="submit" className="bb-btn bb-btn--small">Preview</button>
      </div>

      {preview?.problem && <Refusal text={preview.problem} />}
      {preview && !preview.problem && (
        <div className="bs-preview bs-wide" role="region" aria-label="Beds to register">
          <p className="bs-line">
            {counted(preview.labels.length, 'bed')} will be registered on {ward.code}, each needing a clean
            before its first patient. Nothing is saved until you register them.
          </p>
          <ul className="bs-codes">
            {preview.unitCodes.map((code) => <li key={code} className="bs-code">{code}</li>)}
          </ul>
          <button type="button" className="bb-btn" disabled={busy} onClick={() => { void save() }}>
            Register {counted(preview.labels.length, 'bed')}
          </button>
        </div>
      )}
      <Refusal text={refusal} />
    </form>
  )
}

// ── Reconciliation and register complete ─────────────────────────────────────

interface PendingLink {
  admissionId: string
  code: string
  message: string
}

function EntryLine({ entry }: { entry: ReconciliationEntry }) {
  const typed = [entry.ward, entry.bed ? `bed ${entry.bed}` : null].filter(Boolean).join(', ')
  return (
    <>
      <div className="bb-row__main">
        <span className="bb-row__name">Admission {entry.admission_id}</span>
        <span className="bb-row__mrn">Patient {entry.patient_id}</span>
        {entry.department && <span className="bb-row__dept">{entry.department}</span>}
      </div>
      <div className="bb-row__meta">
        <span>Typed: {typed || 'no ward or bed'}</span>
        {entry.master_unit_code && <span>Bed in setup: {entry.master_unit_code}</span>}
        {entry.reason && <span>{plainWords(RECONCILE_REASON_WORDS, entry.reason)}</span>}
      </div>
    </>
  )
}

function ReconcileBlock({ ward, onModuleOff, onDone }: Omit<SectionProps, 'master'> & { ward: MasterWard }) {
  const [report, setReport] = useState<Reconciliation | null>(null)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingLink | null>(null)
  const [oosReason, setOosReason] = useState('')
  const [linkWords, setLinkWords] = useState<string | null>(null)

  const read = useCallback(async () => {
    setBusy(true)
    setRefusal(null)
    try {
      setReport(await getReconciliation(ward.id))
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal(failure.message)
    } finally {
      setBusy(false)
    }
  }, [ward.id, onModuleOff])

  const link = async (ids: string[], extra: { boarder_acknowledged?: boolean;
    out_of_service_reason?: string } = {}) => {
    setBusy(true)
    setLinkWords(null)
    setRefusal(null)
    try {
      const result = await linkAdmissionsToBeds({ admission_ids: ids, ...extra })
      // One admission the server will link only with words (a boarder, a bed
      // out of service): ask for them by that admission. Linking several at
      // once lists such admissions with the server's words instead.
      const needsWords = ids.length === 1
        ? result.skipped.find((s) => s.admission_id === ids[0]
          && (s.code === 'boarder_acknowledgement_required'
            || s.code === 'out_of_service_reason_required'))
        : undefined
      setPending(needsWords
        ? { admissionId: needsWords.admission_id, code: needsWords.code, message: needsWords.message ?? '' }
        : null)
      if (!needsWords) setOosReason('')
      const skippedWords = result.skipped
        .filter((s) => s !== needsWords)
        .map((s) => `${s.admission_id}: ${s.message ?? plainWords(RECONCILE_REASON_WORDS, s.code)}`)
      setLinkWords([`${result.count} linked.`, ...skippedWords].join(' '))
      setReport(await getReconciliation(ward.id))
      if (result.count > 0) {
        await onDone(`${counted(result.count, 'admission')} on ${ward.code} linked to ${result.count === 1 ? 'its bed' : 'their beds'}.`)
      }
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal(failure.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bs-block">
      <h3 className="bs-sub">Admissions on {ward.code} against its beds</h3>
      <p className="bb-note">
        Admissions recorded before bed setup carry a typed ward and bed. Where the typed bed names
        exactly one bed here, it can be linked. This list names admissions and patients by id,
        and each reading of it is recorded.
      </p>
      <div className="bs-form-actions">
        <button type="button" className="bb-btn bb-btn--small" disabled={busy} onClick={() => { void read() }}>
          {report ? 'Check again' : 'Check admissions on this ward'}
        </button>
      </div>
      <Refusal text={refusal} />
      {linkWords && <p className="bb-note" role="status">{linkWords}</p>}
      {report && (
        <div className="bs-reconcile">
          <p className="bb-note">
            {report.counts.linked ?? 0} linked, {report.counts.matched ?? 0} can be linked,{' '}
            {counted(report.counts.doubled_beds ?? 0, 'bed')} claimed twice, {report.counts.unlinked ?? 0} not
            in bed setup, {report.counts.awaiting ?? 0} with no bed.
          </p>

          <section aria-label="Can be linked">
            <h4 className="bs-minor">Can be linked <span className="bb-list__n">{report.matched.length}</span></h4>
            {report.matched.length > 1 && (
              <button type="button" className="bb-btn bb-btn--small" disabled={busy}
                onClick={() => { void link(report.matched.map((e) => e.admission_id)) }}>
                Link all {report.matched.length}
              </button>
            )}
            <ul className="bb-rows">
              {report.matched.map((e) => (
                <li key={e.admission_id} className="bb-row">
                  <EntryLine entry={e} />
                  <div className="bs-row-actions">
                    <button type="button" className="bb-btn bb-btn--small" disabled={busy}
                      onClick={() => { void link([e.admission_id]) }}>
                      Link to bed {e.master_unit_code}
                    </button>
                  </div>
                  {pending?.admissionId === e.admission_id && (
                    <div className="bb-confirm" role="group" aria-label="Confirm the link">
                      <p className="bb-confirm__words">{pending.message}</p>
                      {pending.code === 'boarder_acknowledgement_required' ? (
                        <button type="button" className="bb-btn bb-btn--small" disabled={busy}
                          onClick={() => { void link([e.admission_id], { boarder_acknowledged: true }) }}>
                          I have read this. Link as a boarder
                        </button>
                      ) : (
                        <>
                          <label className="bb-field">
                            <span className="bb-field__label">Reason the patient is in this bed</span>
                            <input className="bb-input" value={oosReason}
                              onChange={(ev) => setOosReason(ev.target.value)} />
                          </label>
                          <button type="button" className="bb-btn bb-btn--small"
                            disabled={busy || !oosReason.trim()}
                            onClick={() => {
                              void link([e.admission_id], { out_of_service_reason: oosReason.trim() })
                            }}>
                            Link with this reason
                          </button>
                        </>
                      )}
                      <button type="button" className="bb-btn bb-btn--small bb-btn--quiet"
                        onClick={() => { setPending(null); setOosReason('') }}>
                        Cancel
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section aria-label="Beds claimed twice">
            <h4 className="bs-minor">Beds claimed twice <span className="bb-list__n">{report.doubled.length}</span></h4>
            {report.doubled.length > 0 && (
              <p className="bb-note">
                More than one admission names each of these beds. Find out on the ward who lies
                there and correct the other admission; a doubled bed is never linked from here.
              </p>
            )}
            <ul className="bb-rows">
              {report.doubled.map((d) => (
                <li key={d.bed_id} className="bb-row">
                  <div className="bb-row__main">
                    <span className="bb-row__name">{d.unit_code ?? d.bed_id}</span>
                    <span className="bb-row__dept">claimed by {counted(d.admissions.length, 'admission')}</span>
                  </div>
                  <ul className="bb-rows">
                    {d.admissions.map((e) => (
                      <li key={e.admission_id} className="bs-subrow"><EntryLine entry={e} /></li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </section>

          <section aria-label="Not in bed setup">
            <h4 className="bs-minor">Not in bed setup <span className="bb-list__n">{report.unlinked.length}</span></h4>
            <ul className="bb-rows">
              {report.unlinked.map((e) => <li key={e.admission_id} className="bb-row"><EntryLine entry={e} /></li>)}
            </ul>
          </section>

          <section aria-label="No bed recorded">
            <h4 className="bs-minor">No bed recorded <span className="bb-list__n">{report.awaiting.length}</span></h4>
            <ul className="bb-rows">
              {report.awaiting.map((e) => <li key={e.admission_id} className="bb-row"><EntryLine entry={e} /></li>)}
            </ul>
          </section>

          {report.mirror_mismatches.length > 0 && (
            <p className="bb-note bb-note--warn">
              {report.mirror_mismatches.length} linked admissions carry a typed ward or bed that
              differs from their bed in setup.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

interface IncompleteRefusal {
  message: string
  unlinked: string[]
  doubled: string[]
}

function RegisterCompleteBlock({ master, ward, onModuleOff, onDone }: SectionProps & { ward: MasterWard }) {
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<IncompleteRefusal | null>(null)

  const unitCode = (bedId: string) => master.beds.find((b) => b.id === bedId)?.unit_code ?? bedId

  const complete = async () => {
    setBusy(true)
    setRefusal(null)
    try {
      await markRegisterComplete(ward.id)
      await onDone(`The bed register for ${ward.code} is marked complete.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (!failure) return
      const detail = (failure.detail ?? {}) as { unlinked?: unknown; doubled?: unknown }
      const ids = (value: unknown) => (Array.isArray(value) ? value.map(String) : [])
      setRefusal({ message: failure.message, unlinked: ids(detail.unlinked), doubled: ids(detail.doubled) })
    } finally {
      setBusy(false)
    }
  }

  if (ward.register_complete) {
    return (
      <div className="bs-block">
        <h3 className="bs-sub">Bed register</h3>
        <p className="bb-note">
          The bed register for {ward.code} is complete
          {ward.register_complete_at ? `, since ${formatClinicalDateTime(ward.register_complete_at)}` : ''}.
          Its bed figures count from then.
        </p>
      </div>
    )
  }
  return (
    <div className="bs-block">
      <h3 className="bs-sub">Bed register</h3>
      <p className="bb-note">
        Mark the register complete once every bed on {ward.code} is set up and every admission on
        it is linked to its bed. The ward's bed figures count from that moment; earlier days read
        as not enough data.
      </p>
      <div className="bs-form-actions">
        <button type="button" className="bb-btn bb-btn--small" disabled={busy} onClick={() => { void complete() }}>
          Mark the register complete
        </button>
      </div>
      {refusal && (
        <div className="bs-refusal" role="alert">
          <p className="bs-line">{refusal.message}</p>
          {refusal.unlinked.length > 0 && (
            <p className="bs-line">Admissions not linked to a bed: {refusal.unlinked.join(', ')}</p>
          )}
          {refusal.doubled.length > 0 && (
            <p className="bs-line">Beds claimed by more than one admission: {refusal.doubled.map(unitCode).join(', ')}</p>
          )}
        </div>
      )}
    </div>
  )
}

// ── Bed classes ──────────────────────────────────────────────────────────────

function BedClassesSection({ master, onModuleOff, onDone }: SectionProps) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [charge, setCharge] = useState('')
  const [critical, setCritical] = useState(false)
  const [census, setCensus] = useState(true)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [rowRefusal, setRowRefusal] = useState<{ code: string; text: string } | null>(null)

  const rename = (classCode: string) => async (next: string) => {
    try {
      await updateBedClass(classCode, { name: next })
      await onDone(`Bed class ${classCode} saved.`)
      return null
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      return failure ? failure.message : null
    }
  }

  const toggle = async (classCode: string, active: boolean) => {
    setRowRefusal(null)
    try {
      await updateBedClass(classCode, { is_active: active })
      await onDone(`Bed class ${classCode} switched ${active ? 'on' : 'off'}.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRowRefusal({ code: classCode, text: failure.message })
    }
  }

  const add = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setRefusal(null)
    try {
      const saved = await createBedClass({ code: code.trim(), name: name.trim(),
        bed_day_service_code: charge.trim() || undefined, is_critical_care: critical,
        counts_in_census: census })
      setCode('')
      setName('')
      setCharge('')
      await onDone(`Bed class ${saved.code} set up.`)
    } catch (err) {
      const failure = refusalOf(err, onModuleOff)
      if (failure) setRefusal(nothingSaved(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="bb-list" aria-labelledby="bs-classes-title">
      <h2 id="bs-classes-title" className="bb-list__title">
        Bed classes <span className="bb-list__n">{master.bed_classes.length}</span>
      </h2>
      {master.bed_classes.length > 0 && (
        <ul className="bb-rows">
          {master.bed_classes.map((c) => (
            <li key={c.code} className="bb-row">
              <div className="bb-row__main">
                <InlineEdit label={`name of class ${c.code}`} value={c.name} onSave={rename(c.code)}>
                  <span className="bb-row__name">{c.name}</span>
                  <span className="bb-row__dept">{c.code}</span>
                </InlineEdit>
              </div>
              <div className="bb-row__meta">
                <span>
                  {c.bed_day_service_code
                    ? `Bed-day charge ${c.bed_day_service_code}${c.bed_day_charge_listed === false ? ', not on the price list' : ''}`
                    : 'No bed-day charge'}
                </span>
                {c.is_critical_care && <span>Critical care</span>}
                <span>{c.counts_in_census ? 'Counts in the census' : 'Not in the census'}</span>
                {!c.is_active && <span>Switched off</span>}
              </div>
              <div className="bs-row-actions">
                <button type="button" className="bb-btn bb-btn--small bb-btn--quiet"
                  onClick={() => { void toggle(c.code, !c.is_active) }}>
                  {c.is_active ? `Switch off ${c.code}` : `Switch on ${c.code}`}
                </button>
              </div>
              {rowRefusal?.code === c.code && <Refusal text={rowRefusal.text} />}
            </li>
          ))}
        </ul>
      )}
      <form className="bs-form" aria-label="Set up a bed class" onSubmit={(e) => { void add(e) }}>
        <label className="bb-field">
          <span className="bb-field__label">Class code</span>
          <input className="bb-input" value={code} onChange={(e) => setCode(e.target.value)} />
        </label>
        <label className="bb-field">
          <span className="bb-field__label">Class name</span>
          <input className="bb-input" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="bb-field">
          <span className="bb-field__label">Bed-day charge code</span>
          <input className="bb-input" value={charge} onChange={(e) => setCharge(e.target.value)} />
        </label>
        <label className="bs-check">
          <input type="checkbox" checked={critical} onChange={(e) => setCritical(e.target.checked)} />
          <span>Critical care</span>
        </label>
        <label className="bs-check">
          <input type="checkbox" checked={census} onChange={(e) => setCensus(e.target.checked)} />
          <span>Counts in the census</span>
        </label>
        <Refusal text={refusal} />
        <div className="bs-form-actions">
          <button type="submit" className="bb-btn bb-btn--small" disabled={busy || !code.trim() || !name.trim()}>
            Add the bed class
          </button>
        </div>
      </form>
    </section>
  )
}
