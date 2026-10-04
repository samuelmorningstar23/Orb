import styles from './Helix.module.css'

// Shared by the order review modal, the dashboard and the patient board. Kept
// out of HelixOrderReviewModal.tsx so that file exports only its component
// (react-refresh/only-export-components).

// ─── State Machine Badge Helper ───
const STATUS_CONFIG: Record<string, { label: string; class: string }> = {
  pending:              { label: 'Pending',    class: 'badgePending' },
  reviewing:            { label: 'Reviewing',  class: 'badgeReviewing' },
  verified:             { label: 'Verified',   class: 'badgeVerified' },
  rejected:             { label: 'Rejected',   class: 'badgeRejected' },
  substitute_suggested: { label: 'Substitute', class: 'badgeSubstitute' },
  scheduled:            { label: 'Scheduled',  class: 'badgeScheduled' },
  dispensed:            { label: 'Dispensed',   class: 'badgeDispensed' },
  delivered:            { label: 'Delivered',   class: 'badgeDelivered' },
}

export function getStatusBadge(status: string) {
  const cfg = STATUS_CONFIG[status] || { label: status, class: 'badgeNeutral' }
  return { label: cfg.label, className: styles[cfg.class] || styles.badgeNeutral }
}
