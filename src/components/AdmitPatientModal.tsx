import React, { useState, useCallback, useEffect, useSyncExternalStore } from 'react';
import { admitPatient, apiFetch, type AdmissionResult } from '../api/client';
import { useAuth } from '../App';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { formatClinicalDate, formatClinicalDateTime, localDateKey } from '../lib/datetime';
import { currentModuleFlags, subscribeModuleFlags, useClinicalSwitches } from '../shellConfig';
import {
  BED_REFUSAL_CODES,
  EMPTY_BED_CHOICE,
  bedChoiceMissing,
  bedChoicePayload,
  bedChoiceSummary,
  newIdempotencyKey,
  type AdmissionMaster,
  type BedChoice,
} from '../api/adt/moves';
import BedPicker from './adt/BedPicker';
import { ageFromDob, ageInRange } from './admissionAge';
import {
  EMPTY_STATED_IDENTITY,
  admitExistingPatient,
  fetchConversionPreview,
  statedIdentityPayload,
  type ConversionPreview,
  type StatedIdentityInput,
} from '../api/admitExisting';
import {
  ChartedAllergiesNote,
  ConversionRefusalNote,
  ExistingIdentityPanel,
  OpdMedicinesReference,
  OpdObservationsReference,
  RefusedVisitPanel,
} from './AdmitExistingParts';
import {
  previewReadmissionNote,
  readmitCandidatesOf,
  readmitPatient,
  type ReadmissionFields,
  type ReadmitCandidate,
} from '../api/readmission';
import {
  ReadmissionNote,
  ReadmitIdentityPanel,
  ReadmitOffer,
  StoppedMedicinesNote,
} from './ReadmitParts';
import './AdmitPatientModal.css';

/** opd-17a: admit the registered outpatient of this open clinic visit onto
 *  their own record, instead of creating a new patient. */
export interface ExistingPatientAdmission {
  visitId: string;
  /** Ruling OPD-N8: why the visit has no signed note, when it has none. */
  noSignedNoteReason?: string | null;
}

interface Props {
  onClose: () => void;
  onSuccess: () => void;
  /** Existing-patient mode (opd-17a). Without it the form admits a new patient,
   *  exactly as before. */
  existing?: ExistingPatientAdmission;
}

type Tab = 'identity' | 'admission' | 'clinical' | 'history' | 'vitals' | 'notes' | 'review';

/** What the form chooses from (GET /api/admissions/options). No patient data:
 *  department names, doctors, ward and bed labels, and whether a bed is held. */
interface AdmissionOptions {
  departments: string[];
  default_department: string | null;
  doctors: { id: string; name: string; department: string | null }[];
  wards: { ward: string; departments: string[] }[];
  beds: { ward: string; bed: string; in_use: boolean }[];
  /** Only while Beds and admissions (ORB_ADT) is on: bed setup's wards and
   *  beds with their status, for the bed picker (adt-5). */
  master?: AdmissionMaster;
}

/** What the admission answers with Beds and admissions on, beyond the base,
 *  and for a readmission onto the same record (opd-17c). */
type AdmissionAnswer = AdmissionResult & ReadmissionFields & {
  replayed?: boolean;
  location_warning?: string | null;
};

/** Ward and bed labels compare trimmed and case-blind, as the server does. */
const sameLabel = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/** Why a typed admission time cannot be sent, or null. The server enforces
 *  the same window (ruling ADT-P3) and answers 400 in its own words. */
function admittedAtProblem(value: string, backtimeHours: number): string | null {
  if (!value) return null;
  const when = new Date(value).getTime();
  if (Number.isNaN(when)) return 'Give a date and a time.';
  const now = Date.now();
  if (when > now + 5 * 60_000) return 'The admission time is in the future.';
  if (now - when > backtimeHours * 3_600_000) {
    return `The admission time can be entered up to ${backtimeHours} hours back.`;
  }
  return null;
}

// Helper for Chip Input. Defined at module scope so it keeps its identity across
// parent re-renders (defining it inside the component remounted it on every
// formData change, losing input focus after each chip added).
interface ChipInputProps {
  chips: string[];
  placeholder: string;
  onAdd: (value: string) => void;
  onRemove: (chip: string) => void;
}

function ChipInput({ chips, placeholder, onAdd, onRemove }: ChipInputProps) {
  return (
    <div className="admit-chip-container">
      {chips.map(chip => (
        <div key={chip} className="admit-chip">
          {chip}
          <button type="button" aria-label={`Remove ${chip}`} onClick={() => onRemove(chip)}>&times;</button>
        </div>
      ))}
      <input
        type="text"
        className="admit-chip-input"
        placeholder={placeholder}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            onAdd(e.currentTarget.value);
            e.currentTarget.value = '';
          }
        }}
        onBlur={(e) => {
          if (e.target.value) {
            onAdd(e.target.value);
            e.target.value = '';
          }
        }}
      />
    </div>
  );
}

