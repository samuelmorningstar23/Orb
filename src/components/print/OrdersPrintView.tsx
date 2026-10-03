/**
 * Active orders list print view.
 *
 * Print-first, portaled by the caller into document.body (see OrdersPanel), so
 * the shared @media print rules in src/print.css strip the dark app shell and
 * reveal only this .orders-print-layout subtree. Follows the same structure /
 * conventions as HandoffPrintView.
 *
 * This component does NOT own its print lifecycle: the parent fires
 * window.print() via a double-rAF effect once the portal commits, and clears
 * its own print state afterward — the same approach used for the Handoff and
 * Discharge views in VigilPanel. onAfterPrint is wired to the browser's
 * afterprint event by the parent (or here) so the portal is torn down once.
 */

import { useEffect } from 'react'
import { formatClinicalDateTime } from '../../lib/datetime'

interface OrderRow {
  order_type: string
  description: string
  priority: string
  status: string
  result_text?: string | null
  created_at: string
}

interface OrdersPrintViewProps {
  patientId: string
  patientName?: string
  orders: OrderRow[]
  onAfterPrint: () => void
}

function formatDateTime(value: string): string {
  return formatClinicalDateTime(value) || value
}

export default function OrdersPrintView({
  patientId,
  patientName,
  orders,
  onAfterPrint,
}: OrdersPrintViewProps) {
  const generatedAt = formatClinicalDateTime(new Date())

  useEffect(() => {
    // afterprint fires whether the user prints or cancels; tear down once.
    const handleAfterPrint = () => onAfterPrint()
    window.addEventListener('afterprint', handleAfterPrint)

    let cancelled = false
    const rafId = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (cancelled) return
        window.print()
      })
    })

    return () => {
      cancelled = true
      cancelAnimationFrame(rafId)
      window.removeEventListener('afterprint', handleAfterPrint)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="orders-print-layout">
      {/* Header */}
      <div className="orders-header">
        <div className="orders-header-branding">
          <h1>Orb Hospital System</h1>
          <p>Active Orders List</p>
        </div>
        <div className="orders-header-meta">
          <p><strong>Generated:</strong> {generatedAt}</p>
          <p><strong>Status:</strong> CONFIDENTIAL MEDICAL RECORD</p>
        </div>
      </div>

      <hr className="orders-divider" />

      {/* Patient identity band */}
      <div className="orders-section">
        <h2 className="orders-patient-name">{patientName || 'Patient'}</h2>
        <p className="orders-patient-demo">Patient ID: {patientId}</p>
      </div>

      {/* Orders table */}
      <div className="orders-section">
        <h3 className="orders-section-title">Orders</h3>
        {orders.length > 0 ? (
          <table className="orders-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Description</th>
                <th>Priority</th>
                <th>Status</th>
                <th>Result</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order, idx) => (
                <tr key={`${order.description}-${idx}`}>
                  <td>{order.order_type}</td>
                  <td><strong>{order.description}</strong></td>
                  <td>{order.priority}</td>
                  <td>{order.status.replace('_', ' ')}</td>
                  <td>{order.result_text || '—'}</td>
                  <td>{formatDateTime(order.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="orders-empty">No orders recorded for this patient.</p>
        )}
      </div>

      {/* Signature line */}
      <div className="orders-footer">
        <div className="orders-signoff">
          <p>Reviewed by:</p>
          <div className="orders-signature-line"></div>
          <p>Date / Time:</p>
          <div className="orders-signature-line"></div>
        </div>
      </div>
    </div>
  )
}
