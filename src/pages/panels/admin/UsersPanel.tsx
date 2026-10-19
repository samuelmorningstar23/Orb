import { useState, useEffect, useCallback, useRef, useSyncExternalStore } from 'react'
import type { FormEvent } from 'react'
import { apiFetch } from '../../../api/client'
import {
  canStaffRole,
  currentClinicalSwitches,
  currentFeatureFlags,
  currentModuleFlags,
  subscribeModuleFlags,
  usePlatformFlags,
} from '../../../shellConfig'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'

/* ── Types ── */
/** A facility as the server labels it (facility_service.facility_identity). */
interface FacilityLabel {
  id: number
  code: string | null
  name: string | null
}

interface AdminUser {
  id: string
  name: string
  role: string
  department: string | null
  is_active: boolean
  totp_enabled: boolean
  created_at?: string | null
  /** The signed-in admin's own row: no Deactivate (the server refuses it). */
  is_self?: boolean
  /** multisite-6: where the account works. Sent only while the multi-site
   *  switch is on or more than one facility is on record; null for a
   *  department no facility owns. */
  facility?: FacilityLabel | null
  /** Sent only to an administrator who holds group-wide access. */
  enterprise_access?: boolean
  enterprise_grant_id?: number | null
}

interface UsersResponse {
  users: AdminUser[]
  /** Departments patients are admitted under: what a ward scope can match. */
  departments?: string[]
  /* multisite-6, present only with the facility keys above. */
  /** Open facilities this administrator may place staff at. */
  facilities?: FacilityLabel[]
  /** Departments master: department name to the facility that owns it. */
  department_facilities?: Record<string, number>
  /** Where the signed-in administrator works. */
  own_facility_id?: number | null
  /** The signed-in administrator holds group-wide access, so may give or
   *  revoke it (ruling MS-R6 (a)). */
  grants_manageable?: boolean
}

/** What the panel knows about facilities (multisite-6). On a single-site
 *  install with the multi-site switch off (every site today) `show` is false
 *  and the panel is exactly as before. */
interface FacilityContext {
  /** Show where each account works: the switch is on, or more than one
   *  facility is on record. */
  show: boolean
  /** The switch is on: staff may be placed at and moved between facilities. */
  canPlace: boolean
  facilities: FacilityLabel[]
  departmentFacilities: Record<string, number>
  ownFacilityId: number | null
  grantsManageable: boolean
}

const MAIN_FACILITY_ID = 1

const NO_FACILITY: FacilityContext = {
  show: false, canPlace: false, facilities: [], departmentFacilities: {},
  ownFacilityId: null, grantsManageable: false,
}

const facilityName = (f: FacilityLabel | null | undefined) =>
  (f && (f.name || f.code)) || 'Not set'

/** The departments to offer at one facility: the known departments and the
 *  departments master, each where the master says it belongs (a name the
 *  master does not list counts as facility 1's, the facility every account
 *  had before multi-site). */
function departmentsAt(facilityId: number, departments: string[], owners: Record<string, number>): string[] {
  const ownerByKey = new Map(Object.entries(owners).map(([name, fid]) => [name.trim().toLowerCase(), fid]))
  const seen = new Set<string>()
  const out: string[] = []
  for (const name of [...departments, ...Object.keys(owners)]) {
    const key = name.trim().toLowerCase()
    if (!key || seen.has(key)) continue
    seen.add(key)
    if ((ownerByKey.get(key) ?? MAIN_FACILITY_ID) === facilityId) out.push(name)
  }
  return out.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
}

/** The facility to preselect: the administrator's own, when they may place
 *  staff there, else the first one they may. */
function defaultFacilityId(fc: FacilityContext, prefer?: number | null): number {
  const ids = fc.facilities.map(f => f.id)
  if (prefer != null && ids.includes(prefer)) return prefer
  if (fc.ownFacilityId != null && ids.includes(fc.ownFacilityId)) return fc.ownFacilityId
  return ids[0] ?? MAIN_FACILITY_ID
}

