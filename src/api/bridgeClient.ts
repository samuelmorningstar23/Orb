/**
 * Orb Bridge — Patient-portal API helpers.
 *
 * Self-contained (mirrors the origin + orb_token pattern in client.ts) so the
 * new patient-facing features can be added without expanding the large shared
 * client. All endpoints are self-scoped server-side to the logged-in patient.
 */

const API_BASE = `${window.location.origin}/api`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('orb_token');
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers: { ...authHeaders(), ...(init?.headers || {}) } });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try { detail = (await res.json())?.detail || detail; } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export interface CareTeamMember { name: string; role: string; department: string | null }
export interface CareTeamResponse {
  care_team: CareTeamMember[];
  unit: { department: string | null; ward: string | null; bed: string | null };
}
export function getCareTeam() {
  return req<CareTeamResponse>('/bridge/care-team');
}

/** One vital as the patient sees it: the value as charted, when it was
 *  measured and its recent series. No status and no normal range (ruled
 *  2026-10-03, see server/routes/bridge_routes.py _PORTAL_VITALS). */
export interface VitalTrend {
  label: string;
  unit: string;
  current: number | null;
  measured_at: string | null;
  series: number[];
}
export interface VitalsTrendsResponse {
  trends: Record<string, VitalTrend>;
  count: number;
}
export function getVitalsTrends() {
  return req<VitalsTrendsResponse>('/bridge/vitals-trends');
}

export interface Appointment {
  id: string;
  requested_date: string;
  requested_time: string | null;
  department: string | null;
  reason: string | null;
  status: string;
  created_at: string;
}
export function getAppointments() {
  return req<{ appointments: Appointment[] }>('/bridge/appointments');
}

export interface AppointmentRequest {
  requested_date: string;
  requested_time?: string;
  department?: string;
  reason?: string;
}
export function requestAppointment(body: AppointmentRequest) {
  return req<{ id: string; status: string; message: string }>('/bridge/appointments', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}
export function cancelAppointment(id: string) {
  return req<{ id: string; status: string }>(`/bridge/appointments/${id}`, { method: 'DELETE' });
}
