import { useState, useRef, useEffect } from 'react'
import { analyzeLensImage, getPatientsList, assignLensToPatient } from '../../api/client'
import ProvenanceChip from '../../components/ProvenanceChip'
import InvestigationalBanner from '../../components/InvestigationalBanner'
import { withoutAppendedDisclaimer } from '../../lib/lensDisclaimer'
import { useClinicalSwitches } from '../../shellConfig'
import './LensPanel.css'

/** Shown before any upload while ORB_LENS_ENABLED is off (Orb Ward Record). */
export const LENS_OFF_NOTICE =
  'Lens is switched off on this computer. It is not a certified diagnostic device.'

/* ═══════════════════════════════════════════════════
   LENS PANEL — Medical Image Analysis with Patient Linking
   ═══════════════════════════════════════════════════ */

export default function LensPanel() {
  // ORB_LENS_ENABLED, published on /api/health. Off: the tab stays, the notice
  // says why, and the analyse control is disabled (the server refuses anyway).
  const { lensEnabled } = useClinicalSwitches()
  const [imageFile, setImageFile] = useState<File | null>(null)
  const [imagePreview, setImagePreview] = useState<string | null>(null)
  const [context, setContext] = useState('')
  const [imageType, setImageType] = useState('xray')
  const [analysis, setAnalysis] = useState('')
  const [analyzing, setAnalyzing] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // Track the current object URL so we can revoke it when the image changes or
  // the component unmounts (URL.createObjectURL leaks until revoked).
  const objectUrlRef = useRef<string | null>(null)

  // Patient assignment state
  const [showAssign, setShowAssign] = useState(false)
  const [patients, setPatients] = useState<{ id: string; name: string; bed: string }[]>([])
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedPatient, setSelectedPatient] = useState<{ id: string; name: string; bed: string } | null>(null)
  const [assigning, setAssigning] = useState(false)
  const [assignResult, setAssignResult] = useState<string | null>(null)
  // Lens is a clinician REFERENCE tool: the AI read is a draft the doctor reviews,
  // edits, and confirms before it enters the record (non-diagnostic / human-in-the-loop).
  const [reviewedText, setReviewedText] = useState('')
  const [clinicianReviewed, setClinicianReviewed] = useState(false)
  // Concrete id of the analysis row created by the most recent analyze call, so
  // handleAssign files to THAT exact analysis instead of guessing the newest one.
  const [analysisId, setAnalysisId] = useState<number | null>(null)

  const handleFile = (file: File) => {
    // Revoke any object URL from a previous image before replacing it.
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    const url = URL.createObjectURL(file)
    objectUrlRef.current = url
    setImageFile(file)
    setImagePreview(url)
    setAnalysis('')
    setReviewedText('')
    setClinicianReviewed(false)
    setShowAssign(false)
    setAssignResult(null)
    setAnalysisId(null)
  }

  // Revoke the current object URL on unmount so it does not leak.
  useEffect(() => {
    return () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    }
  }, [])

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file?.type.startsWith('image/')) handleFile(file)
  }

  const handleAnalyze = async () => {
    if (!imageFile || !lensEnabled) return
    setAnalyzing(true); setAnalysis(''); setReviewedText(''); setClinicianReviewed(false); setAnalysisId(null)
    try {
      const { analysisId: newId } = await analyzeLensImage(imageFile, { clinical_context: context, image_type: imageType }, (partial) => {
        setAnalysis(partial)
        // The server's appended banner stays out of the editable box: the
        // label is fixed text above, and the assign route adds it to the filing.
        setReviewedText(withoutAppendedDisclaimer(partial))
      })
      setAnalysisId(newId)
      setShowAssign(true)
      // Load patients for the assignment dropdown
      const pts = await getPatientsList()
      setPatients(pts)
    } catch (err) { setAnalysis(`Analysis failed: ${(err instanceof Error ? err.message : '') || 'Ensure Ollama is running with medgemma:27b.'}`) }
    finally { setAnalyzing(false) }
  }

  const handleAssign = async () => {
    if (!selectedPatient || !clinicianReviewed) return
    setAssigning(true)
    try {
      // File to the EXACT analysis this session created. Falling back to the
      // global-newest lookup only if the id is somehow missing — that fallback
      // can race a concurrent analysis by another clinician, so it is last-resort.
      let targetId = analysisId
      if (!targetId) {
        const { getLensAnalyses } = await import('../../api/client')
        const analyses = await getLensAnalyses()
        targetId = analyses?.[0]?.id ?? null
      }
      if (!targetId) throw new Error('No analysis found')

      const result = await assignLensToPatient(targetId, selectedPatient.id, reviewedText)
      setAssignResult(`✓ Report added to ${result.patient_name}'s Vigil record`)
      setShowAssign(false)
    } catch (e) {
      setAssignResult(`✗ Assignment failed: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setAssigning(false)
    }
  }

  const filteredPatients = patients.filter(p =>
    p.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
    p.bed.toLowerCase().includes(searchTerm.toLowerCase())
  )

  return (
    <div className="orb-panel animate-fade-in">
      <div className="lens-container">
        {/* Fixed, before any upload and whatever the model returns. */}
        <InvestigationalBanner detail="Image reads come from a local AI model that has not been clinically validated. They are not a radiologist read." />
        {!lensEnabled && (
          <p className="lens-off-notice" role="note">{LENS_OFF_NOTICE}</p>
        )}
        {!imagePreview ? (
          <div
            className={`lens-dropzone glass-card ${dragOver ? 'lens-dropzone--over' : ''}`}
            onDragOver={e => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
          >
            <svg className="lens-dropzone__icon" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
              <circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>
            </svg>
            <h3 className="lens-dropzone__title">Upload Medical Image</h3>
            <p className="lens-dropzone__hint">Drag and drop a JPG or PNG image, or use the button below.</p>
            {/* The dropzone div's onClick is a mouse affordance only. The file
                input used to carry `hidden`, which removes it from the tab order
                AND the accessibility tree, so this — the only entry point to the
                whole Lens feature — had no keyboard path at all. A real button
                now opens the picker, and the input is visually hidden but
                focusable rather than `hidden`. */}
            <button
              type="button"
              className="lens-dropzone__browse"
              onClick={e => { e.stopPropagation(); fileInputRef.current?.click() }}
            >
              Browse files
            </button>
            <input
              ref={fileInputRef}
              id="lens-file-input"
              className="sr-only"
              type="file"
              accept="image/*"
              aria-label="Choose a medical image to analyse"
              onChange={e => e.target.files?.[0] && handleFile(e.target.files[0])}
            />
          </div>
        ) : (
          <div className="lens-workspace">
            <div className="lens-image-area">
              <img src={imagePreview} alt="Medical image" className="lens-preview" />
              <button className="lens-clear" onClick={() => {
                if (objectUrlRef.current) { URL.revokeObjectURL(objectUrlRef.current); objectUrlRef.current = null }
                setImageFile(null); setImagePreview(null); setAnalysis(''); setShowAssign(false); setAssignResult(null); setAnalysisId(null)
              }}>
                ✕ Clear
              </button>
            </div>

            <div className="lens-controls">
              <select className="lens-type-select" value={imageType} onChange={e => setImageType(e.target.value)}>
                <option value="xray">X-Ray</option>
                <option value="ecg">ECG</option>
                <option value="ct">CT Scan</option>
                <option value="mri">MRI</option>
                <option value="photo">Clinical Photo</option>
                <option value="other">Other</option>
              </select>
              <input className="lens-context-input" type="text" placeholder="Clinical context (optional)…"
                value={context} onChange={e => setContext(e.target.value)} />
              <button
                className="lens-analyze-btn"
                onClick={handleAnalyze}
                disabled={analyzing || !lensEnabled}
                title={lensEnabled ? undefined : LENS_OFF_NOTICE}
              >
                {analyzing ? 'Analyzing…' : 'Analyze Image'}
              </button>
            </div>

            {analysis && (
              <div className="lens-analysis glass-card">
                <h3 className="lens-analysis__title">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                  AI Reference Read
                  <span className="lens-ref-badge">DRAFT · CLINICIAN REVIEW REQUIRED</span>
                  <ProvenanceChip modelKey="sage_llm" compact />
                </h3>
                <p className="lens-disclaimer">
                  Decision-support reference for a clinician, <strong>not a diagnosis</strong> and not a
                  radiologist read. Review and edit the findings below, then confirm before they enter the record.
                </p>
                <textarea
                  className="lens-analysis__edit"
                  value={reviewedText}
                  onChange={e => setReviewedText(e.target.value)}
                  spellCheck={false}
                  aria-label="Editable AI reference findings"
                />
              </div>
            )}

            {/* Patient Assignment Form */}
            {showAssign && !assignResult && (
              <div className="lens-assign glass-card" style={{ marginTop: 16, padding: 20 }}>
                <h3 style={{ fontSize: '0.9rem', fontWeight: 600, marginBottom: 12, color: 'var(--accent-gold)' }}>
                  📋 Assign to Patient Record
                </h3>
                <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 12 }}>
                  Link this analysis to a patient's Vigil record so it appears in their diagnostic reports.
                </p>

                <input
                  type="text"
                  placeholder="Search patient name or bed…"
                  value={searchTerm}
                  onChange={e => { setSearchTerm(e.target.value); setSelectedPatient(null) }}
                  style={{
                    width: '100%', padding: '10px 14px', marginBottom: 8,
                    background: 'var(--bg-glass)', border: '1px solid var(--border-glass)',
                    borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)',
                    fontSize: '0.85rem', fontFamily: 'var(--font-family)',
                  }}
                />

                {searchTerm && !selectedPatient && (
                  <div style={{
                    maxHeight: 160, overflowY: 'auto', marginBottom: 8,
                    border: '1px solid var(--border-glass)', borderRadius: 'var(--radius-sm)',
                    background: 'var(--bg-secondary)',
                  }}>
                    {filteredPatients.map(p => (
                      <button
                        key={p.id}
                        onClick={() => { setSelectedPatient(p); setSearchTerm(p.name) }}
                        style={{
                          display: 'flex', justifyContent: 'space-between', width: '100%',
                          padding: '8px 12px', background: 'none', border: 'none',
                          color: 'var(--text-primary)', fontSize: '0.8rem', cursor: 'pointer',
                          textAlign: 'left', borderBottom: '1px solid var(--border-subtle)',
                          fontFamily: 'var(--font-family)',
                        }}
                        onMouseEnter={e => (e.currentTarget.style.background = 'var(--accent-glow)')}
                        onMouseLeave={e => (e.currentTarget.style.background = 'none')}
                      >
                        <span style={{ fontWeight: 500 }}>{p.name}</span>
                        <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: '0.75rem' }}>{p.bed}</span>
                      </button>
                    ))}
                    {filteredPatients.length === 0 && (
                      <div style={{ padding: 12, color: 'var(--text-muted)', fontSize: '0.8rem' }}>No matching patients</div>
                    )}
                  </div>
                )}

                {selectedPatient && (
                  <div style={{
                    padding: '8px 12px', marginBottom: 8, borderRadius: 'var(--radius-sm)',
                    background: 'var(--accent-glow)', border: '1px solid var(--accent-glow-strong)',
                    fontSize: '0.8rem', color: 'var(--accent-text)',
                  }}>
                    Selected: <strong>{selectedPatient.name}</strong> · Bed {selectedPatient.bed}
                  </div>
                )}

                <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 10, cursor: 'pointer', fontSize: '0.78rem', color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                  <input type="checkbox" checked={clinicianReviewed} onChange={e => setClinicianReviewed(e.target.checked)} style={{ marginTop: 2, flexShrink: 0 }} />
                  <span>I have reviewed this AI reference read, made any necessary edits, and take clinical responsibility for the findings filed to the record.</span>
                </label>

                <div style={{ display: 'flex', gap: 8 }}>
                  <button
                    onClick={handleAssign}
                    disabled={!selectedPatient || !clinicianReviewed || assigning}
                    style={{
                      flex: 1, padding: '10px', borderRadius: 'var(--radius-sm)',
                      /* Disabled and busy both swap to the neutral pair rather
                         than fading: opacity dims label and fill together, so
                         'Filing…' used to render around 2:1. */
                      background: (selectedPatient && clinicianReviewed && !assigning) ? 'var(--accent-deep)' : 'var(--btn-disabled-bg)',
                      color: (selectedPatient && clinicianReviewed && !assigning) ? 'var(--btn-primary-text)' : 'var(--btn-disabled-text)',
                      fontWeight: 600, fontSize: '0.85rem', cursor: (selectedPatient && clinicianReviewed && !assigning) ? 'pointer' : 'not-allowed',
                      border: 'none', fontFamily: 'var(--font-family)',
                    }}
                  >
                    {assigning ? 'Filing…' : 'Confirm & File to Record'}
                  </button>
                  <button
                    onClick={() => setShowAssign(false)}
                    style={{
                      padding: '10px 20px', borderRadius: 'var(--radius-sm)',
                      background: 'var(--bg-glass)', border: '1px solid var(--border-glass)',
                      color: 'var(--text-secondary)', fontSize: '0.85rem', cursor: 'pointer',
                      fontFamily: 'var(--font-family)',
                    }}
                  >
                    Skip
                  </button>
                </div>
              </div>
            )}

            {/* Assignment result */}
            {assignResult && (
              <div style={{
                marginTop: 12, padding: '12px 16px', borderRadius: 'var(--radius-sm)',
                background: assignResult.startsWith('✓') ? 'color-mix(in srgb, var(--status-ok) 10%, transparent)' : 'color-mix(in srgb, var(--status-danger) 10%, transparent)',
                border: `1px solid ${assignResult.startsWith('✓') ? 'color-mix(in srgb, var(--status-ok) 28%, transparent)' : 'color-mix(in srgb, var(--status-danger) 28%, transparent)'}`,
                fontSize: '0.85rem', fontWeight: 500,
                color: assignResult.startsWith('✓') ? 'var(--status-ok-text)' : 'var(--status-danger-text)',
              }}>
                {assignResult}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