// Staff roles, from server/roles.py STORABLE_ROLES (the users.role CHECK
// constraint) minus 'patient': portal accounts are made at admission and the
// server refuses them here. The API 400s anything else, so keep them together.
//
// The last five are the HIS roles (migration 48). Each can be created only
// while its feature is on (server/roles.py ROLE_FEATURE, mirrored in
// src/lib/roles.ts): the server refuses one whose feature is off, so this list
// is filtered through `canStaffRole` before it is offered. The ward clerk is
// also offered on an Orb Ward Record computer (ROLE_FEATURE_ALSO).
const STAFF_ROLES = [
  'doctor', 'nurse', 'admin', 'pharmacist', 'cashier',
  'receptionist', 'ward_clerk', 'lab_technician', 'pathologist', 'phlebotomist',
] as const

const ROLE_LABELS: Record<string, string> = {
  doctor: 'Doctor',
  nurse: 'Nurse',
  admin: 'Administrator',
  pharmacist: 'Pharmacist',
  cashier: 'Cashier',
  receptionist: 'Receptionist',
  ward_clerk: 'Ward clerk',
  lab_technician: 'Lab technician',
  pathologist: 'Pathologist',
  phlebotomist: 'Phlebotomist',
  patient: 'Patient portal',
}

const roleLabel = (role: string) => ROLE_LABELS[role] || role

/** The staff roles this site can staff right now: every role whose feature is
 *  on, plus `keep` (the role a user already holds) so that editing a user whose
 *  feature was switched off later never silently swaps their role for the first
 *  option in the list. Re-renders when /api/health answers. */
function useOfferedRoles(keep?: string): string[] {
  const modules = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)
  const features = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  const clinical = useSyncExternalStore(subscribeModuleFlags, currentClinicalSwitches)
  return STAFF_ROLES.filter(r => r === keep || canStaffRole(r, modules, features, clinical))
}

// Mirrors permissions.DEPARTMENT_REQUIRED_ROLES: for these a blank department
// is not "no ward", it is every ward. The ward clerk moves patients between the
// beds of one department, so a blank department would be every ward.
const DEPARTMENT_REQUIRED = new Set(['doctor', 'nurse', 'ward_clerk'])

const DEPARTMENT_REQUIRED_MSG = 'Department is required for clinical roles'
const WARD_CLERK_DEPARTMENT_MSG = 'Department is required for a ward clerk'

const departmentRequiredMessage = (role: string) =>
  role === 'ward_clerk' ? WARD_CLERK_DEPARTMENT_MSG : DEPARTMENT_REQUIRED_MSG

const ROLE_COLORS: Record<string, { bg: string; color: string; border: string }> = {
  doctor:     { bg: 'var(--accent-glow)', color: 'var(--accent-text)', border: 'var(--accent-glow-strong)' },
  nurse:      { bg: 'color-mix(in srgb, var(--accent-cyan) 10%, transparent)',  color: 'var(--accent-cyan)', border: 'color-mix(in srgb, var(--accent-cyan) 28%, transparent)' },
  admin:      { bg: 'color-mix(in srgb, var(--status-warn) 10%, transparent)',  color: 'var(--status-warn-text)', border: 'color-mix(in srgb, var(--status-warn) 28%, transparent)' },
  pharmacist: { bg: 'color-mix(in srgb, var(--status-ok) 10%, transparent)',  color: 'var(--status-ok-text)', border: 'color-mix(in srgb, var(--status-ok) 28%, transparent)' },
  // Its own colour: without one a cashier took the patient badge (the fallback).
  cashier:    { bg: 'color-mix(in srgb, var(--status-info) 10%, transparent)', color: 'var(--status-info-text)', border: 'color-mix(in srgb, var(--status-info) 28%, transparent)' },
  // The HIS roles reuse existing pairs, so no new colour enters the contrast
  // gate: the office roles take the cashier's, the lab roles the pharmacist's.
  receptionist:   { bg: 'color-mix(in srgb, var(--status-info) 10%, transparent)', color: 'var(--status-info-text)', border: 'color-mix(in srgb, var(--status-info) 28%, transparent)' },
  ward_clerk:     { bg: 'color-mix(in srgb, var(--status-info) 10%, transparent)', color: 'var(--status-info-text)', border: 'color-mix(in srgb, var(--status-info) 28%, transparent)' },
  lab_technician: { bg: 'color-mix(in srgb, var(--status-ok) 10%, transparent)',  color: 'var(--status-ok-text)', border: 'color-mix(in srgb, var(--status-ok) 28%, transparent)' },
  pathologist:    { bg: 'color-mix(in srgb, var(--status-ok) 10%, transparent)',  color: 'var(--status-ok-text)', border: 'color-mix(in srgb, var(--status-ok) 28%, transparent)' },
  phlebotomist:   { bg: 'color-mix(in srgb, var(--status-ok) 10%, transparent)',  color: 'var(--status-ok-text)', border: 'color-mix(in srgb, var(--status-ok) 28%, transparent)' },
  patient:   { bg: 'color-mix(in srgb, var(--text-muted) 10%, transparent)', color: 'var(--text-muted)', border: 'color-mix(in srgb, var(--text-muted) 28%, transparent)' },
}

