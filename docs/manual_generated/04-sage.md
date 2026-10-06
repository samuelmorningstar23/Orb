## 4. Sage: Clinical Intelligence Assistant

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The Sage tab. Suggestion cards start a query; answers carry governance badges and cited sources.](assets/manual/sage-assistant.png)

*The Sage tab. Suggestion cards start a query; answers carry governance badges and cited sources.*

Sage is Orb's clinical intelligence assistant: a chat interface where you ask questions in plain language, about a single patient, the whole ward, a drug, or a protocol, and receive an evidence-grounded answer generated entirely on the hospital's own hardware. Sage reads live chart data, searches the hospital's guideline library, and cites what it used. It is a **decision-support tool, not an ordering system**: it can never change a chart, place an order, or administer anything on its own. Anything that looks like an action becomes a *draft* a clinician must explicitly approve.

Sage appears in three places:

| Surface | Where you find it | What it does |
|---|---|---|
| **Sage tab** | Sidebar → **Sage** | Full chat workspace with history, details, sources, and voice mode |
| **Floating orb** | Bottom corner of every clinician screen | Text-only shortcut ("Ask Sage") that points you to Sage to type; it does not listen |
| **Sage Copilot / insights** | Relay case rooms and the Patients dashboard | Ambient listening and proactive alerts that draft actions for your confirmation |

All three run on the same local AI models and the same safety rules. Nothing you type or say to Sage about a patient leaves the building.

### 4.1 Opening Sage and asking a question

1. Sign in with your employee ID and password, then click **Sage** in the left sidebar.
2. A fresh visit shows the landing screen: the Orb logo, the line **Orb Sage · Clinical Intelligence**, a time-of-day greeting with your name, and the prompt *"How can I assist with your clinical workflow today?"*
3. Type into the box labelled **"Ask Sage a clinical question..."** and press **Enter** (or click the send arrow). **Shift+Enter** inserts a new line without sending.
4. While Sage is generating, the send arrow becomes a **Stop** button ("Stop generating"). Clicking it keeps the text already received and tags the message **⏸ Stopped by user**. Typing a new question mid-answer also works, Sage abandons the old response and starts the new one.

#### The suggestion cards

The landing screen offers four one-click starter queries. Each card sends a fully formed question so you can see what Sage does without composing anything:

| Card | Subtitle | What it asks |
|---|---|---|
| **Patient Summary** 📋 | Overview of current unit census | A brief summary of all active patients and their current status |
| **Lab Insights** 🧪 | Flag recent critical values | The most recent lab results across all patients, with abnormal values highlighted |
| **Discharge Planning** 🏥 | Review discharge readiness | Which patients are closest to discharge and what criteria remain |
| **Drug Interactions** 💊 | Safety check across meds | A review of current medications for all active patients, flagging potential interactions |

#### The conversation sidebar

The left rail shows the **Sage / Clinical AI** brand and two controls:

- **New Chat**, clears the screen *and* deletes your stored Sage conversation history. Use it when switching topics so old patient context cannot bleed into new answers.
- **Recent Conversations**, your last five questions, each labelled with the patient concerned (e.g. "Patient: Ananya Iyer") or "General Medical". Clicking one reloads that exchange. History is per-user and stored locally.

Sage also uses your recent history for context: a follow-up like "and what about her potassium?" is understood because the previous turns are passed back to the model. History is deliberately *not* used for standalone general-medicine questions, so an old patient cannot contaminate an unrelated pharmacology answer.

### 4.2 How Sage builds an answer (RAG, in plain terms)

Sage does not answer from the model's memory alone. Before the model writes a word, Orb assembles an evidence pack, this is the "retrieval-augmented generation" (RAG) pattern:

1. **Query scoping.** Sage first works out what kind of question you asked:
   - **One patient**, you named a patient, bed, or ward ("How is Ramesh in bed 4 doing?"). Matching tolerates partial names and voice-transcription mangling.
   - **Follow-up / pronoun**, "what are *her* latest labs?", "*this patient*". Sage resolves the pronoun against the patient on your screen, or the patient in your recent messages.
   - **Whole-ward**, census, handoff, "all patients", "who is deteriorating". Sage pulls a compact one-line-per-patient snapshot of the live census with latest vitals and risk levels.
   - **Aggregate statistics**, pure counts and distributions ("how many patients are admitted?", "bed occupancy"). Answered from counts only, no identifiers; small counts in breakdowns are masked (shown as "<5") so an individual cannot be inferred from a number.
   - **General medical**, drug mechanisms, side effects, protocol questions with no patient attached.
