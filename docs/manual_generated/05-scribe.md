## 5. Scribe: Clinical Documentation

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![Scribe. Type or dictate, then structure the transcript into a SOAP note, discharge summary, or I-PASS handoff.](assets/manual/scribe-documentation.png)

*Scribe. Type or dictate, then structure the transcript into a SOAP note, discharge summary, or I-PASS handoff.*

### 5.1 What Scribe is for

Scribe is Orb's clinical documentation studio. It turns what you say, or type, into a correctly structured, reviewable, signable clinical note. You dictate a patient encounter in your own words; Scribe transcribes it with a locally-running medical speech engine, cleans up drug names and clinical vocabulary, and drafts the content into one of three standard formats: a SOAP note, a discharge summary, or an I-PASS handoff document. An optional automated verification pass then compares the drafted note against what you actually said and flags anything the AI may have invented or left out, before you sign.

Everything happens on the hospital's own hardware, and every step of the note's life, creation, editing, verification, signing, rejection, revision, is written to Orb's audit trail.

Scribe is available to doctors, nurses, and administrators. Anyone in those roles can dictate, structure, and edit draft notes; only doctors can sign or reject a note (see 5.8).

### 5.2 Opening Scribe

1. Sign in to Orb with your employee ID and password.
2. In the left sidebar, click **Scribe**.

When no note is in progress you will see the Scribe welcome screen: the Orb emblem, the title **Orb Scribe**, and a short description, "Dictate or type clinical notes. Scribe transcribes words in real-time and refines with medical AI for accuracy. Auto-structures into SOAP notes, discharge summaries, and I-PASS handoff documents."

At the bottom of the panel is the input bar, which is always present:

| Control | What it does |
|---|---|
| Text field ("Type clinical notes or use the microphone…") | Type or paste a note directly. Editing this field clears any previously structured note so the draft always matches the transcript. |
| Microphone button ("Start voice dictation") | Starts a standard dictation session: record, then refine (see 5.5.2). While recording, the same button shows a stop square and its tooltip reads "Stop recording". |
| Streaming button ("Start live streaming (VAD)") | Starts live utterance-by-utterance transcription (see 5.5.3). While active, it becomes "Stop live streaming". |

The two voice buttons are mutually exclusive: while one mode is running, the other button is disabled, and both are disabled while the AI is structuring or refining a note.

### 5.3 Choosing a transcription language

Above the transcript area is a **Language** selector. It controls the language used by the local speech-recognition engine and offers three options:

| Option | When to use it |
|---|---|
| **English** | Default. Dictation is entirely in English. |
| **Hindi (हिन्दी)** | Dictation is primarily in Hindi. |
| **Auto-detect / code-switching** | Mixed-language dictation, for example a clinician who moves between Hindi and English mid-sentence. The engine detects the language as you go. |

Choose the language **before** you start recording, the selector is locked while a recording or refinement is in progress. The selection applies to both dictation modes.

Note that the instant on-screen preview text shown during standard dictation (see below) is English-only; the accurate transcript that replaces it when you stop respects your language selection.

### 5.4 Capturing a note: three ways

#### 5.4.1 Typing

The simplest path: click into the input bar, type or paste your note, and proceed straight to structuring (5.6). Nothing about the AI pipeline requires voice, a typed transcript flows through exactly the same drafting and verification stages as a dictated one.

#### 5.4.2 Dictation: record, then refine

This is the standard microphone workflow, designed for a complete dictation done in one take.

1. Click the **microphone button**. Your browser may ask for microphone permission the first time, allow it.
2. The status line shows **"Recording, speak now…"** and a red recording indicator with a running timer appears next to the **Transcript** heading.
3. Speak naturally. Depending on how your deployment is configured, you may see instant live text appear as you talk (see the note below). Don't worry about its accuracy, it is only a preview.
4. Click the microphone button again (**Stop recording**) when you are done.
5. The status changes to **"Refining with medical AI…"** and a "Refining…" indicator appears. Orb now runs the full recording through its local medical speech engine, which is far more accurate on clinical vocabulary and drug names than any live preview.
6. When refinement completes, the transcript is replaced with the accurate version and the status reads **"Medical transcription complete"**. You can now edit the text freely before structuring.

**About the live preview.** During recording, Scribe can display instant, word-by-word text using the browser's built-in speech recognizer. Because that recognizer may send audio to a cloud service, a patient-privacy consideration, Orb only enables it when the deployment's privacy posture permits cloud speech. If your installation disallows it, you will simply see "Listening…" while you speak, and the full transcript appears after you stop, produced entirely by the local engine. Either way, the local engine's output is the source of truth.

