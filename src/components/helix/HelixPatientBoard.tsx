import styles from './Helix.module.css'
import { getStatusBadge } from './HelixOrderReviewModal'
import { localDateKey } from '../../lib/datetime'
import { LOW_STOCK_THRESHOLD, availableStock, formatPackCount } from './stock'

interface Order {
  id: string
  patient_id?: string
  patient_name: string
  medication_id?: string
  substitute_medication_id?: string | null
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
}

interface InventoryItem {
  id: string
  medication_id: string
  lot_number: string
  quantity: number
  expiration_date: string | null
  name: string
  strength: string
  in_date?: number | boolean
}

interface CatalogItem {
  id: string
  form: string
}

export default function HelixPatientBoard({ orders, inventory, catalog = [] }: { orders: Order[], inventory: InventoryItem[], catalog?: CatalogItem[] }) {
  // Grouped by patient id: two patients who share a name are two rows, never
  // one row carrying both patients' medicines under one bed.
  const patientGroups = orders.reduce((acc, order) => {
    const key = order.patient_id || order.patient_name
    if (!acc[key]) {
      acc[key] = { name: order.patient_name, orders: [], ward: order.ward || '', bed: order.bed || '' }
    }
    acc[key].orders.push(order)
    return acc
  }, {} as Record<string, { name: string; orders: Order[]; ward: string; bed: string }>)

  const today = localDateKey()
  const formOf = (medId: string) => catalog.find(c => c.id === medId)?.form

  // The drug the dispense will draw on (the substitute when one was chosen),
  // by id rather than display name, counting only lots a dispense could take:
  // the same rule as the formulary and the dispense itself.
  const stockFor = (order: Order) => {
    const medId = order.substitute_medication_id || order.medication_id || ''
    return { qty: availableStock(inventory, medId, today), form: formOf(medId) }
  }

  // Count fulfillment progress
  const getFulfillmentSummary = (patientOrders: Order[]) => {
    const total = patientOrders.length
    const delivered = patientOrders.filter(o => o.status === 'delivered').length
    const dispensed = patientOrders.filter(o => o.status === 'dispensed').length
    const rejected = patientOrders.filter(o => o.status === 'rejected').length
    const active = total - rejected
    return { total, delivered, dispensed, active, rejected }
  }

  return (
    <div className={styles.contentArea}>
      <div className={styles.tableContainer}>
        <div className={styles.tableHeader}>
          <h3 className={styles.tableTitle}>Patient Medication Board</h3>
          <span className={styles.cellSecondary}>{Object.keys(patientGroups).length} patients</span>
        </div>
        {Object.keys(patientGroups).length === 0 ? (
          <div className={styles.emptyState}>
            <div className={styles.emptyStateIcon}>
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                <circle cx="9" cy="7" r="4"></circle>
                <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
                <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
              </svg>
            </div>
            <p>No active patients with medication orders.</p>
          </div>
        ) : (
          <div className={styles.tableScrollArea}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Patient</th>
                  <th>Medications</th>
                  <th>Fulfillment</th>
                  <th>Stock</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(patientGroups).map(([patientKey, { name: patientName, orders: patientOrders, ward, bed }]) => {
                  const summary = getFulfillmentSummary(patientOrders)
                  return (
                    <tr key={patientKey}>
                      <td>
                        <div className={styles.cellPrimary}>{patientName}</div>
                        <div className={styles.cellSecondary}>{ward} / {bed}</div>
                      </td>
                      <td>
                        <div className={styles.flexCol}>
                          {patientOrders.map(order => {
                            const badge = getStatusBadge(order.status)
                            return (
                              <div key={order.id} className={styles.flexRow}>
                                <span className={styles.cellPrimary} style={{ minWidth: 120 }}>
                                  {order.substitute_name || order.medication_name}
                                </span>
                                <span className={styles.cellSecondary}>({order.dosage})</span>
                                <span className={`${styles.badge} ${badge.className}`} style={{ marginLeft: 'auto' }}>
                                  {badge.label}
                                </span>
                              </div>
                            )
                          })}
                        </div>
                      </td>
                      <td>
                        <div className={styles.cellPrimary}>
                          {summary.delivered}/{summary.active} delivered
                        </div>
                        {summary.dispensed > 0 && (
                          <div className={styles.cellSecondary}>{summary.dispensed} in transit</div>
                        )}
                      </td>
                      <td>
                        <div className={styles.flexCol}>
                          {patientOrders.map(order => {
                            const { qty, form } = stockFor(order)
                            return (
                              <div key={order.id}>
                                {qty === 0 ? (
                                  <span className={`${styles.badge} ${styles.badgeCritical}`}>Out of Stock</span>
                                ) : qty < LOW_STOCK_THRESHOLD ? (
                                  <span className={`${styles.badge} ${styles.badgeWarning}`}>Low: {formatPackCount(qty, form)}</span>
                                ) : (
                                  <span className={styles.cellSecondary}>{formatPackCount(qty, form)}</span>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
