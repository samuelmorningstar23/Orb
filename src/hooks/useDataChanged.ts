import { useEffect, useRef } from 'react'
import { DATA_CHANGED_EVENT, usePanelActive } from '../contexts/PanelActiveContext'

/**
 * Run `callback` when shared data changed elsewhere in the shell (a patient was
 * just admitted), for loaders that fetch once on mount and do not poll.
 *
 * Admitting used to reload the page, which refreshed every list and threw away
 * every draft. Without the reload a panel that had already loaded its patient
 * list kept the old one, so the new patient was missing from the Orders picker
 * and a clinician could admit them a second time looking for them.
 *
 * In the panel on screen the callback runs at once. A hidden panel runs it once
 * when it is shown again, so a background tab does not fetch for nobody.
 */
export function useDataChanged(callback: () => void): void {
  const saved = useRef(callback)
  useEffect(() => { saved.current = callback }, [callback])
  const panelActive = usePanelActive()
  const pending = useRef(false)

  useEffect(() => {
    if (panelActive && pending.current) {
      pending.current = false
      saved.current()
    }
    const onChanged = () => {
      if (panelActive) saved.current()
      else pending.current = true
    }
    window.addEventListener(DATA_CHANGED_EVENT, onChanged)
    return () => window.removeEventListener(DATA_CHANGED_EVENT, onChanged)
  }, [panelActive])
}