**Shared microphone.** Scribe shares the microphone with Sage's voice features. If Sage is actively listening, Scribe will refuse to start and display "Microphone is in use by Sage, stop it before dictating." Stop the Sage voice session first, then start dictation.

**Very short recordings.** A recording of less than roughly a second is rejected with "Recording too short", record again and speak for at least a few seconds.

#### 5.4.3 Live streaming: utterance by utterance

The streaming button (the microphone icon with a green dot) starts a continuous live-transcription session that is better suited to long, interrupted dictations, for example documenting during a ward round.

1. Click the **streaming button** ("Start live streaming (VAD)").
2. A live indicator appears next to the **Transcript** heading showing one of three states, **Speaking** (red, you are talking), **Processing** (gold, an utterance is being transcribed), or **Listening** (green, waiting for speech), along with a small audio-level meter and a running count of utterances captured.
3. Speak in natural sentences. Scribe detects when you pause (voice-activity detection), transcribes that utterance on the server, and appends the result to the transcript. The status line alternates between **"Listening…"** and **"Processing utterance…"**.
4. Click the button again ("Stop live streaming") when you are finished. The accumulated transcript remains on screen for editing and structuring.

Streaming mode transcribes each utterance with the same local medical speech engine used by the refine step, including drug-name correction, so the text you see is already the accurate version, there is no separate refinement pass at the end.

**The local speech engine is an optional server component.** Both dictation modes depend on Orb's locally installed speech-recognition engine (a Whisper model accelerated for Apple Silicon). On some installations this component is deferred at setup time and installed later. If it is not present, typing still works fully, and the live browser preview (where permitted) still works, but the refine step will report **"Refinement unavailable, using browser transcript"** and live streaming will return errors. Ask your administrator to complete the speech-engine installation if you see this persistently; administrators can check the engine's warm-daemon status from the server's diagnostics.

### 5.5 Linking the note to a patient

Once a transcript exists and recording has stopped, an action row appears beneath the transcript. Its first control is a patient selector:

- **"No patient linked"** (the default), the note is saved without a patient association, attributed only to you as its author.
- Any admitted patient, the list shows each patient's name and bed (for example "Asha Rao · ICU-4"). Selecting one files the structured note under that patient's record, so it appears alongside their chart and is attributed to them in the audit trail.

Link the patient **before** clicking Structure Note; the association is stored when the note is created. If you dictated a note about a patient who has not yet been admitted, you can admit them directly from the note afterwards, see **Admit to Vigil** (5.10).

### 5.6 Structuring the note

Next to the patient selector are the format selector and the **Structure Note** button.

#### 5.6.1 Choosing a format

| Format | Purpose | Typical structure |
|---|---|---|
| **SOAP Note** | Routine encounter or progress documentation | Subjective, Objective, Assessment, Plan |
| **Discharge Summary** | End-of-admission summary for the patient and receiving providers | Course of admission, diagnoses, medications, follow-up |
| **I-PASS Handoff** | Shift-change or transfer-of-care handoff | Illness severity, Patient summary, Action list, Situation awareness, Synthesis |

#### 5.6.2 What happens when you click Structure Note

Click **✨ Structure Note**. The button changes to "Processing..." and the **Structured AI Note** window opens, streaming the note onto the screen word by word as the AI writes it. Behind the scenes, Scribe's pipeline has up to four stages:

1. **Transcription**, the speech engine converts audio to text (already done by the time you click, for dictated notes). A local drug-name dictionary corrects phonetically mangled medication names ("phosphomycin" → "Fosfomycin").
2. **Correction**, for longer transcripts, a language model fixes acoustic and grammatical errors without summarizing or removing content. Very short dictations skip this stage, since the speech engine is already highly accurate on them and skipping saves time.
3. **Drafting**, the main clinical language model reshapes the corrected transcript into the format you chose. This is the streaming text you watch appear in the window.
4. **Verification (optional)**, after the draft is delivered and saved, a background check compares the draft against your original transcript, hunting for unsupported claims (hallucinations), omissions, and mismatched clinical entities. Verification runs only if it is enabled in your server's configuration; because it is a background task, you can read and edit the note while it runs.

The note is saved as a **Draft** the moment drafting completes, closing the window does not lose it, and the audit trail records its creation.

A small footer line in the window reports how long each stage took, for example "Pipeline: ASR 1.2s · Correct 4.1s · Draft 8.3s · Verify 6.7s".

### 5.7 The Structured AI Note window

The window titled **✨ Structured AI Note** is where the note lives through its whole lifecycle. Its header shows two kinds of badges.

**Note status badges:**

