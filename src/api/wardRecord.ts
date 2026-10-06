/**
 * Orb Ward Record: each ward's observation interval, and the monthly rounds
 * file for the quality team (server/routes/ward_record_routes.py).
 *
 * The settings calls go through apiFetch. The two downloads use a raw fetch,
 * because the body is a file (CSV or Markdown), not JSON, and the file the
 * user saves must be byte for byte the one whose SHA-256 the server logged.
 */
import { API_BASE, apiFetch, buildHeaders, errorDetailMessage } from './client';

export interface WardSetting {
  ward: string;
  interval_minutes: number;
  grace_minutes: number;
  set_by: string;
  set_at: string;
  note?: string | null;
}

export interface WardSettingRow {
  ward: string;
  setting: WardSetting | null;
}

export interface WardSettingsResponse {
  wards: WardSettingRow[];
  limits: {
    interval_minutes: [number, number];
    grace_minutes: [number, number];
    default_grace_minutes: number;
  };
}

export interface WardSettingInput {
  interval_minutes: number;
  grace_minutes: number;
  set_by: string;
  note?: string | null;
}

export function getWardSettings(): Promise<WardSettingsResponse> {
  return apiFetch<WardSettingsResponse>('/ward-record/settings');
}

export function saveWardSetting(ward: string, body: WardSettingInput): Promise<WardSetting> {
  return apiFetch<WardSetting>(`/ward-record/settings/${encodeURIComponent(ward)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

/** A calendar month, 'YYYY-MM', as its first and last day. */
export function monthRange(month: string): { start: string; end: string } | null {
  const m = /^(\d{4})-(\d{2})$/.exec(month);
  if (!m) return null;
  const year = Number(m[1]);
  const mon = Number(m[2]);
  if (mon < 1 || mon > 12) return null;
  const last = new Date(Date.UTC(year, mon, 0)).getUTCDate();
  return { start: `${m[1]}-${m[2]}-01`, end: `${m[1]}-${m[2]}-${String(last).padStart(2, '0')}` };
}

export interface RoundsFile {
  fileName: string;
  text: string;
  reference: string | null;
  sha256: string | null;
}

export class WardRecordError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = 'WardRecordError';
    this.status = status;
    this.code = code;
  }
}

/** Fetch the rounds file for one ward and period, as text. */
export async function fetchRoundsFile(params: {
  ward: string;
  start: string;
  end: string;
  format: 'csv' | 'md';
  includePatientNumbers?: boolean;
}): Promise<RoundsFile> {
  const query = new URLSearchParams({ ward: params.ward, start: params.start, end: params.end });
  if (params.includePatientNumbers) query.set('include_patient_identifiers', 'true');
  const response = await fetch(`${API_BASE}/ward-record/rounds.${params.format}?${query.toString()}`, {
    method: 'GET',
    credentials: 'include',
    headers: buildHeaders('GET'),
  });
  if (!response.ok) {
    const parsed = await response.json().catch(() => null);
    const detail = parsed && typeof parsed.detail === 'object' ? parsed.detail : null;
    throw new WardRecordError(errorDetailMessage(parsed?.detail, response.status),
                              response.status, detail?.error || `http_${response.status}`);
  }
  const disposition = response.headers.get('Content-Disposition') || '';
  const named = /filename="([^"]+)"/.exec(disposition);
  return {
    fileName: named ? named[1] : `rounds_${params.start}_${params.end}.${params.format}`,
    text: await response.text(),
    reference: response.headers.get('X-Ward-Record-Reference'),
    sha256: response.headers.get('X-Content-SHA256'),
  };
}

/** Hand the browser a file the server already produced. */
export function saveTextFile(fileName: string, text: string, type: string): void {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
