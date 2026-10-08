/**
 * Orb, Laboratory analyser registry API (lis-23). Every call goes through
 * ./index, so a refusal arrives as a LabApiError with the server's code.
 *
 * WHO DOES WHAT. Registering an analyser, changing its details, switching it
 * on or off and issuing its key need lab.interface (the administrator). Which
 * Orb test each analyser code is needs lab.master (the pathologist). Every
 * laboratory role reads the list.
 *
 * THE KEY IS SHOWN ONCE. issueAnalyserKey answers with the new key, and no
 * other call ever returns it: Orb stores only a scrambled form that cannot be
 * turned back into the key. A caller shows it, lets the person copy it, and
 * then forgets it. It is never put in storage, a URL or a log.
 */
import { labGet, labPatch, labPost, labPut } from './index'

export type AnalyserProtocol = 'hl7v2' | 'astm'
export type AnalyserTransport = 'http' | 'mllp_listen' | 'tcp_client' | 'tcp_listen'

/** Whether a key has been issued, and when and by whom. Never the key. */
export interface AnalyserKeyState {
  issued: boolean
  issued_at: string | null
  issued_by: string | null
  issued_by_name: string | null
}

export interface AnalyserCalibration {
  last_done_date: string | null
  next_due_date: string | null
  overdue: boolean
}

/** The analyser's entry in the biomedical register, when it is linked. */
export interface AnalyserBiomedical {
  id: number
  found: boolean
  asset_tag?: string
  name?: string
  status?: string
  in_register?: boolean
  calibration?: AnalyserCalibration | null
}

/** One analyser code and the Orb test it is. */
export interface AnalyserMapping {
  instrument_id: string
  instrument_code: string
  test_id: string
  test_name: string | null
  test_short_name: string | null
  test_unit: string | null
  test_result_type: string | null
  test_active: boolean
  test_performed_at: string | null
  test_found: boolean
  factor_milli: number
  /** The factor as text, for example "1" or "0.1". */
  factor: string
  instrument_unit: string | null
  active: boolean
  note: string
  approved_by: string
  approved_by_name: string | null
  approved_at: string
  retired_by: string | null
  retired_by_name: string | null
  retired_at: string | null
  retire_reason: string | null
  version: number
}

export interface Analyser {
  id: string
  name: string
  make: string | null
  model: string | null
  serial: string | null
  section_id: string | null
  section_name: string | null
  protocol: AnalyserProtocol
  protocol_label: string
  transport: AnalyserTransport
  transport_label: string
  /** False for a protocol or connection this version of Orb cannot receive. */
  supported: boolean
  host: string | null
  port: number | null
  assumed_timezone: string | null
  biomedical_asset_id: number | null
  biomedical: AnalyserBiomedical | null
  enabled: boolean
  status_reason: string | null
  last_message_at: string | null
  key: AnalyserKeyState
  mapped_codes: number
  version: number
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
  updated_by_name: string | null
  /** Present on a single analyser's read. */
  map?: AnalyserMapping[]
}

export interface AnalyserChoice {
  value: string
  label: string
  supported: boolean
}

export interface AnalyserOptions {
  protocols: AnalyserChoice[]
  transports: AnalyserChoice[]
  hospital_timezone: string
  listener: { enabled: boolean; port: number }
  /** False until Orb can receive results from analysers at all (lis-24). */
  http_intake_built: boolean
}

export interface AnalyserList {
  analysers: Analyser[]
  options: AnalyserOptions
}

export interface AnalyserRegistration {
  id: string
  name: string
  make?: string | null
  model?: string | null
  serial?: string | null
  section_id?: string | null
  protocol?: AnalyserProtocol
  transport?: AnalyserTransport
  host?: string | null
  port?: number | null
  assumed_timezone?: string | null
  biomedical_asset_id?: number | null
}

export type AnalyserChanges = Partial<Omit<AnalyserRegistration, 'id'>> & { version?: number }

/** The one answer that carries the key. Show it once, then let it go. */
export interface IssuedAnalyserKey {
  instrument: Analyser
  key: string
  issued_at: string
  replaced: boolean
}

export interface AnalyserMappingBody {
  instrument_code: string
  test_id: string
  /** As the person typed it, for example "1" or "0.1". */
  factor?: string
  instrument_unit?: string | null
  note: string
}

function analyserPath(id: string, rest = ''): string {
  return `/analysers/${encodeURIComponent(id)}${rest}`
}

export function listAnalysers(): Promise<AnalyserList> {
  return labGet('/analysers')
}

export function getAnalyser(id: string): Promise<Analyser> {
  return labGet(analyserPath(id))
}

export function registerAnalyser(body: AnalyserRegistration): Promise<Analyser> {
  return labPost('/analysers', body)
}

export function changeAnalyser(id: string, changes: AnalyserChanges): Promise<Analyser> {
  return labPatch(analyserPath(id), changes)
}

export function setAnalyserSwitchedOn(id: string, enabled: boolean, reason: string): Promise<Analyser> {
  return labPost(analyserPath(id, '/status'), { enabled, reason })
}

export function issueAnalyserKey(id: string, reason?: string): Promise<IssuedAnalyserKey> {
  return labPost(analyserPath(id, '/key'), reason ? { reason } : {})
}

export function saveAnalyserMapping(id: string, body: AnalyserMappingBody): Promise<AnalyserMapping> {
  return labPut(analyserPath(id, '/map'), body)
}

export function retireAnalyserMapping(id: string, instrumentCode: string, reason: string):
  Promise<AnalyserMapping> {
  return labPost(analyserPath(id, '/map/retire'), { instrument_code: instrumentCode, reason })
}

// ── Words for the screen ─────────────────────────────────────────────────────

/** 2 to 32 capitals, digits, '-' or '_' (the server's rule, after capitals). */
export const ANALYSER_ID_PATTERN = /^[A-Z0-9][A-Z0-9_-]{1,31}$/

/** A factor exact in thousandths and above zero, as the server requires. */
export function factorProblem(raw: string): string | null {
  const text = raw.trim()
  if (!text) return null
  if (!/^\d+(?:\.\d{1,3})?$/.test(text)) {
    return 'The factor is a number with at most three decimal places.'
  }
  if (Number(text) <= 0) return 'The factor must be above zero.'
  return null
}

/** "Calibration overdue since ...", "Calibration due ...", or why there is
 *  nothing to say. Dates are formatted by the caller. */
export function calibrationState(bio: AnalyserBiomedical | null):
  'not_linked' | 'not_found' | 'no_schedule' | 'overdue' | 'due' {
  if (!bio) return 'not_linked'
  if (!bio.found) return 'not_found'
  if (!bio.calibration || !bio.calibration.next_due_date) return 'no_schedule'
  return bio.calibration.overdue ? 'overdue' : 'due'
}
