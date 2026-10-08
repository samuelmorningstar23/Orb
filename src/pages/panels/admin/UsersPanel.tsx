import { useState, useEffect, useCallback, useRef, useSyncExternalStore } from 'react'
import type { FormEvent } from 'react'
import { apiFetch } from '../../../api/client'
import {
  canStaffRole,
  currentClinicalSwitches,
  currentFeatureFlags,
  currentModuleFlags,
  subscribeModuleFlags,
} from '../../../shellConfig'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'

/* ── Types ── */
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
}

interface UsersResponse {
  users: AdminUser[]
  /** Departments patients are admitted under: what a ward scope can match. */
  departments?: string[]
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
function CreateUserModal({ departments, onClose, onCreated }: { departments: string[]; onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState({ id: '', name: '', role: 'doctor' as string, department: '', password: '' })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useFocusTrap<HTMLDivElement>(submitting ? undefined : onClose)
  const offeredRoles = useOfferedRoles()

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
              departments={departments} onChange={(v) => set('department', v)} />
          </div>
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
function ChangeRoleModal({ target, departments, onClose, onDone }: { target: AdminUser; departments: string[]; onClose: () => void; onDone: () => void }) {
  const [role, setRole] = useState(target.role)
  const [department, setDepartment] = useState(target.department || '')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useFocusTrap<HTMLDivElement>(submitting ? undefined : onClose)
  const offeredRoles = useOfferedRoles(target.role)

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
            departments={departments} onChange={setDepartment} />
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
      .then((res) => { setUsers(res.users); setDepartments(res.departments || []) })
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
                  <td><MfaChip on={u.totp_enabled} /></td>
                  <td><StatusChip active={u.is_active} /></td>
                  <td>
                    <div className="admin-row-actions">
                      {/* A portal account's role is fixed: the server refuses
                          turning a patient login into staff or back. */}
                      {u.role !== 'patient' && (
                        <button className="admin-btn-ghost admin-btn-ghost--sm" aria-label={`Change role for ${u.name}`} onClick={() => setRoleTarget(u)}>Role</button>
                      )}
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
          onClose={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); flash('User created'); fetchUsers() }}
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
