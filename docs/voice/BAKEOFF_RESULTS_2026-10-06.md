# Local voice bake-off: stage 2 results

6 October 2026. Companion to `LOCAL_VOICE_PLAN_2026-10.md`.

Everything here ran on the development Mac (M2 Max), never on an appliance, in
`~/orb-voice-bakeoff/` with its own Python environment. No hospital audio was
used. The raw per-clip results and summaries are in that folder under
`results/`.

## The short answer

- **Speech recognition: Qwen3-ASR 1.7B, 8-bit.** It is the best or close to
  the best on every test set, English and Hinglish, and runs about 30 times
  faster than real time. One model can cover both languages.
- **Speaking: Kokoro-82M**, in English and in Hindi. It was the clearest
  voice in both languages and needed 0.13 to 0.17 s to produce a whole
  sentence. Every other voice was at least ten times slower.
- **The gap to the cloud is medical words, not general accuracy.** On ordinary
  speech the local models now match the published cloud numbers. On drug
  names the best local model still gets about 1 in 5 wrong. The published
  cloud medical models are roughly twice as good on the same keywords (vendor
  numbers, and our scoring is not yet the same as theirs).
- **Nothing passes the plan's gates for notes or orders yet.** Sage questions
  are close. The next experiment is biasing the recogniser with the hospital's
  formulary, which is the one lever that targets drug names directly.

## What was tested

| Set | What it is | Clips used |
|---|---|---|
| Eka English | Eka Care medical speech set: Indian clinicians, Indian brand names, MIT licence | 1,000 spread through 3,619 (2.3 h) |
| Eka Hindi | The same set's Hindi part: Hinglish, references written in two scripts | all 320 (1.6 h) |
| Svarah | Indian-accented English, general speech | 1,000 spread through 6,656 |
| FLEURS Hindi | Read Hindi, general speech, one script | all 418 |

The Hindi references in Eka mix Devanagari and Roman script, and the models
write English words in Devanagari, so plain word error rates for Eka Hindi are
inflated by spelling, not by mishearing. For Hindi I report a "skeleton"
character error rate that ignores which script a word was written in.

Keyword error is measured on the medical words Eka tags (drugs, diagnostics,
findings, advice). This is our own scoring of their tags, not Eka's official
script, so compare it with the published cloud numbers with care.

## Speech recognition, English

Eka English (1,000 clips). Lower is better except number recall.

| Model | Licence | Word error | Medical keyword error | Drug names wrong | Numbers recalled | Speed (x real time) | Peak memory |
|---|---|---|---|---|---|---|---|
| **Qwen3-ASR 1.7B 8-bit** | Apache-2.0 | **9.5%** | 13.2% | 22.6% | 91% | 30x | 4.5 GB |
| Qwen3-ASR 1.7B 4-bit | Apache-2.0 | 9.5% | 13.2% | 23.0% | 92% | 33x | 3.7 GB |
| Whisper large-v3 turbo | MIT | 10.4% | 14.0% | 23.0% | 93% | 21x | 4.7 GB |
| Whisper large-v3 | MIT | 10.5% | **12.5%** | **20.7%** | **94%** | 14x | 7.6 GB |
| Parakeet v3 0.6B | CC-BY-4.0 | 12.2% | 17.4% | 28.5% | 85% | 62x | 36 GB (see note) |
| Cohere Transcribe 2B | gated, check | 13.5% | 18.6% | 32.1% | 76% | 41x | 5.1 GB |
| Nemotron 3.5 0.6B | NVIDIA OpenMDW | 15.6% | 22.3% | 31.4% | 81% | 17x | 2.7 GB |

Published cloud results on the same set (vendor numbers): Deepgram Nova-3
Medical 7.65% keyword error, ElevenLabs Scribe v2 Medical 6.02%. Our best is
12.5% to 13.2%.

Svarah (Indian-accented general English): Whisper large-v3 6.0%, Whisper
turbo 7.3%, Cohere 7.6%, Qwen3-ASR 8-bit 8.2%, Qwen3-ASR 4-bit 8.6%, Parakeet
9.4%, Nemotron 12.5%. The plan quoted 3.5% for Qwen3-ASR on Indian English from
published results. That did not reproduce under our text normalisation, which
is stricter about numbers and spelling.

Note on Parakeet: its peak memory footprint reached 36 GB during the run. That
looks like a cache that grows with the session, not the model's real size, but
it would need fixing before it could sit beside MedGemma.

## Speech recognition, Hindi and Hinglish

| Model | Eka Hindi skeleton error | Eka Hindi keywords wrong | FLEURS Hindi word error | FLEURS skeleton error | Median time per clip (Eka) |
|---|---|---|---|---|---|
| **Qwen3-ASR 1.7B 8-bit** | **8.8%** | **24%** | 10.5% | 3.0% | 1.3 s |
| Nemotron 3.5 0.6B | 10.7% | 28% | 10.6% | 3.8% | 1.0 s |
| IndicConformer 600M | 14.5% | 28% | **8.7%** | **2.4%** | 0.6 s |
| Whisper large-v3 | 21.3% | 35% | 26.3% | 6.2% | 2.2 s |
| Whisper large-v3 turbo | 21.9% | 36% | 29.2% | 6.8% | 0.7 s |
| Qwen3-ASR 1.7B 4-bit | 28.8% | 27% | 32.3% | 23.3% | 1.0 s |

