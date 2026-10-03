/**
 * Where a medication order placed under Orders or Order sets actually goes.
 *
 * Nowhere a nurse or pharmacist works. These orders are written to the orders
 * list only: they never reach the medication chart, the drug round, the
 * pharmacy queue or billing. The screens used to read as if the drug had been
 * ordered, so every medication line on them carries this sentence until the
 * two paths are joined. A shared module (not an export from either panel) so
 * the lazily loaded Order sets bundle is not pulled into the Orders chunk.
 */
export const MED_NOT_SENT =
  'Not sent to the ward or pharmacy. Prescribe from the patient chart (Medications) so nurses can give it.'

export function isMedicationOrder(orderType: string | null | undefined): boolean {
  return orderType === 'medication'
}
