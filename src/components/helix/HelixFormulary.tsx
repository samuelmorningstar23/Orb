import { useSyncExternalStore } from 'react'

import styles from './Helix.module.css'
import { formatClinicalDate, localDateKey } from '../../lib/datetime'
import { currentModuleFlags, subscribeModuleFlags } from '../../shellConfig'
import { expiryKey, formatPackCount, isLowStock, lotState, type LotState } from './stock'

interface CatalogItem {
  id: string
  name: string
  generic_name: string
  form: string
  strength: string
  /** The pharmacy store's reorder level for this drug (migration 54). Used
   *  only while the pharmacy store module is on; null means none was set,
   *  and the fixed threshold applies. */
  reorder_level_units?: number | null
}

interface InventoryItem {
  id: string
  medication_id: string
  lot_number: string
  quantity: number
  expiration_date: string | null
  name: string
  strength: string
  /** The server's dispense rule for this lot: 1 when it has a recorded expiry
   *  on or after today. Absent from an older server, then judged here. */
  in_date?: number | boolean
}

/** Lots expiring within this many days are flagged so they are used first or
 *  returned before they lapse. */
const EXPIRY_WARNING_DAYS = 90

/** "31 Oct 2029". An expiry is a calendar date, not a UTC instant, so it is
 *  built in local time and never shifted by the viewer's offset. */
function expiryLabel(key: string): string {
  const [y, m, d] = key.split('-').map(Number)
  return formatClinicalDate(new Date(y, m - 1, d))
}

function LotLine({ lot, state, form }: { lot: InventoryItem; state: LotState; form: string }) {
  const key = expiryKey(lot.expiration_date)
  const expiry = key ? `exp ${expiryLabel(key)}` : 'no expiry recorded'
  let note = ''
  let color = 'var(--text-secondary)'
  if (state === 'expired') {
    note = 'Expired, not dispensable'
    color = 'var(--status-danger-text)'
  } else if (state === 'undated') {
    note = 'No expiry recorded, not dispensable'
    color = 'var(--status-danger-text)'
  } else if (state === 'soon') {
    note = `Expires within ${EXPIRY_WARNING_DAYS} days`
    color = 'var(--status-warn-text)'
  }
  return (
    <li style={{ color, fontWeight: note ? 600 : 400 }}>
      {lot.lot_number}: {formatPackCount(lot.quantity, form)}, {expiry}
      {note ? `. ${note}` : ''}
    </li>
  )
}

export default function HelixFormulary({ catalog, inventory }: { catalog: CatalogItem[], inventory: InventoryItem[] }) {
  // The server's per-drug reorder level replaces the fixed threshold only
  // while the pharmacy store module is on; with it off the screen reads
  // exactly as it always has, whatever levels are stored.
  const storeOn = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)['pharmacy-store'] === true
  const today = localDateKey()
  const soon = new Date()
  soon.setDate(soon.getDate() + EXPIRY_WARNING_DAYS)
  const soonCutoff = localDateKey(soon)

  // Available stock is what a dispense could actually take: in-date lots with
  // units left. Summing every lot counted expired and undated stock that the
  // dispense refuses, so a drug read "In Stock" with nothing usable on the shelf.
  const formularyView = catalog.map(item => {
    const lots = inventory
      .filter(inv => inv.medication_id === item.id)
      .map(lot => ({ lot, state: lotState(lot, today, soonCutoff) }))
    const totalQty = lots
      .filter(l => l.state === 'ok' || l.state === 'soon')
      .reduce((sum, l) => sum + l.lot.quantity, 0)
    const unusableQty = lots
      .filter(l => (l.state === 'expired' || l.state === 'undated') && l.lot.quantity > 0)
      .reduce((sum, l) => sum + l.lot.quantity, 0)
    const isLow = isLowStock(totalQty, item, storeOn)
    const isOut = totalQty === 0
    // Empty lots are history, not stock: an expired lot still holding units
    // stays listed, because someone has to take it off the shelf.
    const shownLots = lots.filter(l => l.lot.quantity > 0)

    return {
      ...item,
      totalQty,
      unusableQty,
      shownLots,
      isLow,
      isOut
    }
  })

  return (
    <div className={styles.contentArea}>
      <div className={styles.tableContainer}>
        <div className={styles.tableHeader}>
          <h3 className={styles.tableTitle}>Medication Formulary & Inventory</h3>
        </div>
        <div className={styles.tableScrollArea}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Medication</th>
                <th>Formulation</th>
                <th>Available Stock</th>
                <th>Lots</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {formularyView.map(med => (
                <tr key={med.id}>
                  <td>
                    <div className={styles.cellPrimary}>{med.name}</div>
                    <div className={styles.cellSecondary}>{med.generic_name}</div>
                  </td>
                  <td>
                    <div className={styles.cellPrimary}>{med.form}</div>
                    <div className={styles.cellSecondary}>{med.strength}</div>
                  </td>
                  <td>
                    <div className={styles.cellPrimary}>{formatPackCount(med.totalQty, med.form)}</div>
                    {med.unusableQty > 0 && (
                      <div className={styles.cellSecondary} style={{ color: 'var(--status-danger-text)' }}>
                        {formatPackCount(med.unusableQty, med.form)} expired or undated, not counted
                      </div>
                    )}
                  </td>
                  <td>
                    {med.shownLots.length ? (
                      <ul className={styles.cellSecondary} style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                        {med.shownLots.map(({ lot, state }) => <LotLine key={lot.id} lot={lot} state={state} form={med.form} />)}
                      </ul>
                    ) : (
                      <div className={styles.cellSecondary}>None</div>
                    )}
                  </td>
                  <td>
                    {med.isOut ? (
                      <span className={`${styles.badge} ${styles.badgeCritical}`}>Out of Stock</span>
                    ) : med.isLow ? (
                      <span className={`${styles.badge} ${styles.badgeWarning}`}>Low Stock</span>
                    ) : (
                      <span className={`${styles.badge} ${styles.badgeSuccess}`}>In Stock</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
