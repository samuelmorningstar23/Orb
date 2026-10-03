import { useState } from 'react'
import styles from './Helix.module.css'
import { getStatusBadge } from './HelixOrderReviewModal'
import { formatClinicalDateTime, formatUTCTime, localDateKey, parseUTC } from '../../lib/datetime'

interface Order {
  id: string
  patient_name: string
  medication_name: string
  dosage: string
  frequency: string
  route: string
  status: string
  prescribed_by_name: string
  created_at: string
  ward?: string
  bed?: string
  substitute_name?: string
  pharmacist_notes?: string | null
  reviewed_at?: string | null
  /** Set only when the ward signs for the drug. */
  received_at?: string | null
}

interface Props {
  orders: Order[]
  onSelectOrder: (order: Order) => void
}

/** Received on the ward on today's local calendar date. The card said
 *  "completed today" while counting every delivery ever made. A delivered row
 *  with no receipt time predates ward receipt and has no day to count on. */
function receivedToday(order: Order, today: string): boolean {
  if (!order.received_at) return false
  const at = parseUTC(order.received_at)
  return !isNaN(at.getTime()) && localDateKey(at) === today
}

export default function HelixDashboard({ orders, onSelectOrder }: Props) {
  const [showRejected, setShowRejected] = useState(false)
  const today = localDateKey()
  const pending = orders.filter(o => o.status === 'pending')
  const reviewing = orders.filter(o => o.status === 'reviewing')
  const verified = orders.filter(o => o.status === 'verified')
  const scheduled = orders.filter(o => o.status === 'scheduled')
  const dispensed = orders.filter(o => o.status === 'dispensed')
  const deliveredToday = orders.filter(o => o.status === 'delivered' && receivedToday(o, today))
  const rejected = orders.filter(o => o.status === 'rejected')
  const substituted = orders.filter(o => o.status === 'substitute_suggested')

  const activeQueue = [...pending, ...reviewing, ...substituted]
  const inFlight = [...verified, ...scheduled, ...dispensed]
  const rejectedOpen = showRejected && rejected.length > 0

  return (
    <div className={styles.contentArea}>
      {/* Metrics */}
      <div className={styles.bentoGrid}>
        <MetricCard icon="pending" label="Pending Review" value={pending.length + reviewing.length + substituted.length} sub="Orders awaiting action" />
        <MetricCard icon="check" label="Verified" value={verified.length} sub="Ready for scheduling" />
        <MetricCard icon="clock" label="Scheduled" value={scheduled.length} sub="Awaiting dispense" />
        <MetricCard icon="truck" label="In Transit" value={dispensed.length} sub="Dispensed, awaiting delivery" />
        <MetricCard icon="done" label="Delivered" value={deliveredToday.length} sub="Received on the ward today" />
        {/* A count with nowhere to go told the pharmacist something needed
            attention and gave no way to see what. The card opens the list. */}
        {rejected.length > 0 && (
          <MetricCard
            icon="alert"
            label="Rejected"
            value={rejected.length}
            sub={rejectedOpen ? 'Hide rejected orders' : 'Show rejected orders'}
            critical
            onClick={() => setShowRejected(v => !v)}
            expanded={rejectedOpen}
            controls="helix-rejected-orders"
          />
        )}
      </div>

      {rejectedOpen && (
        <div id="helix-rejected-orders" className={`${styles.tableContainer} ${styles.tableGap}`}>
          <div className={styles.tableHeader}>
            <h3 className={styles.tableTitle}>Rejected Orders</h3>
            <span className={styles.cellSecondary}>{rejected.length} rejected</span>
          </div>
          <div className={styles.tableScrollArea}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Patient</th>
                  <th>Medication</th>
                  <th>Reason</th>
                  <th>Rejected</th>
                </tr>
              </thead>
              <tbody>
                {rejected.map(order => (
                  <tr key={order.id} className={styles.clickableRow} onClick={() => onSelectOrder(order)}>
                    <td>
                      <button
                        type="button"
                        className={styles.rowBtn}
                        onClick={e => { e.stopPropagation(); onSelectOrder(order) }}
                        aria-label={`Open rejected order: ${order.medication_name} for ${order.patient_name}`}
                      >
                        <span className={styles.cellPrimary}>{order.patient_name}</span>
                        <span className={styles.cellSecondary}>{order.ward} / {order.bed}</span>
                      </button>
                    </td>
                    <td>
                      <div className={styles.cellPrimary}>{order.medication_name}</div>
                      <div className={styles.cellSecondary}>{order.dosage}</div>
                    </td>
                    <td><div className={styles.cellSecondary}>{order.pharmacist_notes || 'No reason recorded'}</div></td>
                    <td><div className={styles.cellSecondary}>{formatClinicalDateTime(order.reviewed_at) || 'Not recorded'}</div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Active Queue */}
      <div className={styles.tableContainer}>
        <div className={styles.tableHeader}>
          <h3 className={styles.tableTitle}>Pharmacist Work Queue</h3>
          <span className={styles.cellSecondary}>{activeQueue.length} active</span>
        </div>
        {activeQueue.length === 0 ? (
          <div className={styles.emptyState}>
            <div className={styles.emptyStateIcon}>
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                <polyline points="22 4 12 14.01 9 11.01"/>
              </svg>
            </div>
            <p>Queue clear, no pending orders.</p>
            {/* An empty queue beside a ward full of active medicines read as
                "pharmacy has seen everything". Say which orders arrive here. */}
            <p className={styles.cellSecondary} style={{ maxWidth: '60ch', margin: '8px auto 0' }}>
              Only medication requests a clinician approves from Sage arrive in this queue.
              Medicines prescribed in the patient chart go to the ward medication chart, and
              medication orders placed under Orders or Order sets are not sent to pharmacy.
            </p>
          </div>
        ) : (
          <div className={styles.tableScrollArea}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Patient</th>
                  <th>Medication</th>
                  <th>Prescriber</th>
                  <th>Time</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {activeQueue.map(order => {
                  const badge = getStatusBadge(order.status)
                  return (
                    <tr key={order.id} className={styles.clickableRow} onClick={() => onSelectOrder(order)}>
                      {/* The row's onClick is a mouse convenience only — a <tr> is
                          not focusable, so without a real control in the row a
                          pharmacist on the keyboard (or in gloves, on a
                          workstation with a dead trackpad) had no way to open an
                          order for verification at all. */}
                      <td>
                        <button
                          type="button"
                          className={styles.rowBtn}
                          onClick={e => { e.stopPropagation(); onSelectOrder(order) }}
                          aria-label={`Review order: ${order.medication_name} for ${order.patient_name}`}
                        >
                          <span className={styles.cellPrimary}>{order.patient_name}</span>
                          <span className={styles.cellSecondary}>{order.ward} / {order.bed}</span>
                        </button>
                      </td>
                      <td>
                        <div className={styles.cellPrimary}>{order.medication_name}</div>
                        <div className={styles.cellSecondary}>{order.dosage} · {order.frequency} · {order.route}</div>
                      </td>
                      <td>
                        <div className={styles.cellSecondary}>{order.prescribed_by_name || 'System'}</div>
                      </td>
                      <td>
                        <div className={styles.cellSecondary}>
                          {formatUTCTime(order.created_at)}
                        </div>
                      </td>
                      <td>
                        <span className={`${styles.badge} ${badge.className}`}>{badge.label}</span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* In-Flight Orders */}
      {inFlight.length > 0 && (
        <div className={styles.tableContainer} style={{ marginTop: 16 }}>
          <div className={styles.tableHeader}>
            <h3 className={styles.tableTitle}>Fulfillment Pipeline</h3>
            <span className={styles.cellSecondary}>{inFlight.length} in-flight</span>
          </div>
          <div className={styles.tableScrollArea}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Patient</th>
                  <th>Medication</th>
                  <th>Ward / Bed</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {inFlight.map(order => {
                  const badge = getStatusBadge(order.status)
                  return (
                    <tr key={order.id} className={styles.clickableRow} onClick={() => onSelectOrder(order)}>
                      <td>
                        <button
                          type="button"
                          className={styles.rowBtn}
                          onClick={e => { e.stopPropagation(); onSelectOrder(order) }}
                          aria-label={`Open in-flight order: ${order.substitute_name || order.medication_name} for ${order.patient_name}`}
                        >
                          <span className={styles.cellPrimary}>{order.patient_name}</span>
                        </button>
                      </td>
                      <td>
                        <div className={styles.cellPrimary}>{order.substitute_name || order.medication_name}</div>
                        <div className={styles.cellSecondary}>{order.dosage}</div>
                      </td>
                      <td><div className={styles.cellSecondary}>{order.ward} / {order.bed}</div></td>
                      <td><span className={`${styles.badge} ${badge.className}`}>{badge.label}</span></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Metric Card ───
function MetricCard({ icon, label, value, sub, critical, onClick, expanded, controls }: {
  icon: string; label: string; value: number; sub: string; critical?: boolean
  onClick?: () => void; expanded?: boolean; controls?: string
}) {
  const iconSvg = {
    pending: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></>,
    check: <><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></>,
    clock: <><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></>,
    truck: <><rect x="1" y="3" width="15" height="13"/><polygon points="16 8 20 8 23 11 23 16 16 16 16 8"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/></>,
    done: <><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></>,
    alert: <><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></>,
  }[icon] || null

  const className = `${styles.bentoCard} ${critical ? styles.bentoCardCritical : ''}`
  const body = (
    <>
      <span className={styles.bentoHeader}>
        <svg className={styles.bentoIcon} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          {iconSvg}
        </svg>
        {label}
      </span>
      <span className={styles.bentoValue}>{value}</span>
      <span className={styles.bentoSubtext}>{sub}</span>
    </>
  )
  if (onClick) {
    return (
      <button type="button" className={`${className} ${styles.bentoCardButton}`} onClick={onClick}
        aria-expanded={expanded} aria-controls={controls}>
        {body}
      </button>
    )
  }
  return <div className={className}>{body}</div>
}
