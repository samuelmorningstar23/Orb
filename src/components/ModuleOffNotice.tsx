import { useTranslation } from 'react-i18next'
import type { ModuleOffState } from '../shellConfig'

/**
 * WHAT AN ACCOUNT SEES WHEN ITS WHOLE JOB IS SWITCHED OFF.
 *
 * The HIS roles (receptionist, ward clerk, the three lab roles) each exist only
 * while one feature is on: patient registration, Beds, the laboratory. When it
 * is off their rail is empty, and an empty rail used to mean a blank body or,
 * worse, the unknown-role fallback of Patients and Messages, both of which
 * refuse the account. This says what is going on in one plain sentence and who
 * can change it.
 *
 * Also the honest placeholder for the OPD token board kiosk ('opd'), whose
 * screen is built in a later wave.
 *
 * Four states, decided by shellConfig.getModuleOffNotice:
 *   checking     the server has not answered yet;
 *   unreachable  the server could not be asked, so Orb does not claim "off";
 *   off          the server answered and the feature is off;
 *   unavailable  the feature is on, but its screen cannot open on this site yet.
 *
 * Styled with the shell's neutral panel note (.orb-panel-loading) so it adds no
 * new colour pair: no status colour, because nothing here is wrong with a
 * patient.
 */
export type ModuleOffSubject = 'registration' | 'adt' | 'lab' | 'opd'

export interface ModuleOffNoticeProps {
  readonly subject: ModuleOffSubject
  readonly state: ModuleOffState
}

const HINT_KEY: Record<ModuleOffState, string | null> = {
  checking: null,
  off: 'moduleOff.offHint',
  unavailable: 'moduleOff.unavailableHint',
  unreachable: 'moduleOff.unreachableHint',
}

export default function ModuleOffNotice({ subject, state }: ModuleOffNoticeProps) {
  const { t } = useTranslation()
  const name = t(`moduleOff.name.${subject}`)
  const hintKey = HINT_KEY[state]
  return (
    <div
      className="orb-panel-loading module-off-notice"
      role="status"
      aria-live="polite"
      data-subject={subject}
      data-state={state}
    >
      <p style={{ margin: 0, color: 'var(--text-primary)', fontWeight: 550 }}>
        {t(`moduleOff.${state}`, { name })}
      </p>
      {hintKey && <p style={{ margin: 0 }}>{t(hintKey)}</p>}
    </div>
  )
}
