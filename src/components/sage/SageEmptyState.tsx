import React from 'react'
import OrbLogo from '../OrbLogo'
import { greetingName } from './greetingName'

/** One Sage tagline, used on this screen, the history sidebar and the nav. */
export const SAGE_TAGLINE = 'Clinical assistant'

// Line icons (stroke follows the text colour), not emoji: emoji render
// differently on every tablet and cannot take the theme's colour.
const ICON_PROPS = {
  width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  'aria-hidden': true,
}

const ICONS: Record<string, React.ReactNode> = {
  summary: (
    <svg {...ICON_PROPS}>
      <rect x="6" y="4" width="12" height="17" rx="2" />
      <path d="M9 4h6v3H9z" /><path d="M9 11h6" /><path d="M9 15h4" />
    </svg>
  ),
  labs: (
    <svg {...ICON_PROPS}>
      <path d="M9 3h6" /><path d="M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3" />
      <path d="M7.5 15h9" />
    </svg>
  ),
  discharge: (
    <svg {...ICON_PROPS}>
      <path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" />
      <path d="M10 16l-4-4 4-4" /><path d="M6 12h10" />
    </svg>
  ),
  medicines: (
    <svg {...ICON_PROPS}>
      <rect x="3" y="9" width="18" height="6" rx="3" transform="rotate(-45 12 12)" />
      <path d="M9.5 9.5l5 5" />
    </svg>
  ),
}

const QUICK_ACTIONS = [
  { label: 'Patient summary', icon: 'summary', query: 'Give me a brief summary of all active patients and their current status.', desc: 'Current status of each patient on your ward' },
  { label: 'Lab insights', icon: 'labs', query: 'What are the most recent lab results across all patients? Highlight any abnormal values.', desc: 'Flag recent critical values' },
  { label: 'Discharge planning', icon: 'discharge', query: 'Which patients are closest to discharge? What criteria still need to be met?', desc: 'Review discharge readiness' },
  { label: 'Drug interactions', icon: 'medicines', query: 'Review current medications for all active patients and flag any potential drug interactions.', desc: 'Safety check across medicines' },
]

interface Props {
  userName?: string
  onQuickAction: (query: string) => void
}

export function SageEmptyState({ userName, onQuickAction }: Props) {
  const displayName = greetingName(userName)
  const greeting = (() => {
    const hour = new Date().getHours()
    if (hour < 12) return 'Good morning'
    if (hour < 17) return 'Good afternoon'
    return 'Good evening'
  })()

  return (
    <div className="sage-empty">
      <div className="sage-empty__logo">
        <OrbLogo size={44} />
      </div>
      <div className="sage-empty__eyebrow">Sage · {SAGE_TAGLINE}</div>
      <h1 className="sage-empty__title">
        {greeting}{displayName ? <>, <em>{displayName}</em></> : null}
      </h1>
      <p className="sage-empty__subtitle">What would you like to check?</p>

      <div className="sage-empty__suggestions">
        {QUICK_ACTIONS.map((action, i) => (
          <button
            key={i}
            className="sage-suggestion"
            onClick={() => onQuickAction(action.query)}
          >
            <div className="sage-suggestion__icon">{ICONS[action.icon]}</div>
            <div className="sage-suggestion__title">{action.label}</div>
            <div className="sage-suggestion__desc">{action.desc}</div>
          </button>
        ))}
      </div>
    </div>
  )
}
