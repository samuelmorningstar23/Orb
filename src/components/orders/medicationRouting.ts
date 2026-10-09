/**
 * Where a medication order placed under Orders or Order sets actually goes.
 *
 * With the medication-order lifecycle off (the default), nowhere a nurse or
 * pharmacist works. These orders are written to the orders list only: they
 * never reach the medication chart, the drug round, the pharmacy queue or
 * billing. The screens used to read as if the drug had been ordered, so every
 * medication line on them carries MED_NOT_SENT.
 *
 * With the lifecycle on (the server's 'med_order_lifecycle' feature,
 * medorders-7), each such order is also a draft medication order that a
 * prescriber signs; the signature puts it on the ward chart and the drug
 * round. It is still not sent to the pharmacy queue (no supply request is
 * raised for it yet). The copy below says exactly that, per state.
 *
 * A shared module (not an export from either panel) so the lazily loaded
 * Order sets bundle is not pulled into the Orders chunk.
 */
import { useSyncExternalStore } from 'react'
import { currentFeatureFlags, subscribeModuleFlags } from '../../shellConfig'
import type { LinkedMedOrder } from '../../api/medorders'

export const MED_NOT_SENT =
  'Not sent to the ward or pharmacy. Prescribe from the patient chart (Medications) so nurses can give it.'

/** The form and the order set's item line, with the lifecycle on. */
export const MED_AFTER_SIGN =
  'Reaches the ward chart and the drug round once a prescriber signs it under Orders. It is not sent to the pharmacy.'

/** Ruling MO-R11 (a): a medicine not on the formulary may be ordered by
 *  name, and says so. */
export const MED_NOT_ON_FORMULARY = 'Not supplied by pharmacy: this medicine is not on the formulary.'

export function isMedicationOrder(orderType: string | null | undefined): boolean {
  return orderType === 'medication'
}

/** Is the server's medication-order lifecycle on? Read live from the health
 *  probe's feature flags, so the copy follows the switch. */
export function useMedicationLifecycle(): boolean {
  const flags = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  return flags.med_order_lifecycle === true
}

/** The routing sentence for a new medication order (the order form and an
 *  order set's medication item). */
export function medicationRoutingNotice(lifecycleOn: boolean): string {
  return lifecycleOn ? MED_AFTER_SIGN : MED_NOT_SENT
}

/** The sentence an order set's apply result shows for its medicines. */
export function orderSetMedicationNotice(count: number, lifecycleOn: boolean): string {
  const one = count === 1
  if (lifecycleOn) {
    return `The ${count} medication ${one ? 'order waits' : 'orders wait'} for a prescriber to sign ` +
      `${one ? 'it' : 'them'} under Orders. Once signed, ${one ? 'it reaches' : 'they reach'} the ` +
      'ward chart and the drug round. They are not sent to the pharmacy.'
  }
  return `The ${count} medication ${one ? 'order is' : 'orders are'} not sent to the ward or ` +
    `pharmacy. Prescribe ${one ? 'it' : 'them'} from the patient chart (Medications) so nurses ` +
    `can give ${one ? 'it' : 'them'}.`
}

/** Where a linked medication order is now, in the ward's words. */
export function linkedOrderLine(order: LinkedMedOrder): string {
  switch (order.status) {
    case 'draft':
      return order.waiting_over_24h
        ? 'Waiting over 24 hours for a prescriber to sign it. Not on the ward chart yet.'
        : 'Waiting for a prescriber to sign it. Not on the ward chart yet.'
    case 'prescribed':
    case 'verified':
      return `Signed${order.prescriber_name ? ` by ${order.prescriber_name}` : ''}. ` +
        'On the ward chart and the drug round. Not sent to the pharmacy.'
    case 'held':
      return 'On hold: not given until it is resumed.'
    case 'stopped':
      return 'Stopped: no longer on the drug round.'
    case 'completed':
      return 'Course complete: no longer on the drug round.'
    case 'cancelled':
      return order.status_reason ? `Cancelled: ${order.status_reason}` : 'Cancelled.'
    case 'rejected':
      return 'Rejected by the pharmacist: not on the drug round.'
    default:
      return 'The state of this medicine could not be read.'
  }
}

/** True when a linked order can be signed now (an unsigned draft). */
export function canBeSigned(order: LinkedMedOrder | null | undefined): boolean {
  return !!order && order.status === 'draft'
}
