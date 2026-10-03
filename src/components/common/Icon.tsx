import type { CSSProperties, ReactNode } from 'react'

/**
 * Line icons for buttons, banners and section titles.
 *
 * Emoji were used as icons across the app. They render differently on every
 * tablet, cannot take the theme's colour, and a screen reader reads each one
 * out ("pill", "memo"). These are plain strokes in currentColor, hidden from
 * assistive technology unless a `title` is given, so the label beside the icon
 * carries the meaning. New icons go here rather than inline in a panel.
 */
const PATHS = {
  image: (<><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="9" cy="9" r="2" /><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" /></>),
  flask: (<><path d="M9 3h6" /><path d="M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3" /><path d="M7.5 15h9" /></>),
  pill: (<><rect x="3" y="9" width="18" height="6" rx="3" transform="rotate(-45 12 12)" /><path d="M9.5 9.5l5 5" /></>),
  note: (<><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /><path d="M9 13h6" /><path d="M9 17h4" /></>),
  timeline: (<><path d="M3 6h.01" /><path d="M3 12h.01" /><path d="M3 18h.01" /><path d="M8 6h13" /><path d="M8 12h13" /><path d="M8 18h13" /></>),
  handoff: (<><path d="m16 3 4 4-4 4" /><path d="M20 7H4" /><path d="m8 21-4-4 4-4" /><path d="M4 17h16" /></>),
  offline: (<><path d="M12 20h.01" /><path d="M8.5 16.4a5 5 0 0 1 7 0" /><path d="M5 12.9a10 10 0 0 1 5.2-2.7" /><path d="M16.9 11.3a10 10 0 0 1 2.2 1.6" /><path d="M2 8.8a15 15 0 0 1 4.2-2.6" /><path d="M10.7 5c4-.4 8.1.9 11.3 3.8" /><path d="m2 2 20 20" /></>),
  printer: (<><path d="M6 9V2h12v7" /><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2" /><rect x="6" y="14" width="12" height="8" /></>),
  exit: (<><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="m16 17 5-5-5-5" /><path d="M21 12H9" /></>),
  alert: (<><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path d="M12 9v4" /><path d="M12 17h.01" /></>),
  search: (<><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></>),
  battery: (<><rect x="2" y="7" width="16" height="10" rx="2" /><path d="M22 11v2" /><path d="M6 11v2" /></>),
  book: (<><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" /></>),
  clipboard: (<><rect x="8" y="2" width="8" height="4" rx="1" /><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" /></>),
  building: (<><path d="M3 21h18" /><path d="M5 21V7l7-4 7 4v14" /><path d="M12 9v6" /><path d="M9 12h6" /></>),
  stethoscope: (<><path d="M5 2H4a2 2 0 0 0-2 2v5a6 6 0 0 0 12 0V4a2 2 0 0 0-2-2h-1" /><path d="M8 15v1a6 6 0 0 0 12 0v-4" /><circle cx="20" cy="10" r="2" /></>),
  siren: (<><path d="M7 18v-6a5 5 0 1 1 10 0v6" /><path d="M5 21a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-1a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2z" /><path d="M21 12h1" /><path d="M2 12h1" /><path d="M12 2v1" /><path d="m18.5 4.5-.7.7" /><path d="m4.9 4.9.7.7" /></>),
  shield: (<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />),
  zap: (<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />),
  pause: (<><rect x="6" y="4" width="4" height="16" rx="1" /><rect x="14" y="4" width="4" height="16" rx="1" /></>),
  moon: (<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z" />),
  bed: (<><path d="M2 4v16" /><path d="M2 8h18a2 2 0 0 1 2 2v10" /><path d="M2 17h20" /><path d="M6 8v9" /></>),
  thermometer: (<path d="M14 4v10.5a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0z" />),
  'trending-up': (<><path d="M22 7 13.5 15.5 8.5 10.5 2 17" /><path d="M16 7h6v6" /></>),
} satisfies Record<string, ReactNode>

export type IconName = keyof typeof PATHS

interface IconProps {
  name: IconName
  size?: number
  className?: string
  style?: CSSProperties
  /** Only for an icon that stands alone with no visible label beside it. */
  title?: string
}

export default function Icon({ name, size = 14, className, style, title }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={{ flexShrink: 0, verticalAlign: '-0.125em', ...style }}
      role={title ? 'img' : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  )
}
