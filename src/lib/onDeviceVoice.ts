/** The voice Sage reads an answer aloud with, or none.
 *
 *  Only voices that run on this device (`localService === true`). A browser
 *  voice with localService false is synthesised by an outside service, so the
 *  answer text, which is often about a named patient, would leave the
 *  building. Orb is strictly offline (owner ruling, 4 October 2026): when no
 *  on-device voice exists the answer is not spoken at all, and stays on screen.
 *
 *  Among the on-device voices, the macOS enhanced English voices come first,
 *  then any English voice, then any on-device voice. */

const PREFERRED = [
  'Samantha', 'Karen', 'Daniel', 'Zoe', 'Alex',
  'Moira', 'Tessa', 'Rishi', 'Fiona', 'Victoria',
]

export function pickOnDeviceVoice(voices: readonly SpeechSynthesisVoice[]): SpeechSynthesisVoice | null {
  const local = voices.filter(v => v.localService === true)
  if (local.length === 0) return null

  for (const name of PREFERRED) {
    const v = local.find(v => v.name.includes(name) && v.lang.startsWith('en'))
    if (v) return v
  }

  return local.find(v => v.lang.startsWith('en')) || local[0]
}