function RoleChip({ role }: { role: string }) {
  const s = ROLE_COLORS[role] || ROLE_COLORS.patient
  return (
    <span className="admin-badge" style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {roleLabel(role)}
    </span>
  )
}

/** Department entry: free text (a new hospital has no admissions yet) with the
 *  known departments offered, and a note when the value matches none of them,
 *  because a department no patient is admitted under scopes to no one. */
function DepartmentField({
  id, role, value, departments, onChange, autoFocus,
}: {
  id: string
  role: string
  value: string
  departments: string[]
  onChange: (v: string) => void
  autoFocus?: boolean
}) {
  const required = DEPARTMENT_REQUIRED.has(role)
  const typed = value.trim()
  const unknown = typed !== '' && departments.length > 0
    && !departments.some((d) => d.toLowerCase() === typed.toLowerCase())
  const hintId = `${id}-hint`
  return (
    <label className="admin-field">
      <span className="admin-field__label">
        Department {!required && <span className="admin-field__opt">(optional)</span>}
      </span>
      <input className="admin-filter-input" list={`${id}-options`} value={value} required={required}
        autoFocus={autoFocus} aria-describedby={unknown ? hintId : undefined}
        onChange={(e) => onChange(e.target.value)} placeholder={departments[0] || 'Cardiology'} />
      <datalist id={`${id}-options`}>
        {departments.map((d) => <option key={d} value={d} />)}
      </datalist>
      {unknown && (
        <span id={hintId} className="admin-field__opt">
          No patient is admitted under this department yet, so this account will see no patients until one is.
        </span>
      )}
    </label>
  )
}

/** Facility picker (multisite-6). For staff without a department it is their
 *  home facility; for staff with one it narrows the department list, because
 *  a department belongs to one facility. The hint says which. */
function FacilitySelect({
  id, value, facilities, hint, onChange, autoFocus,
}: {
  id: string
  value: number
  facilities: FacilityLabel[]
  hint?: string
  onChange: (v: number) => void
  autoFocus?: boolean
}) {
  const hintId = `${id}-hint`
  return (
    <div className="admin-field">
      <label className="admin-field__label" htmlFor={id}>Facility</label>
      <select id={id} className="admin-filter-select" value={value} autoFocus={autoFocus}
        aria-describedby={hint ? hintId : undefined}
        onChange={(e) => onChange(Number(e.target.value))}>
        {facilities.map((f) => <option key={f.id} value={f.id}>{facilityName(f)}</option>)}
      </select>
      {hint && <span id={hintId} className="admin-field__opt">{hint}</span>}
    </div>
  )
}

/** Marks an administrator who holds group-wide access. Neutral tokens only,
 *  so no new colour pair enters the contrast gate. */
function GroupAccessChip() {
  return (
    <span className="admin-badge"
      style={{ background: 'var(--bg-glass)', color: 'var(--text-primary)', border: '1px solid var(--border-subtle)', marginLeft: 6 }}>
      All facilities
    </span>
  )
}

