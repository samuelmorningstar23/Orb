/* The Web Speech recognizer, typed for the members Orb uses. TypeScript's DOM
   lib ships the result types (SpeechRecognitionResultList and below) but not
   the recognizer, its events, or the window constructors: Chrome and Safari
   expose it only as `webkitSpeechRecognition`.

   Every caller streams audio to the browser vendor's cloud ASR through this,
   so each one gates it on the server's cloud-speech posture first. */

/** A `result` event. `results` holds every result of the session so far;
 *  `resultIndex` is the first one this event changed. */
export interface SpeechRecognitionEvent extends Event {
  readonly resultIndex: number
  readonly results: SpeechRecognitionResultList
}

/** An `error` event. `error` is the code: 'not-allowed', 'service-not-allowed',
 *  'no-speech', 'aborted', 'network' and so on. */
export interface SpeechRecognitionErrorEvent extends Event {
  readonly error: string
  readonly message: string
}

export interface SpeechRecognition extends EventTarget {
  continuous: boolean
  interimResults: boolean
  lang: string
  onstart: ((this: SpeechRecognition, ev: Event) => void) | null
  onresult: ((this: SpeechRecognition, ev: SpeechRecognitionEvent) => void) | null
  onerror: ((this: SpeechRecognition, ev: SpeechRecognitionErrorEvent) => void) | null
  onend: ((this: SpeechRecognition, ev: Event) => void) | null
  start(): void
  stop(): void
  abort(): void
}

export type SpeechRecognitionConstructor = new () => SpeechRecognition

type SpeechRecognitionWindow = Window & {
  SpeechRecognition?: SpeechRecognitionConstructor
  webkitSpeechRecognition?: SpeechRecognitionConstructor
}

/** The browser's recognizer constructor, standard name first, then the
 *  webkit-prefixed one. Undefined when the browser has neither. */
export function getSpeechRecognitionCtor(): SpeechRecognitionConstructor | undefined {
  const w = window as SpeechRecognitionWindow
  return w.SpeechRecognition || w.webkitSpeechRecognition
}