What this says:

- IndicConformer is the best at clean, read, pure Hindi (FLEURS), as its
  published numbers said. On real clinical Hinglish (Eka) it falls to third.
  Clinical speech in India is Hinglish, so Eka is the set that matters.
- The 4-bit Qwen3-ASR is as good as the 8-bit in English but collapses in
  Hindi. Use the 8-bit.
- Letting Qwen3-ASR detect the language itself gave the same accuracy (8.9%)
  but was 70% slower, so Orb should pass the language when it knows it.
- Whisper is not usable for Hindi.

## Speaking (text to speech)

Each voice read 50 clinical lines in English and 50 in Hindi (doses, vitals,
bed numbers, drug names). To check that the words came out right, each
recording was transcribed again by a recogniser and compared with the script.
I did this twice, with Whisper large-v3 and with Qwen3-ASR 8-bit, because
Whisper is weak at Hindi. Both listeners ranked the voices the same way. The
table uses Qwen3-ASR.

| Voice | Licence | English word error | English numbers heard right | Hindi skeleton error | Hindi numbers heard right | Time to first audio | Speed vs real time | Memory once loaded |
|---|---|---|---|---|---|---|---|---|
| **Kokoro-82M** | Apache-2.0 | **5.1%** | **100%** | **7.3%** | **71%** | **0.17 s** (Hindi 0.13 s) | 25x faster | 0.4 GB loaded |
| Veena 3B | Apache-2.0 (Llama terms) | n/a | n/a | 9.4% | 60% | 6.5 s | 2x slower | 8.7 GB |
| Chatterbox multilingual | MIT | 12.0% | 94% | 13.5% | 50% | 1.8 s | 2x faster | 3.3 GB |
| Magpie 357M | NVIDIA Open Model | 32.1% | 14% | 14.2% | 19% | 9.9 s | 2.5x slower | 6.1 GB |
| Svara v1 | Apache-2.0 | n/a | n/a | 15.0% | 47% | 8.6 s | 2x slower | 6.9 GB |

What this says:

- Kokoro is the only voice fast enough to answer in under a second, and it was
  also the clearest. The plan had Veena as the Hindi voice. Veena was second
  on clarity but takes 6.5 s before any sound on this Mac and is slower than
  real time, so it fails the speed target unless it gets much faster on an M5.
- Magpie cannot read digits: "Metformin 500 mg BD" came back as "Metformin
  A-Ot-M-G-B-D". Any voice Orb uses must be given numbers already written out
  as words.
- Hindi numbers are the weakest point for every voice. With Kokoro, "286" was
  heard as "260", "130 by 90" as "132/90", and "8.2" lost its decimal point.
  Some of these may be the listener's mistakes, but a dose read aloud in Hindi
  is not reliable enough today.
- Clarity is not quality. This test checks that the words are right. It does
  not check whether the voice sounds natural or Indian. Kokoro has no
  Indian-accented English voice. That needs a short listening test by people
  who will use it.

## Against the plan's gates

| Use | Gate | Where we are |
|---|---|---|
| Sage questions | WER 12% or better on our own set, medical terms recalled 95%+ | 9.5% WER on Eka English passes; medical terms recalled about 75% (Qwen3-ASR entity recall), fails |
| Notes (drafts) | WER 8%, drugs 97%+, numbers with units 99%+ | Fails: drugs about 77%, exact numbers with units 36% under our strict scoring |
| Orders (drafts) | zero unflagged drug, dose, unit or patient errors | Not attempted, and should not be until the notes gate passes |

The gates are written for our own recorded set, which does not exist yet
(decision 6). Eka is the nearest public stand-in.

## What this changes in the design

1. **Numbers are written out before Orb speaks.** A small normaliser turns
   "500 mg" into "five hundred milligrams" in the language being spoken, with
   decimals said as "point". This also makes the voice choice less risky.
2. **Doses and values are spoken in English words, even in a Hindi answer, and
   always shown on screen.** This is my recommendation for decision 5, based on
   the Hindi number results above.
3. **Voice never fills a drug field from raw recognition.** Every drug name is
   resolved against the formulary and shown for confirmation, which the plan
   already required. These results show it is necessary, not just careful.
4. **One recogniser for both languages** (Qwen3-ASR 8-bit) instead of
   separate English and Hindi models. This saves memory on the appliance.

## What is still to run

1. **Formulary biasing.** Give Qwen3-ASR the formulary's drug names as context
   and measure drug-name errors again on Eka English and Hindi. This is the
   most important next test.
2. **Eka's own scoring script**, so our keyword numbers compare fairly with the
   published cloud numbers.
3. **Stage 3: running beside MedGemma.** Run speech recognition and Kokoro
   while MedGemma 27B answers, and measure how much each slows the other.
4. **Live words while speaking.** Nemotron was the plan's choice for showing
   words as they are said. It was the least accurate model here, which is
   acceptable for a preview the final transcript replaces, but its streaming
   delay still needs measuring.
5. **A five-minute listening test** of Kokoro's English and Hindi voices.

Decisions 2 to 7 in the plan are still open.
