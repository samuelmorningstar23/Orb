# Local voice for Orb: research and plan

4 October 2026. Orb is strictly offline (standing rule, same day), so voice has to
be as good as a cloud assistant with nothing leaving the building. This is what
five research passes found and what I propose to build. Nothing here is built
yet. Model numbers come from public leaderboards and papers read on 4 October;
vendor-reported numbers are marked as such. A test on our own speakers decides
every model choice before anything ships.

## The short answer

Yes, it can be done locally, with one honest exception.

- **Understanding speech:** open models now match the cloud on benchmarks. On
  Indian English the best open models score 3.4 to 3.5 word error rate (WER)
  against 3.0 to 3.5 for the best cloud services. On Hindi, AI4Bharat's
  IndicConformer (MIT) scores 8.5, level with ElevenLabs and ahead of Azure,
  AssemblyAI, Deepgram and OpenAI; only Sarvam and Gemini are better (about 5
  to 6). Whisper, which Orb wires in today, is poor at Hindi (28 to 40) and
  should not be our Hindi engine.
- **The gap is clinical vocabulary.** No open model is trained on Indian
  clinical speech, and real ward audio is much harder than benchmarks: on
  recorded psychiatric interviews at an Indian hospital even Gemini scored 14
  (English) and 18 (Hindi). We close this with our own test set, the hospital
  formulary as vocabulary, and human review, not by trusting a leaderboard.
- **Speaking answers is the one place local trails the cloud.** The best
  commercially licensed open voices (Kokoro-82M, NVIDIA Magpie 357M) are clean
  and natural, but listeners prefer ElevenLabs about 4 times in 5. The open
  models that come close are all non-commercial and cannot be used.
- **The slowest part is not voice at all.** Measured on this Mac (M2 Max):
  MedGemma 27B takes 17.3 s to its first word when its prompt is not cached,
  and 0.49 s when it is. Keeping that cache warm matters more than any voice
  model.

## Design

A chain of separate steps, not one end-to-end voice model:

```
microphone -> speech detection -> speech-to-text -> Sage (text, unchanged) -> text-to-speech -> speaker
```

Why: every protection Sage has today works on text (the mutation gate, scope
checks, retrieval, the anti-hallucination strip in its three lanes, the 27B
safety floor, provenance). The transcript is the auditable record, and what is
logged is exactly what was said. End-to-end voice models (Moshi, Qwen3-Omni and
others) are faster but give no transcript to check, are not MedGemma-grade, and
the best published one gets tool arguments right only 42% of the time.

**On the device (browser):** a 16 kHz audio worklet, Silero VAD v6 (2 MB,
detects speech), an optional wake word, a live transcript on screen, and an
on-screen read-back before anything is acted on. Every script and model file is
served by the appliance; nothing loads from a CDN.

**On the appliance:** a small voice service next to the Python server that runs
speech-to-text and text-to-speech on Apple's Neural Engine through Core ML
(FluidAudio or speech-swift, both Apache-2.0). That keeps the GPU free for
MedGemma; today Whisper and any TTS would compete with it and bypass both of
Orb's GPU gates. The cost is a Swift sidecar process, a new language in the
stack.

## Model choices (candidates for the bake-off)

| Job | First choice | Licence | Fallback | Why |
|---|---|---|---|---|
| Live words while speaking | Nemotron 3.5 streaming 0.6B on the Neural Engine | NVIDIA OpenMDW (commercial use stated; check) | WhisperKit large-v3-turbo (MIT) | Word-by-word results 80 ms to 1.1 s after speech; covers Hindi; accepts a custom vocabulary |
| Final transcript, English | Qwen3-ASR 1.7B | Apache-2.0 | Parakeet v3 (CC-BY-4.0, attribution) | 3.5 WER on Indian English; also does Hindi, so one model could cover both |
| Final transcript, Hindi | IndicConformer 600M | MIT | Qwen3-ASR | 8.5 WER on Hindi; no verified Mac port yet, so this is the first thing to prove |
| Hinglish (mixed) | Bodhan Indic-Transcribe flex | Indic Open Model License (attribution, terms carry to derivatives) | Qwen3-ASR | Writes mixed-script Hinglish with numbers formatted; vendor numbers only |
| Speech detection | Silero VAD v6 | MIT | none needed | 1 ms per 30 ms chunk, runs in the browser |
| End of turn | Smart Turn v3.1 | BSD-2 | silence timer | 12 ms on a CPU; never used for orders (see safety) |
| Wake word | a custom phrase trained with livekit-wakeword | Apache-2.0 (code and models) | openWakeWord trained ourselves | Porcupine is out: its key is checked online. openWakeWord's ready-made models are non-commercial |
| Speaking, English | Kokoro-82M on the Neural Engine | Apache-2.0 | Magpie 357M (NVIDIA Open Model License) | 0.2 to 0.5 s to first audio, under 0.5 GB; no Indian-accent voice |
| Speaking, Hindi and Hinglish | Veena (Maya Research), 3B | Apache-2.0 on Llama 3.2 (Llama terms likely apply) | Magpie 357M Hindi | The only commercially usable open model with native Indian voices that switch between Hindi and English in one voice; not yet speed-tested on a Mac |
| Who spoke (notes) | pyannote community-1 | CC-BY-4.0 (gated download) | NVIDIA Sortformer | Doctor vs patient in Scribe; needs a voice-enrolment step |

