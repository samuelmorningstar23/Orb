import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { getSageAiPosture, type SageAiPosture } from '../api/client'

/** Shared runtime for every Sage surface (floating orb, SagePanel, Scribe):
 *
 *  1. GOVERNANCE POSTURE — fetched once from /sage/ai-posture. Until it
 *     arrives, cloud speech is treated as NOT allowed (fail-closed): the Web
 *     Speech API streams mic audio to a cloud ASR, which is egress of spoken
 *     PHI, so it may only start after the server says the posture permits it.
 *  2. MIC OWNERSHIP — one registry so the floating assistant's continuous
 *     recognition, SagePanel's voice recording, and Scribe's transcription
 *     stop fighting over the single input device. A surface claims the mic
 *     before opening it and releases it after; everyone else observes.
 */

interface SageRuntimeContextType {
  posture: SageAiPosture | null
  cloudSpeechAllowed: boolean
  /** Can on-appliance transcription actually run? `null` until the posture has
   *  loaded, so a control can say "checking" rather than guessing wrong in
   *  either direction. A CAPABILITY fact, not a privacy gate — see
   *  ai_governance.local_asr_available. */
  localAsrAvailable: boolean | null
  micOwner: string | null
  claimMic: (owner: string) => boolean
  releaseMic: (owner: string) => void
}

const SageRuntimeContext = createContext<SageRuntimeContextType>({
  posture: null,
  cloudSpeechAllowed: false,
  localAsrAvailable: null,
  micOwner: null,
  claimMic: () => false,
  releaseMic: () => {},
})

export const useSageRuntime = () => useContext(SageRuntimeContext)

export function SageRuntimeProvider({ children }: { children: ReactNode }) {
  const [posture, setPosture] = useState<SageAiPosture | null>(null)
  const [micOwner, setMicOwner] = useState<string | null>(null)
  const micOwnerRef = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getSageAiPosture()
      .then(p => { if (!cancelled) setPosture(p) })
      .catch(() => { /* fail-closed: posture stays null, cloud speech stays off */ })
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
      cloudSpeechAllowed: posture?.cloud_speech_allowed === true,
      localAsrAvailable: posture ? posture.local_asr_available === true : null,
      micOwner,
      claimMic,
      releaseMic,
    }),
    [posture, micOwner, claimMic, releaseMic],
  )

  return <SageRuntimeContext.Provider value={value}>{children}</SageRuntimeContext.Provider>
}