export default function AdmitPatientModal({ onClose, onSuccess, existing }: Props) {
  /* On an Orb Ward Record computer the drug chart stays on paper: the form
     asks for no current medicines (the server keeps none either). */
  const { wardRecord } = useClinicalSwitches();
  const { user } = useAuth();
  const [activeTab, setActiveTab] = useState<Tab>('identity');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // What the clinician must act on now the patient is admitted: the one-time
  // Bridge login (shown ONCE) and whether a discharge link will be possible.
  const [credential, setCredential] = useState<{ login: string | null; password: string | null; dobRecorded: boolean; bedWarning: string | null; locationWarning?: string | null; replayed?: boolean; readmission?: ReadmissionFields | null } | null>(null);
  // Department, attending and bed come from lists the server also checks
  // against: a typed department put the patient on nobody's board, and a typed
  // doctor id failed as a raw error after the whole form was filled.
  const [options, setOptions] = useState<AdmissionOptions | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  // Beds and admissions (adt-5): with the module on (flags.adt) AND the server
  // sending bed setup's beds, the ward and bed come from the bed picker, the
  // admission time may be set back within the server's window, and the send
  // carries one Idempotency-Key per form so a retry never admits twice. With
  // the module off nothing below changes the form.
  const moduleFlags = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags);
  const master: AdmissionMaster | null = moduleFlags.adt ? (options?.master ?? null) : null;
  const [bedChoice, setBedChoice] = useState<BedChoice>(EMPTY_BED_CHOICE);
  const [admittedAt, setAdmittedAt] = useState('');
  const [admitKey] = useState(newIdempotencyKey);
  const [refusal, setRefusal] = useState<{ code: string; message: string } | null>(null);
  // Existing-patient mode (opd-17a): the record's identity and the clinic's
  // observations and medicines come from the server (read-only, for
  // reference); the clinician types only what the patient STATES (OPD-S1).
  const existingVisitId = existing?.visitId ?? null;
  const [preview, setPreview] = useState<ConversionPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [stated, setStated] = useState<StatedIdentityInput>(EMPTY_STATED_IDENTITY);
  // Only a visit the patient can be admitted from carries their record; a
  // refused one is the visit and the refusal alone, nothing about the patient.
  const record = preview && preview.admittable ? preview : null;
  const refusedVisit = preview && !preview.admittable ? preview : null;
  // opd-17c: a discharged patient whose clinic visit admits them onto their
  // record. The server's words for what that does, or null.
  const visitReadmissionNote = record ? previewReadmissionNote(record) : null;
  // Readmission onto the same record (opd-17c, server switch 'readmission').
  // A new admission the patient index refuses may list records that can be
  // readmitted; choosing one turns this form into a readmission of that
  // record, with its own Idempotency-Key and the same stated-identity check.
  const [readmitCandidates, setReadmitCandidates] = useState<ReadmitCandidate[]>([]);
  const [readmitTarget, setReadmitTarget] = useState<ReadmitCandidate | null>(null);
  const [readmitKey, setReadmitKey] = useState(newIdempotencyKey);
  const readmitting = !existing && readmitTarget !== null;

  // Focus-trap for the modal. Escape closes the active surface: on the credential
  // hand-off screen that means finishing (onSuccess), otherwise cancelling. While
  // a one-time password is on screen only "Done" finishes: it can never be shown
  // again, so a stray key (or backdrop click) must not throw it away.
  // useFocusTrap always calls the latest handler (an effect event inside it),
  // so this closure is passed as it is rather than through a ref.
  const modalRef = useFocusTrap<HTMLDivElement>(() => {
    if (!credential) onClose();
    else if (!credential.password) onSuccess();
  });

  // Form State
  const [formData, setFormData] = useState({
    // Identity
    name: '',
    age: '',
    // Optional (an admission never waits on paperwork), but it is the second
    // factor on the patient's discharge link, so without it no link can open.
    date_of_birth: '',
    sex: '',
    phone: '',
    emergency_contact_name: '',
    emergency_contact_phone: '',
    address: '',
    
    // Admission
    admission_source: 'er',
    admission_type: 'inpatient',
    department: user?.department || '',
    ward: '',
    bed: '',
    attending_doctor_id: user?.id || '',
    referring_clinician: '',
    
    // Clinical
    chief_complaint: '',
    initial_diagnosis: '',
    triage_severity: '',
    
    // History (Arrays)
    comorbidities: [] as string[],
    allergies: [] as string[],
    current_medications: [] as string[],
    past_medical_history: '',
    past_surgical_history: '',
    family_history: '',
    social_history: '',
    
    // Vitals
    hr: '',
    bp_sys: '',
    bp_dia: '',
    spo2: '',
    temperature: '',
    rr: '',
    // '' is "not assessed", sent as null. Both used to start at Alert and room
    // air, so a form nobody touched recorded an assessment nobody made, NEWS2
    // scored it as a complete set, and a confused patient on oxygen could be
    // admitted 5 points low (ruled 2026-10-03, "fix and build everything").
    consciousness: '',
    on_oxygen: '',

    // Notes
    admitting_notes: '',
    nursing_notes: '',
    isolation_precautions: [] as string[],
    code_status: 'full_code',
  });

  const handleChange = (field: string, value: string) => {
    setFormData(prev => ({ ...prev, [field]: value }));
    // A boarder acknowledgement names the treating department, so a new
    // department needs it read again.
    if (field === 'department') setBedChoice(prev => ({ ...prev, boarderAcknowledged: false }));
  };

  const loadOptions = useCallback(async () => {
    try {
      const opts = await apiFetch<AdmissionOptions>('/admissions/options');
      setOptions(opts);
      setOptionsError(null);
      // The defaults (the clinician's own department and id) are kept only when
      // the lists hold them, so the form never starts on a value it would refuse.
      setFormData(prev => ({
        ...prev,
        department: opts.departments.find(d => sameLabel(d, prev.department)) ?? opts.default_department ?? '',
        attending_doctor_id: opts.doctors.some(d => d.id === prev.attending_doctor_id) ? prev.attending_doctor_id : '',
      }));
    } catch {
      setOptionsError('Could not load the departments and doctors to choose from.');
    }
  }, []);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the option lists arriving from the server, not state derived from props or state.
  useEffect(() => { void loadOptions(); }, [loadOptions]);

  // Existing-patient mode: what the record holds for this visit. The form
  // starts from the consultation's complaint and diagnosis, which stay
  // editable; nothing about who the patient is can be typed.
  useEffect(() => {
    if (!existingVisitId) return;
    let alive = true;
    fetchConversionPreview(existingVisitId)
      .then(p => {
        if (!alive) return;
        setPreview(p);
        setPreviewError(null);
        const carried = p.admittable ? p.carried : null;
        setFormData(prev => ({
          ...prev,
          admission_source: 'clinic',
          chief_complaint: prev.chief_complaint || carried?.chief_complaint || '',
          initial_diagnosis: prev.initial_diagnosis || carried?.initial_diagnosis || '',
        }));
      })
      .catch(err => {
        if (!alive) return;
        setPreviewError((err instanceof Error && err.message) || 'Could not load this patient\u2019s record.');
      });
    return () => { alive = false; };
  }, [existingVisitId]);

  // A date of birth fills Age when Age is still empty; an Age already typed is
  // left alone and any disagreement is shown, never silently overwritten.
  const handleDobChange = (value: string) => {
    setFormData(prev => {
      const derived = value ? ageFromDob(value) : null;
      return {
        ...prev,
        date_of_birth: value,
        age: prev.age === '' && derived !== null ? String(derived) : prev.age,
      };
    });
  };

  const handleChipAdd = (field: 'comorbidities' | 'allergies' | 'current_medications' | 'isolation_precautions', value: string) => {
    if (!value.trim()) return;
    setFormData(prev => ({
      ...prev,
      [field]: [...new Set([...prev[field], value.trim()])]
    }));
  };

  const handleChipRemove = (field: 'comorbidities' | 'allergies' | 'current_medications' | 'isolation_precautions', value: string) => {
    setFormData(prev => ({
      ...prev,
      [field]: prev[field].filter(item => item !== value)
    }));
  };

  // Validation
  // Mirrors the server's registration name rule (server/services/
  // patient_name_rule.py): a name needs at least two letters, in any script.
  // The server enforces it with a 400; this keeps the submit button honest
  // and says why before the round trip.
  const nameLetterCount = Array.from(formData.name).filter(ch => /\p{L}/u.test(ch)).length;
  const nameTooShort = formData.name.trim() !== '' && nameLetterCount < 2;
  // A date of birth is optional, but one that is typed must be a real past
  // date: the server silently drops anything else, and the clinician would
  // only find out at the discharge desk.
  const today = localDateKey();
  const dobAge = formData.date_of_birth ? ageFromDob(formData.date_of_birth, today) : null;
  const dobInvalid = formData.date_of_birth !== '' && dobAge === null;
  const ageMismatch = dobAge !== null && formData.age !== '' && Number(formData.age) !== dobAge;
  const ageInvalid = formData.age !== '' && !ageInRange(formData.age);
  const statedIdentity = existing || readmitting ? statedIdentityPayload(stated) : null;
  const isIdentityValid = existing
    ? !!record && statedIdentity !== null
    : readmitting
      ? statedIdentity !== null
      : formData.name.trim() !== '' && !nameTooShort && formData.age !== '' && !ageInvalid && formData.sex !== '' && !dobInvalid;
  const departmentListed = !!options?.departments.includes(formData.department);
  const attending = options?.doctors.find(d => d.id === formData.attending_doctor_id);
  // With the bed picker the place is valid when the picker has everything the
  // server will ask for; without it, as before, when a ward is typed.
  const bedMissing = master ? bedChoiceMissing(master, bedChoice, formData.department) : null;
  const admittedAtError = master ? admittedAtProblem(admittedAt, master.backtime_hours) : null;
  const placeValid = master ? !bedMissing && !admittedAtError : formData.ward.trim() !== '';
  const isAdmissionValid = formData.admission_source !== '' && formData.admission_type !== '' && !!attending && departmentListed && placeValid;
  // Wards in use in the chosen department first; any ward when none is known.
  // Suggestions only: a ward or bed not listed is still accepted.
  const departmentWards = (options?.wards ?? []).filter(w => w.departments.includes(formData.department));
  const wardChoices = departmentWards.length ? departmentWards : (options?.wards ?? []);
  const bedChoices = (options?.beds ?? []).filter(b => sameLabel(b.ward, formData.ward));
  // Warned, not blocked: whether a held bed may be doubled up is the founder's
  // call, and the server admits with the same warning.
  const bedHeld = formData.ward.trim() !== '' && formData.bed.trim() !== ''
    && bedChoices.some(b => b.in_use && sameLabel(b.bed, formData.bed));
  const isClinicalValid = formData.chief_complaint.trim() !== '';

  const isFormValid = isIdentityValid && isAdmissionValid && isClinicalValid;

  // Admission happens ONLY from the Review step's Confirm button. The form's
  // own submit (Enter in a field) is swallowed, so no keypress on an earlier
  // step can admit a patient the clinician has not reviewed.
  const handleSubmit = async () => {
    if (!isFormValid || activeTab !== 'review') return;

    setIsSubmitting(true);
    setError(null);

    try {
      // Build API payload
      const payload = {
        ...formData,
        // parseInt(...) || undefined dropped an age of 0, i.e. every neonate.
        age: Number.isFinite(parseInt(formData.age)) ? parseInt(formData.age) : undefined,
        date_of_birth: formData.date_of_birth || undefined,
        triage_severity: parseInt(formData.triage_severity) || undefined,
        vitals: (formData.hr || formData.bp_sys || formData.spo2) ? {
          hr: parseFloat(formData.hr) || undefined,
          bp_sys: parseFloat(formData.bp_sys) || undefined,
          bp_dia: parseFloat(formData.bp_dia) || undefined,
          spo2: parseFloat(formData.spo2) || undefined,
          temperature: parseFloat(formData.temperature) || undefined,
          rr: parseFloat(formData.rr) || undefined,
          // null, never a guess: the server stores NULL and NEWS2 reports the
          // set as incomplete. "" would be stored as on air (bool("") is False).
          consciousness: formData.consciousness || null,
          on_oxygen: formData.on_oxygen === 'yes' ? true : formData.on_oxygen === 'no' ? false : null,
        } : undefined
      };

      let result: AdmissionAnswer;
      if (existing) {
        // Existing-patient mode (opd-17a): no identity field and no
        // observations are sent (the record's own are kept, and the ward
        // charts a fresh set), only what the patient stated for the check,
        // the admission's fields and this form's one Idempotency-Key.
        setRefusal(null);
        const body = {
          opd_visit_id: existing.visitId,
          identity_check: statedIdentity,
          no_signed_note_reason: existing.noSignedNoteReason || undefined,
          admission_source: formData.admission_source,
          admission_type: formData.admission_type,
          department: formData.department,
          ward: formData.ward,
          bed: formData.bed,
          attending_doctor_id: formData.attending_doctor_id,
          referring_clinician: formData.referring_clinician || undefined,
          chief_complaint: formData.chief_complaint,
          initial_diagnosis: formData.initial_diagnosis || undefined,
          triage_severity: parseInt(formData.triage_severity) || undefined,
          comorbidities: formData.comorbidities,
          allergies: formData.allergies,
          ...(wardRecord ? {} : { current_medications: formData.current_medications }),
          past_medical_history: formData.past_medical_history || undefined,
          past_surgical_history: formData.past_surgical_history || undefined,
          admitting_notes: formData.admitting_notes || undefined,
          nursing_notes: formData.nursing_notes || undefined,
          isolation_precautions: formData.isolation_precautions,
          code_status: formData.code_status,
          ...(master ? bedChoicePayload(master, bedChoice) : {}),
          ...(master && admittedAt ? { admitted_at: new Date(admittedAt).toISOString() } : {}),
        };
        if (master && bedChoice.terminalAuthoriserPin) {
          setBedChoice(prev => ({ ...prev, terminalAuthoriserPin: '' }));
        }
        result = await admitExistingPatient(body, admitKey);
      } else if (readmitting && readmitTarget) {
        // Readmission onto the same record (opd-17c): the record's identity
        // is kept, so none is sent, only what the patient stated for the
        // check; no observations (the ward charts a fresh set); this
        // readmission's one Idempotency-Key.
        setRefusal(null);
        const body = {
          patient_id: readmitTarget.patient_id,
          identity_check: statedIdentity,
          admission_source: formData.admission_source,
          admission_type: formData.admission_type,
          department: formData.department,
          ward: formData.ward,
          bed: formData.bed,
          attending_doctor_id: formData.attending_doctor_id,
          referring_clinician: formData.referring_clinician || undefined,
          chief_complaint: formData.chief_complaint,
          initial_diagnosis: formData.initial_diagnosis || undefined,
          triage_severity: parseInt(formData.triage_severity) || undefined,
          comorbidities: formData.comorbidities,
          allergies: formData.allergies,
          ...(wardRecord ? {} : { current_medications: formData.current_medications }),
          past_medical_history: formData.past_medical_history || undefined,
          past_surgical_history: formData.past_surgical_history || undefined,
          admitting_notes: formData.admitting_notes || undefined,
          nursing_notes: formData.nursing_notes || undefined,
          isolation_precautions: formData.isolation_precautions,
          code_status: formData.code_status,
          ...(master ? bedChoicePayload(master, bedChoice) : {}),
          ...(master && admittedAt ? { admitted_at: new Date(admittedAt).toISOString() } : {}),
        };
        if (master && bedChoice.terminalAuthoriserPin) {
          setBedChoice(prev => ({ ...prev, terminalAuthoriserPin: '' }));
        }
        result = await readmitPatient(body, readmitKey);
      } else if (master) {
        // Beds and admissions on: the place from the picker, the admission
        // time when set back, and this form's one Idempotency-Key.
        setRefusal(null);
        const body = JSON.stringify({
          ...payload,
          ...bedChoicePayload(master, bedChoice),
          ...(admittedAt ? { admitted_at: new Date(admittedAt).toISOString() } : {}),
        });
        // A signing PIN typed by the person authorising a bed awaiting a
        // terminal clean is sent once and never kept: whatever the answer,
        // the next send needs it typed again.
        if (bedChoice.terminalAuthoriserPin) {
          setBedChoice(prev => ({ ...prev, terminalAuthoriserPin: '' }));
        }
        result = await apiFetch<AdmissionAnswer>('/admissions/admit', {
          method: 'POST',
          headers: { 'Idempotency-Key': admitKey },
          body,
        });
      } else {
        result = await admitPatient(payload);
      }
      // Surface the patient's one-time Bridge credential before finishing, so the
      // clinician can hand it over. It is never retrievable again. A missing date
      // of birth also stops here: said now, while someone can still ask the
      // patient, rather than at discharge.
      const dobRecorded = result?.date_of_birth_recorded !== false;
      const bedWarning: string | null = result?.bed_warning || null;
      const locationWarning: string | null = result?.location_warning || null;
      const replayed = result?.replayed === true;
      // A readmission always stops here: the earlier stay's medicines it
      // stopped are listed for the prescriber (ruling OPD-S8 (a)).
      const readmission = result?.readmission === true ? result : null;
      if (result?.temporary_password || !dobRecorded || bedWarning || locationWarning || replayed || readmission) {
        setCredential({
          login: result?.patient_login_id ?? null,
          password: result?.temporary_password ?? null,
          dobRecorded,
          bedWarning,
          locationWarning,
          replayed,
          readmission,
        });
        setIsSubmitting(false);
      } else {
        onSuccess();
      }
    } catch (err) {
      const message = (err instanceof Error ? err.message : '') || 'Failed to admit patient';
      const code = (err as { detail?: { error?: unknown } } | null)?.detail?.error;
      if (!existing && !readmitting) {
        // The patient index refused a new chart: offer the records the server
        // says may be readmitted (empty while readmission is switched off).
        setReadmitCandidates(readmitCandidatesOf((err as { detail?: unknown } | null)?.detail));
      }
      if (master && typeof code === 'string' && BED_REFUSAL_CODES.has(code)) {
        // The bed changed under the form (taken, retired, a clean raised) or
        // the server wants words the picker did not have: show its words by
        // the picker and read the beds again.
        setRefusal({ code, message });
        void loadOptions();
      }
      setError(message);
      setIsSubmitting(false);
    }
  };

  // One-time credential hand-off screen (shown after a successful admission).
  // Same backdrop > dialog element pair as the form, so React keeps the node the
  // focus trap is bound to. The backdrop is inert here: a click outside used to
  // finish, which discarded the password for good. Only "Done" leaves.
  if (credential) {
    return (
      <div className="admit-modal-backdrop">
        <div className="admit-modal admit-modal--credential glass-card" ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="admit-credential-title">
          <div className="admit-modal__header">
            <div className="admit-modal__title-group">
              <h2 className="admit-modal__title" id="admit-credential-title">{credential.readmission ? 'Patient readmitted ✓' : 'Patient admitted ✓'}</h2>
              {credential.password && (
                <span className="admit-modal__subtitle">
                  {credential.readmission?.portal_password_reissued
                    ? 'Give the patient their new one-time Bridge portal password now. The one from their earlier stay no longer works, and this one cannot be shown again.'
                    : 'Give the patient their one-time Bridge portal login now. It cannot be shown again.'}
                </span>
              )}
            </div>
          </div>
          <div className="admit-modal__body admit-modal__body--stack">
            {credential.readmission && <StoppedMedicinesNote result={credential.readmission} />}
            {credential.bedWarning && (
              <p
                role="note"
                style={{
                  margin: '0 0 16px', padding: 12, borderRadius: 8, fontSize: '0.875rem',
                  color: 'var(--status-warn-text)',
                  background: 'color-mix(in srgb, var(--status-warn) 10%, transparent)',
                  border: '1px solid color-mix(in srgb, var(--status-warn) 40%, transparent)',
                }}
              >
                {credential.bedWarning}
              </p>
            )}
            {credential.replayed && (
              <p role="note" className="bed-picker__note" style={{ marginBottom: 16 }}>
                This admission was already recorded when the form was first sent, so it was not
                recorded twice. The one-time password was shown then and cannot be shown again.
              </p>
            )}
            {credential.locationWarning && (
              <p role="note" className="bed-picker__note" style={{ marginBottom: 16 }}>
                {credential.locationWarning}
              </p>
            )}
            {!credential.dobRecorded && (
              <p
                role="note"
                style={{
                  margin: '0 0 16px', padding: 12, borderRadius: 8, fontSize: '0.875rem',
                  color: 'var(--status-warn-text)',
                  background: 'color-mix(in srgb, var(--status-warn) 10%, transparent)',
                  border: '1px solid color-mix(in srgb, var(--status-warn) 40%, transparent)',
                }}
              >
                No date of birth was recorded. The patient's secure discharge link is unlocked with it,
                so no discharge link can be issued until a date of birth is on the record.
              </p>
            )}
            {credential.password && (
              <div className="glass-card" style={{ padding: 16, display: 'grid', gap: 12 }}>
                <div>
                  <div style={{ fontSize: '0.8em', color: 'var(--text-tertiary)' }}>Login ID</div>
                  <div style={{ fontFamily: 'monospace', fontSize: '1.1em', userSelect: 'all' }}>{credential.login}</div>
                </div>
                <div>
                  <div style={{ fontSize: '0.8em', color: 'var(--text-tertiary)' }}>One-time password</div>
                  <div style={{ fontFamily: 'monospace', fontSize: '1.1em', userSelect: 'all' }}>{credential.password}</div>
                </div>
                <button
                  type="button"
                  className="admit-btn"
                  onClick={() => navigator.clipboard?.writeText(`Login: ${credential.login}\nPassword: ${credential.password}`)}
                >
                  Copy credentials
                </button>
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
              <button type="button" className="admit-btn admit-btn--primary" onClick={() => onSuccess()}>
                Done
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="admit-modal-backdrop" onClick={onClose}>
      <div className="admit-modal glass-card" ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="admit-modal-title" onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="admit-modal__header">
          <div className="admit-modal__title-group">
            <h2 className="admit-modal__title" id="admit-modal-title">{existing ? 'Admit from outpatients' : readmitting ? 'Readmit patient' : 'Admit Patient'}</h2>
            <span className="admit-modal__subtitle">{existing || readmitting ? 'Onto the patient\u2019s own record, which keeps its hospital number' : 'Create clinical admission record across Orb modules'}</span>
          </div>
          <button className="admit-modal__close" onClick={onClose} aria-label="Close">&times;</button>
        </div>

        <div className="admit-modal__body">
          
          {/* Main Content Area */}
          <div className="admit-modal__content">
            {/* Tabs */}
            <div className="admit-modal__tabs">
              {[
                { id: 'identity', label: 'Identity', valid: isIdentityValid },
                { id: 'admission', label: 'Admission', valid: isAdmissionValid },
                { id: 'clinical', label: 'Clinical', valid: isClinicalValid },
                { id: 'history', label: 'History', valid: true },
                { id: 'vitals', label: existing || readmitting ? 'Observations' : 'Vitals', valid: true },
                { id: 'notes', label: 'Notes', valid: true },
                { id: 'review', label: 'Review', valid: isFormValid }
              ].map(tab => (
                <button
                  key={tab.id}
                  type="button"
                  className={`admit-modal__tab ${activeTab === tab.id ? 'admit-modal__tab--active' : ''}`}
                  onClick={() => setActiveTab(tab.id as Tab)}
                >
                  {tab.label}
                  {tab.id !== 'review' && (
                    <span className={`admit-modal__tab-indicator ${tab.valid ? 'admit-modal__tab-indicator--complete' : ''}`} />
                  )}
                </button>
              ))}
            </div>

            {/* Scrollable Form Area */}
            <div className="admit-modal__scroll">
              <form id="admit-form" onSubmit={e => e.preventDefault()}>
                
                {activeTab === 'identity' && existing && (
                  refusedVisit ? (
                    <RefusedVisitPanel visit={refusedVisit.visit} refusal={refusedVisit.refusal} />
                  ) : record ? (
                    <>
                      {visitReadmissionNote !== null && <ReadmissionNote note={visitReadmissionNote} />}
                      <ExistingIdentityPanel preview={record} stated={stated} onChange={setStated} />
                    </>
                  ) : (
                    <p role={previewError ? 'alert' : 'status'} className={previewError ? 'admit-modal__error' : undefined}>
                      {previewError ?? 'Loading the patient\u2019s record\u2026'}
                    </p>
                  )
                )}

                {activeTab === 'identity' && readmitting && readmitTarget && (
                  <ReadmitIdentityPanel
                    candidate={readmitTarget}
                    stated={stated}
                    onChange={setStated}
                    onCancel={() => { setReadmitTarget(null); setStated(EMPTY_STATED_IDENTITY); setError(null); }}
                  />
                )}

                {activeTab === 'identity' && !existing && !readmitting && (
                  <div className="admit-modal__section">
                    <div className="admit-modal__grid">
                      <div className="admit-form-group" style={{ gridColumn: '1 / -1' }}>
                        <label>Full Name <span>*</span></label>
                        <input type="text" className="admit-input" value={formData.name} onChange={e => handleChange('name', e.target.value)} required data-autofocus />
                        {nameTooShort && <span className="admit-modal__error">Patient name must contain at least two letters.</span>}
                      </div>
                      <div className="admit-form-group">
                        <label htmlFor="admit-dob">Date of Birth</label>
                        <input id="admit-dob" type="date" className="admit-input" value={formData.date_of_birth} max={today} onChange={e => handleDobChange(e.target.value)} />
                        {dobInvalid
                          ? <span className="admit-modal__error">Date of birth must be a real date, not in the future.</span>
                          : <span style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>Needed to issue the patient's discharge link.</span>}
                      </div>
                      <div className="admit-form-group">
                        <label>Age <span>*</span></label>
                        <input type="number" className="admit-input" min={0} max={130} step={1} value={formData.age} onChange={e => handleChange('age', e.target.value)} required />
                        {ageInvalid
                          ? <span className="admit-modal__error">Age must be a whole number from 0 to 130.</span>
                          : ageMismatch && <span className="admit-modal__error">The date of birth gives an age of {dobAge}. Check which is right.</span>}
                      </div>
                      <div className="admit-form-group">
                        <label>Sex <span>*</span></label>
                        <select className="admit-input" value={formData.sex} onChange={e => handleChange('sex', e.target.value)} required>
                          <option value="">Select...</option>
                          <option value="M">Male</option>
                          <option value="F">Female</option>
                          <option value="O">Other</option>
                        </select>
                      </div>
                      <div className="admit-form-group">
                        <label>Phone</label>
                        <input type="tel" className="admit-input" value={formData.phone} onChange={e => handleChange('phone', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Address</label>
                        <input type="text" className="admit-input" value={formData.address} onChange={e => handleChange('address', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Emergency Contact Name</label>
                        <input type="text" className="admit-input" value={formData.emergency_contact_name} onChange={e => handleChange('emergency_contact_name', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Emergency Contact Phone</label>
                        <input type="tel" className="admit-input" value={formData.emergency_contact_phone} onChange={e => handleChange('emergency_contact_phone', e.target.value)} />
                      </div>
                    </div>
                  </div>
                )}

                {activeTab === 'admission' && (
                  <div className="admit-modal__section">
                    <div className="admit-modal__grid">
                      <div className="admit-form-group">
                        <label>Admission Source <span>*</span></label>
                        <select className="admit-input" value={formData.admission_source} onChange={e => handleChange('admission_source', e.target.value)} required>
                          <option value="er">Emergency Room</option>
                          <option value="clinic">Outpatient Clinic</option>
                          <option value="transfer">Hospital Transfer</option>
                          <option value="direct">Direct Admission</option>
                        </select>
                      </div>
                      <div className="admit-form-group">
                        <label>Admission Type <span>*</span></label>
                        <select className="admit-input" value={formData.admission_type} onChange={e => handleChange('admission_type', e.target.value)} required>
                          <option value="inpatient">Inpatient</option>
                          <option value="observation">Observation</option>
                          <option value="icu">ICU</option>
                          <option value="emergency">Emergency</option>
                        </select>
                      </div>
                      {optionsError && (
                        <p role="alert" className="admit-modal__error" style={{ gridColumn: '1 / -1', margin: 0 }}>
                          {optionsError}{' '}
                          <button type="button" className="admit-btn" onClick={() => { void loadOptions(); }}>Try again</button>
                        </p>
                      )}
                      <div className="admit-form-group">
                        <label htmlFor="admit-department">Department <span>*</span></label>
                        <select id="admit-department" className="admit-input" value={formData.department} onChange={e => handleChange('department', e.target.value)} required>
                          <option value="">{options ? 'Select...' : 'Loading...'}</option>
                          {(options?.departments ?? []).map(d => <option key={d} value={d}>{d}</option>)}
                        </select>
                      </div>
                      <div className="admit-form-group">
                        <label htmlFor="admit-attending">Attending Doctor <span>*</span></label>
                        <select id="admit-attending" className="admit-input" value={formData.attending_doctor_id} onChange={e => handleChange('attending_doctor_id', e.target.value)} required>
                          <option value="">{options ? 'Select...' : 'Loading...'}</option>
                          {(options?.doctors ?? []).map(d => (
                            <option key={d.id} value={d.id}>{d.department ? `${d.name} (${d.department})` : d.name}</option>
                          ))}
                        </select>
                      </div>
                      {master ? (
                        <>
                          <BedPicker
                            master={master}
                            department={formData.department}
                            value={bedChoice}
                            onChange={next => { setBedChoice(next); setRefusal(null); }}
                            refusal={refusal}
                          />
                          {bedMissing && (
                            <span role="status" style={{ gridColumn: '1 / -1', fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
                              {bedMissing}
                            </span>
                          )}
                          <div className="admit-form-group">
                            <label htmlFor="admit-admitted-at">Admitted at</label>
                            <input id="admit-admitted-at" type="datetime-local" className="admit-input" value={admittedAt} onChange={e => setAdmittedAt(e.target.value)} />
                            {admittedAtError
                              ? <span className="admit-modal__error">{admittedAtError}</span>
                              : <span style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>Leave blank for now. Up to {master.backtime_hours} hours back.</span>}
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="admit-form-group">
                            <label htmlFor="admit-ward">Ward <span>*</span></label>
                            <input id="admit-ward" type="text" className="admit-input" list="admit-ward-options" autoComplete="off" value={formData.ward} onChange={e => handleChange('ward', e.target.value)} required />
                            <datalist id="admit-ward-options">
                              {wardChoices.map(w => <option key={w.ward} value={w.ward} />)}
                            </datalist>
                          </div>
                          <div className="admit-form-group">
                            <label htmlFor="admit-bed">Bed</label>
                            <input id="admit-bed" type="text" className="admit-input" list="admit-bed-options" autoComplete="off" value={formData.bed} onChange={e => handleChange('bed', e.target.value)} />
                            <datalist id="admit-bed-options">
                              {bedChoices.map(b => <option key={b.bed} value={b.bed} label={b.in_use ? 'In use' : undefined} />)}
                            </datalist>
                            {bedHeld && (
                              <span role="status" style={{ fontSize: '0.8125rem', color: 'var(--status-warn-text)' }}>
                                This bed is already held by an admitted patient. Check it with the ward before admitting.
                              </span>
                            )}
                          </div>
                        </>
                      )}
                      <div className="admit-form-group" style={{ gridColumn: '1 / -1' }}>
                        <label>Referring Clinician</label>
                        <input type="text" className="admit-input" value={formData.referring_clinician} onChange={e => handleChange('referring_clinician', e.target.value)} />
                      </div>
                    </div>
                  </div>
                )}

                {activeTab === 'clinical' && (
                  <div className="admit-modal__section">
                    <div className="admit-modal__grid admit-modal__grid--full">
                      <div className="admit-form-group">
                        <label>Chief Complaint <span>*</span></label>
                        <textarea className="admit-input" value={formData.chief_complaint} onChange={e => handleChange('chief_complaint', e.target.value)} required placeholder="Primary reason for admission..." />
                      </div>
                      <div className="admit-form-group">
                        <label>Initial Diagnosis</label>
                        <input type="text" className="admit-input" value={formData.initial_diagnosis} onChange={e => handleChange('initial_diagnosis', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Triage Severity (1-5)</label>
                        <select className="admit-input" value={formData.triage_severity} onChange={e => handleChange('triage_severity', e.target.value)}>
                          <option value="">Select...</option>
                          <option value="1">1 - Resuscitation</option>
                          <option value="2">2 - Emergent</option>
                          <option value="3">3 - Urgent</option>
                          <option value="4">4 - Less Urgent</option>
                          <option value="5">5 - Non Urgent</option>
                        </select>
                      </div>
                    </div>
                  </div>
                )}

                {activeTab === 'history' && (
                  <div className="admit-modal__section">
                    <div className="admit-modal__grid admit-modal__grid--full">
                      {existing && record && <ChartedAllergiesNote preview={record} />}
                      {existing && record && !wardRecord && <OpdMedicinesReference preview={record} />}
                      <div className="admit-form-group">
                        <label>{existing || readmitting ? 'Add allergies' : 'Allergies'}</label>
                        <ChipInput chips={formData.allergies} placeholder="Type allergy and press Enter..." onAdd={v => handleChipAdd('allergies', v)} onRemove={v => handleChipRemove('allergies', v)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Comorbidities</label>
                        <ChipInput chips={formData.comorbidities} placeholder="Type comorbidity and press Enter..." onAdd={v => handleChipAdd('comorbidities', v)} onRemove={v => handleChipRemove('comorbidities', v)} />
                      </div>
                      {!wardRecord && (
                        <div className="admit-form-group">
                          <label>Current Medications</label>
                          <ChipInput chips={formData.current_medications} placeholder="Type medication and press Enter..." onAdd={v => handleChipAdd('current_medications', v)} onRemove={v => handleChipRemove('current_medications', v)} />
                        </div>
                      )}
                      <div className="admit-form-group">
                        <label>Past Medical History</label>
                        <textarea className="admit-input" value={formData.past_medical_history} onChange={e => handleChange('past_medical_history', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Past Surgical History</label>
                        <textarea className="admit-input" value={formData.past_surgical_history} onChange={e => handleChange('past_surgical_history', e.target.value)} />
                      </div>
                    </div>
                  </div>
                )}

                {activeTab === 'vitals' && existing && (
                  refusedVisit
                    ? <ConversionRefusalNote refusal={refusedVisit.refusal} />
                    : record
                      ? <OpdObservationsReference preview={record} />
                      : <p role="status">{'Loading the patient\u2019s record\u2026'}</p>
                )}

                {activeTab === 'vitals' && readmitting && (
                  <div className="admit-modal__section">
                    <p className="admit-existing__hint" role="note">
                      No observations are sent with a readmission. The earlier stay&rsquo;s
                      observations are not current: the ward charts a fresh set when the patient
                      arrives.
                    </p>
                  </div>
                )}

                {activeTab === 'vitals' && !existing && !readmitting && (
                  <div className="admit-modal__section">
                    <p style={{ color: 'var(--text-secondary)', fontSize: '0.875rem', marginBottom: 16 }}>Entering initial vitals will immediately compute risk scores and display the patient in Vigil.</p>
                    <div className="admit-modal__grid">
                      <div className="admit-form-group">
                        <label>Heart Rate (bpm)</label>
                        <input type="number" className="admit-input" value={formData.hr} onChange={e => handleChange('hr', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>SpO2 (%)</label>
                        <input type="number" className="admit-input" value={formData.spo2} onChange={e => handleChange('spo2', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Systolic BP</label>
                        <input type="number" className="admit-input" value={formData.bp_sys} onChange={e => handleChange('bp_sys', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Diastolic BP</label>
                        <input type="number" className="admit-input" value={formData.bp_dia} onChange={e => handleChange('bp_dia', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Temperature (°C)</label>
                        <input type="number" step="0.1" className="admit-input" value={formData.temperature} onChange={e => handleChange('temperature', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Resp Rate</label>
                        <input type="number" className="admit-input" value={formData.rr} onChange={e => handleChange('rr', e.target.value)} />
                      </div>
                      <div className="admit-form-group">
                        <label htmlFor="admit-acvpu">Consciousness (ACVPU)</label>
                        <select id="admit-acvpu" className="admit-input" value={formData.consciousness} onChange={e => handleChange('consciousness', e.target.value)}>
                          <option value="">Not assessed</option>
                          <option value="alert">Alert</option>
                          <option value="confusion">New confusion</option>
                          <option value="voice">Responds to voice</option>
                          <option value="pain">Responds to pain</option>
                          <option value="unresponsive">Unresponsive</option>
                        </select>
                      </div>
                      <div className="admit-form-group">
                        <label htmlFor="admit-oxygen">On supplemental O₂</label>
                        <select id="admit-oxygen" className="admit-input" value={formData.on_oxygen} onChange={e => handleChange('on_oxygen', e.target.value)}>
                          <option value="">Not assessed</option>
                          <option value="no">No (room air)</option>
                          <option value="yes">Yes</option>
                        </select>
                      </div>
                    </div>
                  </div>
                )}

                {activeTab === 'notes' && (
                  <div className="admit-modal__section">
                    <div className="admit-modal__grid admit-modal__grid--full">
                      <div className="admit-form-group">
                        <label>Code Status</label>
                        <select className="admit-input" value={formData.code_status} onChange={e => handleChange('code_status', e.target.value)}>
                          <option value="full_code">Full Code</option>
                          <option value="dnr">DNR (Do Not Resuscitate)</option>
                          <option value="dni">DNI (Do Not Intubate)</option>
                          <option value="comfort">Comfort Care Only</option>
                        </select>
                      </div>
                      <div className="admit-form-group">
                        <label>Isolation Precautions</label>
                        <ChipInput chips={formData.isolation_precautions} placeholder="Type precaution (e.g. Contact, Droplet) and press Enter..." onAdd={v => handleChipAdd('isolation_precautions', v)} onRemove={v => handleChipRemove('isolation_precautions', v)} />
                      </div>
                      <div className="admit-form-group">
                        <label>Admitting Notes</label>
                        <textarea className="admit-input" value={formData.admitting_notes} onChange={e => handleChange('admitting_notes', e.target.value)} style={{ minHeight: 120 }} />
                      </div>
                    </div>
                  </div>
                )}

                {activeTab === 'review' && (
                  <div className="admit-modal__section">
                    {!existing && !readmitting && (
                      <ReadmitOffer
                        candidates={readmitCandidates}
                        onChoose={c => {
                          setReadmitTarget(c);
                          setStated(EMPTY_STATED_IDENTITY);
                          setReadmitKey(newIdempotencyKey());
                          setError(null);
                          setActiveTab('identity');
                        }}
                      />
                    )}
                    <div className="admit-review-grid">
                      {readmitting && readmitTarget ? (
                        <div className="admit-review-section">
                          <h3>Patient on file</h3>
                          <div className="admit-review-list">
                            <div className="admit-review-item"><span>Name</span><span>{readmitTarget.name}</span></div>
                            <div className="admit-review-item"><span>UHID</span><span>{readmitTarget.uhid ?? '-'}</span></div>
                            <div className="admit-review-item"><span>Identity</span><span>{statedIdentity ? 'Checked against the record when you admit' : 'Not confirmed yet'}</span></div>
                            <div className="admit-review-item"><span>Readmission</span><span>Earlier medicines are stopped and listed for you</span></div>
                          </div>
                        </div>
                      ) : existing ? (
                        <div className="admit-review-section">
                          <h3>Patient on file</h3>
                          <div className="admit-review-list">
                            <div className="admit-review-item"><span>Name</span><span>{record?.patient.name ?? '-'}</span></div>
                            <div className="admit-review-item"><span>UHID</span><span>{record?.patient.uhid ?? '-'}</span></div>
                            <div className="admit-review-item"><span>Age/Sex</span><span>{record?.patient.age != null ? `${record.patient.age}y` : '-'} {record?.patient.sex ?? ''}</span></div>
                            <div className="admit-review-item"><span>Identity</span><span>{statedIdentity ? 'Checked against the record when you admit' : 'Not confirmed yet'}</span></div>
                            <div className="admit-review-item"><span>From</span><span>{preview ? `${preview.visit.department} clinic, token ${preview.visit.token_number}` : '-'}</span></div>
                          </div>
                        </div>
                      ) : (
                      <div className="admit-review-section">
                        <h3>Patient Identity</h3>
                        <div className="admit-review-list">
                          <div className="admit-review-item"><span>Name</span><span>{formData.name || '-'}</span></div>
                          <div className="admit-review-item"><span>Age/Sex</span><span>{formData.age ? `${formData.age}y` : '-'} {formData.sex}</span></div>
                          <div className="admit-review-item"><span>Date of Birth</span><span>{formData.date_of_birth && !dobInvalid ? formatClinicalDate(new Date(`${formData.date_of_birth}T00:00:00`)) : 'Not recorded (no discharge link)'}</span></div>
                          <div className="admit-review-item"><span>Contact</span><span>{formData.phone || '-'}</span></div>
                        </div>
                      </div>
                      )}
                      <div className="admit-review-section">
                        <h3>Admission Context</h3>
                        <div className="admit-review-list">
                          <div className="admit-review-item"><span>Type</span><span>{formData.admission_type.toUpperCase()} via {formData.admission_source.toUpperCase()}</span></div>
                          <div className="admit-review-item"><span>Location</span><span>{master ? `${formData.department} - ${bedChoiceSummary(master, bedChoice)}` : `${formData.department} - ${formData.ward} ${formData.bed}`}</span></div>
                          {master && admittedAt && !admittedAtError && (
                            <div className="admit-review-item"><span>Admitted at</span><span>{formatClinicalDateTime(new Date(admittedAt))}</span></div>
                          )}
                          {!master && bedHeld && (
                            <div className="admit-review-item"><span>Bed</span><span style={{ color: 'var(--status-warn-text)' }}>Already held by an admitted patient</span></div>
                          )}
                          <div className="admit-review-item"><span>Attending</span><span>{attending?.name || '-'}</span></div>
                        </div>
                      </div>
                      <div className="admit-review-section" style={{ gridColumn: '1 / -1' }}>
                        <h3>Clinical Overview</h3>
                        <div className="admit-review-list">
                          <div className="admit-review-item" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
                            <span>Chief Complaint</span>
                            <span style={{ textAlign: 'left', fontWeight: 400 }}>{formData.chief_complaint || '-'}</span>
                          </div>
                          <div className="admit-review-item"><span>Initial Diagnosis</span><span>{formData.initial_diagnosis || '-'}</span></div>
                          <div className="admit-review-item"><span>Triage</span><span>{formData.triage_severity ? `Level ${formData.triage_severity}` : '-'}</span></div>
                          <div className="admit-review-item"><span>Code Status</span><span>{formData.code_status.replace('_', ' ').toUpperCase()}</span></div>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

              </form>
            </div>
          </div>

          {/* Side Summary Panel */}
          <div className="admit-modal__summary">
            <div className="admit-summary-group">
              <h4>Identity</h4>
              <div className="admit-summary-item">
                {existing ? (
                  <>
                    <span className="admit-summary-value">{record?.patient.name ?? <span className="admit-summary-value--empty">{refusedVisit ? 'Cannot be admitted from this visit' : 'Loading the record...'}</span>}</span>
                    <span className="admit-summary-label">{record?.patient.age != null ? `${record.patient.age}y` : ''} {record?.patient.sex ?? ''}</span>
                  </>
                ) : readmitting && readmitTarget ? (
                  <>
                    <span className="admit-summary-value">{readmitTarget.name}</span>
                    <span className="admit-summary-label">{readmitTarget.age != null ? `${readmitTarget.age}y` : ''} {readmitTarget.sex ?? ''}</span>
                  </>
                ) : (
                  <>
                    <span className="admit-summary-value">{formData.name || <span className="admit-summary-value--empty">Pending Name...</span>}</span>
                    <span className="admit-summary-label">{formData.age ? `${formData.age}y` : ''} {formData.sex}</span>
                  </>
                )}
              </div>
            </div>
            
            <div className="admit-summary-group">
              <h4>Location</h4>
              <div className="admit-summary-item">
                {master ? (
                  <>
                    <span className="admit-summary-value">{bedChoice.wardId ? bedChoiceSummary(master, bedChoice) : <span className="admit-summary-value--empty">Pending Ward...</span>}</span>
                    <span className="admit-summary-label">{formData.department}</span>
                  </>
                ) : (
                  <>
                    <span className="admit-summary-value">{formData.ward || <span className="admit-summary-value--empty">Pending Ward...</span>}</span>
                    <span className="admit-summary-label">{formData.department} {formData.bed ? `· Bed ${formData.bed}` : ''}</span>
                  </>
                )}
              </div>
            </div>

            <div className="admit-summary-group">
              <h4>Clinical Focus</h4>
              <div className="admit-summary-item">
                <span className="admit-summary-value">{formData.initial_diagnosis || <span className="admit-summary-value--empty">Pending Dx...</span>}</span>
                <span className="admit-summary-label">{formData.chief_complaint ? `CC: ${formData.chief_complaint.substring(0, 40)}...` : 'No complaint entered'}</span>
              </div>
            </div>

            {(formData.allergies.length > 0 || formData.comorbidities.length > 0) && (
              <div className="admit-summary-group">
                <h4>Flags</h4>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  {formData.allergies.map(a => <span key={a} style={{ fontSize: '0.65rem', background: 'color-mix(in srgb, var(--status-danger) 18%, transparent)', color: 'var(--status-danger-text)', padding: '2px 6px', borderRadius: 4 }}>{a}</span>)}
                  {formData.comorbidities.map(c => <span key={c} style={{ fontSize: '0.65rem', background: 'color-mix(in srgb, var(--status-warn) 18%, transparent)', color: 'var(--status-warn-text)', padding: '2px 6px', borderRadius: 4 }}>{c}</span>)}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="admit-modal__footer">
          {error && <span className="admit-modal__error">{error}</span>}
          
          <button type="button" className="admit-btn admit-btn--cancel" onClick={onClose} disabled={isSubmitting}>
            Cancel
          </button>
          
          {/* Distinct keys, and Confirm is NOT a submit button. Without keys
              React reused one <button> for both and flipped it to
              type="submit" mid-click, so "Review Admission" admitted the
              patient and the Review step never appeared. */}
          {activeTab !== 'review' ? (
            <button key="review" type="button" className="admit-btn admit-btn--submit" onClick={() => setActiveTab('review')} disabled={!isFormValid}>
              Review Admission
            </button>
          ) : (
            <button key="confirm" type="button" className="admit-btn admit-btn--submit" onClick={() => { void handleSubmit(); }} disabled={!isFormValid || isSubmitting}>
              {isSubmitting ? 'Admitting...' : 'Confirm Admission'}
            </button>
          )}
        </div>

      </div>
    </div>
  );
}