Ruled out, and why: Supertonic-3 (licence forbids medical advice use), Voxtral
TTS, Fish S2, Breeze TTS 2, F5-TTS, XTTS-v2, Spark-TTS (non-commercial),
Apple SpeechAnalyzer (Hindi 101 WER, downloads language assets), Chrome's
"Google" and Edge's "Online" voices (cloud), Porcupine (online key check),
Kyutai Unmute (needs an NVIDIA GPU).

## How fast it can be

| Step | Target (warm) |
|---|---|
| Notice you stopped talking | 0.3 to 0.4 s |
| Final transcript | 0.1 to 0.3 s (live words already on screen) |
| Hospital network | under 0.1 s |
| Sage's first sentence | 4B: about 0.5 s. 27B: 1.3 to 3.9 s |
| First audio of the answer | 0.05 to 0.15 s |
| **Voice to voice** | **General: about 1 s. Patient or dose questions (27B): 2 to 4.5 s** |

Cloud assistants answer in about 0.2 to 0.8 s. We get close on general turns.
For patient questions, which stay on the 27B by the safety-floor ruling, we
cover the wait honestly: a short spoken acknowledgement ("Checking bed 12") at
about 0.3 s, answers streamed sentence by sentence, and the 27B prompt warmed
when the wake word fires or the chart opens. The risk is several wards at once:
Ollama keeps one cached prompt per model, so users evict each other. That needs
measuring on the M5 Ultra, which is claimed (unverified) to process prompts 3
to 4 times faster than this M2 Max.

## Safety rules for voice

Speech recognition without review is not safe in medicine: in one study 63% of
dictated notes had at least one error, one word in 250 was clinically
significant, and review brought it to 0.3%. ECRI records an insulin dose of 8
heard as 80. So:

1. **Voice never commits anything.** It drafts; an authenticated clinician taps
   to confirm, as every Sage action works today.
2. **Every drug resolves to a formulary item.** Sound-alikes force an explicit
   choice with tall-man lettering, including Indian traps such as Medzol (one
   brand used for both midazolam and pantoprazole) and Olvance vs Oleanz.
3. **Doses are read back** digit by digit for risky numbers (fifteen vs fifty),
   with units limited to those valid for that item, and dose-range checks.
4. **Voice never chooses the patient.** It is bound to the open chart and reads
   back name, bed and UHID.
5. **Automatic cleanup may never change** a number, a negation or left/right,
   and shows what it changed. It may only pick among the recogniser's own
   alternatives, never invent a drug.
6. **Orders use push-to-talk only**, because a pause in the middle of a dose
   ("ceftriaxone... one gram") must not end the turn.
7. **What Sage says is exactly what the screen shows.** Doses are turned into
   words by fixed code, never by the AI ("zero point five milligrams", not
   "half a milligram"), with pauses between drug, dose, route and frequency.
   A pharmacist reviews the pronunciation list, and a nightly check plays every
   entry back through speech recognition.
8. **Insulin, heparin, potassium chloride, opioids and chemotherapy** are
   excluded from voice ordering in the pilot.
9. **Privacy:** wake-word detection stays on the device; nothing is sent before
   it; audio is not kept by default (the transcript is, encrypted); a visible
   listening light and a mute control; voice is not identity.

## Proving "as good as the cloud"

- **A public yardstick:** Eka Care's medical speech set (MIT licence, Indian
  English and Hindi with Indian brand names). Published cloud results on it:
  Deepgram Nova-3 Medical 7.65 keyword WER, ElevenLabs Scribe v2 Medical 6.02
  (vendor numbers). Target: 7.7 or better; stretch: 6.0.