| Badge | Meaning |
|---|---|
| **Draft** | Editable; not yet part of the permanent record. |
| **Signed** | Finalized by a doctor; read-only. |
| **Rejected** | A doctor rejected the draft, with a recorded reason. |
| **Superseded** | A newer signed revision has replaced this note. |

**Verification badges:**

| Badge | Meaning |
|---|---|
| **Verifying…** (spinner) | The background faithfulness check is still running. The panel polls for the result automatically for about a minute. |
| **✅ Verified** | The check passed, the draft is faithful to the transcript. Hover to see the confidence percentage. |
| **⚠️ Review suggested** | The check finished but found issues. Click the badge to expand the **Verification Findings** list: warnings tagged by note section (with the type of issue), and any **Omission** entries for content you said that did not make it into the draft. Review each finding against your transcript and edit the note accordingly. |
| **⚠ Verification unavailable** + **↻ Retry** | The check could not complete (engine busy, timeout, or a server restart interrupted it). Click **↻ Retry** to run it again. |

Below the badges is the note body. While a draft is streaming you will see a typing cursor; once complete, the full note is shown.

### 5.8 Reviewing, editing, signing, and rejecting

All lifecycle actions live in the button row at the bottom of the Structured AI Note window. What you see depends on the note's status and on your role.

#### 5.8.1 Editing a draft

1. Click **✏️ Edit**. The note body becomes an editable text area.
2. Make your corrections, fix findings from verification, adjust wording, add anything the AI missed.
3. Click **💾 Save** to store the edit (the edit is recorded in the audit trail, including who made it and when), or **Cancel** to discard your changes.

Only drafts can be edited. Signed notes are locked (see 5.8.4 for how to amend one).

#### 5.8.2 Signing a note (doctors only)

Clicking **✍ Sign & Finalize** transitions the note from Draft to **Signed**. Signing is the clinical act of finalization: it stamps the note with your identity and the signing time, locks its content, and publishes a signing event into Orb's audit chain. A green banner then reads: "Signed and finalized, this note is read-only. To make changes, create a revision."

**The sign-gate.** Orb will not let an AI-generated note be finalized casually. If the automated verification did not positively pass, because it found high-severity problems, could not complete, or never ran, the system blocks one-click signing and requires the signer to explicitly acknowledge that they are signing an unverified AI note. That acknowledgement, and the fact that the note was signed over an unverified verdict, is permanently recorded in the audit trail alongside the signature. In practice: wait for the **✅ Verified** badge (or resolve the findings behind **⚠️ Review suggested**, retry verification, and confirm the result) before signing.

The Sign and Reject buttons are only shown to users with signing authority (doctors). Nurses can create, dictate, and edit drafts, but their drafts must be signed by a doctor.

#### 5.8.3 Rejecting a note (doctors only)

If a draft is unsalvageable, wrong patient, garbled dictation, fundamentally incorrect content:

1. Click **✕ Reject**.
2. An input appears: "Reason for rejection…". A reason is mandatory; the **Confirm** button stays disabled until you type one.
3. Click **Confirm** (or **Cancel** to back out).

The note moves to **Rejected**, and the reason is stored with the note and written to the audit log. Rejected notes are not deleted, they remain part of the record of what the AI produced and why it was declined.

#### 5.8.4 Amending a signed note: revisions

Signed notes are never edited in place. To amend one:

1. Open the signed note and click **📋 Create Revision**.
2. Orb clones the signed content into a **new draft** with a version number one higher than the original, linked back to the note it supersedes.
3. Edit the new draft as usual, then have a doctor sign it.
4. Only at the moment the revision is **signed** does the original note change status to **Superseded**. Until then, the original remains the authoritative signed record.

This means there is never a gap where no signed note exists, and the full lineage, version 1 signed, version 2 draft, version 2 signed, version 1 superseded, is preserved and auditable.

### 5.9 Versioning and the audit trail

Every consequential Scribe action generates an audit event that administrators can review in Orb's audit tooling:

- Draft creation (including revisions, with their version number and the note they supersede)
- Draft edits (editor, timestamp)
- Verification lifecycle: started, completed (with the verdict and confidence), failed, manually retried, and reconciled after a server restart
- Signing (signer, version, the verification status at signing time, and whether an unverified verdict was explicitly acknowledged)
- Rejection (rejector and the stated reason)
- Supersession (which note replaced which)

The raw transcript is stored alongside every structured note, so it is always possible to compare what was said with what was documented. Dictation audio saved on the server is encrypted at rest.

### 5.10 Admit to Vigil: from note to admitted patient

If you dictated a note about a new patient, for example an emergency presentation, you can admit them straight from the structured note.