2. **Patient facts.** For patient-scoped questions Sage assembles: demographics and diagnosis, latest vitals with trend arrows, the current risk score and its drivers, active medications, recent labs, the most recent *signed* Scribe note, the latest Lens imaging analysis, and the patient's trajectory over time. It also tracks what is *missing* (e.g. "No recent labs"), see the Partial-data badge below.
3. **Guideline retrieval.** Sage searches the local clinical knowledge base, a guideline corpus stored and indexed on the hospital server, combining meaning-based (semantic) and keyword search with a second-stage reranker. Nothing is fetched from the internet.
4. **Generation.** The evidence pack plus your question goes to the local language model, which writes the answer under a strict response contract (see section 4.11).

#### Trust tiers: local policy beats external guidance

Every document in the guideline library carries a trust tier, and Sage is instructed that **local policy overrides external guidance**:

| Tier | Label in the details view | Meaning |
|---|---|---|
| 1 | Local Policy | Your hospital's own SOPs and protocols, highest authority |
| 2 | Local Formulary | Your hospital's formulary and pharmacy rules |
| 3 | External Guideline | Medical-society guidance and literature, advisory |

In the expanded details of an answer, retrieved evidence is split into **🏥 Local Policy** and **📚 External Guidance** sections so you can see which tier drove the recommendation. Demonstration documents are explicitly marked as synthetic.

#### Citations are verified, not trusted

The model must list which documents it relied on. Orb then **validates those citations against what was actually retrieved**: if the model cites a document that was not in the evidence pack (i.e. it may have invented a source), the citation is discarded and the answer's confidence is downgraded to *low*, which triggers the "Limited evidence" badge. The source count shown next to **Show details** (e.g. "· 2 sources") reflects only verified citations.

### 4.3 Reading a Sage answer: badges, confidence, and display modes

#### Governance badges

Coloured banners can appear above an answer. They are applied by Orb's own logic, not by the model, so the model cannot suppress them:

| Badge | Colour | Trigger |
|---|---|---|
| **⚠️ High-Risk Query, Verify with attending** | Red | The question matched a high-risk pattern: chest pain/ACS, respiratory failure, stroke/tPA, sepsis and shock, high-risk medications (anticoagulants, insulin infusions, opioids, potassium), or any dose change. Treat the answer as a discussion aid and confirm with the attending before acting |
| **🔍 Limited evidence, Verify independently** | Amber | Retrieval was weak, or the model's citations failed validation. Check the source material yourself |
| **⚠️ Partial data: …** | Amber | The chart is missing categories Sage would normally use (e.g. "No recent labs, No signed Scribe notes"), the answer was formed without that data |
| **⏸ Stopped by user** | Grey | You pressed Stop mid-generation; the answer is incomplete |

For high-risk scenarios Sage is additionally instructed never to give a definitive, unverified drug dose, it will recommend escalation instead.

#### Concise vs. detailed display

Sage answers in a short, colleague-style **concise answer** by default. The fuller reasoning lives behind the **Show details / Hide details** button beneath the bubble, which expands into the sections that have content: **📋 Patient Facts**, **🏥 Local Policy**, **📚 External Guidance**, **⚕️ Clinical Assessment**, **⚠️ Caveats**, and **🚨 Escalation**. The details row also shows a gold patient chip (the patient the answer was scoped to, marked "(discharged)" in amber if no longer admitted) and the verified source count.

Whether the panel starts open is decided by Orb, not the model: it **auto-expands** whenever the answer is high-risk, confidence is low, critical chart data was missing, or you explicitly asked for reasoning or evidence. The model's own preference is advisory only.

#### Disambiguation and non-existent patients

- If your question matches **more than one patient** (two Sharmas on the ward), Sage will not guess, it lists the matches with bed and ward and asks you to specify.
- If you ask about a patient **who is not on the census or in recent discharges**, Sage is hard-grounded to say no matching patient was found. It is explicitly forbidden from inventing vitals, labs, or diagnoses for a name it cannot find.

