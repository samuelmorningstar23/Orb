/**
 * Sage speaks only with a voice that runs on the device. A voice with
 * localService false is a network voice: reading an answer with it sends the
 * answer text off the appliance, so it is never chosen, whatever its name.
 */
import { describe, expect, it } from 'vitest'
import { pickOnDeviceVoice } from './onDeviceVoice'

function voice(name: string, lang: string, localService: boolean): SpeechSynthesisVoice {
  return { name, lang, localService, default: false, voiceURI: name } as SpeechSynthesisVoice
}

describe('pickOnDeviceVoice', () => {
  it('returns nothing when every voice is a network voice', () => {
    expect(pickOnDeviceVoice([
      voice('Google UK English Female', 'en-GB', false),
      voice('Microsoft Aria Online (Natural)', 'en-US', false),
    ])).toBeNull()
  })

  it('returns nothing when the browser has no voices yet', () => {
    expect(pickOnDeviceVoice([])).toBeNull()
  })

  it('never picks a preferred name that is a network voice', () => {
    const local = voice('Fred', 'en-US', true)
    expect(pickOnDeviceVoice([voice('Samantha', 'en-US', false), local])).toBe(local)
  })

  it('prefers an enhanced English voice among the on-device ones', () => {
    const samantha = voice('Samantha', 'en-US', true)
    expect(pickOnDeviceVoice([voice('Fred', 'en-US', true), samantha])).toBe(samantha)
  })

  it('falls back to any on-device English voice, then any on-device voice', () => {
    const en = voice('Fred', 'en-US', true)
    expect(pickOnDeviceVoice([voice('Amelie', 'fr-CA', true), en])).toBe(en)
    const hi = voice('Lekha', 'hi-IN', true)
    expect(pickOnDeviceVoice([hi, voice('Google UK English Male', 'en-GB', false)])).toBe(hi)
  })
})
