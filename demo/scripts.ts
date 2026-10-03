/**
 * Scripted workflow demos for the website.
 *
 * Each script drives the real front end through one workflow, step by step,
 * with a caption per step. The website's frame asks for a step, the director
 * in ./shim.ts performs the actions against the live DOM, and the frame shows
 * the caption. The app is locked to the workflow while a script runs: the
 * sidebar is hidden, shortcuts are off, and only that workflow's recording is
 * loaded, so a visitor sees the workflow and not the whole product.
 *
 * Selectors mirror docs/website-assets/capture.cjs, which records these same
 * flows against the real backend.
 */
export type Persona = 'doctor' | 'surgeon' | 'nurse' | 'pharmacist' | 'admin' | 'patient'

export type Action =
  | { do: 'click'; sel: string; text?: RegExp; nth?: number; optional?: boolean }
  | { do: 'fill'; sel: string; value: string }
  | { do: 'select'; sel: string; text: RegExp }
  | { do: 'press'; key: 'Enter' | 'Escape'; sel?: string }
  | { do: 'wait'; sel: string; timeout?: number; optional?: boolean }
  | { do: 'waitText'; re: RegExp; timeout?: number; optional?: boolean }
  | { do: 'waitEnabled'; sel: string; timeout?: number }
  | { do: 'scroll'; sel: string; optional?: boolean }
  | { do: 'sleep'; ms: number }

export interface ScriptStep { caption: string; actions: Action[] }

export interface DemoScript {
  id: string
  title: string
  persona: Persona
  steps: ScriptStep[]
}

const LLM = 120_000

const CONSULT = 'Sixty-seven year old man, day three of a COPD exacerbation. Still breathless on minimal exertion, saturations ninety-one percent on two litres, respiratory rate twenty-four, afebrile, chest with scattered wheeze and prolonged expiration. Continue nebulised salbutamol and ipratropium, prednisolone forty milligrams once daily for five days, oxygen to target saturation eighty-eight to ninety-two percent, repeat blood gas this evening, physiotherapy review, reassess for discharge in forty-eight hours if improving.'

