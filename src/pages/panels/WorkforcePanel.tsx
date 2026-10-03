/**
 * Orb — Workforce: duty roster, attendance & payroll export.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ THIS IS NOT PAYROLL. Orb records who was on and who turned up.         ║
 * ║ PF, ESI, Professional Tax and TDS are computed by the hospital's       ║
 * ║ payroll provider — and this screen says so before it says anything.    ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * WHY THE SENTENCE IS ON THE SCREEN AND NOT ONLY IN A DESIGN DOCUMENT.
 * An HR clerk who opens a screen called "Workforce" with an export button on it
 * reasonably assumes the export contains pay. If they discover otherwise only
 * when the bureau calls, the module has failed them once — and if they assume
 * Orb computed the PF and send the file on unchecked, it has failed the
 * hospital. So the boundary is stated in the header on the FIRST PAINT, before
 * any network round trip and still there if every request fails, and again on
 * the export screen next to the download.
 *
 * WHAT THIS SCREEN IS ORGANISED AROUND. Not "here are your tables". A nursing
 * superintendent opens the day on one question — WHO IS ON, and did they come?
 * — so the roster board leads, with planned and actual side by side. The
 * monthly muster and the export are the month-end job and sit behind their own
 * tabs.
 *
 * THE THING THIS UI REFUSES TO SMOOTH OVER. A day nobody recorded shows as
 * "Not recorded", in its own column, everywhere it appears — never folded into
 * present and never into absent. It is the difference between a nurse being
 * paid and not, and a screen that quietly rounds it is the screen that causes
 * the error. `attendance_complete` gates the export button's tone for the same
 * reason: the file still generates (a hospital that cannot run payroll goes back
 * to paper), but it cannot generate QUIETLY.
 *
 * NO MONEY IS IMPORTED IN THIS FILE. No `formatInr`, no paise, no rupee glyph.
 * That absence is the design.
 */
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  assignRoster, cancelRosterEntry, downloadCsv, formatDuration,
  generatePayrollExport, getAttendance, getEmployees, getExportRuns, getHolidays,
  getMeta, getRoster, getRosterConflicts, getSummary, isoToLocalInput,
  localInputToIso, previewPayrollExport, recordAttendance, saveEmployee,
  saveHoliday, WorkforceApiError,
  type ActualStatus, type AttendanceRow, type DayStatus, type Employee,
  type EmployeeSummary, type GeneratedExport, type Holiday, type PayrollExportRun,
  type PeriodSummary, type RosterBoard, type StandingConflict, type WorkforceMeta,
} from '../../api/workforce'
import { formatClinicalDate, formatClinicalDayShort } from '../../lib/datetime'
import { humaniseEnum } from '../../lib/humanise'
import './WorkforcePanel.css'

/* ── The boundary sentence, duplicated on purpose ────────────────────────────
 * The server sends this same statement on /meta and the panel prefers the
 * server's copy once it has one. This literal exists so the words are on screen
 * in the FIRST PAINT and still there if every request fails. The one message
 * that must never be missing cannot be one that depends on the network. */
const BOUNDARY_NOTE =
  'Orb records the duty roster and attendance. The facts only Orb can see. '
  + 'It does not compute pay. PF, ESI, Professional Tax and TDS are calculated '
  + 'by the hospital’s payroll provider: those rules change with every '
  + 'budget and differ by state, and getting them wrong is a statutory '
  + 'liability. The export below carries days, hours and shift counts, with the '
  + 'statutory columns deliberately empty.'

type View = 'roster' | 'attendance' | 'muster' | 'export' | 'people'

const VIEWS: { id: View; label: string }[] = [
  { id: 'roster', label: 'Duty roster' },
  { id: 'attendance', label: 'Attendance' },
  { id: 'muster', label: 'Monthly muster' },
  { id: 'export', label: 'Payroll export' },
  { id: 'people', label: 'People & calendar' },
]

const STATUS_LABEL: Record<ActualStatus, string> = {
  present: 'Present',
  absent: 'Absent',
  leave: 'Leave',
  weekly_off: 'Weekly off',
  holiday: 'Holiday',
  unrecorded: 'Not recorded',
  scheduled: 'Scheduled',
}

/* `unrecorded` is deliberately NOT 'neutral'. A day nobody wrote down is a gap
 * somebody has to close before payroll runs, so it reads as a warning — the one
 * tone that means "this needs you" without meaning "something went wrong". */
const STATUS_TONE: Record<ActualStatus, string> = {
  present: 'ok',
  absent: 'danger',
  leave: 'info',
  weekly_off: 'neutral',
  holiday: 'neutral',
  unrecorded: 'warn',
  // A future rostered line has not happened, and attendance cannot be
  // recorded for it, so it is not a gap and must not read as one.
  scheduled: 'neutral',
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function todayIso(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00`)
  d.setDate(d.getDate() + days)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function prettyDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`)
  if (Number.isNaN(d.getTime())) return iso
  // "Thu 1 Oct" on every device; the browser locale printed "Thu, Oct 1".
  return formatClinicalDayShort(d)
}

/* ── Small presentational pieces ─────────────────────────────────────────── */

function Badge({ status }: { status: ActualStatus }) {
  return (
    <span className={`wf-badge wf-badge--${STATUS_TONE[status]}`}>
      {STATUS_LABEL[status]}
    </span>
  )
}

function Tile({ value, caption, tone }: { value: string; caption: string; tone?: string }) {
  return (
    <div className="wf-tile">
      <div className={`wf-tile__value${tone ? ` wf-tile__value--${tone}` : ''}`}>{value}</div>
      <div className="wf-tile__caption">{caption}</div>
    </div>
  )
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="wf-empty">
      <div className="wf-empty__title">{title}</div>
      <div className="wf-empty__detail">{detail}</div>
    </div>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="wf-field">
      <span className="wf-field__label">{label}</span>
      {children}
      {hint && <span className="wf-field__hint">{hint}</span>}
    </label>
  )
}

