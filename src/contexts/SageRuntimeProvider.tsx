import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { getSageAiPosture, type SageAiPosture } from '../api/client'
import { SageRuntimeContext } from './SageRuntimeContext'
import { useClinicalSwitches } from '../shellConfig'

export function SageRuntimeProvider({ children }: { children: ReactNode }) {
  const [posture, setPosture] = useState<SageAiPosture | null>(null)
  const [micOwner, setMicOwner] = useState<string | null>(null)
  const micOwnerRef = useRef<string | null>(null)
  /* With the models switched off on this computer (ORB_MODELS_ENABLED, or the
     Orb Ward Record, which forces them off) /sage/ai-posture answers 404
     Switched off, so it is not asked at all, and nothing can transcribe. */
  const { modelsEnabled, wardRecord } = useClinicalSwitches()
  const modelsOn = modelsEnabled && !wardRecord

  useEffect(() => {
    if (!modelsOn) return
    let cancelled = false
    getSageAiPosture()
      .then(p => { if (!cancelled) setPosture(p) })
      .catch(() => { /* posture stays null: transcription capability stays unknown */ })
    return () => { cancelled = true }
  }, [modelsOn])

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
      posture: modelsOn ? posture : null,
      localAsrAvailable: !modelsOn ? false : posture ? posture.local_asr_available === true : null,
      micOwner,
      claimMic,
      releaseMic,
    }),
    [modelsOn, posture, micOwner, claimMic, releaseMic],
  )

  return <SageRuntimeContext.Provider value={value}>{children}</SageRuntimeContext.Provider>
}
