import { createContext, useContext } from 'react'

/** What the clinician has open inside the active panel — an order being
 *  reviewed, a Relay channel, a lab, a bed. Published by each panel the same
 *  way VigilPanel publishes the active patient. */
export interface ViewSelection {
  kind: string            // 'order' | 'channel' | 'lab' | 'bed' | 'analysis' | ...
  id?: string
  label?: string
  patientId?: string | null
}

export interface ActiveViewContextType {
  activeTab: string
  selection: ViewSelection | null
  setViewTab: (tab: string) => void
  setViewSelection: (sel: ViewSelection | null) => void
}

// The provider lives in ActiveViewProvider.tsx, so this module exports no
// component (react-refresh/only-export-components) and the hook keeps its path.
export const ActiveViewContext = createContext<ActiveViewContextType>({
  activeTab: '',
  selection: null,
  setViewTab: () => {},
  setViewSelection: () => {},
})

export const useActiveView = () => useContext(ActiveViewContext)

/** Build the wire-format view_context for /sage/query and /sage/voice.
 *  The backend whitelists + length-clamps these fields and treats them as
 *  DATA inside the fenced clinical-context block. An on-screen patient makes
 *  the turn PHI for routing purposes (it stays on the local lane). */
export function buildWireViewContext(
  activeTab: string,
  patientId: string | null,
  patientName: string | null,
  selection: ViewSelection | null,
): Record<string, unknown> | undefined {
  if (!activeTab && !patientId && !selection) return undefined
  const wire: Record<string, unknown> = {}
  if (activeTab) wire.tab = activeTab
  if (patientId) wire.patient_id = patientId
  if (patientName) wire.patient_name = patientName
  if (selection) {
    wire.selection = {
      kind: selection.kind,
      id: selection.id,
      label: selection.label,
      patient_id: selection.patientId ?? undefined,
    }
  }
  return wire
}
