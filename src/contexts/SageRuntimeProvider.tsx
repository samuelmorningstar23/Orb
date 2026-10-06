import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { getSageAiPosture, type SageAiPosture } from '../api/client'
import { SageRuntimeContext } from './SageRuntimeContext'

export function SageRuntimeProvider({ children }: { children: ReactNode }) {
  const [posture, setPosture] = useState<SageAiPosture | null>(null)
  const [micOwner, setMicOwner] = useState<string | null>(null)
  const micOwnerRef = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getSageAiPosture()
      .then(p => { if (!cancelled) setPosture(p) })
      .catch(() => { /* posture stays null: transcription capability stays unknown */ })
    return () => { cancelled = true }
  }, [])

  const claimMic = useCallback((owner: string): boolean => {
    if (micOwnerRef.current && micOwnerRef.current !== owner) return false
    micOwnerRef.current = owner
    setMicOwner(owner)
    return true
  }, [])

  const releaseMic = useCallback((owner: string) => {
    if (micOwnerRef.current === owner) {
      micOwnerRef.current = null
      setMicOwner(null)
    }
  }, [])

  const value = useMemo(
    () => ({
      posture,
      localAsrAvailable: posture ? posture.local_asr_available === true : null,
      micOwner,
      claimMic,
      releaseMic,
    }),
    [posture, micOwner, claimMic, releaseMic],
  )

  return <SageRuntimeContext.Provider value={value}>{children}</SageRuntimeContext.Provider>
}
