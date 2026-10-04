import React, { useState } from 'react';
import { useParams } from 'react-router-dom';
import { verifyDischargeAccess, getVerifiedDischargeSummary } from '../api/client';
import { DischargePrintView, type PrintAllergy } from '../components/DischargePrintView';
import { formatClinicalDateTime } from '../lib/datetime';
import './DischargeAccess.css';

/** What /discharge-access/{token}/summary returns. `summary` is null until a
 *  clinician has reviewed the drafted text; `summary_message` says why. */
interface VerifiedSummary {
  summary: string | null;
  summary_status: 'draft' | 'reviewed';
  summary_message?: string | null;
  reviewed_by_name?: string | null;
  reviewed_at?: string | null;
  discharged_at?: string | null;
  timeline: any[];
  patient: any;
  allergies: PrintAllergy[] | null;
  comorbidities?: string[];
  medications: any[];
  unverified_medication_count?: number;
  site_name?: string | null;
}

export default function DischargeAccess() {
  const { token } = useParams<{ token: string }>();
  const [dob, setDob] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<{ message: string; lockedUntil?: string } | null>(null);
  const [summaryData, setSummaryData] = useState<VerifiedSummary | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token) return;
    
    setLoading(true);
    setError(null);
    
    try {
      // 1. Verify DOB
      const verifyRes = await verifyDischargeAccess(token, dob);
      const accessGrant = verifyRes.access_grant;
      
      // 2. Fetch Summary using the short-lived grant
      const summaryRes = await getVerifiedDischargeSummary(token, accessGrant);
      setSummaryData({
        ...summaryRes,
        // Absent is not "none": an allergy list the server did not send is
        // rendered as unreadable, never as "No allergies recorded".
        allergies: Array.isArray(summaryRes.allergies) ? summaryRes.allergies : null,
        medications: Array.isArray(summaryRes.medications) ? summaryRes.medications : [],
        timeline: Array.isArray(summaryRes.timeline) ? summaryRes.timeline : [],
      });
      
    } catch (err: any) {
      const detail = err?.detail || err;
      if (detail.reason === 'invalid_dob') {
        // No attempt count: the server deliberately withholds it, and printing
        // the missing field read "You have undefined attempts remaining".
        setError({ message: 'That date of birth does not match our records. Please check it and try again.' });
      } else if (detail.reason === 'locked') {
        const lockTime = formatClinicalDateTime(detail.locked_until) || 'later';
        setError({ message: `Too many failed attempts. Access is temporarily locked until ${lockTime}.`, lockedUntil: detail.locked_until });
      } else if (detail.reason === 'expired') {
        setError({ message: 'This secure link has expired for your protection. Please contact the hospital for a new link.' });
      } else if (detail.reason === 'revoked') {
        setError({ message: 'This secure link has been revoked. Please contact the hospital.' });
      } else {
        setError({ message: 'We could not verify your identity. Please ensure you used the correct link.' });
      }
    } finally {
      setLoading(false);
    }
  };

  if (summaryData && summaryData.patient && Object.keys(summaryData.patient).length > 0) {
    return (
      <div className="discharge-access-success">
        <div className="patient-portal-header">
          {/* The hospital's configured name, never an invented one. */}
          <div className="brand">{summaryData.site_name || 'Patient Portal'}</div>
          <div className="portal-title">Patient Portal: Discharge Summary</div>
        </div>
        <div className="print-view-wrapper">
          <div className="summary-card" style={{ padding: 0 }}>
            {/* We render the DischargePrintView inside the card. We remove inner card padding so the print view takes full space if it has its own padding, or we keep it. */}
            {/* The old title said "Discharge Summary ✓ Verified". We can add a banner. */}
            <div style={{ padding: '24px', background: 'color-mix(in srgb, var(--status-ok) 15%, transparent)', color: 'var(--text-primary)', borderBottom: '1px solid color-mix(in srgb, var(--status-ok) 30%, transparent)', borderTopLeftRadius: '22px', borderTopRightRadius: '22px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <h2 style={{ margin: 0, fontSize: '18px' }}>Secure Medical Record</h2>
              <span style={{ fontWeight: 600, background: 'color-mix(in srgb, var(--status-ok) 25%, transparent)', color: 'var(--text-primary)', padding: '4px 12px', borderRadius: '4px', fontSize: '14px' }}>✓ Verified Identity</span>
            </div>
            
            <div style={{ padding: '32px' }}>
              {summaryData.summary_status !== 'reviewed' && (
                <p role="status" data-testid="discharge-pending-review" style={{ margin: '0 0 20px', padding: '12px 16px', borderRadius: '8px', border: '1px solid var(--status-info)', color: 'var(--text-primary)', fontWeight: 600 }}>
                  {summaryData.summary_message || 'Your discharge summary is being finalised by your doctor.'}
                </p>
              )}
              {/* Ruling 2026-10-04 (option A): this page only opens on a network
                  routed to the appliance, never from home. So the patient keeps
                  a copy before leaving, and a draft is not worth keeping.
                  Outside .discharge-print-layout, so print.css leaves it off
                  the saved copy. */}
              {summaryData.summary_status === 'reviewed' ? (
                <div className="portal-save" data-testid="discharge-save-copy">
                  <button type="button" className="verify-btn" onClick={() => window.print()}>
                    Save a copy
                  </button>
                  <p>
                    Save it to your phone now, before you leave the hospital. This page only
                    opens on the hospital&apos;s Wi-Fi, so it will not open from home.
                  </p>
                  <p>
                    The button opens your phone&apos;s print screen. On Android, choose Save as
                    PDF. On iPhone, tap Share, then Save to Files.
                  </p>
                </div>
              ) : (
                <div className="portal-save" data-testid="discharge-save-later">
                  <p>
                    Your doctor has not finished this summary yet, so there is nothing final to
                    save. Before you leave, ask the ward staff to finish it, then scan the code
                    again. This page only opens on the hospital&apos;s Wi-Fi.
                  </p>
                </div>
              )}
              <DischargePrintView
                patient={summaryData.patient}
                medications={summaryData.medications}
                allergies={summaryData.allergies}
                comorbidities={summaryData.comorbidities || []}
                summary={summaryData.summary}
                summaryStatus={summaryData.summary_status === 'reviewed' ? 'reviewed' : 'draft'}
                summaryMessage={summaryData.summary_message}
                reviewedByName={summaryData.reviewed_by_name}
                reviewedAt={summaryData.reviewed_at}
                dischargedAt={summaryData.discharged_at}
                siteName={summaryData.site_name}
                unverifiedMedicationCount={summaryData.unverified_medication_count || 0}
                timeline={summaryData.timeline}
              />
            </div>
          </div>
        </div>
      </div>
    );
  }

  // If verify succeeds but payload is genuinely empty (no patient data somehow), show error
  if (summaryData && (!summaryData.patient || Object.keys(summaryData.patient).length === 0)) {
    return (
      <div className="discharge-access-container">
        <div className="verification-card">
          <div className="verification-header">
            <h2>Record Unavailable</h2>
            <p>Your identity was verified, but the clinical record could not be loaded or is empty.</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="discharge-access-container">
      <div className="verification-card">
        <div className="verification-header">
          <h2>Secure Medical Record</h2>
          <p>Please verify your identity to access your discharge summary.</p>
        </div>
        
        <form onSubmit={handleSubmit} className="verification-form">
          <div className="input-group">
            <label htmlFor="dob">Date of Birth</label>
            <input
              type="date"
              id="dob"
              value={dob}
              onChange={(e) => setDob(e.target.value)}
              required
              disabled={loading || !!error?.lockedUntil}
            />
          </div>
          
          {error && (
            <div className="verification-error">
              {error.message}
            </div>
          )}
          
          <button 
            type="submit" 
            className="verify-btn"
            disabled={loading || !dob || !!error?.lockedUntil}
          >
            {loading ? 'Verifying...' : 'Access Records'}
          </button>
        </form>
        
        <div className="security-notice">
          <small>For your privacy, this link is monitored and will expire automatically.</small>
        </div>
      </div>
    </div>
  );
}
