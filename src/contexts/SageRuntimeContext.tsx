import { createContext, useContext } from 'react'
import type { SageAiPosture } from '../api/client'

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

export interface SageRuntimeContextType {
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

// The provider lives in SageRuntimeProvider.tsx, so this module exports no
// component (react-refresh/only-export-components) and the hook keeps its path.
export const SageRuntimeContext = createContext<SageRuntimeContextType>({
  posture: null,
  cloudSpeechAllowed: false,
  localAsrAvailable: null,
  micOwner: null,
  claimMic: () => false,
  releaseMic: () => {},
})

export const useSageRuntime = () => useContext(SageRuntimeContext)
