/**
 * Laboratory, unmatched results (lis-19): the Reconcile view.
 *
 * Found by LabPanel through its glob; this file names its own tab. Shown only
 * to a holder of ingest.reconcile (laboratory technicians and pathologists,
 * ruling LIS-R17), and kept while the module drains, because a result that
 * matched no patient still needs a decision when new laboratory work has
 * stopped. The server refuses every call the role cannot make whatever this
 * tab strip shows.
 */
import QuarantineQueue from '../../../components/lab/QuarantineQueue'

export const viewId = 'reconcile'
export const viewLabel = 'Unmatched results'
export const viewOrder = 88
export const viewPermission = 'ingest.reconcile'
export const viewWhenDraining = 'show'

/** The panel hands every view LabViewProps (./viewRegistry); this one needs
 *  none of them: the queue answers for itself, under /api/ingest. */
export default function ReconcileView() {
  return <QuarantineQueue mode="reconcile" />
}
