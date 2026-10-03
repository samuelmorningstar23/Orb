import type { ReactNode } from 'react'
import { humaniseEnum } from '../../../lib/humanise'

/* ─── Outcome Badge ─── */
const OUTCOME_STYLES: Record<string, { bg: string; color: string; border: string }> = {
  success:         { bg: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', color: 'var(--admin-ok-badge)',   border: 'color-mix(in srgb, var(--status-ok) 28%, transparent)' },
  failure:         { bg: 'color-mix(in srgb, var(--status-danger) 10%, transparent)',  color: 'var(--status-danger)', border: 'color-mix(in srgb, var(--status-danger) 28%, transparent)' },
  partial_success: { bg: 'color-mix(in srgb, var(--status-warn) 10%, transparent)',  color: 'var(--admin-warn-badge)',   border: 'color-mix(in srgb, var(--status-warn) 28%, transparent)' },
  denied:          { bg: 'color-mix(in srgb, var(--text-muted) 10%, transparent)', color: 'var(--text-muted)',    border: 'color-mix(in srgb, var(--text-muted) 28%, transparent)' },
}

export function OutcomeBadge({ outcome }: { outcome: string }) {
  const s = OUTCOME_STYLES[outcome] || OUTCOME_STYLES.denied
  return (
    <span className="admin-badge" style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {humaniseEnum(outcome)}
    </span>
  )
}

/* ─── Source Badge ─── */
const SOURCE_STYLES: Record<string, { bg: string; color: string; border: string; label: string }> = {
  simulated:     { bg: 'color-mix(in srgb, var(--text-muted) 10%, transparent)', color: 'var(--text-muted)',    border: 'color-mix(in srgb, var(--text-muted) 28%, transparent)', label: 'Simulated' },
  ingested_hl7:  { bg: 'color-mix(in srgb, var(--status-info) 10%, transparent)',   color: 'var(--status-info)',    border: 'color-mix(in srgb, var(--status-info) 28%, transparent)',   label: 'HL7' },
  ingested_json: { bg: 'color-mix(in srgb, var(--admin-indigo) 10%, transparent)',   color: 'var(--admin-indigo)',   border: 'color-mix(in srgb, var(--admin-indigo) 28%, transparent)',  label: 'JSON' },
  csv_replay:    { bg: 'color-mix(in srgb, var(--status-warn) 10%, transparent)',   color: 'var(--admin-warn-badge)', border: 'color-mix(in srgb, var(--status-warn) 28%, transparent)',  label: 'CSV Replay' },
}

export function SourceBadge({ source }: { source: string }) {
  const s = SOURCE_STYLES[source] || { bg: 'var(--bg-glass)', color: 'var(--text-secondary)', border: 'var(--border-subtle)', label: source || '—' }
  return (
    <span className="admin-badge" style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {s.label}
    </span>
  )
}

/* ─── Parse Status Badge ─── */
const PARSE_STYLES: Record<string, { bg: string; color: string; border: string }> = {
  parsed:     { bg: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', color: 'var(--admin-ok-badge)',   border: 'color-mix(in srgb, var(--status-ok) 28%, transparent)' },
  failed:     { bg: 'color-mix(in srgb, var(--status-danger) 10%, transparent)',  color: 'var(--status-danger)', border: 'color-mix(in srgb, var(--status-danger) 28%, transparent)' },
  duplicate:  { bg: 'color-mix(in srgb, var(--status-warn) 10%, transparent)',  color: 'var(--admin-warn-badge)',   border: 'color-mix(in srgb, var(--status-warn) 28%, transparent)' },
  received:   { bg: 'color-mix(in srgb, var(--text-muted) 10%, transparent)', color: 'var(--text-muted)',   border: 'color-mix(in srgb, var(--text-muted) 28%, transparent)' },
}

export function ParseStatusBadge({ status }: { status: string }) {
  const s = PARSE_STYLES[status] || PARSE_STYLES.received
  return (
    <span className="admin-badge" style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {humaniseEnum(status)}
    </span>
  )
}

/* ─── Action Type Icon ─── */
const ACTION_ICONS: Record<string, ReactNode> = {
  ingestion_request_received: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
  ),
  ingestion_parse_success: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
  ),
  vital_inserted: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12h4l3-9 4 18 3-9h4"/></svg>
  ),
  news2_recomputed: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L11 3l-3 9H4"/></svg>
  ),
  vigil_alert_generated: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
  ),
  relay_alert_generated: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
  ),
}

export function ActionIcon({ action }: { action: string }) {
  return <span className="admin-action-icon">{ACTION_ICONS[action] || null}</span>
}