### 4.4 Patient-context awareness: "this patient" works everywhere

Every clinician screen quietly publishes a small description of what you are looking at, the active tab, the open patient's name, and any open item (for example, a selected report). Sage uses this two ways: **pronoun resolution**, with a chart open, "summarise this chart" or "what are her latest labs?" scopes automatically to the on-screen patient, in the Sage tab, voice mode, and the floating orb alike, and **situational awareness**, letting the answer reflect what you have open.

Two properties are worth knowing: the on-screen context is treated strictly as *data*, never as instructions to the model; and opening a patient pre-warms Sage's context for them in the background, so your first question about them answers faster.

### 4.5 Voice mode in the Sage tab

The microphone icon in the composer toggles **Voice mode** (tooltip "Voice mode" / "Switch to text").

1. Click the microphone. Sage shows "Voice Mode, Sage is ready to listen" and begins recording automatically. If the local speech engine is not installed on your Orb server, Sage does not switch to voice mode; it says straight away **"Voice questions are not available on this appliance: local speech recognition is not installed. Type your question instead."**
2. Speak your question. The status reads **"Listening… (local transcription; tap to stop)"** with an elapsed timer. No text appears while you speak, and recording does not stop on its own: tap the stop square when you have finished.
3. The audio is transcribed **locally by Whisper** ("Transcribing with Whisper AI…"). Transcription is biased with the current patient names so spoken names resolve correctly, and a clinical spelling corrector fixes common drug-name mis-hearings (e.g. "meropenim" → "Meropenem").
4. The transcript is sent to Sage ("Sending to Sage…"), and the reply is displayed and **spoken aloud** sentence-by-sentence by a voice that runs on your device. If your device has no on-device voice, Sage does not speak and the answer is shown on screen only. When Sage finishes speaking (or, with no on-device voice, when the answer is complete) it automatically listens for your follow-up, giving a continuous back-and-forth.

Voice replies come from the faster conversational model rather than the large reasoning model, trading some depth for speed. Status lines cycle through "Sage is thinking…", "Sage is speaking…", and "Tap to speak".

**Local speech only.** Everything in voice mode happens on the Orb server and your device: the recording is transcribed by the local speech engine, and answers are spoken only by a voice that runs on your device. Orb never uses the browser's own cloud speech recognition, and there is no setting that turns it on.

**One microphone, many surfaces.** Only one voice surface may hold the microphone at a time; if Scribe or Ward Round dictation has it, the panel shows "Microphone is in use by another Sage surface."

### 4.6 The floating Ask Sage orb

A small orb sits in the bottom corner of **every** clinician screen, Patients, Orders, Relay, anywhere. Its tooltip reads **"Ask Sage"**. The orb is text-only: it never listens. Clicking it opens a small card, **"Voice is not available here. Type your question in Sage instead."**, with an **Open Sage to type** button that takes you to the Sage tab. Press Escape or click the orb again to close the card. Sage still knows what is on your screen, so "summarise this chart" works on whatever patient you have open.

There is no wake word and nothing listens in the background. To ask by voice, use voice mode in the Sage tab (4.5), which uses the local speech engine on the Orb server.

### 4.7 Asking Sage to *do* something: drafted actions, never executions

Sage is read-only against the chart. When you ask it to act, "Add paracetamol 500 mg for Ajay", two independent detectors fire: a deterministic phrase gate on your request, and the model's own declared intent. Either puts the turn into **action mode**:

1. The model's prose answer is **discarded and replaced** with a fixed, policy-written message, the model never describes an action in its own words.
2. A **Chart Action card** appears, titled e.g. **"Chart Action: ADD MEDICATION"**, showing the patient, the medication, and a status badge:

   | Badge | Meaning |
   |---|---|
   | **Drafted** (amber) | The medication was found in the pharmacy formulary; a request has been drafted and awaits your approval |
   | **Not Available in Pharmacy** (red) | The medication is not in the formulary; nothing was drafted |
   | **Sent to Pharmacy** (green) | You approved the draft and it was sent to Helix, the pharmacy queue |
   | **Not Executed** | The requested change (e.g. an arbitrary chart edit) is not supported; no chart change was made |
   | **Failed** (red) | The send to pharmacy failed |

