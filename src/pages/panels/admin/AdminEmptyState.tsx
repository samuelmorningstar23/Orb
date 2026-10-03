export function AdminEmptyState({ icon, title, message }: { icon?: React.ReactNode; title: string; message?: string }) {
  return (
    <div className="admin-empty">
      {icon && <div className="admin-empty__icon">{icon}</div>}
      <p className="admin-empty__title">{title}</p>
      {message && <p className="admin-empty__message">{message}</p>}
    </div>
  )
}

export function AdminLoadingState({ message = 'Loading...' }: { message?: string }) {
  return (
    <div className="admin-empty">
      <div className="admin-loading-spinner" />
      <p className="admin-empty__title">{message}</p>
    </div>
  )
}

export function AdminErrorState({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="admin-empty">
      <div className="admin-empty__icon" style={{ color: 'var(--status-danger)' }}>
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
      </div>
      <p className="admin-empty__title">Something went wrong</p>
      <p className="admin-empty__message">{error}</p>
      {onRetry && <button className="admin-retry-btn" onClick={onRetry}>Retry</button>}
    </div>
  )
}