export const SCRIPTS: DemoScript[] = [
  {
    id: 'ward-is-alive', title: 'The ward watches itself', persona: 'doctor',
    steps: [
      { caption: 'Orb Today lists what needs you first: deteriorating patients, new results, pending signatures.', actions: [{ do: 'wait', sel: '#panel-today, #panel-vigil' }] },
      { caption: 'Patients: the ward ordered by risk. The order is NEWS2-derived, not hand-placed.', actions: [{ do: 'click', sel: '#nav-vigil' }, { do: 'wait', sel: '.patient-row' }] },
      { caption: 'Open a chart. The NEWS2 band is computed by Orb’s deterministic engine, never by a model.', actions: [{ do: 'click', sel: '.patient-row', text: /Rajesh Iyer/ }, { do: 'wait', sel: '.command-bar, .patient-detail' }, { do: 'sleep', ms: 600 }] },
      { caption: 'Why this score? Every parameter’s contribution is shown. Orb will not give you a number without showing where it came from.', actions: [{ do: 'scroll', sel: '.news2-drivers', optional: true }, { do: 'sleep', ms: 400 }] },
      { caption: 'Patient Story: alerts, notes, doses given, handovers and bundles in one scrollable narrative.', actions: [{ do: 'click', sel: '#btn-story' }, { do: 'wait', sel: '.story-modal' }] },
    ],
  },
  {
    id: 'allergy-interlock', title: 'The allergy interlock', persona: 'doctor',
    steps: [
      { caption: 'Orders, then Order sets: cited protocols. Every item still goes through the allergy interlock, the dose guard and terminology validation.', actions: [{ do: 'click', sel: '#nav-orders' }, { do: 'wait', sel: '#panel-orders' }, { do: 'click', sel: '#panel-orders button', text: /Order sets/ }, { do: 'wait', sel: '.order-sets__card' }] },
      { caption: 'Pick Sepsis Six. The gates are patient-specific, so there is nothing honest to show until Orb knows whose chart this is.', actions: [{ do: 'click', sel: '.order-sets__card', text: /Sepsis Six/i }, { do: 'sleep', ms: 500 }] },
      { caption: 'Apply to Suresh Reddy, 55, community-acquired pneumonia with early sepsis.', actions: [{ do: 'fill', sel: '.order-sets input[placeholder*="Search by name"]', value: 'Suresh' }, { do: 'wait', sel: '.patient-picker__option' }, { do: 'click', sel: '.patient-picker__option', text: /Suresh/ }, { do: 'wait', sel: '.order-sets__block-chip, .order-sets__ok-chip', timeout: 30_000 }] },
      { caption: 'Eight items clear. Ceftriaxone is STOPPED: a documented anaphylaxis. The Apply button is disabled, because a half-applied sepsis bundle is its own emergency.', actions: [{ do: 'scroll', sel: '.order-sets__block-chip', optional: true }, { do: 'sleep', ms: 500 }] },
      { caption: 'Same set, different patient: Ananya Kapoor has a moderate penicillin rash. Orb raises a cross-reactivity alert but does not block.', actions: [{ do: 'fill', sel: '.order-sets input[placeholder*="Search by name"]', value: 'Ananya' }, { do: 'wait', sel: '.patient-picker__option' }, { do: 'click', sel: '.patient-picker__option', text: /Ananya/ }, { do: 'wait', sel: '.order-sets__caution-chip, .order-sets__ok-chip', timeout: 30_000, optional: true }] },
    ],
  },
  {
    id: 'wrong-room-warning', title: 'The wrong-room warning', persona: 'doctor',
    steps: [
      { caption: 'Messages: every patient has a case room. Open Suresh Reddy’s.', actions: [{ do: 'click', sel: '#nav-relay' }, { do: 'wait', sel: '.relay-channel-item' }, { do: 'click', sel: '.relay-channel-item', text: /Suresh Reddy/ }, { do: 'wait', sel: '.relay-chat__header' }] },
      { caption: 'Type an order about a different patient into this room: “Give Rajesh Iyer 40mg prednisolone now.”', actions: [{ do: 'fill', sel: 'input[placeholder="Type a message…"], textarea[placeholder="Type a message…"]', value: 'Give Rajesh Iyer 40mg prednisolone now.' }, { do: 'sleep', ms: 400 }] },
      { caption: 'Sent, not blocked. A nurse mid-emergency does not need a modal. Orb delivers it and says: this looks like it belongs to Rajesh Iyer, want me to move it?', actions: [{ do: 'press', key: 'Enter', sel: 'input[placeholder="Type a message…"], textarea[placeholder="Type a message…"]' }, { do: 'wait', sel: '.room-fit', timeout: 15_000 }] },
    ],
  },
  {
    id: 'sage-ask', title: 'Ask Sage', persona: 'doctor',
    steps: [
      { caption: 'Sage runs entirely on the appliance. No question, and no chart, leaves the building.', actions: [{ do: 'click', sel: '#nav-sage' }, { do: 'wait', sel: '#panel-sage' }] },
      { caption: 'Fast mode answers general questions with the small model. Patient, medication and dose questions still go to the careful model.', actions: [{ do: 'click', sel: '.sage-think-toggle--on', optional: true }, { do: 'sleep', ms: 500 }] },
      { caption: 'Ask: what are the classic ECG findings in hyperkalaemia?', actions: [{ do: 'fill', sel: '.sage-composer__input', value: 'What are the classic ECG findings in hyperkalaemia?' }, { do: 'sleep', ms: 300 }] },
      { caption: 'The answer, with a safety line inline and the guidance it retrieved listed under “Show details”.', actions: [{ do: 'press', key: 'Enter', sel: '.sage-composer__input' }, { do: 'waitText', re: /first words|Show details|Hide details/, timeout: LLM }, { do: 'click', sel: 'button', text: /^Show details$/, optional: true }, { do: 'sleep', ms: 600 }] },
    ],
  },
  {
    id: 'sage-refusal', title: 'Out of scope', persona: 'doctor',
    steps: [
      { caption: 'New chat. This login is General Medicine; cardiology is not its ward.', actions: [{ do: 'click', sel: '#nav-sage' }, { do: 'wait', sel: '#panel-sage' }, { do: 'click', sel: 'button', text: /New Chat/, optional: true }, { do: 'sleep', ms: 500 }] },
      { caption: 'Ask for the potassium result of the patient in cardiology bed C-201.', actions: [{ do: 'fill', sel: '.sage-composer__input', value: 'What is the potassium result for the patient in cardiology bed C-201?' }, { do: 'sleep', ms: 300 }] },
      { caption: 'No result. The cardiology chart is outside this login’s scope, so Sage is never shown it. Scoping is enforced in the server; a direct API call for that chart returns 404.', actions: [{ do: 'press', key: 'Enter', sel: '.sage-composer__input' }, { do: 'waitText', re: /first words|Show details|Hide details/, timeout: LLM }, { do: 'sleep', ms: 600 }] },
    ],
  },
  {
    id: 'nurse-shift', title: 'The nurse’s shift', persona: 'nurse',
    steps: [
      { caption: 'A nurse lands on My Shift, not on a chart: medications and observations due now, each with how late it is.', actions: [{ do: 'wait', sel: '#panel-shift' }] },
      { caption: 'My patients: the ward as this nurse owns it.', actions: [{ do: 'click', sel: '#nurse-tab-patients' }, { do: 'sleep', ms: 600 }] },
      { caption: 'Record observations: big numeric targets built for a tablet on a trolley.', actions: [{ do: 'click', sel: '#panel-shift button', text: /Record obs/ }, { do: 'wait', sel: '#obs-respiratory_rate', timeout: 10_000 }] },
      { caption: 'NEWS2 computes live as the values go in, with the RCP escalation sentence underneath. She sees what she is about to trigger before she saves it.', actions: [{ do: 'fill', sel: '#obs-respiratory_rate', value: '24' }, { do: 'fill', sel: '#obs-spo2', value: '91' }, { do: 'fill', sel: '#obs-systolic_bp', value: '98' }, { do: 'fill', sel: '#obs-diastolic_bp', value: '60' }, { do: 'fill', sel: '#obs-heart_rate', value: '112' }, { do: 'fill', sel: '#obs-temperature', value: '38.4' }, { do: 'click', sel: '.obs-segment__options button', text: /On oxygen/, optional: true }, { do: 'click', sel: '.obs-segment__options button', text: /Alert/, optional: true }, { do: 'sleep', ms: 1200 }] },
      { caption: 'Handover: an I-PASS draft written from recorded data, to be read against the patient before handing over.', actions: [{ do: 'click', sel: '[aria-label="Close observation entry"]', optional: true }, { do: 'sleep', ms: 300 }, { do: 'click', sel: 'button', text: /^Discard$/, optional: true }, { do: 'sleep', ms: 300 }, { do: 'click', sel: '#nurse-tab-handover' }, { do: 'sleep', ms: 800 }] },
    ],
  },
  {
    id: 'show-your-work', title: 'Show your work', persona: 'admin',
    steps: [
      { caption: 'Trust Center: compliance posture aggregated from the running system, not a PDF. Amber is amber; an all-green screen should be trusted less.', actions: [{ do: 'click', sel: '#nav-admin' }, { do: 'wait', sel: '#panel-admin' }, { do: 'click', sel: '#admin-tab-trust', optional: true }, { do: 'waitText', re: /Trust score|Compliance/i, timeout: 15_000, optional: true }, { do: 'sleep', ms: 600 }] },
      { caption: 'Flight Recorder: a SHA-256 hash chain over every audit row. Alter or delete one and the chain breaks. Every question Sage was asked is in there, and every refusal.', actions: [{ do: 'click', sel: '#admin-tab-flight', optional: true }, { do: 'sleep', ms: 1200 }] },
      { caption: 'Model Governance: each model, its version, and whether it has been validated. The forecast model says “untrained”. The validated one is the deterministic score that drives escalation.', actions: [{ do: 'click', sel: '#admin-tab-models', optional: true }, { do: 'sleep', ms: 1000 }] },
      { caption: 'Pilot Scorecard: how the pilot will be measured. Shadow-mode episodes, adjudication, outcomes, denominators included.', actions: [{ do: 'click', sel: '[id^="admin-tab-"]', text: /Scorecard|Pilot/i, optional: true }, { do: 'sleep', ms: 1200 }] },
    ],
  },
  {
    id: 'scribe-note', title: 'Scribe', persona: 'doctor',
    steps: [
      { caption: 'Scribe: dictate or type. Nothing is sent to a cloud transcription service.', actions: [{ do: 'click', sel: '#nav-scribe' }, { do: 'wait', sel: '#panel-scribe' }] },
      { caption: 'The consultation goes in, and the patient is linked.', actions: [{ do: 'fill', sel: '#scribe-input', value: CONSULT }, { do: 'wait', sel: '[data-testid="scribe-patient-select"]', timeout: 10_000 }, { do: 'select', sel: '[data-testid="scribe-patient-select"]', text: /Rajesh Iyer/ }, { do: 'sleep', ms: 400 }] },
      { caption: 'Structure Note: the local model drafts a SOAP note, then a second pass checks it against the transcript for unsupported claims and omissions.', actions: [{ do: 'click', sel: 'button', text: /Structure Note/ }, { do: 'waitText', re: /Note structured|Structuring failed/, timeout: LLM }, { do: 'waitText', re: /Verification Findings|Verified|Sign & Finalize/, timeout: 60_000, optional: true }, { do: 'sleep', ms: 800 }] },
      { caption: 'Sign & Finalize. This note was typed, never verified against audio, so Orb makes the clinician type SIGN: accepting an unverified AI note is recorded in their own name.', actions: [{ do: 'click', sel: 'button', text: /Sign & Finalize/ }, { do: 'wait', sel: '#scribe-unverified-confirm', timeout: 8000, optional: true }, { do: 'sleep', ms: 600 }] },
      { caption: 'Signed. Signing is the trigger: medications named in the note are extracted, screened and queued for a pharmacist before they become active orders.', actions: [{ do: 'fill', sel: '#scribe-unverified-confirm', value: 'SIGN' }, { do: 'click', sel: 'button', text: /Sign anyway/, optional: true }, { do: 'waitText', re: /Signed and finalized/, timeout: 30_000, optional: true }, { do: 'sleep', ms: 600 }] },
    ],
  },
  {
    id: 'pharmacy-verify', title: 'Pharmacist verification', persona: 'pharmacist',
    steps: [
      { caption: 'Helix: the pharmacy overview.', actions: [{ do: 'wait', sel: '#panel-helix' }] },
      { caption: 'The verify queue: medications extracted from signed notes wait here for a pharmacist before they become active orders. When a drug is outside the interaction knowledge base, Orb says NOT SCREENED instead of pretending.', actions: [{ do: 'click', sel: '#nav-pharmacy-verify' }, { do: 'wait', sel: '#panel-pharmacy-verify table tbody tr', timeout: 15_000, optional: true }, { do: 'sleep', ms: 600 }] },
      { caption: 'One row per extracted medication: dose, route and frequency editable, the source marked AI EXTRACTED, the safety line in plain words. Verify or Reject, on the record.', actions: [{ do: 'scroll', sel: '#panel-pharmacy-verify table tbody tr', optional: true }, { do: 'sleep', ms: 400 }] },
    ],
  },
  {
    id: 'patient-portal', title: 'Bridge, the patient portal', persona: 'patient',
    steps: [
      { caption: 'Bridge: care team, medications, results and documents for the patient, with a one-time login issued at admission.', actions: [{ do: 'wait', sel: '#bridge-logout', timeout: 20_000, optional: true }, { do: 'sleep', ms: 600 }] },
      { caption: 'Ask a question in plain language. The answer is scoped to this patient and never gives medical advice; it points back to the nurse or doctor.', actions: [{ do: 'scroll', sel: '#bridge-ask-input' }, { do: 'fill', sel: '#bridge-ask-input', value: 'When can I go home?' }, { do: 'click', sel: '#bridge-ask-btn' }, { do: 'wait', sel: '.bridge__answer', timeout: LLM }, { do: 'waitEnabled', sel: '#bridge-ask-btn', timeout: LLM }, { do: 'scroll', sel: '.bridge__answer', optional: true }] },
    ],
  },
  // ── Modules without a recorded storyline: a short guided look, still locked to the module ──
  {
    id: 'lens', title: 'Lens', persona: 'doctor',
    steps: [{ caption: 'Lens takes an X-ray, an ECG or a photo of a wound and drafts a first read on the appliance. Nothing is uploaded anywhere else.', actions: [{ do: 'click', sel: '#nav-lens' }, { do: 'wait', sel: '#panel-lens' }] }],
  },
  {
    id: 'forecast', title: 'Forecast', persona: 'doctor',
    steps: [
      { caption: 'Census against capacity for the week ahead. The badge says it plainly: the model is untrained until real data exists.', actions: [{ do: 'click', sel: '#nav-forecast' }, { do: 'wait', sel: '#panel-forecast' }] },
      { caption: 'Ward capacity, bed by bed.', actions: [{ do: 'click', sel: '#panel-forecast button', text: /Ward Capacity/ }, { do: 'sleep', ms: 800 }] },
      { caption: 'The discharge board: who is likely ready, and what is blocking the rest.', actions: [{ do: 'click', sel: '#panel-forecast button', text: /Discharge Board/ }, { do: 'sleep', ms: 800 }] },
    ],
  },
  {
    id: 'command-center', title: 'Command Center', persona: 'doctor',
    steps: [{ caption: 'Census, critical patients, sepsis bundle compliance and downtime readiness on one screen, with the ward acuity map and the active deteriorations by name.', actions: [{ do: 'click', sel: '#nav-command' }, { do: 'wait', sel: '#panel-command' }] }],
  },
  {
    id: 'surge-simulator', title: 'Surge Simulator', persona: 'doctor',
    steps: [
      { caption: 'The simulator starts from today’s census, beds and admissions per day, not a spreadsheet typed in for the meeting.', actions: [{ do: 'click', sel: '#nav-capacity' }, { do: 'wait', sel: '#panel-capacity' }] },
      { caption: 'Twenty admissions tonight: peak occupancy, hours to overflow, beds short and the nurses you would need.', actions: [{ do: 'click', sel: '.capsim__preset', nth: 0 }, { do: 'sleep', ms: 1200 }] },
      { caption: 'A mass casualty of thirty over twelve hours.', actions: [{ do: 'click', sel: '.capsim__preset', nth: 3 }, { do: 'sleep', ms: 1200 }] },
      { caption: 'Aggressive discharge, to see what it buys back.', actions: [{ do: 'click', sel: '.capsim__preset', nth: 4 }, { do: 'sleep', ms: 1200 }] },
    ],
  },
  {
    id: 'appointments', title: 'Appointments', persona: 'doctor',
    steps: [{ caption: 'Follow-ups, medication reviews and post-discharge checks in day columns, each with the patient’s risk band.', actions: [{ do: 'click', sel: '#nav-appointments' }, { do: 'wait', sel: '#panel-appointments' }] }],
  },
  {
    id: 'revenue-integrity', title: 'Revenue Integrity', persona: 'doctor',
    steps: [{ caption: 'Pick a patient and press Analyze: Orb reads the notes already on the chart and lists the codes they support and the documentation gaps that block them.', actions: [{ do: 'click', sel: '#nav-coding' }, { do: 'wait', sel: '#panel-coding' }] }],
  },
  {
    id: 'surgical-suite', title: 'Surgical Suite', persona: 'surgeon',
    steps: [
      { caption: 'The week: every listed procedure with its theatre, surgeon and readiness.', actions: [{ do: 'click', sel: '#nav-surgical-suite', optional: true }, { do: 'wait', sel: '#panel-surgical-suite' }] },
      { caption: 'Active cases as they move through the WHO surgical safety checklist.', actions: [{ do: 'click', sel: '#panel-surgical-suite button', text: /Active Cases/i, optional: true }, { do: 'sleep', ms: 800 }] },
    ],
  },
  {
    id: 'pulse', title: 'Pulse', persona: 'doctor',
    steps: [
      { caption: 'Pulse sits beside the ward: weather, air quality, flu surveillance and drug recalls, read from public feeds.', actions: [{ do: 'click', sel: '#nav-today' }, { do: 'wait', sel: '#panel-today' }, { do: 'click', sel: '#btn-pulse' }, { do: 'wait', sel: '.pulse-panel', timeout: 10_000 }] },
      { caption: 'Inbound only: the one call out carries a map coordinate and no patient. Every call is logged, and the firewall can block it with no loss of clinical function.', actions: [{ do: 'sleep', ms: 800 }] },
    ],
  },
]

export const scriptById = (id: string): DemoScript | undefined => SCRIPTS.find(s => s.id === id)
