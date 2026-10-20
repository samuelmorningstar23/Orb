/**
 * The front desk's group identity match (multisite-17).
 *
 * A satellite desk sees only its own facility's patients. When a patient says
 * they were seen at another facility of the group, the desk searches the whole
 * group by four things the patient states (name, age or date of birth, sex and
 * phone) instead of registering them a second time, which would split their
 * allergy list. The server answers who each match is and nothing clinical, and
 * a ticket that lets this desk register their visit here. Every search is
 * recorded with the desk's reason and limited per hour.
 *
 * Offered only while the server reports ORB_MULTI_SITE on and more than one
 * facility on record (shellConfig's platform flags); with one facility every
 * patient is already in the desk's own list.
 */
import { apiFetch } from './client'

/** What the patient states. Exactly one of age or date of birth is needed. */
export interface GroupMatchQuery {
  name: string
  ageYears?: number | null
  dateOfBirth?: string | null
  /** 'F', 'M' or 'O'. */
  sex: string
  phone: string
  /** Why the desk is searching the whole group (recorded with the search). */
  reason: string
}

export interface GroupMatch {
  id: string
  name: string
  age: number | null
  sex: 'F' | 'M' | 'O' | null
  /** The group record number (UHID), one per person across the group. */
  group_mrn: string | null
  registered_facility: { id: number | null; code: string | null }
  /** Lets THIS account register a visit for this patient here, for a while. */
  ticket: string
}

export interface GroupMatchResult {
  matches: GroupMatch[]
  truncated: boolean
  searched_at: string
  searches_left: number
  ticket_seconds: number
}

export function searchGroupIdentity(query: GroupMatchQuery): Promise<GroupMatchResult> {
  return apiFetch<GroupMatchResult>('/frontdesk/group-match', {
    method: 'POST',
    body: JSON.stringify({
      name: query.name,
      age_years: query.ageYears ?? null,
      date_of_birth: query.dateOfBirth || null,
      sex: query.sex,
      phone: query.phone,
      reason: query.reason,
    }),
  })
}

export interface MatchedVisitInput {
  patientId: string
  ticket: string
  department: string
  doctorId?: string
  /** What the patient stated, for the identity check the server runs while
   *  the registration feature is on (ignored with it off). */
  stated: { name: string; ageYears?: number | null; dateOfBirth?: string | null; phone: string }
  idempotencyKey: string
}

export interface MatchedVisit {
  id: string
  patient_id: string
  department: string
  token_number: number
}

/** The last four digits of a phone number, or undefined when it has fewer. */
export function phoneLast4(phone: string): string | undefined {
  const digits = phone.replace(/\D/g, '')
  return digits.length >= 4 ? digits.slice(-4) : undefined
}

export function registerMatchedVisit(input: MatchedVisitInput): Promise<MatchedVisit> {
  return apiFetch<MatchedVisit>('/frontdesk/opd/visits', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      department: input.department,
      doctor_id: input.doctorId,
      visit_type: 'new',
      idempotency_key: input.idempotencyKey,
      group_match_ticket: input.ticket,
      identity_check: {
        name: input.stated.name,
        age_years: input.stated.dateOfBirth ? null : (input.stated.ageYears ?? null),
        date_of_birth: input.stated.dateOfBirth || null,
        phone_last4: phoneLast4(input.stated.phone) ?? null,
      },
    }),
  })
}
