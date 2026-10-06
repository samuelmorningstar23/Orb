import { createContext, useContext } from 'react'
import type { SageAiPosture } from '../api/client'

/** Shared runtime for every surface that listens (SagePanel, Scribe, the ward
 *  round):
 *
 *  1. AI POSTURE: fetched once from /sage/ai-posture. It names the local model
 *     and says whether on-appliance transcription can run. There is no browser
 *     or cloud speech recognition anywhere: Orb is strictly offline (owner
 *     ruling, 4 October 2026), so local Whisper is the only way speech becomes
 *     text.
 *  2. MIC OWNERSHIP: one registry so SagePanel's voice recording, Scribe's
 *     transcription and the round's dictation stop fighting over the single
 *     input device. A surface claims the mic before opening it and releases it
 *     after; everyone else observes.
 */

export interface SageRuntimeContextType {
  posture: SageAiPosture | null
  /** Can on-appliance transcription actually run? `null` until the posture has
   *  loaded, so a control can say "checking" rather than guessing wrong in
   *  either direction. See ai_governance.local_asr_available. */
  localAsrAvailable: boolean | null
  micOwner: string | null
  claimMic: (owner: string) => boolean
  releaseMic: (owner: string) => void
}

// The provider lives in SageRuntimeProvider.tsx, so this module exports no
// component (react-refresh/only-export-components) and the hook keeps its path.
export const SageRuntimeContext = createContext<SageRuntimeContextType>({
  posture: null,
  localAsrAvailable: null,
  micOwner: null,
  claimMic: () => false,
  releaseMic: () => {},
})

export const useSageRuntime = () => useContext(SageRuntimeContext)
