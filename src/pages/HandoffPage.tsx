import { useParams } from 'react-router-dom'
import IpassHandoffView from '../components/handoff/IpassHandoffView'

/** Route wrapper: renders the auto-drafted I-PASS handoff for a patient. The
 *  view is the whole page here, so it names the browser tab. */
export default function HandoffPage() {
  const { patientId } = useParams<{ patientId: string }>()
  if (!patientId) return <div style={{ padding: 24 }}>No patient specified.</div>
  return <IpassHandoffView patientId={patientId} setPageTitle />
}