- **Our own set:** about 10 to 12 hours from 30 to 40 clinicians (at least 40%
  nurses, 40% women, four or more language backgrounds), reading synthetic,
  PHI-free scripts built from a hospital formulary: about 3,000 drug mentions,
  3,000 numbers with units, 500 negations, 300 left/right, Hinglish phrasing.
  Recorded on the appliance's own microphones, quiet and with ward noise.
  Written consent; audio stays on the encrypted appliance.
- **Gates before each use:**

| Use | Must reach |
|---|---|
| Sage questions | WER 12% or better on our set, medical terms recalled 95%+, zero wrong-patient answers |
| Notes (draft, clinician signs) | WER 8% or better, drugs 97%+, numbers with units 99%+, negation or left/right flips 0.5% or fewer and all flagged, no more than one invented phrase per 10 hours |
| Orders (drafts only) | zero unflagged drug, dose, unit or patient errors in 3,000+ order utterances |

After go-live, keep measuring in shadow mode, using clinicians' edits as the
error labels.

## What already exists, and what is broken

From an audit of the code:

- **Speech-to-text is wired but nothing runs.** Whisper large-v3 through
  lightning-whisper-mlx, a warm daemon and a cold subprocess. But the runtime
  is not installed (R019), nothing starts the daemon, `pyzmq` is missing, and
  the daemon's 5 s timeout sends every real dictation to the cold path.
- **The live-streaming path** has no patient-scope check, no audit, no limit on
  its audio buffer, and writes unencrypted temporary audio files.
- **Audio from Sage and ward rounds** is stored encrypted but never linked to a
  note, so erasure requests cannot find it, and the 30-day audio deletion is a
  policy with no job behind it.
- **Sage's voice mode** sends the transcript without letting the clinician see
  or correct it first.
- **The automatic cleanup step** calls MedGemma outside Orb's GPU queue.

The cleanup of 4 October (browser speech removed, the floating orb made
text-only, spoken answers limited to on-device voices, the speech engine
stopped from downloading at runtime) is the starting point for this plan.

## Build order

Rough sizes; each phase ends with a test you can see.

0. **Foundations (1 to 2 weeks).** Fix the broken parts above. Start the voice
   service sidecar. Build the recording kit for the test set.
1. **Accuracy bake-off (2 to 3 weeks).** Record the test set. Run every
   candidate on the Eka set and ours, fully offline. Add formulary vocabulary
   and the dose-formatting rules. Measure MedGemma's speed with speech running
   beside it. Pick the models.
2. **Push-to-talk Sage voice with spoken answers (2 to 3 weeks).** Live words,
   final transcript, read-back screen, Kokoro and Veena with the dose
   formatter and pronunciation list, prompt warming, spoken acknowledgements.
3. **Scribe and ward-round dictation on the new engine (about 2 weeks),** with
   who-spoke-when for consultations.
4. **Hands-free (2 to 3 weeks).** Wake word on dedicated ward tablets, end-of-
   turn detection, interrupting Sage while it speaks, then a shadow trial on
   one ward.
5. **Orders by voice, drafts only,** and only after the gates above are met and
   a pharmacist has signed off.

## Decisions I need from you

1. **R019 for the bake-off:** download the candidate speech and voice models
   (about 6 to 10 GB in total) on the development Mac, not on any appliance.
2. **Cloud comparison:** use only published cloud numbers, or also run the
   cloud services on the public Eka set from the development Mac (no hospital
   audio, ever)? I recommend published numbers only.
3. **A Swift sidecar** for the Neural Engine path, or stay in Python and MLX
   and accept GPU contention with MedGemma. I recommend the sidecar.
4. **Wake word:** off by default, on only for dedicated ward tablets, and a
   longer phrase than "Sage", which hides inside "dosage".
5. **Hinglish transcripts** in Roman or Devanagari script, and whether Sage
   speaks doses in Hindi with English numbers.
6. **Who records the test set,** and the consent form.
7. **Licence review** for Veena and other Llama-based voices, the NVIDIA model
   licences, and attribution for the CC-BY models.

## Main risks

1. 27B prompt time and cache eviction with several wards talking at once.
2. Neural Engine and GPU still share memory bandwidth; a 3B voice model could
   slow MedGemma's answers (estimated, not measured).
3. Indian-accented drug names and numbers.
4. End-of-turn detection cutting a dose short (hence push-to-talk for orders).
5. Wake-word false triggers in a busy ward.
6. Echo when Sage speaks through desktop speakers (headsets, or a delay before
   an interruption counts).
7. The hospital certificate: iPads must trust it for the microphone to work.
8. No open voice yet matches the best cloud voices.