1. After structuring, click **✨ Admit to Vigil** (available both inside the Structured AI Note window and on the main panel next to **View Note** once the window is closed).
2. The **✨ Quick Admission** form opens. While it loads you will see "Auto-filling from context...": Orb's AI reads your note and transcript and pre-fills the form, name, age, sex, ward, bed, and a chief-complaint summary, extracting the initial diagnosis from the note's Assessment section where present.
3. Review and complete the fields. All are editable, and several are required:
   - **Patient Name**
   - **Age** and **Sex** (Male / Female / Other)
   - **Attending Doctor** (chosen from the hospital's doctor list)
   - **Ward** and **Bed Number**
   - **Initial Diagnosis (Assessed)**
4. Click **✨ Confirm Admission**.

On success the patient is created and appears in the Patients (Vigil) list immediately. If your deployment issues patient-portal accounts, the confirmation message also shows the patient's one-time Bridge login, an ID and temporary password, with the warning that it is **shown once**. Record it before the dialog closes (it stays on screen for a few seconds); it cannot be retrieved again.

Always verify the auto-filled demographics against the source dictation. The extraction is an AI convenience, not a data-entry guarantee.

### 5.11 Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "Microphone access denied." | The browser blocked microphone access. Allow microphone permission for Orb in your browser's site settings and try again. |
| "Microphone is in use by Sage, stop it before dictating." | Sage's voice listening is holding the microphone. Stop the Sage voice session, then start Scribe. |
| "Recording too short" | The recording was under about a second. Record again and speak for several seconds. |
| "Whisper empty, using browser transcript" | The local speech engine returned nothing (usually silence or an audio problem). The live-preview text, if any, is kept. Re-record closer to the microphone. |
| "Refinement unavailable, using browser transcript" | The local speech engine could not be reached, it may not be installed yet on this server, or its warm daemon is down. Typing still works; ask your administrator to check the speech-engine installation and daemon status. |
| "Streaming error" messages in live mode | The live-transcription session lost its connection or the speech engine failed on an utterance. Stop and restart streaming; check the network connection to the Orb server. |
| "Structuring failed: … Is Ollama running?" | The local AI model service is not running or the model is not loaded. An administrator should confirm the AI runtime is up; then click **✨ Structure Note** again, your transcript is not lost. |
| Badge shows "Verification timed out, try again later" or "⚠ Verification unavailable" | The background check exceeded its polling window or failed (a server restart mid-check will also do this). Click **↻ Retry** next to the badge. If verification is disabled in server configuration, it will not run at all. |
| **Sign & Finalize** does not finalize the note | The sign-gate is refusing an unverified note. Wait for verification to complete, retry it if it failed, resolve any high-severity findings, and sign again. Also confirm you are signed in with a doctor account, other roles cannot sign. |
| Sign / Reject buttons are missing | You are signed in as a nurse or another role without signing authority. Ask the attending doctor to review and sign the draft. |
| The dictation upload is refused as too large | Audio uploads are capped at 100 MB. Break very long dictations into shorter sessions, or use live streaming mode, which sends audio in small utterances. |

### 5.12 Safety & limitations

- **The AI drafts; you author.** Every structured note is created as a draft and is presumed unreliable until a clinician has read it against the transcript. Nothing Scribe produces enters the permanent record without a human signature.
- **Verification is an aid, not a guarantee.** The hallucination check is itself an AI model. A **✅ Verified** badge raises confidence but does not replace your own reading; conversely, findings under **⚠️ Review suggested** may include false positives. The one thing the system enforces is that a note which *failed* or *never received* verification cannot be silently one-click signed.
- **Fail-safe verification.** If the verifier itself breaks mid-check, Orb records "could not verify" rather than inventing a pass, an unverifiable note is treated the same as a failing one at the sign-gate.
- **Speech recognition mishears.** Clinical dictation is hard for any speech engine. Orb biases recognition toward medical vocabulary and corrects drug names against a pharmacology dictionary, but numbers, doses, and similar-sounding drugs remain the highest-risk transcription errors. Always check doses and medication names in the draft against what you said.
- **Live preview privacy.** The instant word-by-word preview during dictation may use the browser's cloud speech service and is disabled entirely on deployments that forbid sending audio off-site. The accurate transcript is always produced locally.
- **Signed means signed.** Signed notes cannot be edited or deleted from the interface, only superseded through the revision workflow, keeping the original visible in the record. Rejections require a stated reason. All of it is audited.
- **Not a decision-support tool.** Scribe documents what you said. It does not validate the clinical correctness of your assessment or plan, and its output must not be used as diagnostic advice.