function Drawer({ title, subtitle, onClose, children }: {
  title: string; subtitle?: string; onClose: () => void; children: ReactNode
}) {
  // Portalled to document.body so no ancestor's overflow/transform can clip it.
  return createPortal(
    <div className="wf-drawer" role="dialog" aria-modal="true" aria-label={title}>
      <button type="button" className="wf-drawer__scrim" aria-label="Close" onClick={onClose} />
      <div className="wf-drawer__panel">
        <header className="wf-drawer__head">
          <div>
            <h2 className="wf-drawer__title">{title}</h2>
            {subtitle && <p className="wf-drawer__sub">{subtitle}</p>}
          </div>
          <button type="button" className="wf-btn" onClick={onClose}>Close</button>
        </header>
        <div className="wf-drawer__body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

/* ── Drawers ─────────────────────────────────────────────────────────────── */

/** Taking someone off a shift is asked, never done on one tap: a mis-tap left
 *  a shift a person short with nothing to say so, and a refusal from the server
 *  surfaced nowhere. */
function RemoveRosterDrawer({ who, detail, shiftName, date, busy, error, onClose, onConfirm }: {
  who: string
  detail: string
  shiftName: string
  date: string
  busy: boolean
  error: string | null
  onClose: () => void
  onConfirm: () => void
}) {
  return (
    <Drawer title="Remove from the roster" onClose={onClose}>
      <div className="wf-form">
        <p style={{ margin: 0, lineHeight: 1.5 }}>
          Take <strong>{who}</strong>{detail ? ` (${detail})` : ''} off the{' '}
          <strong>{shiftName}</strong> shift on <strong>{prettyDate(date)}</strong>?
          The shift will be one person short until someone is assigned.
        </p>
        {error && <div className="wf-notice wf-notice--danger" role="alert">{error}</div>}
        <div className="wf-drawer__actions">
          <button type="button" className="wf-btn" onClick={onClose}>Go back</button>
          <button type="button" className="wf-btn wf-btn--primary" disabled={busy}
                  onClick={onConfirm}>
            {busy ? 'Removing…' : 'Remove from roster'}
          </button>
        </div>
      </div>
    </Drawer>
  )
}

function AssignDrawer({ meta, employees, date, onClose, onSubmit, busy, error, conflicts }: {
  meta: WorkforceMeta | null
  employees: Employee[]
  date: string
  onClose: () => void
  busy: boolean
  error: string | null
  conflicts: { message: string }[]
  onSubmit: (payload: {
    roster_date: string; shift_code: string; employee_code: string
    department?: string | null; ward?: string | null
  }) => void
}) {
  const [rosterDate, setRosterDate] = useState(date)
  const [shift, setShift] = useState(meta?.shift_types[0]?.code ?? '')
  const [employee, setEmployee] = useState('')
  const [ward, setWard] = useState('')

  return (
    <Drawer title="Add to the roster"
            subtitle="One person, one shift, one day."
            onClose={onClose}>
      <div className="wf-form">
        <Field label="Date">
          <input type="date" className="wf-input" value={rosterDate}
                 onChange={e => setRosterDate(e.target.value)} />
        </Field>
        <Field label="Shift">
          <select className="wf-input" value={shift} onChange={e => setShift(e.target.value)}>
            {(meta?.shift_types ?? []).filter(s => s.is_active).map(s => (
              <option key={s.code} value={s.code}>
                {s.name}{s.crosses_midnight ? ', crosses midnight' : ''}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Person">
          <select className="wf-input" value={employee}
                  onChange={e => setEmployee(e.target.value)}>
            <option value="">Select…</option>
            {employees.map(e => (
              <option key={e.employee_code} value={e.employee_code}>
                {e.full_name}, {e.employee_code}{e.department ? ` (${e.department})` : ''}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Ward / unit" hint="Optional. The department comes from the employment record.">
          <input className="wf-input" value={ward} onChange={e => setWard(e.target.value)} />
        </Field>

        {error && (
          <div className="wf-notice wf-notice--danger">
            <strong>{error}</strong>
            {conflicts.length > 0 && (
              <ul className="wf-conflict-list">
                {conflicts.map((c, i) => <li key={i}>{c.message}</li>)}
              </ul>
            )}
          </div>
        )}

        <div className="wf-drawer__actions">
          <button type="button" className="wf-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="wf-btn wf-btn--primary"
                  disabled={busy || !shift || !employee}
                  onClick={() => onSubmit({
                    roster_date: rosterDate, shift_code: shift,
                    employee_code: employee, ward: ward || null,
                  })}>
            {busy ? 'Saving…' : 'Add to roster'}
          </button>
        </div>
      </div>
    </Drawer>
  )
}

function AttendanceDrawer({ meta, employees, date, existing, onClose, onSubmit, busy, error }: {
  meta: WorkforceMeta | null
  employees: Employee[]
  date: string
  existing: AttendanceRow | null
  onClose: () => void
  busy: boolean
  error: string | null
  onSubmit: (payload: Parameters<typeof recordAttendance>[0]) => void
}) {
  const [employee, setEmployee] = useState(existing?.employee_code ?? '')
  const [day, setDay] = useState(existing?.attendance_date ?? date)
  const [status, setStatus] = useState<DayStatus>(existing?.status ?? 'present')
  const [shift, setShift] = useState(existing?.shift_code ?? '')
  const [inAt, setInAt] = useState(isoToLocalInput(existing?.first_in_at))
  const [outAt, setOutAt] = useState(isoToLocalInput(existing?.last_out_at))
  const [leaveType, setLeaveType] = useState(existing?.leave_type_code ?? '')
  const [remarks, setRemarks] = useState(existing?.remarks ?? '')

  const needsTimes = status === 'present'
  const needsLeave = status === 'leave'

  return (
    <Drawer title={existing ? 'Correct this day' : 'Record attendance'}
            subtitle={existing
              ? 'A correction replaces the day. There is one record per person per day.'
              : 'One person, one day.'}
            onClose={onClose}>
      <div className="wf-form">
        <Field label="Person">
          <select className="wf-input" value={employee} disabled={!!existing}
                  onChange={e => setEmployee(e.target.value)}>
            <option value="">Select…</option>
            {employees.map(e => (
              <option key={e.employee_code} value={e.employee_code}>
                {e.full_name}, {e.employee_code}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Date">
          <input type="date" className="wf-input" value={day} disabled={!!existing}
                 onChange={e => setDay(e.target.value)} />
        </Field>
        <Field label="What happened">
          <select className="wf-input" value={status}
                  onChange={e => setStatus(e.target.value as DayStatus)}>
            {(meta?.day_statuses ?? []).map(s => (
              <option key={s} value={s}>{STATUS_LABEL[s]}</option>
            ))}
          </select>
        </Field>

        {needsLeave && (
          <Field label="Leave type"
                 hint="Required. The payroll provider cannot price a leave day that does not say which leave it was.">
            <select className="wf-input" value={leaveType}
                    onChange={e => setLeaveType(e.target.value)}>
              <option value="">Select…</option>
              {(meta?.leave_types ?? []).map(l => (
                <option key={l.code} value={l.code}>
                  {l.name} ({l.is_paid ? 'paid' : 'unpaid'})
                </option>
              ))}
            </select>
          </Field>
        )}

        {needsTimes && (
          <>
            <Field label="Shift worked" hint="Used for the late mark and the shift-allowance count.">
              <select className="wf-input" value={shift} onChange={e => setShift(e.target.value)}>
                <option value="">Not specified</option>
                {(meta?.shift_types ?? []).map(s => (
                  <option key={s.code} value={s.code}>{s.name}</option>
                ))}
              </select>
            </Field>
            <div className="wf-form__pair">
              <Field label="In">
                <input type="datetime-local" className="wf-input" value={inAt}
                       onChange={e => setInAt(e.target.value)} />
              </Field>
              <Field label="Out"
                     hint="A night shift ends on the NEXT date. Set the date as well as the time.">
                <input type="datetime-local" className="wf-input" value={outAt}
                       onChange={e => setOutAt(e.target.value)} />
              </Field>
            </div>
          </>
        )}

        <Field label="Remarks">
          <input className="wf-input" value={remarks} onChange={e => setRemarks(e.target.value)} />
        </Field>

        {error && <div className="wf-notice wf-notice--danger">{error}</div>}

        <div className="wf-drawer__actions">
          <button type="button" className="wf-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="wf-btn wf-btn--primary"
                  disabled={busy || !employee || (needsLeave && !leaveType)
                            || (needsTimes && (!inAt || !outAt))}
                  onClick={() => onSubmit({
                    employee_code: employee, attendance_date: day, status,
                    shift_code: needsTimes ? (shift || null) : null,
                    first_in_at: needsTimes ? localInputToIso(inAt) : null,
                    last_out_at: needsTimes ? localInputToIso(outAt) : null,
                    leave_type_code: needsLeave ? leaveType : null,
                    remarks: remarks || null,
                  })}>
            {busy ? 'Saving…' : 'Save day'}
          </button>
        </div>
      </div>
    </Drawer>
  )
}

function EmployeeDrawer({ meta, existing, onClose, onSubmit, busy, error }: {
  meta: WorkforceMeta | null
  existing: Employee | null
  onClose: () => void
  busy: boolean
  error: string | null
  onSubmit: (payload: Parameters<typeof saveEmployee>[0]) => void
}) {
  const [code, setCode] = useState(existing?.employee_code ?? '')
  const [name, setName] = useState(existing?.full_name ?? '')
  const [department, setDepartment] = useState(existing?.department ?? '')
  const [designation, setDesignation] = useState(existing?.designation ?? '')
  const [type, setType] = useState(existing?.employment_type ?? 'permanent')
  const [joining, setJoining] = useState(existing?.date_of_joining ?? '')
  const [exit, setExit] = useState(existing?.date_of_exit ?? '')
  const [weeklyOff, setWeeklyOff] = useState<string>(
    existing?.weekly_off_day === null || existing?.weekly_off_day === undefined
      ? '' : String(existing.weekly_off_day))
  const [bureauRef, setBureauRef] = useState(existing?.payroll_bureau_ref ?? '')
  const [active, setActive] = useState(existing ? existing.is_active : true)

  return (
    <Drawer title={existing ? 'Employment record' : 'Add an employee'}
            subtitle="Employment, not login. Most of a hospital's payroll has no Orb account."
            onClose={onClose}>
      <div className="wf-form">
        <div className="wf-form__pair">
          <Field label="Employee code" hint="The hospital's own staff number.">
            <input className="wf-input" value={code} disabled={!!existing}
                   onChange={e => setCode(e.target.value)} />
          </Field>
          <Field label="Full name">
            <input className="wf-input" value={name} onChange={e => setName(e.target.value)} />
          </Field>
        </div>
        <div className="wf-form__pair">
          <Field label="Department">
            <input className="wf-input" value={department}
                   onChange={e => setDepartment(e.target.value)} />
          </Field>
          <Field label="Designation">
            <input className="wf-input" value={designation}
                   onChange={e => setDesignation(e.target.value)} />
          </Field>
        </div>
        <Field label="Employment type">
          <select className="wf-input" value={type} onChange={e => setType(e.target.value)}>
            {(meta?.employment_types ?? ['permanent']).map(t => (
              <option key={t} value={t}>{humaniseEnum(t)}</option>
            ))}
          </select>
        </Field>
        <div className="wf-form__pair">
          <Field label="Date of joining">
            <input type="date" className="wf-input" value={joining}
                   onChange={e => setJoining(e.target.value)} />
          </Field>
          <Field label="Date of exit" hint="Leave blank for current staff.">
            <input type="date" className="wf-input" value={exit}
                   onChange={e => setExit(e.target.value)} />
          </Field>
        </div>
        <Field label="Weekly off"
               hint="Rotational means the day off moves with the roster. Orb will not infer one, so it must be recorded as attendance.">
          <select className="wf-input" value={weeklyOff}
                  onChange={e => setWeeklyOff(e.target.value)}>
            <option value="">Rotational</option>
            {WEEKDAYS.map((d, i) => <option key={d} value={String(i)}>{d}</option>)}
          </select>
        </Field>
        <Field label="Payroll provider reference"
               hint="The bureau's own id for this person, carried through the export untouched.">
          <input className="wf-input" value={bureauRef}
                 onChange={e => setBureauRef(e.target.value)} />
        </Field>
        <label className="wf-check">
          <input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)} />
          <span>Currently employed</span>
        </label>

        {error && <div className="wf-notice wf-notice--danger">{error}</div>}

        <div className="wf-drawer__actions">
          <button type="button" className="wf-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="wf-btn wf-btn--primary"
                  disabled={busy || !code.trim() || !name.trim()}
                  onClick={() => onSubmit({
                    employee_code: code.trim(), full_name: name.trim(),
                    department: department || null, designation: designation || null,
                    employment_type: type,
                    date_of_joining: joining || null, date_of_exit: exit || null,
                    weekly_off_day: weeklyOff === '' ? null : Number(weeklyOff),
                    payroll_bureau_ref: bureauRef || null, is_active: active,
                    // Adding refuses a code already on the register instead of
                    // overwriting that person; only the Edit drawer updates.
                    create_only: !existing,
                  })}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </Drawer>
  )
}

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function WorkforcePanel() {
  const [view, setView] = useState<View>('roster')
  const [meta, setMeta] = useState<WorkforceMeta | null>(null)
  const [moduleOff, setModuleOff] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const [employees, setEmployees] = useState<Employee[]>([])
  const [department, setDepartment] = useState('')

  const [rosterFrom, setRosterFrom] = useState(todayIso())
  const [board, setBoard] = useState<RosterBoard | null>(null)
  const [conflicts, setConflicts] = useState<StandingConflict[]>([])

  const [attendanceDate, setAttendanceDate] = useState(todayIso())
  const [attendance, setAttendance] = useState<AttendanceRow[]>([])

  const now = new Date()
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth() + 1)
  const [muster, setMuster] = useState<PeriodSummary | null>(null)

  const [exportPreview, setExportPreview] = useState<Awaited<
    ReturnType<typeof previewPayrollExport>> | null>(null)
  const [runs, setRuns] = useState<PayrollExportRun[]>([])
  const [generated, setGenerated] = useState<GeneratedExport | null>(null)

  const [holidays, setHolidays] = useState<Holiday[]>([])

  const [drawer, setDrawer] = useState<
    null | { kind: 'assign' } | { kind: 'attendance'; row: AttendanceRow | null }
    | { kind: 'employee'; row: Employee | null }
    | { kind: 'remove'; rosterId: number; who: string; detail: string; shiftName: string; date: string }>(null)
  const [busy, setBusy] = useState(false)
  const [drawerError, setDrawerError] = useState<string | null>(null)
  const [drawerConflicts, setDrawerConflicts] = useState<{ message: string }[]>([])

  const handle = useCallback((err: unknown): string => {
    if (err instanceof WorkforceApiError) {
      if (err.isModuleDisabled) setModuleOff(true)
      return err.message
    }
    return err instanceof Error ? err.message : 'Something went wrong.'
  }, [])

  const rosterTo = useMemo(() => addDays(rosterFrom, 6), [rosterFrom])

  const loadMeta = useCallback(async () => {
    setLoading(true)
    try {
      const [m, e] = await Promise.all([getMeta(), getEmployees({ include_inactive: true })])
      setMeta(m)
      setEmployees(e.employees)
      setModuleOff(false)
      setLoadError(null)
    } catch (err) {
      setLoadError(handle(err))
    } finally {
      setLoading(false)
    }
  }, [handle])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void loadMeta() }, [loadMeta])

  const loadRoster = useCallback(async () => {
    try {
      const [b, c] = await Promise.all([
        getRoster({ date_from: rosterFrom, date_to: rosterTo, department: department || undefined }),
        getRosterConflicts({ date_from: rosterFrom, date_to: rosterTo,
                             department: department || undefined }),
      ])
      setBoard(b)
      setConflicts(c.conflicts)
    } catch (err) { setLoadError(handle(err)) }
  }, [rosterFrom, rosterTo, department, handle])

  const loadAttendance = useCallback(async () => {
    try {
      const a = await getAttendance({ date_from: attendanceDate, date_to: attendanceDate,
                                      department: department || undefined })
      setAttendance(a.attendance)
    } catch (err) { setLoadError(handle(err)) }
  }, [attendanceDate, department, handle])

  const loadMuster = useCallback(async () => {
    try {
      const s = await getSummary({ year, month, department: department || undefined })
      setMuster(s as PeriodSummary)
    } catch (err) { setLoadError(handle(err)) }
  }, [year, month, department, handle])

  const loadExport = useCallback(async () => {
    try {
      const [p, r] = await Promise.all([
        previewPayrollExport({ year, month, department: department || undefined }),
        getExportRuns(10),
      ])
      setExportPreview(p)
      setRuns(r.runs)
    } catch (err) { setLoadError(handle(err)) }
  }, [year, month, department, handle])

  const loadHolidays = useCallback(async () => {
    try {
      const h = await getHolidays({ date_from: `${year}-01-01`, date_to: `${year}-12-31` })
      setHolidays(h.holidays)
    } catch (err) { setLoadError(handle(err)) }
  }, [year, handle])

  useEffect(() => {
    if (moduleOff || loading) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    if (view === 'roster') void loadRoster()
    else if (view === 'attendance') void loadAttendance()
    else if (view === 'muster') void loadMuster()
    else if (view === 'export') void loadExport()
    else if (view === 'people') void loadHolidays()
  }, [view, moduleOff, loading, loadRoster, loadAttendance, loadMuster, loadExport,
      loadHolidays])

  const departments = useMemo(() => {
    const set = new Set<string>()
    for (const e of employees) if (e.department) set.add(e.department)
    return Array.from(set).sort()
  }, [employees])

  const closeDrawer = () => { setDrawer(null); setDrawerError(null); setDrawerConflicts([]) }

  const submitAssign = async (payload: Parameters<typeof assignRoster>[0]) => {
    setBusy(true); setDrawerError(null); setDrawerConflicts([])
    try {
      await assignRoster(payload)
      closeDrawer()
      await loadRoster()
    } catch (err) {
      setDrawerError(handle(err))
      if (err instanceof WorkforceApiError) setDrawerConflicts(err.conflicts)
    } finally { setBusy(false) }
  }

  const submitAttendance = async (payload: Parameters<typeof recordAttendance>[0]) => {
    setBusy(true); setDrawerError(null)
    try {
      await recordAttendance(payload)
      closeDrawer()
      if (view === 'roster') await loadRoster(); else await loadAttendance()
    } catch (err) { setDrawerError(handle(err)) } finally { setBusy(false) }
  }

  const submitRemove = async (rosterId: number) => {
    setBusy(true); setDrawerError(null)
    try {
      await cancelRosterEntry(rosterId)
      closeDrawer()
      await loadRoster()
    } catch (err) { setDrawerError(handle(err)) } finally { setBusy(false) }
  }

  const submitEmployee = async (payload: Parameters<typeof saveEmployee>[0]) => {
    setBusy(true); setDrawerError(null)
    try {
      await saveEmployee(payload)
      closeDrawer()
      const e = await getEmployees({ include_inactive: true })
      setEmployees(e.employees)
    } catch (err) { setDrawerError(handle(err)) } finally { setBusy(false) }
  }

  const doGenerate = async () => {
    setBusy(true)
    try {
      const g = await generatePayrollExport({ year, month, department: department || undefined })
      setGenerated(g)
      downloadCsv(g.file_name, g.csv)
      const r = await getExportRuns(10)
      setRuns(r.runs)
    } catch (err) { setLoadError(handle(err)) } finally { setBusy(false) }
  }

  /* ── Module off ────────────────────────────────────────────────────────── */

  if (moduleOff) {
    return (
      <div className="wf-panel">
        <header className="wf-head">
          <div>
            <h1 className="wf-title">Workforce</h1>
            <p className="wf-sub">Duty roster, attendance & payroll export</p>
          </div>
        </header>
        <div className="wf-notice wf-notice--off">
          <strong>Not available at this site.</strong> This deployment keeps its
          duty roster and attendance in another system (ORB_WORKFORCE=0). Nothing
          is missing from Orb. This module is switched off here.
        </div>
      </div>
    )
  }

  const boundary = meta?.boundary.message ?? BOUNDARY_NOTE

  return (
    <div className="wf-panel">
      <header className="wf-head">
        <div>
          <h1 className="wf-title">Workforce</h1>
          <p className="wf-sub">Duty roster, attendance, overtime & payroll export</p>
        </div>
        <div className="wf-head__controls">
          <label className="wf-field wf-field--inline">
            <span className="wf-field__label">Department</span>
            <select className="wf-input" value={department}
                    onChange={e => setDepartment(e.target.value)}>
              <option value="">All departments</option>
              {departments.map(d => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
        </div>
      </header>

      {/* The boundary. Rendered from a local literal on the first paint and
          replaced by the server's own words once /meta answers, so it is on
          screen even when every request has failed. */}
      <div className="wf-notice">{boundary}</div>

      {loadError && !moduleOff && (
        <div className="wf-notice wf-notice--danger">{loadError}</div>
      )}

      <nav className="wf-tabs" role="tablist" aria-label="Workforce views">
        {VIEWS.map(v => (
          <button key={v.id} type="button" role="tab" aria-selected={view === v.id}
                  className={`wf-tab${view === v.id ? ' wf-tab--active' : ''}`}
                  onClick={() => setView(v.id)}>
            {v.label}
          </button>
        ))}
      </nav>

      {/* ── Duty roster ─────────────────────────────────────────────────── */}
      {view === 'roster' && (
        <section className="wf-section">
          <div className="wf-toolbar">
            <label className="wf-field wf-field--inline">
              <span className="wf-field__label">Week from</span>
              <input type="date" className="wf-input" value={rosterFrom}
                     onChange={e => setRosterFrom(e.target.value)} />
            </label>
            <button type="button" className="wf-btn"
                    onClick={() => setRosterFrom(addDays(rosterFrom, -7))}>
              Previous week
            </button>
            <button type="button" className="wf-btn"
                    onClick={() => setRosterFrom(addDays(rosterFrom, 7))}>
              Next week
            </button>
            <span className="wf-spacer" />
            <button type="button" className="wf-btn wf-btn--primary"
                    onClick={() => { setDrawer({ kind: 'assign' }); setDrawerError(null) }}>
              Add to roster
            </button>
          </div>

          {conflicts.length > 0 && (
            <div className="wf-notice wf-notice--danger">
              <strong>{conflicts.length} clash{conflicts.length === 1 ? '' : 'es'} already on this roster.</strong>
              <ul className="wf-conflict-list">
                {conflicts.slice(0, 8).map((c, i) => <li key={i}>{c.message}</li>)}
              </ul>
            </div>
          )}

          {!board || board.days.every(d => d.shifts.length === 0) ? (
            <Empty title="Nobody is rostered this week"
                   detail="Add an assignment to start the week's roster. A shift with nobody on it is not shown. An empty board means nothing has been planned yet, not that the ward is empty." />
          ) : (
            <div className="wf-roster">
              {board.days.map(day => (
                <div key={day.date} className="wf-day">
                  <div className="wf-day__head">
                    <span className="wf-day__date">{prettyDate(day.date)}</span>
                    {day.is_holiday && <span className="wf-badge wf-badge--info">Holiday</span>}
                  </div>
                  {day.shifts.length === 0 ? (
                    <p className="wf-day__none">No one rostered</p>
                  ) : day.shifts.map(slot => (
                    <div key={slot.shift_code} className="wf-slot">
                      <div className="wf-slot__head">
                        <span className="wf-slot__name">{slot.shift_name}</span>
                        <span className="wf-slot__counts">
                          {slot.planned_count} planned · {slot.present_count} present
                          {slot.unrecorded_count > 0 && (
                            <span className="wf-slot__gap">
                              {' '}· {slot.unrecorded_count} not recorded
                            </span>
                          )}
                          {(slot.scheduled_count ?? 0) > 0 && (
                            <>{' '}· {slot.scheduled_count} scheduled</>
                          )}
                        </span>
                      </div>
                      <ul className="wf-slot__list">
                        {slot.assignments.map(a => (
                          <li key={a.roster_id} className="wf-assign">
                            <span className="wf-assign__name">{a.employee_name}</span>
                            <span className="wf-assign__meta">
                              {a.designation || a.employee_code}
                              {a.ward ? ` · ${a.ward}` : ''}
                            </span>
                            <Badge status={a.actual_status} />
                            {a.overtime_minutes > 0 && (
                              <span className="wf-assign__ot">
                                +{formatDuration(a.overtime_minutes)} OT
                              </span>
                            )}
                            <button type="button" className="wf-btn wf-btn--tiny"
                                    aria-label={`Remove ${a.employee_name} from ${slot.shift_name}`}
                                    onClick={() => {
                                      setDrawerError(null)
                                      setDrawer({
                                        kind: 'remove', rosterId: a.roster_id, who: a.employee_name,
                                        detail: [a.designation || a.employee_code, a.ward]
                                          .filter(Boolean).join(', '),
                                        shiftName: slot.shift_name, date: day.date,
                                      })
                                    }}>
                              Remove
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {/* ── Attendance ──────────────────────────────────────────────────── */}
      {view === 'attendance' && (
        <section className="wf-section">
          <div className="wf-toolbar">
            <label className="wf-field wf-field--inline">
              <span className="wf-field__label">Date</span>
              <input type="date" className="wf-input" value={attendanceDate}
                     onChange={e => setAttendanceDate(e.target.value)} />
            </label>
            <span className="wf-spacer" />
            <button type="button" className="wf-btn wf-btn--primary"
                    onClick={() => { setDrawer({ kind: 'attendance', row: null }); setDrawerError(null) }}>
              Record a day
            </button>
          </div>

          {attendance.length === 0 ? (
            <Empty title="Nothing recorded for this date"
                   detail="No attendance record exists for anyone on this day. That is NOT the same as everybody being absent. Until a day is recorded it counts as 'not recorded' in the muster and in the payroll export." />
          ) : (
            <div className="wf-table-wrap">
              <table className="wf-table">
                <thead>
                  <tr>
                    <th scope="col">Person</th>
                    <th scope="col">Status</th>
                    <th scope="col">Shift</th>
                    <th scope="col">In</th>
                    <th scope="col">Out</th>
                    <th scope="col" className="wf-num">Worked</th>
                    <th scope="col" className="wf-num">Overtime</th>
                    <th scope="col" className="wf-num">Late</th>
                    <th scope="col"><span className="wf-sr">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {attendance.map(row => (
                    <tr key={row.id}>
                      <td>
                        <span className="wf-cell__primary">{row.employee_name}</span>
                        <span className="wf-cell__meta">{row.employee_code}</span>
                      </td>
                      <td>
                        <Badge status={row.status} />
                        {row.leave_type_code && (
                          <span className="wf-cell__meta">{row.leave_type_code}</span>
                        )}
                      </td>
                      <td>{row.shift_code || '—'}</td>
                      <td>{row.first_in_at ? isoToLocalInput(row.first_in_at).replace('T', ' ') : '—'}</td>
                      <td>{row.last_out_at ? isoToLocalInput(row.last_out_at).replace('T', ' ') : '—'}</td>
                      <td className="wf-num">{formatDuration(row.worked_minutes)}</td>
                      <td className="wf-num">{formatDuration(row.overtime_minutes)}</td>
                      <td className="wf-num">{formatDuration(row.late_minutes)}</td>
                      <td>
                        <button type="button" className="wf-btn wf-btn--tiny"
                                onClick={() => { setDrawer({ kind: 'attendance', row }); setDrawerError(null) }}>
                          Correct
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* ── Monthly muster ──────────────────────────────────────────────── */}
      {view === 'muster' && (
        <section className="wf-section">
          <PeriodPicker year={year} month={month} onYear={setYear} onMonth={setMonth} />

          {muster && (
            <>
              <div className="wf-tiles">
                <Tile value={String(muster.employee_count)} caption="Employees in this period" />
                <Tile value={String(muster.totals.days_present ?? 0)} caption="Days present" />
                <Tile value={String(muster.totals.overtime_hours ?? '0.00')} caption="Overtime hours" />
                <Tile value={String(muster.unrecorded_day_count)}
                      caption="Days not recorded"
                      tone={muster.unrecorded_day_count > 0 ? 'warn' : undefined} />
                {muster.period_closed === false && (
                  <Tile value={String(muster.still_to_come_day_count ?? 0)}
                        caption="Days still to come" />
                )}
              </div>

              {/* Counted to today. Future days are not gaps: nobody can record
                  them yet, so they are reported apart and never flagged. */}
              {muster.period_closed === false && (
                <div className="wf-notice" data-testid="wf-muster-open-month">
                  {muster.counted_through
                    ? <>Counted up to {formatClinicalDate(new Date(`${muster.counted_through}T00:00:00`))}. The rest of the month is still to come, so no one&rsquo;s attendance is complete until it ends.</>
                    : <>This month has not started. Nothing is counted yet.</>}
                </div>
              )}

              {!muster.identity_holds && (
                <div className="wf-notice wf-notice--danger">
                  <strong>The day counts do not add up for at least one employee.</strong>{' '}
                  Do not send a payroll file from this period. Raise it as a defect.
                </div>
              )}

              {muster.employees.length === 0 ? (
                <Empty title="Nobody was employed in this period"
                       detail="Add employment records under People & calendar first." />
              ) : (
                <div className="wf-table-wrap">
                  <table className="wf-table">
                    <thead>
                      <tr>
                        <th scope="col">Person</th>
                        <th scope="col" className="wf-num">Working</th>
                        <th scope="col" className="wf-num">Present</th>
                        <th scope="col" className="wf-num">Absent</th>
                        <th scope="col" className="wf-num">Leave</th>
                        <th scope="col" className="wf-num">Not recorded</th>
                        <th scope="col" className="wf-num">Overtime</th>
                        <th scope="col" className="wf-num">Nights</th>
                      </tr>
                    </thead>
                    <tbody>
                      {muster.employees.map(e => (
                        <MusterRow key={e.employee_code} row={e} />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>
      )}

      {/* ── Payroll export ──────────────────────────────────────────────── */}
      {view === 'export' && (
        <section className="wf-section">
          <PeriodPicker year={year} month={month} onYear={setYear} onMonth={setMonth} />

          {/* The boundary, again, immediately above the download. This is the
              one place in Orb where somebody could reasonably believe a pay
              figure was computed for them. */}
          <div className="wf-statutory">
            <h2 className="wf-statutory__title">What is in this file, and what is not</h2>
            <div className="wf-statutory__cols">
              <div>
                <h3 className="wf-statutory__head">Orb provides</h3>
                <ul className="wf-statutory__list">
                  <li>Employee code and the payroll provider&rsquo;s own reference</li>
                  <li>Days present, absent, on leave (paid and unpaid separately)</li>
                  <li>Days nobody recorded, counted openly and never guessed</li>
                  <li>Hours worked and overtime hours</li>
                  <li>Night, evening, holiday and weekly-off shift counts</li>
                </ul>
              </div>
              <div>
                <h3 className="wf-statutory__head">The payroll provider computes</h3>
                <ul className="wf-statutory__list">
                  <li>Salary, allowances and the value of overtime</li>
                  <li>Provident Fund (employee and employer)</li>
                  <li>ESI (employee and employer)</li>
                  <li>Professional Tax and TDS</li>
                  <li>Payslips and the statutory returns</li>
                </ul>
                <p className="wf-statutory__note">
                  Those columns are present in the file and deliberately EMPTY,
                  and every row is stamped
                  {' '}<code>statutory_computed_by={meta?.statutory_computed_by ?? 'payroll_provider'}</code>.
                  These rules change with each budget and differ by state; a
                  wrong PF figure is a demand notice, not a bug report.
                </p>
              </div>
            </div>
          </div>

          {exportPreview && (
            <>
              <div className="wf-tiles">
                <Tile value={String(exportPreview.employee_count)} caption="Employees in the file" />
                <Tile value={String(exportPreview.incomplete_employee_count)}
                      caption={exportPreview.period_closed === false ? 'Not complete (month not over)' : 'With unrecorded days'}
                      tone={exportPreview.incomplete_employee_count > 0 ? 'warn' : undefined} />
                <Tile value={String(exportPreview.columns.length)} caption="Columns" />
              </div>

              {exportPreview.warnings.map((w, i) => (
                <div key={i} className="wf-notice wf-notice--warn">{w}</div>
              ))}

              <div className="wf-toolbar">
                <span className="wf-file">{exportPreview.file_name}</span>
                <span className="wf-spacer" />
                <button type="button" className="wf-btn wf-btn--primary"
                        disabled={busy || exportPreview.employee_count === 0}
                        onClick={() => { void doGenerate() }}>
                  {busy ? 'Generating…' : 'Generate & download'}
                </button>
              </div>

              {generated && (
                <div className="wf-notice">
                  <strong>{generated.file_name} downloaded.</strong> Checksum{' '}
                  <code className="wf-hash">{generated.sha256}</code>, recorded
                  against this send, so a query about a figure months from now is
                  answered against the exact file the provider holds.
                </div>
              )}

              {exportPreview.rows.length > 0 && (
                <div className="wf-table-wrap">
                  <table className="wf-table wf-table--compact">
                    <thead>
                      <tr>
                        {exportPreview.columns.map(c => (
                          <th key={c} scope="col"
                              className={meta?.statutory_columns.includes(c) ? 'wf-col-empty' : ''}>
                            {c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {exportPreview.rows.slice(0, 25).map((row, i) => (
                        <tr key={i}>
                          {exportPreview.columns.map(c => (
                            <td key={c}
                                className={meta?.statutory_columns.includes(c) ? 'wf-col-empty' : ''}>
                              {row[c] === '' ? '—' : row[c]}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}

          {runs.length > 0 && (
            <>
              <h2 className="wf-section__title">Files already sent</h2>
              <div className="wf-table-wrap">
                <table className="wf-table">
                  <thead>
                    <tr>
                      <th scope="col">File</th>
                      <th scope="col">Period</th>
                      <th scope="col" className="wf-num">Rows</th>
                      <th scope="col" className="wf-num">Incomplete</th>
                      <th scope="col">Checksum</th>
                      <th scope="col">Generated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map(r => (
                      <tr key={r.id}>
                        <td>{r.file_name}</td>
                        <td>{r.period_start} → {r.period_end}</td>
                        <td className="wf-num">{r.row_count}</td>
                        <td className="wf-num">{r.incomplete_employee_count}</td>
                        <td><code className="wf-hash">{r.sha256.slice(0, 16)}…</code></td>
                        <td>{r.generated_at}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>
      )}

      {/* ── People & calendar ───────────────────────────────────────────── */}
      {view === 'people' && (
        <section className="wf-section">
          <div className="wf-toolbar">
            <span className="wf-spacer" />
            <button type="button" className="wf-btn wf-btn--primary"
                    onClick={() => { setDrawer({ kind: 'employee', row: null }); setDrawerError(null) }}>
              Add an employee
            </button>
          </div>

          {employees.length === 0 ? (
            <Empty title="No employment records yet"
                   detail="An employment record is not an Orb login: most of a hospital's payroll (housekeeping, security, kitchen, drivers) never has one. Add them here so they can be rostered and appear in the payroll export." />
          ) : (
            <div className="wf-table-wrap">
              <table className="wf-table">
                <thead>
                  <tr>
                    <th scope="col">Person</th>
                    <th scope="col">Department</th>
                    <th scope="col">Type</th>
                    <th scope="col">Weekly off</th>
                    <th scope="col">Provider ref</th>
                    <th scope="col">Status</th>
                    <th scope="col"><span className="wf-sr">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {employees.map(e => (
                    <tr key={e.employee_code}>
                      <td>
                        <span className="wf-cell__primary">{e.full_name}</span>
                        <span className="wf-cell__meta">{e.employee_code}</span>
                      </td>
                      <td>{e.department || '—'}</td>
                      <td>{humaniseEnum(e.employment_type)}</td>
                      <td>{e.weekly_off_label}</td>
                      <td>{e.payroll_bureau_ref || '—'}</td>
                      <td>
                        <span className={`wf-badge wf-badge--${e.is_active ? 'ok' : 'neutral'}`}>
                          {e.is_active ? 'Employed' : 'Left'}
                        </span>
                      </td>
                      <td>
                        <button type="button" className="wf-btn wf-btn--tiny"
                                onClick={() => { setDrawer({ kind: 'employee', row: e }); setDrawerError(null) }}>
                          Edit
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2 className="wf-section__title">Declared holidays: {year}</h2>
          <HolidayEditor year={year} holidays={holidays}
                         onSaved={() => { void loadHolidays() }} />
        </section>
      )}

      {drawer?.kind === 'assign' && (
        <AssignDrawer meta={meta} employees={employees.filter(e => e.is_active)}
                      date={rosterFrom} onClose={closeDrawer} onSubmit={submitAssign}
                      busy={busy} error={drawerError} conflicts={drawerConflicts} />
      )}
      {drawer?.kind === 'attendance' && (
        <AttendanceDrawer meta={meta} employees={employees} date={attendanceDate}
                          existing={drawer.row} onClose={closeDrawer}
                          onSubmit={submitAttendance} busy={busy} error={drawerError} />
      )}
      {drawer?.kind === 'employee' && (
        <EmployeeDrawer meta={meta} existing={drawer.row} onClose={closeDrawer}
                        onSubmit={submitEmployee} busy={busy} error={drawerError} />
      )}
      {drawer?.kind === 'remove' && (
        <RemoveRosterDrawer who={drawer.who} detail={drawer.detail}
                            shiftName={drawer.shiftName} date={drawer.date}
                            busy={busy} error={drawerError} onClose={closeDrawer}
                            onConfirm={() => { void submitRemove(drawer.rosterId) }} />
      )}
    </div>
  )
}

function PeriodPicker({ year, month, onYear, onMonth }: {
  year: number; month: number; onYear: (y: number) => void; onMonth: (m: number) => void
}) {
  const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December']
  return (
    <div className="wf-toolbar">
      <label className="wf-field wf-field--inline">
        <span className="wf-field__label">Month</span>
        <select className="wf-input" value={month} onChange={e => onMonth(Number(e.target.value))}>
          {months.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
        </select>
      </label>
      <label className="wf-field wf-field--inline">
        <span className="wf-field__label">Year</span>
        <input type="number" className="wf-input wf-input--short" value={year}
               min={2000} max={2100} onChange={e => onYear(Number(e.target.value))} />
      </label>
    </div>
  )
}

function MusterRow({ row }: { row: EmployeeSummary }) {
  return (
    <tr className={row.attendance_complete ? '' : 'wf-row--incomplete'}>
      <td>
        <span className="wf-cell__primary">{row.employee_name}</span>
        <span className="wf-cell__meta">
          {row.employee_code}{row.department ? ` · ${row.department}` : ''}
        </span>
      </td>
      <td className="wf-num">{row.days_working}</td>
      <td className="wf-num">{row.days_present}</td>
      <td className="wf-num">{row.days_absent}</td>
      <td className="wf-num">
        {row.days_leave_total}
        {row.days_leave_unpaid > 0 && (
          <span className="wf-cell__meta">{row.days_leave_unpaid} unpaid</span>
        )}
      </td>
      {/* Never blank and never a dash when it is zero — a column that
          disappears when it is empty is a column people stop checking. */}
      <td className={`wf-num${row.days_unrecorded > 0 ? ' wf-num--warn' : ''}`}>
        {row.days_unrecorded}
      </td>
      <td className="wf-num">{row.overtime_hours}</td>
      <td className="wf-num">{row.night_shift_count}</td>
    </tr>
  )
}

function HolidayEditor({ year, holidays, onSaved }: {
  year: number; holidays: Holiday[]; onSaved: () => void
}) {
  const [day, setDay] = useState(`${year}-01-01`)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const add = async () => {
    setBusy(true); setError(null)
    try {
      await saveHoliday({ holiday_date: day, name })
      setName('')
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save that holiday.')
    } finally { setBusy(false) }
  }

  return (
    <>
      <div className="wf-toolbar">
        <label className="wf-field wf-field--inline">
          <span className="wf-field__label">Date</span>
          <input type="date" className="wf-input" value={day}
                 onChange={e => setDay(e.target.value)} />
        </label>
        <label className="wf-field wf-field--inline">
          <span className="wf-field__label">Name</span>
          <input className="wf-input" value={name} onChange={e => setName(e.target.value)} />
        </label>
        <button type="button" className="wf-btn" disabled={busy || !name.trim()}
                onClick={() => { void add() }}>
          {busy ? 'Saving…' : 'Add holiday'}
        </button>
      </div>
      {error && <div className="wf-notice wf-notice--danger">{error}</div>}
      {holidays.length === 0 ? (
        <Empty title="No declared holidays for this year"
               detail="A declared holiday is removed from everyone's working days, so the monthly muster and the payroll export both depend on this list being right." />
      ) : (
        <ul className="wf-holiday-list">
          {holidays.map(h => (
            <li key={h.holiday_date} className="wf-holiday">
              <span className="wf-holiday__date">{prettyDate(h.holiday_date)}</span>
              <span className="wf-holiday__name">{h.name}</span>
              {h.is_restricted && <span className="wf-badge wf-badge--neutral">Restricted</span>}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
