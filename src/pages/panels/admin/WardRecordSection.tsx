/**
 * Ward Record: each ward's observation interval and grace window, as the
 * nursing superintendent set them in writing, and the monthly rounds file for
 * the quality team (CSV or Markdown).
 *
 * The file is worked out on the server from the times written on the paper
 * sheet. This screen only sets the interval and asks for the file; it shows
 * no counts of its own, so nothing here can disagree with the document.
 */
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { formatClinicalDateTime } from '../../../lib/datetime'
import {
  fetchRoundsFile,
  getWardSettings,
  monthRange,
  saveTextFile,
  saveWardSetting,
} from '../../../api/wardRecord'
import type { WardSettingRow, WardSettingsResponse } from '../../../api/wardRecord'

function lastMonth(today: Date = new Date()): string {
  const d = new Date(today.getFullYear(), today.getMonth() - 1, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function everyWords(minutes: number): string {
  if (minutes % 60 === 0) {
    const hours = minutes / 60
    return hours === 1 ? 'every hour' : `every ${hours} hours`
  }
  return `every ${minutes} minutes`
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

interface EditState {
  ward: string
  interval: string
  grace: string
  setBy: string
  note: string
}

export default function WardRecordSection() {
  const [data, setData] = useState<WardSettingsResponse | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [edit, setEdit] = useState<EditState | null>(null)
  const [saving, setSaving] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)
  const [fileWard, setFileWard] = useState('')
  const [month, setMonth] = useState(lastMonth())
  const [withNumbers, setWithNumbers] = useState(false)
  const [fileBusy, setFileBusy] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)
  const [fileDone, setFileDone] = useState<string | null>(null)

  const [reloadKey, setReloadKey] = useState(0)
  const load = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let alive = true
    getWardSettings()
      .then((res) => {
        if (!alive) return
        setLoadError(null)
        setData(res)
        setFileWard((current) => current || res.wards.find((w) => w.setting)?.ward || '')
      })
      .catch((err) => { if (alive) setLoadError(errorText(err, 'Could not load the wards.')) })
    return () => { alive = false }
  }, [reloadKey])

  const startEdit = (row: WardSettingRow) => {
    setEditError(null)
    setEdit({
      ward: row.ward,
      interval: String(row.setting?.interval_minutes ?? ''),
      grace: String(row.setting?.grace_minutes ?? data?.limits.default_grace_minutes ?? 30),
      setBy: row.setting?.set_by ?? '',
      note: row.setting?.note ?? '',
    })
  }

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!edit) return
    const interval = Number(edit.interval)
    const grace = Number(edit.grace)
    if (!Number.isInteger(interval) || !Number.isInteger(grace)) {
      setEditError('Give the interval and the grace window as whole minutes.')
      return
    }
    if (!edit.setBy.trim()) {
      setEditError('Say who set this in writing.')
      return
    }
    setSaving(true)
    setEditError(null)
    try {
      await saveWardSetting(edit.ward, {
        interval_minutes: interval,
        grace_minutes: grace,
        set_by: edit.setBy.trim(),
        note: edit.note.trim() || null,
      })
      setEdit(null)
      load()
    } catch (err) {
      setEditError(errorText(err, 'Could not save the interval.'))
    } finally {
      setSaving(false)
    }
  }

  const download = async (format: 'csv' | 'md') => {
    setFileError(null)
    setFileDone(null)
    const range = monthRange(month)
    if (!fileWard || !range) {
      setFileError('Choose a ward and a month.')
      return
    }
    setFileBusy(true)
    try {
      const file = await fetchRoundsFile({
        ward: fileWard, start: range.start, end: range.end, format,
        includePatientNumbers: withNumbers,
      })
      saveTextFile(file.fileName, file.text,
                   format === 'csv' ? 'text/csv;charset=utf-8' : 'text/markdown;charset=utf-8')
      setFileDone(file.reference ? `Saved. Reference ${file.reference}.` : 'Saved.')
    } catch (err) {
      setFileError(errorText(err, 'Could not make the rounds file.'))
    } finally {
      setFileBusy(false)
    }
  }

  const wards = data?.wards ?? []

  return (
    <section aria-label="Ward Record" style={{ marginTop: 20 }}>
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px' }}>
        <div className="admin-filter-row">
          <span className="admin-section__title">Ward Record</span>
          <span className="admin-page-info">Observation rounds, from the paper sheet</span>
        </div>
      </div>

      <div className="glass-card" style={{ padding: 16, marginTop: 8 }}>
        <p className="admin-modal__text" style={{ marginTop: 0 }}>
          Each ward's observations are due at a fixed interval set in writing by the
          nursing superintendent, with a grace window before a round counts as late.
          The rounds file uses only these times and the times written on the sheet.
        </p>

        {loadError && <p className="admin-form__error" role="alert">{loadError}</p>}

        {data && wards.length === 0 && (
          <p className="admin-modal__text">No wards yet. Wards appear here once a patient is on one.</p>
        )}

        {wards.length > 0 && (
          <div className="admin-table-wrap" style={{ marginTop: 12 }}>
            <table className="admin-table" aria-label="Ward observation intervals">
              <thead>
                <tr>
                  <th>Ward</th>
                  <th>Observations due</th>
                  <th>Grace window</th>
                  <th>Set in writing by</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {wards.map((row) => (
                  <tr key={row.ward}>
                    <td>{row.ward}</td>
                    <td>{row.setting ? everyWords(row.setting.interval_minutes) : 'Not set'}</td>
                    <td>{row.setting ? `${row.setting.grace_minutes} minutes` : ''}</td>
                    <td>
                      {row.setting
                        ? `${row.setting.set_by}, entered ${formatClinicalDateTime(row.setting.set_at) || row.setting.set_at}`
                        : ''}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="admin-btn-ghost admin-btn-ghost--sm"
                        onClick={() => startEdit(row)}
                        aria-label={`Change the interval for ${row.ward}`}
                      >
                        {row.setting ? 'Change' : 'Set'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {edit && (
          <form className="admin-form" onSubmit={save} style={{ marginTop: 12 }}
                aria-label={`Observation interval for ${edit.ward}`}>
            <p className="admin-modal__text" style={{ marginTop: 0 }}>
              <strong>{edit.ward}</strong>: copy these from the nursing superintendent's written order.
            </p>
            <div className="admin-form__row">
              <label className="admin-field">
                <span className="admin-field__label">Observations due every (minutes)</span>
                <input
                  className="admin-filter-input"
                  type="number"
                  min={data?.limits.interval_minutes[0] ?? 30}
                  max={data?.limits.interval_minutes[1] ?? 1440}
                  step={1}
                  value={edit.interval}
                  onChange={(e) => setEdit({ ...edit, interval: e.target.value })}
                />
              </label>
              <label className="admin-field">
                <span className="admin-field__label">Grace window (minutes)</span>
                <input
                  className="admin-filter-input"
                  type="number"
                  min={data?.limits.grace_minutes[0] ?? 0}
                  max={data?.limits.grace_minutes[1] ?? 240}
                  step={1}
                  value={edit.grace}
                  onChange={(e) => setEdit({ ...edit, grace: e.target.value })}
                />
              </label>
            </div>
            <label className="admin-field">
              <span className="admin-field__label">Set in writing by</span>
              <input
                className="admin-filter-input"
                value={edit.setBy}
                onChange={(e) => setEdit({ ...edit, setBy: e.target.value })}
                placeholder="Name of the nursing superintendent"
              />
            </label>
            <label className="admin-field">
              <span className="admin-field__label">Note (optional)</span>
              <input
                className="admin-filter-input"
                value={edit.note}
                onChange={(e) => setEdit({ ...edit, note: e.target.value })}
                placeholder="For example the memo number"
              />
            </label>
            {editError && <p className="admin-form__error" role="alert">{editError}</p>}
            <div className="admin-modal__actions">
              <button type="button" className="admin-btn-ghost" onClick={() => setEdit(null)} disabled={saving}>
                Cancel
              </button>
              <button type="submit" className="admin-filter-btn" disabled={saving}>
                {saving ? 'Saving…' : 'Save interval'}
              </button>
            </div>
          </form>
        )}

        <div style={{ marginTop: 16 }}>
          <span className="admin-section__title">Monthly rounds file</span>
          <p className="admin-modal__text" style={{ marginTop: 4 }}>
            Shift by shift: which rounds were due, done, late or missed, which readings
            were left blank or could not be read, and how long the clerk took to key them.
          </p>
          <div className="admin-field__inline" style={{ marginTop: 8, flexWrap: 'wrap', gap: 8 }}>
            <label className="admin-field">
              <span className="admin-field__label">Ward</span>
              <select
                className="admin-filter-input"
                value={fileWard}
                onChange={(e) => { setFileWard(e.target.value); setFileError(null) }}
              >
                <option value="">Choose a ward</option>
                {wards.map((w) => (
                  <option key={w.ward} value={w.ward}>{w.ward}</option>
                ))}
              </select>
            </label>
            <label className="admin-field">
              <span className="admin-field__label">Month</span>
              <input
                className="admin-filter-input"
                type="month"
                value={month}
                onChange={(e) => { setMonth(e.target.value); setFileError(null) }}
              />
            </label>
            <label className="admin-field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
              <input
                type="checkbox"
                checked={withNumbers}
                onChange={(e) => setWithNumbers(e.target.checked)}
              />
              <span>Show hospital patient numbers in the file</span>
            </label>
          </div>
          <div className="admin-field__inline" style={{ marginTop: 8, gap: 8 }}>
            <button
              type="button"
              className="admin-filter-btn"
              onClick={() => { void download('csv') }}
              disabled={fileBusy}
            >
              Download the rounds file (CSV)
            </button>
            <button
              type="button"
              className="admin-filter-btn"
              onClick={() => { void download('md') }}
              disabled={fileBusy}
            >
              Download the rounds file (Markdown)
            </button>
          </div>
          {fileError && <p className="admin-form__error" role="alert" style={{ marginTop: 8 }}>{fileError}</p>}
          {fileDone && <p className="admin-modal__text" role="status" style={{ marginTop: 8 }}>{fileDone}</p>}
        </div>
      </div>
    </section>
  )
}