function StatusChip({ active }: { active: boolean }) {
  return (
    <span
      className="admin-badge"
      style={
        active
          ? { background: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', color: 'var(--status-ok-text)', border: '1px solid color-mix(in srgb, var(--status-ok) 28%, transparent)' }
          : { background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', color: 'var(--status-danger-text)', border: '1px solid color-mix(in srgb, var(--status-danger) 28%, transparent)' }
      }
    >
      {active ? 'Active' : 'Disabled'}
    </span>
  )
}

function MfaChip({ on }: { on: boolean }) {
  return (
    <span
      className="admin-badge"
      style={
        on
          ? { background: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', color: 'var(--status-ok-text)', border: '1px solid color-mix(in srgb, var(--status-ok) 28%, transparent)' }
          : /* --text-muted measured 1.56:1 on the amber-tinted failure rows —
               the theme-adaptive primary token stays readable on every row tint */
            { background: 'var(--bg-glass)', color: 'var(--text-primary)', border: '1px solid var(--border-subtle)' }
      }
    >
      {on ? 'On' : 'Off'}
    </span>
  )
}

/* ── Generic confirm dialog ── */
function ConfirmDialog({
  title, message, confirmLabel, danger, busy, error, onConfirm, onClose,
}: {
  title: string
  message: string
  confirmLabel: string
  danger?: boolean
  busy?: boolean
  /** Shown inside the dialog: a toast would render behind the overlay. */
  error?: string | null
  onConfirm: () => void
  onClose: () => void
}) {
  const ref = useFocusTrap<HTMLDivElement>(busy ? undefined : onClose)
  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose() }}>
      <div ref={ref} className="admin-modal admin-modal--sm" role="dialog" aria-modal="true" aria-labelledby="admin-confirm-title">
        <h3 id="admin-confirm-title" className="admin-modal__title">{title}</h3>
        <p className="admin-modal__text">{message}</p>
        {error && <p className="admin-form__error" role="alert">{error}</p>}
        <div className="admin-modal__actions">
          <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={busy}>Cancel</button>
          <button
            type="button"
            className={danger ? 'admin-btn-danger' : 'admin-filter-btn'}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ── Create user modal ── */
function CreateUserModal({ departments, fc = NO_FACILITY, onClose, onCreated }: { departments: string[]; fc?: FacilityContext; onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState({ id: '', name: '', role: 'doctor' as string, department: '', password: '' })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useFocusTrap<HTMLDivElement>(submitting ? undefined : onClose)
  const offeredRoles = useOfferedRoles()
  // multisite-6: with the multi-site switch on, a facility is chosen. It is
  // sent as the home facility only for staff without a department; a
  // department's own facility is the only source for everyone else.
  const placing = fc.canPlace && fc.facilities.length > 0
  const [facilityId, setFacilityId] = useState(() => defaultFacilityId(fc))
  const deptOptions = placing ? departmentsAt(facilityId, departments, fc.departmentFacilities) : departments

  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }))

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (form.password.length < 8) { setError('Password must be at least 8 characters.'); return }
    if (DEPARTMENT_REQUIRED.has(form.role) && !form.department.trim()) { setError(departmentRequiredMessage(form.role)); return }
    setSubmitting(true)
    try {
      await apiFetch('/admin/users', {
        method: 'POST',
        body: JSON.stringify({
          id: form.id.trim(),
          name: form.name.trim(),
          role: form.role,
          department: form.department.trim() || undefined,
          password: form.password,
          facility_id: placing && !form.department.trim() ? facilityId : undefined,
        }),
      })
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create user')
      setSubmitting(false)
    }
  }

  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !submitting) onClose() }}>
      <div ref={ref} className="admin-modal" role="dialog" aria-modal="true" aria-labelledby="admin-create-title">
        <h3 id="admin-create-title" className="admin-modal__title">Create User</h3>
        <form className="admin-form" onSubmit={submit}>
          <label className="admin-field">
            <span className="admin-field__label">User ID</span>
            <input className="admin-filter-input" autoFocus required value={form.id}
              onChange={(e) => set('id', e.target.value)} placeholder="e.g. DR-0142" />
          </label>
          <label className="admin-field">
            <span className="admin-field__label">Full name</span>
            <input className="admin-filter-input" required value={form.name}
              onChange={(e) => set('name', e.target.value)} placeholder="Dr. Jane Doe" />
          </label>
          <div className="admin-form__row">
            <label className="admin-field">
              <span className="admin-field__label">Role</span>
              <select className="admin-filter-select" value={form.role} onChange={(e) => set('role', e.target.value)}>
                {offeredRoles.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
              </select>
            </label>
            <DepartmentField id="admin-create-dept" role={form.role} value={form.department}
              departments={deptOptions} onChange={(v) => set('department', v)} />
          </div>
          {placing && (
            <FacilitySelect id="admin-create-facility" value={facilityId} facilities={fc.facilities}
              onChange={setFacilityId}
              hint={form.department.trim()
                ? "Staff with a department work at that department's facility."
                : 'Staff without a department work at this facility.'} />
          )}
          <label className="admin-field">
            <span className="admin-field__label">Temporary password</span>
            <input className="admin-filter-input" type="text" required minLength={8} value={form.password}
              onChange={(e) => set('password', e.target.value)} placeholder="At least 8 characters" />
          </label>
          {error && <p className="admin-form__error" role="alert">{error}</p>}
          <div className="admin-modal__actions">
            <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className="admin-filter-btn" disabled={submitting}>
              {submitting ? 'Creating…' : 'Create user'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ── Reset password modal ── */
function generatePassword(): string {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'
  const buf = new Uint32Array(16)
  crypto.getRandomValues(buf)
  return Array.from(buf, (n) => chars[n % chars.length]).join('')
}

function ResetPasswordModal({ target, onClose, onDone }: { target: AdminUser; onClose: () => void; onDone: () => void }) {
  const [pw, setPw] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const ref = useFocusTrap<HTMLDivElement>(submitting ? undefined : onClose)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (pw.length < 8) { setError('Password must be at least 8 characters.'); return }
    setSubmitting(true)
    try {
      await apiFetch(`/admin/users/${encodeURIComponent(target.id)}/reset-password`, {
        method: 'POST',
        body: JSON.stringify({ new_password: pw }),
      })
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reset password')
      setSubmitting(false)
    }
  }

  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !submitting) onClose() }}>
      <div ref={ref} className="admin-modal admin-modal--sm" role="dialog" aria-modal="true" aria-labelledby="admin-reset-title">
        <h3 id="admin-reset-title" className="admin-modal__title">Reset password</h3>
        <p className="admin-modal__text">
          Set a new password for <strong>{target.name}</strong> ({target.id}). This revokes all of their active sessions.
        </p>
        <form className="admin-form" onSubmit={submit}>
          <label className="admin-field">
            <span className="admin-field__label">New password</span>
            <div className="admin-field__inline">
              <input className="admin-filter-input" type="text" required minLength={8} value={pw}
                autoFocus onChange={(e) => { setPw(e.target.value); setCopied(false) }} placeholder="Type or generate" />
              <button type="button" className="admin-btn-ghost admin-btn-ghost--sm"
                onClick={() => { setPw(generatePassword()); setCopied(false) }}>Generate</button>
              <button type="button" className="admin-btn-ghost admin-btn-ghost--sm" disabled={!pw}
                onClick={() => { navigator.clipboard.writeText(pw).then(() => setCopied(true)).catch(() => {}) }}>
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          </label>
          {error && <p className="admin-form__error" role="alert">{error}</p>}
          <div className="admin-modal__actions">
            <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className="admin-filter-btn" disabled={submitting}>
              {submitting ? 'Saving…' : 'Set password'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ── Change role modal ── */
function ChangeRoleModal({ target, departments, fc = NO_FACILITY, onClose, onDone }: { target: AdminUser; departments: string[]; fc?: FacilityContext; onClose: () => void; onDone: () => void }) {
  const [role, setRole] = useState(target.role)
  const [department, setDepartment] = useState(target.department || '')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useFocusTrap<HTMLDivElement>(submitting ? undefined : onClose)
  const offeredRoles = useOfferedRoles(target.role)
  // multisite-6: offer the departments of the facility the account works at.
  // A department of another facility can still be typed; the server decides.
  const deptOptions = fc.canPlace && fc.facilities.length > 0
    ? departmentsAt(target.facility?.id ?? defaultFacilityId(fc), departments, fc.departmentFacilities)
    : departments

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (DEPARTMENT_REQUIRED.has(role) && !department.trim()) { setError(departmentRequiredMessage(role)); return }
    setSubmitting(true)
    try {
      await apiFetch(`/admin/users/${encodeURIComponent(target.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ role, department: department.trim() }),
      })
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update user')
      setSubmitting(false)
    }
  }

  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !submitting) onClose() }}>
      <div ref={ref} className="admin-modal admin-modal--sm" role="dialog" aria-modal="true" aria-labelledby="admin-role-title">
        <h3 id="admin-role-title" className="admin-modal__title">Change role &amp; department</h3>
        <p className="admin-modal__text">Updating <strong>{target.name}</strong> ({target.id}).</p>
        <form className="admin-form" onSubmit={submit}>
          <label className="admin-field">
            <span className="admin-field__label">Role</span>
            <select className="admin-filter-select" autoFocus value={role} onChange={(e) => setRole(e.target.value)}>
              {offeredRoles.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
            </select>
          </label>
          <DepartmentField id="admin-role-dept" role={role} value={department}
            departments={deptOptions} onChange={setDepartment} />
          {error && <p className="admin-form__error" role="alert">{error}</p>}
          <div className="admin-modal__actions">
            <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className="admin-filter-btn" disabled={submitting}>
              {submitting ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ── Move facility modal (multisite-6) ── */
function MoveFacilityModal({ target, fc, onClose, onDone }: { target: AdminUser; fc: FacilityContext; onClose: () => void; onDone: () => void }) {
  const [facilityId, setFacilityId] = useState(() => defaultFacilityId(fc, target.facility?.id))
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useFocusTrap<HTMLDivElement>(submitting ? undefined : onClose)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await apiFetch(`/admin/users/${encodeURIComponent(target.id)}/facility`, {
        method: 'POST',
        body: JSON.stringify({ facility_id: facilityId, reason: reason.trim() || undefined }),
      })
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to change the facility')
      setSubmitting(false)
    }
  }

  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !submitting) onClose() }}>
      <div ref={ref} className="admin-modal admin-modal--sm" role="dialog" aria-modal="true" aria-labelledby="admin-facility-title">
        <h3 id="admin-facility-title" className="admin-modal__title">Change facility</h3>
        <p className="admin-modal__text">
          <strong>{target.name}</strong> ({target.id}) works at {facilityName(target.facility)}. Their
          patients and screens follow the facility you choose.
        </p>
        <form className="admin-form" onSubmit={submit}>
          <FacilitySelect id="admin-move-facility" value={facilityId} facilities={fc.facilities}
            onChange={setFacilityId} autoFocus />
          <label className="admin-field">
            <span className="admin-field__label">Reason <span className="admin-field__opt">(optional)</span></span>
            <input className="admin-filter-input" value={reason} maxLength={500}
              onChange={(e) => setReason(e.target.value)} placeholder="Joined the satellite pharmacy" />
          </label>
          {error && <p className="admin-form__error" role="alert">{error}</p>}
          <div className="admin-modal__actions">
            <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className="admin-filter-btn" disabled={submitting}>
              {submitting ? 'Saving…' : 'Change facility'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ── Group-wide access modal (multisite-6, ruling MS-R6 (a)) ── */
function GroupAccessModal({ target, onClose, onDone }: { target: AdminUser; onClose: () => void; onDone: (granted: boolean) => void }) {
  const revoking = !!target.enterprise_access
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useFocusTrap<HTMLDivElement>(submitting ? undefined : onClose)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (reason.trim().length < 3) { setError('Give a reason of at least 3 characters.'); return }
    setSubmitting(true)
    try {
      if (revoking) {
        await apiFetch(`/admin/users/enterprise-grants/${encodeURIComponent(String(target.enterprise_grant_id))}/revoke`, {
          method: 'POST',
          body: JSON.stringify({ reason: reason.trim() }),
        })
      } else {
        await apiFetch('/admin/users/enterprise-grants', {
          method: 'POST',
          body: JSON.stringify({ user_id: target.id, reason: reason.trim() }),
        })
      }
      onDone(!revoking)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to change access')
      setSubmitting(false)
    }
  }

  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !submitting) onClose() }}>
      <div ref={ref} className="admin-modal admin-modal--sm" role="dialog" aria-modal="true" aria-labelledby="admin-group-title">
        <h3 id="admin-group-title" className="admin-modal__title">
          {revoking ? 'Remove access to all facilities' : 'Give access to all facilities'}
        </h3>
        <p className="admin-modal__text">
          {revoking
            ? <><strong>{target.name}</strong> ({target.id}) will see only their own facility from now on.</>
            : <><strong>{target.name}</strong> ({target.id}) will see every facility&apos;s patients and staff, and can give or remove this access for other administrators. Every change is recorded.</>}
        </p>
        <form className="admin-form" onSubmit={submit}>
          <label className="admin-field">
            <span className="admin-field__label">Reason</span>
            <input className="admin-filter-input" required value={reason} maxLength={500} autoFocus
              onChange={(e) => setReason(e.target.value)} placeholder="Group IT lead" />
          </label>
          {error && <p className="admin-form__error" role="alert">{error}</p>}
          <div className="admin-modal__actions">
            <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className={revoking ? 'admin-btn-danger' : 'admin-filter-btn'} disabled={submitting}>
              {submitting ? 'Saving…' : revoking ? 'Remove access' : 'Give access'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ── Main panel ── */
type ConfirmState =
  | { kind: 'deactivate'; user: AdminUser }
  | { kind: 'activate'; user: AdminUser }
  | null

export default function UsersPanel() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [departments, setDepartments] = useState<string[]>([])
  const [showPatients, setShowPatients] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [roleTarget, setRoleTarget] = useState<AdminUser | null>(null)
  const [confirm, setConfirm] = useState<ConfirmState>(null)
  const [confirmBusy, setConfirmBusy] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // multisite-6: what the server said about facilities, the platform flags
  // from /api/health, and the two facility dialogs.
  const [facilityInfo, setFacilityInfo] = useState<UsersResponse | null>(null)
  const [moveTarget, setMoveTarget] = useState<AdminUser | null>(null)
  const [groupTarget, setGroupTarget] = useState<AdminUser | null>(null)
  const platform = usePlatformFlags()
  const fc: FacilityContext = (platform.multiSite || platform.multiFacilityData)
    ? {
        show: true,
        canPlace: platform.multiSite,
        facilities: facilityInfo?.facilities ?? [],
        departmentFacilities: facilityInfo?.department_facilities ?? {},
        ownFacilityId: facilityInfo?.own_facility_id ?? null,
        grantsManageable: !!facilityInfo?.grants_manageable,
      }
    : NO_FACILITY

  const flash = useCallback((msg: string) => {
    setToast(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }, [])

  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current) }, [])

  const fetchUsers = useCallback(() => {
    setLoading(true)
    setError(null)
    apiFetch<UsersResponse>(showPatients ? '/admin/users?include_patients=true' : '/admin/users')
      .then((res) => { setUsers(res.users); setDepartments(res.departments || []); setFacilityInfo(res) })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load users'))
      .finally(() => setLoading(false))
  }, [showPatients])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchUsers() }, [fetchUsers])

  const runConfirm = async () => {
    if (!confirm) return
    setConfirmBusy(true)
    setConfirmError(null)
    const { kind, user } = confirm
    const path = `/admin/users/${encodeURIComponent(user.id)}/${kind}`
    try {
      await apiFetch(path, { method: 'POST' })
      setConfirm(null)
      flash(kind === 'deactivate' ? `${user.name} deactivated` : `${user.name} activated`)
      fetchUsers()
    } catch (e) {
      setConfirmError(e instanceof Error ? e.message : 'Action failed')
    } finally {
      setConfirmBusy(false)
    }
  }

  return (
    <div className="admin-security">
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px' }}>
        <div className="admin-filter-row">
          <span className="admin-section__title">User Management</span>
          <span className="admin-page-info">{users.length} user{users.length === 1 ? '' : 's'}</span>
          <label className="admin-page-info" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, minHeight: 24 }}>
            <input type="checkbox" checked={showPatients} onChange={(e) => setShowPatients(e.target.checked)} />
            Show patient portal accounts
          </label>
        </div>
        <button className="admin-filter-btn" onClick={() => setShowCreate(true)}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Create user
        </button>
      </div>

      {loading ? (
        <AdminLoadingState message="Loading users…" />
      ) : error ? (
        <AdminErrorState error={error} onRetry={fetchUsers} />
      ) : users.length === 0 ? (
        <AdminEmptyState title="No users" message="Create the first user to get started." />
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>ID</th>
                <th>Role</th>
                <th>Department</th>
                {fc.show && <th>Facility</th>}
                <th>MFA</th>
                <th>Status</th>
                <th style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className={u.is_active ? undefined : 'admin-tr--failure'}>
                  <td style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{u.name}</td>
                  <td className="admin-td--mono admin-td--small">{u.id}</td>
                  <td><RoleChip role={u.role} /></td>
                  <td>{u.department || <span style={{ color: 'var(--text-muted)' }}>—</span>}</td>
                  {fc.show && (
                    <td>
                      {u.facility
                        ? facilityName(u.facility)
                        : <span style={{ color: 'var(--text-muted)' }}>Not set</span>}
                      {u.enterprise_access && <GroupAccessChip />}
                    </td>
                  )}
                  <td><MfaChip on={u.totp_enabled} /></td>
                  <td><StatusChip active={u.is_active} /></td>
                  <td>
                    <div className="admin-row-actions">
                      {/* A portal account's role is fixed: the server refuses
                          turning a patient login into staff or back. */}
                      {u.role !== 'patient' && (
                        <button className="admin-btn-ghost admin-btn-ghost--sm" aria-label={`Change role for ${u.name}`} onClick={() => setRoleTarget(u)}>Role</button>
                      )}
                      {/* multisite-6: only staff without a department have a
                          home facility; a department's facility is its own. */}
                      {fc.canPlace && fc.facilities.length > 0 && u.role !== 'patient' && !u.department && (
                        <button className="admin-btn-ghost admin-btn-ghost--sm" aria-label={`Change facility for ${u.name}`} onClick={() => setMoveTarget(u)}>Facility</button>
                      )}
                      {/* Group-wide access: shown only to an administrator who
                          holds it. Giving needs the multi-site switch; removing
                          never does. */}
                      {fc.grantsManageable && u.role === 'admin' && (u.enterprise_access
                        ? <button className="admin-btn-ghost admin-btn-ghost--sm" aria-label={`Remove access to all facilities for ${u.name}`} onClick={() => setGroupTarget(u)}>Remove group access</button>
                        : (fc.canPlace && u.is_active && (
                          <button className="admin-btn-ghost admin-btn-ghost--sm" aria-label={`Give access to all facilities to ${u.name}`} onClick={() => setGroupTarget(u)}>Group access</button>
                        )))}
                      <button className="admin-btn-ghost admin-btn-ghost--sm" aria-label={`Reset password for ${u.name}`} onClick={() => setResetTarget(u)}>Reset password</button>
                      {u.is_self ? null : u.is_active ? (
                        <button className="admin-btn-danger admin-btn-danger--sm" onClick={() => { setConfirmError(null); setConfirm({ kind: 'deactivate', user: u }) }}>Deactivate</button>
                      ) : (
                        <button className="admin-btn-ghost admin-btn-ghost--sm" onClick={() => { setConfirmError(null); setConfirm({ kind: 'activate', user: u }) }}>Activate</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {toast && <div className="admin-toast" role="status">{toast}</div>}

      {showCreate && (
        <CreateUserModal
          departments={departments}
          fc={fc}
          onClose={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); flash('User created'); fetchUsers() }}
        />
      )}
      {moveTarget && (
        <MoveFacilityModal
          target={moveTarget}
          fc={fc}
          onClose={() => setMoveTarget(null)}
          onDone={() => { setMoveTarget(null); flash('Facility changed'); fetchUsers() }}
        />
      )}
      {groupTarget && (
        <GroupAccessModal
          target={groupTarget}
          onClose={() => setGroupTarget(null)}
          onDone={(granted) => { setGroupTarget(null); flash(granted ? 'Access to all facilities given' : 'Access to all facilities removed'); fetchUsers() }}
        />
      )}
      {resetTarget && (
        <ResetPasswordModal
          target={resetTarget}
          onClose={() => setResetTarget(null)}
          onDone={() => { setResetTarget(null); flash('Password reset'); fetchUsers() }}
        />
      )}
      {roleTarget && (
        <ChangeRoleModal
          target={roleTarget}
          departments={departments}
          fc={fc}
          onClose={() => setRoleTarget(null)}
          onDone={() => { setRoleTarget(null); flash('User updated'); fetchUsers() }}
        />
      )}
      {confirm && (
        <ConfirmDialog
          title={confirm.kind === 'deactivate' ? 'Deactivate user?' : 'Activate user?'}
          message={
            confirm.kind === 'deactivate'
              ? `${confirm.user.name} (${confirm.user.id}) will be blocked from logging in and all their active sessions will be revoked.`
              : `${confirm.user.name} (${confirm.user.id}) will be able to log in again.`
          }
          confirmLabel={confirm.kind === 'deactivate' ? 'Deactivate' : 'Activate'}
          danger={confirm.kind === 'deactivate'}
          busy={confirmBusy}
          error={confirmError}
          onConfirm={runConfirm}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  )
}
