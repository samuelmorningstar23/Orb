import { useTranslation } from 'react-i18next'
import { nextTheme, type Theme } from '../lib/theme'

interface Props {
  theme: Theme
  onToggle: () => void
  /** The header recipe by default; pre-login surfaces pass their own box. */
  className?: string
  id?: string
}

/** The dark / light / night switch. One component so the pre-login screens and
 *  the clinician header cannot drift on icon, label or cycle order. */
export default function ThemeToggle({ theme, onToggle, className = 'shell__theme-toggle', id = 'btn-theme-toggle' }: Props) {
  const { t } = useTranslation()
  // Label names the NEXT theme in the cycle (what the click switches to).
  const label = t('shell.themeSwitchTo', { theme: t(`theme.${nextTheme(theme)}`) })

  return (
    <button className={className} onClick={onToggle} title={label} aria-label={label} id={id} type="button">
      {theme === 'dark' ? (
        /* sun → currently dark */
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="5"/>
          <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
          <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
        </svg>
      ) : theme === 'light' ? (
        /* moon → currently light */
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
        </svg>
      ) : (
        /* stars → currently night shift */
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z" opacity="0.9"/>
          <path d="M18 4v3M16.5 5.5h3" strokeWidth="1.2"/>
        </svg>
      )}
    </button>
  )
}
