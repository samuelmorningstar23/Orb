/**
 * Collect: the laboratory's phlebotomy list (lis-8).
 *
 * The same bedside steps as the ward's Samples view (../nurse/SamplesSheet):
 * check two identifiers, print the labels, mark each tube collected. Here the
 * list covers every ward, grouped by ward, because the phlebotomist and the
 * laboratory roles are hospital-wide (ruling LIS-R12). The server decides the
 * scope from the signed-in account; this view never asks for one.
 *
 * Collecting starts new work, so the view is left out while the module drains
 * (ruling LIS-R21); the server refuses its calls then anyway.
 */
import SamplesSheet from '../nurse/SamplesSheet'

export const viewId = 'collect'
export const viewLabel = 'Collect'
export const viewOrder = 10
export const viewPermission = 'lab.collect'
export const viewWhenDraining = 'hide'

// The panel's props (LabViewProps) are not needed: the sheet reads its own
// list and says in words when the module refuses new work.
export default function CollectView() {
  return <SamplesSheet variant="laboratory" />
}
