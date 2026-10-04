import { useState } from 'react'
import styles from './EmergencyIntakePopup.module.css'
import { acknowledgeEmergency } from '../../api/client'

interface EmergencyIntakePopupProps {
  surgeryId: string
  patientName: string
  procedure: string
  indication: string
  onClose: () => void
  onOpenCase: () => void
}

export default function EmergencyIntakePopup({ surgeryId, patientName, procedure, indication, onClose, onOpenCase }: EmergencyIntakePopupProps) {
  const [loading, setLoading] = useState(false)

  const handleAcknowledge = async () => {
    setLoading(true)
    try {
      await acknowledgeEmergency(surgeryId)
      onClose()
    } catch (e) {
      console.error(e)
      setLoading(false)
    }
  }

  return (
    <div className={styles.overlay}>
      <div className={styles.popup}>
        <div className={styles.header}>
          <div className={styles.urgentBadge}>
            <span className={styles.pulse} />
            EMERGENCY INTAKE
          </div>
          <span className={styles.source}>Escalated from Relay</span>
        </div>

        <div className={styles.body}>
          <h2 className={styles.patient}>{patientName}</h2>
          <div className={styles.details}>
            <div className={styles.row}>
              <span className={styles.label}>Procedure:</span>
              <span className={styles.value}>{procedure}</span>
            </div>
            <div className={styles.row}>
              <span className={styles.label}>Indication:</span>
              <span className={styles.value}>{indication}</span>
            </div>
          </div>
        </div>

        <div className={styles.footer}>
          <button 
            className={styles.btnSecondary} 
            onClick={handleAcknowledge}
            disabled={loading}
          >
            {loading ? 'Acknowledging...' : 'Acknowledge'}
          </button>
          <button 
            className={styles.btnPrimary} 
            onClick={() => {
              handleAcknowledge()
              onOpenCase()
            }}
          >
            Open Case
          </button>
        </div>
      </div>
    </div>
  )
}
