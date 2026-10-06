import { useClinicalSwitches } from '../shellConfig'

/**
 * WHAT A DIRECT VISIT TO A SCREEN ORB WARD RECORD DOES NOT SELL SHOWS.
 *
 * On a computer running the Orb Ward Record profile (ORB_WARD_RECORD, the
 * 'ward_record' clinical switch on /api/health) the rail carries only the ward
 * list and chart, observation entry, Beds, Admin and the downtime page
 * (shellConfig WARD_RECORD_TAB_IDS). A shortcut, a link or a saved address can
 * still name one of the other screens; this is what it gets instead: one plain
 * page that says the screen is not part of Orb Ward Record. It never says the
 * screen is broken or that a permission is missing, because neither is true.
 *
 * With only the models switched off (ORB_MODELS_ENABLED) a visit to Sage,
 * Scribe or Lens gets the same page, saying the screen is switched off on
 * this computer instead.
 *
 * Styled with the shell's neutral panel note (.orb-panel-loading), the same as
 * ModuleOffNotice: no status colour, because nothing here is wrong.
 */
export interface NotInWardRecordProps {
  /** The screen that was asked for, by its rail name, when there is one. */
  readonly screen?: string | null
}

export default function NotInWardRecord({ screen }: NotInWardRecordProps) {
  /* The same page serves a computer with only the models switched off
     (ORB_MODELS_ENABLED), where Orb is not Orb Ward Record: it says so plainly
     rather than naming a product the hospital is not running. */
  const { wardRecord } = useClinicalSwitches()
  if (!wardRecord) {
    return (
      <div className="orb-panel-loading not-in-ward-record" role="status" aria-live="polite"
        data-testid="not-in-ward-record">
        <h2 style={{ margin: 0, color: 'var(--text-primary)', fontSize: '1.05rem', fontWeight: 600 }}>
          Switched off on this computer
        </h2>
        <p style={{ margin: 0 }}>
          {screen ? `${screen} uses the AI models` : 'This screen uses the AI models'}, and they are
          {' '}switched off on this computer. Everything else in Orb works as usual.
        </p>
      </div>
    )
  }
  return (
    <div className="orb-panel-loading not-in-ward-record" role="status" aria-live="polite"
      data-testid="not-in-ward-record">
      <h2 style={{ margin: 0, color: 'var(--text-primary)', fontSize: '1.05rem', fontWeight: 600 }}>
        Not part of Orb Ward Record
      </h2>
      <p style={{ margin: 0 }}>
        {screen ? `${screen} is not used on this computer.` : 'This screen is not used on this computer.'}
        {' '}Orb Ward Record keeps the ward&apos;s observation record and the monthly rounds file.
      </p>
    </div>
  )
}
