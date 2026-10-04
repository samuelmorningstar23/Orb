import { useState, useCallback, type ReactNode } from 'react'
import { ActivePatientContext } from './ActivePatientContext'

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
