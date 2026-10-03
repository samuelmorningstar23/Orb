import { createContext, useContext } from 'react'

/**
 * Whether the shell panel this component lives in is the one on screen.
 *
 * The clinician shell keeps every visited panel mounted (display:none) so
 * drafts survive a tab switch. Without this flag their pollers kept fetching
 * for screens nobody was looking at. The shell provides one value per panel;
 * anything rendered outside a panel (the floating Sage assistant, tests)
 * reads the default, true.
 */
export const PanelActiveContext = createContext<boolean>(true)

export function usePanelActive(): boolean {
  return useContext(PanelActiveContext)
}

/** Window event that asks every live loader to refresh now, in place of a
 *  full page reload (which threw away half-written drafts). Pollers hear it
 *  through useVisiblePolling, load-once lists through useDataChanged; a hidden
 *  panel refreshes when it is shown again. */
export const DATA_CHANGED_EVENT = 'orb:data-changed'

export function announceDataChanged(): void {
  window.dispatchEvent(new CustomEvent(DATA_CHANGED_EVENT))
}
