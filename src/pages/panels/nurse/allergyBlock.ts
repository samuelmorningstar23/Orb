import type { MedTask } from '../../../api/nurse'

/**
 * True when the shift board's allergy screen says this dose will be refused at
 * the bedside: a 'block' that did not fail and that no prescriber has already
 * authorised for this order (nurse_shift_service._preview_allergy).
 *
 * Advisory only. The server screens again when Given is pressed, so this can
 * only ever say a dose will stop, never let one through. Kept out of api/nurse
 * so the screens that mock that module still get the real rule.
 */
export function allergyBlocked(task: Pick<MedTask, 'allergy'>): boolean {
  const a = task.allergy
  return !!a && a.decision === 'block' && !a.screen_failed && !a.override
}
