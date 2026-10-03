/**
 * Scribe Quick Admission: thin wrappers over /api/scribe/admission-options and
 * /api/scribe/quick-admit.
 *
 * The quick admission goes through the server's admit_patient transaction (an
 * admissions row, so the patient is on the department's ward board), and the
 * attending doctor is picked from the staff table, never from a list compiled
 * into the bundle. The department is picked from the covered departments the
 * server lists, since the server refuses any other (a typed one put the patient
 * on nobody's ward board).
 */
import { apiFetch } from './client'

export interface AttendingDoctor {
  id: string
  name: string
  department: string | null
}

export interface QuickAdmitOptions {
  doctors: AttendingDoctor[]
  /** Departments an active doctor or nurse covers, in their canonical spelling. */
  departments: string[]
  /** The caller's department when it is on that list, else null. */
  default_department: string | null
}

export interface QuickAdmitRequest {
  name: string
  age: number | null
  sex: string
  department: string
  ward: string
  bed: string
  attending_doctor_id: string
  admission_source: string
  chief_complaint: string
  allergies: string
}

export interface QuickAdmitResult {
  success: boolean
  patient_id: string
  admission_id: string
  mrn: string
  patient_login_id: string
  temporary_password: string
  date_of_birth_recorded: boolean
}

export function getQuickAdmitOptions(): Promise<QuickAdmitOptions> {
  return apiFetch<QuickAdmitOptions>('/scribe/admission-options')
}

export function quickAdmit(body: QuickAdmitRequest): Promise<QuickAdmitResult> {
  return apiFetch<QuickAdmitResult>('/scribe/quick-admit', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
