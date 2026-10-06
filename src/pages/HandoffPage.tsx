import { useEffect } from 'react'
import { useParams } from 'react-router-dom'
import IpassHandoffView from '../components/handoff/IpassHandoffView'
import NotInWardRecord from '../components/NotInWardRecord'
import { refreshModuleFlags, useClinicalSwitches } from '../shellConfig'

/** Route wrapper: renders the auto-drafted I-PASS handoff for a patient. The
 *  view is the whole page here, so it names the browser tab.
 *
 *  Opened in a tab of its own, so it asks /api/health for the clinical
 *  switches itself: under the Orb Ward Record profile the handover is
 *  switched off (every I-PASS route answers 404), and this page says so in
 *  plain words instead. */
export default function HandoffPage() {
  const { patientId } = useParams<{ patientId: string }>()
  const { wardRecord } = useClinicalSwitches()
  useEffect(() => { void refreshModuleFlags() }, [])
  if (wardRecord) return <NotInWardRecord screen="The handover" />
  if (!patientId) return <div style={{ padding: 24 }}>No patient specified.</div>
  return <IpassHandoffView patientId={patientId} setPageTitle />
}
