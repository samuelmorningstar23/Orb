import { useMemo, useState, useCallback, type ReactNode } from 'react'
import { ActiveViewContext, type ViewSelection } from './ActiveViewContext'

export function ActiveViewProvider({ children }: { children: ReactNode }) {
  const [activeTab, setViewTab] = useState('')
  const [selection, setSelection] = useState<ViewSelection | null>(null)

  const setViewSelection = useCallback((sel: ViewSelection | null) => {
    setSelection(sel)
  }, [])

  const value = useMemo(
    () => ({ activeTab, selection, setViewTab, setViewSelection }),
    [activeTab, selection, setViewSelection],
  )

  return <ActiveViewContext.Provider value={value}>{children}</ActiveViewContext.Provider>
}