3. For a drafted medication, the card's footer shows **"Approve & Send to Helix"**. Only clicking that button, a deliberate clinician action, sends the request onward, and even then it lands in the pharmacy verification queue, not directly on the medication chart. The footer text spells this out: the action "requires explicit clinician approval before sending to Helix."

Any other chart mutation is refused with: *"I understood your request to update the chart, but direct modifications … are not currently supported in this environment. No chart change has been made."*

### 4.8 Sage beyond the chat: proactive insights and ambient actions in Relay

#### "Sage: needs your attention" (Patients dashboard)

Server-side surveillance continuously evaluates admitted patients. When it finds something noteworthy, a card stack titled **"Sage, needs your attention"** appears on the Patients (Vigil) dashboard, each insight carrying a severity dot (red critical, amber warning, blue info), a title, and detail text. Each offers:

- **Dismiss**, clears it. Dismissals are tracked: insight patterns clinicians consistently dismiss are suppressed over time, except critical ones, which are never suppressed.
- **⚡ Draft …** (e.g. "⚡ Draft Pantoprazole"), one click turns the suggestion into a draft action card in that patient's Relay case room, confirmed by a toast: *"Drafted '…' in \<patient\>'s case room, open Relay to confirm."* Nothing has been executed at this point.

Four insights show by default; **"Show N more"** expands the list.

#### Sage Copilot (Relay case rooms)

Inside a patient's case room in Messages (Relay), Sage passively listens to the team's conversation, filtering out chit-chat and acknowledgements, and maintains a compact room memory. When it has something to show, the **Sage Copilot** sidebar appears with:

- **Discussion summary** and the **current concern** the team is working.
- **Risk Flags**, shown only at 80%+ confidence, each labelled **Active** or **Stale** (older than 30 minutes) with the message signal that triggered it.
- **Suggested Actions**, cards (e.g. START MEDICATION, TRANSFER TO SURGERY) drafted from the conversation, each with target, triggering signal, and three buttons: **Acknowledge**, **Dismiss**, **Confirm**. Suggestions below 75% confidence are not shown at all.

**Confirm is the only button that does anything downstream.** It dispatches an auditable handoff to the right module, medications to Helix (the pharmacy queue), documentation to Scribe, surgical transfers to the surgery/transfer workflow, and the card moves to **Handled Actions** with a status badge: **Sent to Helix** (or Scribe/Transfer), **Charted only**, **handoff pending**, **handoff failed**, **Blocked**, or **Unsupported route**. **Blocked** means the drug-safety screen stopped a medication order, so nothing was charted; the reason is shown under the card. **Charted only** means the medication is on the chart for review but no pharmacy order exists; the reason is shown under the card. When the screen raised alerts that did not stop the order, they are listed under the card as **Drug-safety alerts when confirmed**. Dismissed and confirmed cards remain visible as a record of what was suggested and decided.

### 4.9 Speed: what to expect on local hardware

Sage runs a 27-billion-parameter medical model on the hospital's own Apple Silicon machine. That is the price of keeping patient data on-premises, and it is measurably slower than cloud chatbots:

- **First words on a question that carries a patient's chart: about 6–8 seconds, median, with the model warm**, the measured figure across five 70-turn evaluation runs, not a target. **Under three seconds is not reachable for chart-carrying questions**; ~1,500 tokens of chart and guideline text must be read at ~163 tokens/second before the first word. Sage streams sentence-by-sentence, so you read while it writes.
- **Full answers**, especially detailed single-patient syntheses and census summaries, can take tens of seconds to finish.
- **The first question after a quiet period is the slowest**, because the model may need to load into memory. Opening a patient chart pre-warms their context, which helps.
- **Drafted-action turns appear all at once** at the end rather than streaming, deliberate, because the safety layer must rewrite an action answer before anything is shown.
- **Voice replies are faster**, using the smaller 4B conversational model.
- Requests from multiple clinicians queue for the same local model, so answers slow under simultaneous heavy use.

Each answer's timing (retrieval, first token, total) is recorded in its metadata for administrators reviewing performance.

