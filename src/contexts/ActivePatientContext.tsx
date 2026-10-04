import { createContext, useContext } from 'react'

export interface ActivePatientContextType {
  activePatientId: string | null
  activePatientName: string | null
  setActivePatient: (id: string | null, name?: string | null) => void
}

// The provider lives in ActivePatientProvider.tsx, so this module exports no
// component (react-refresh/only-export-components) and the hook keeps its path.
export const ActivePatientContext = createContext<ActivePatientContextType>({
  activePatientId: null,
  activePatientName: null,
  setActivePatient: () => {},
})

export const useActivePatient = () => useContext(ActivePatientContext)
