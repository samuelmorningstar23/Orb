import { localDateKey } from '../lib/datetime';

/* The admission form's age rules, kept apart from AdmitPatientModal.tsx so
   that file exports only components (fast refresh). */

/** A whole number of years from 0 to 130, the range the server accepts. */
export function ageInRange(raw: string): boolean {
  return /^\d{1,3}$/.test(raw.trim()) && Number(raw) <= 130;
}

/** Whole years from a YYYY-MM-DD date of birth to `today` (also YYYY-MM-DD), or
 *  null for a date the server would not keep (discharge_access_service.
 *  usable_dob: not a real calendar date, before 1900, or in the future).
 *  Compared as calendar dates so the viewer's timezone cannot shift a birthday
 *  by a day. */
export function ageFromDob(dob: string, today: string = localDateKey()): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dob);
  const t = /^(\d{4})-(\d{2})-(\d{2})$/.exec(today);
  if (!m || !t || dob > today) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const real = new Date(y, mo - 1, d);
  if (y < 1900 || real.getFullYear() !== y || real.getMonth() !== mo - 1 || real.getDate() !== d) return null;
  let age = Number(t[1]) - y;
  if (Number(t[2]) < mo || (Number(t[2]) === mo && Number(t[3]) < d)) age -= 1;
  return age;
}