### 4.10 Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| **"Connection failed. Ensure local models are running and responsive."** | The local model server is down or the model is not installed | Ask an administrator to check the AI service; the Pulse popover in the header shows system health |
| First answer very slow, then fine | Model loading into memory after idle | Wait it out once; subsequent answers are faster |
| Answer stops mid-sentence | You pressed Stop, or the model hit its output limit | Re-ask, or ask Sage to continue |
| Sage lists several patients and asks which one | Your question matched more than one patient | Repeat with the full name or bed number |
| Sage says no matching patient was found | The name/bed is not on the census or recent discharges | Check spelling; the patient may be discharged |
| Sage refuses and mentions safety rules | Your phrasing tripped the harm or injection gate | Rephrase as a genuine clinical question; for real safety concerns escalate to the senior clinician on duty |
| **"Limited evidence"** on most answers | The corpus lacks material for your specialty, or citations failed validation | Verify against primary sources; ask an administrator about extending the corpus |
| **"Mic blocked…" / "Mic busy…" / "No mic detected…"** | Browser or OS microphone problem | Follow the on-screen message; the panel retries mic access once automatically |
| **"Microphone is in use by another Sage surface."** | Scribe or Ward Round dictation holds the mic | Stop the other surface, then retry |
| Clicking the floating orb shows "Voice is not available here" | The orb is text-only by design | Press **Open Sage to type**, or use voice mode in the Sage tab |
| **"Voice questions are not available on this appliance…"** | The local speech engine is not installed on this Orb server | Type your question; ask your administrator to complete the speech-engine installation |
| Sage answers on screen but does not speak | This device has no on-device voice, and Sage never uses one that sends text to an outside service | Read the answer on screen; ask IT whether an on-device voice can be installed on this computer |
| An old patient keeps appearing in answers | Conversation history is scoping follow-ups to them | Press **New Chat** before switching topics |

### 4.11 Safety & limitations

Sage's guardrails are layered, and most are enforced by deterministic software *around* the model, so a model error cannot bypass them:

- **No autonomous chart mutations.** Sage cannot write to the chart. Medication requests become drafts requiring the **Approve & Send to Helix** click; ambient suggestions in Relay require **Confirm**; everything else is refused with an explicit "No chart change has been made."
- **Pre-model refusal gates.** Requests expressing intent to harm a patient, and prompt-injection attempts ("ignore your previous instructions", "reveal your system prompt"), are refused by a deterministic filter *before* the model runs, in text and voice alike.
- **Forbidden-phrase stripping.** Phrases in which the model falsely claims to have acted, "I've added…", "Order placed", "Chart updated", a bare confirmatory "Done.", are stripped from every response, typed or spoken. Streaming is sentence-gated so an unsanitized fragment can never reach your screen, and action-mode turns are held back entirely until the safety layer has rewritten them.
- **Record content is data, not instructions.** Everything retrieved from the chart or guideline library is fenced off as data; instruction-like text inside a note or lab comment is ignored as a command.
- **Cited-source validation.** Citations are checked against what was actually retrieved; unverifiable ones are dropped and confidence is downgraded, surfacing the "Limited evidence" badge.
- **No fabrication.** Census answers may only reference patients actually in the census, and questions about unknown patients return "no matching patient" rather than invented data.
- **Privacy by architecture.** Every Sage request, patient-scoped or not, is answered by the local model on this machine. Orb ships exactly one chat provider and carries no cloud AI SDK in its dependencies, so there is no external lane to enable and no provider to configure. Hospital-wide questions are additionally answered from counts and distributions only, with no patient row produced and any non-zero count below five masked as "<5" (plus a second cell masked so the suppressed one cannot be recovered by subtraction). Every inference is recorded, provider, model, and a lane value that can only ever read `local`, in the tamper-evident audit chain.
- **High-risk discipline.** High-risk scenarios trigger the escalation badge, forced display of reasoning, and a prohibition on definitive unverified dosing.

**Limitations to keep in mind.** Sage's knowledge is bounded by its local guideline corpus and the model's training; it does not search the internet. A small corpus means tangential matches are possible, exactly what the "Limited evidence" badge flags. Voice transcription, even with clinical spell-correction, can mis-hear names and doses: read back the transcript before relying on it. Ambient Copilot suggestions come from informal team chat and carry confidence thresholds precisely because conversational language is ambiguous. Above all: **Sage advises, clinicians decide.** Every answer, especially one carrying a badge, is an input to your judgement, not a substitute for it.
