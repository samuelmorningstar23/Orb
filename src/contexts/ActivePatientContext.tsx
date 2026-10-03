import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'

interface ActivePatientContextType {
  activePatientId: string | null
  activePatientName: string | null
  setActivePatient: (id: string | null, name?: string | null) => void
}

const ActivePatientContext = createContext<ActivePatientContextType>({
  activePatientId: null,
  activePatientName: null,
  setActivePatient: () => {},
})

export const useActivePatient = () => useContext(ActivePatientContext)

export function ActivePatientProvider({ children }: { children: ReactNode }) {
  const [activePatientId, setId] = useState<string | null>(null)
  const [activePatientName, setName] = useState<string | null>(null)

  const setActivePatient = useCallback((id: string | null, name?: string | null) => {
    setId(id)
    setName(name ?? null)
  }, [])

  return (
    <ActivePatientContext.Provider value={{ activePatientId, activePatientName, setActivePatient }}>
      {children}
    </ActivePatientContext.Provider>
  )
}
