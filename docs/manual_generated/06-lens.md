## 6. Lens: Medical Image Analysis

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![Lens. Drag in an X-ray, ECG, CT, or clinical photo; the draft that returns is always for clinician review.](assets/manual/lens-image-analysis.png)

*Lens. Drag in an X-ray, ECG, CT, or clinical photo; the draft that returns is always for clinician review.*

Lens is Orb's medical image analysis workspace. You give it a picture, a chest X-ray, an ECG strip, a CT slice, an MRI image, or a clinical photograph, and Orb's locally hosted vision-capable AI model produces a structured first-pass reading, streamed onto your screen as it is generated. The reading is organised into **Findings**, **Impression**, and **Recommendations**, and it is always, without exception, presented as a *draft reference for a clinician*, never as a diagnosis and never as a substitute for a radiologist's report.

Lens is designed around a strict human-in-the-loop principle: the AI's text is an editable draft, and nothing enters a patient's record until a clinician has read it, corrected it where necessary, and explicitly taken responsibility for it by ticking a review declaration. The system enforces this, the filing button is physically disabled until the declaration is confirmed.

Like every AI feature in Orb, Lens runs entirely on the hospital's own hardware. The image never leaves the building: it is analysed by the same local model that powers Sage, and the report is stored in the hospital's own database.

### 6.1 Who can use Lens and how to open it

Lens appears as the **Lens** tab in the left sidebar of the clinician shell for doctors, surgeons, and nurses. Administrators and pharmacists do not see the tab; filed reports still reach them through each patient's record in Patients.

To open the module:

1. Sign in with your employee ID and password (for the demo environment, DOC001 or NUR001 with the password `password`).
2. Click **Lens** in the left sidebar.

You will land on the upload screen described below. Viewing previously saved analyses does not require the Lens tab at all, filed reports appear inside each patient's record (see section 6.7).

### 6.2 Uploading an image

When no image is loaded, Lens shows a single large drop zone titled **Upload Medical Image**, with the hint "Drag & drop an X-ray, ECG, CT scan, or clinical photo. Or click to browse."

There are two ways to load an image:

- **Drag and drop.** Drag an image file from your desktop or file manager anywhere onto the drop zone. The zone highlights while you hover over it; release to load the file.
- **Click to browse.** Click anywhere on the drop zone to open your computer's standard file picker, then choose an image file.

As soon as a file is loaded, the drop zone is replaced by the analysis workspace: your image is shown on a dark viewing card (scaled to fit without cropping), with the analysis controls beneath it. A **✕ Clear** button on the image discards the current image and any draft analysis and returns you to the drop zone. Loading a new image also resets everything, a stale reading can never sit next to a fresh image.

The preview you see at this point is entirely local to your browser. Nothing is uploaded to the server until you click **Analyze Image**.

#### Supported files

Lens accepts common raster image formats only. The server verifies the actual content of every upload, not just the file name, so a mislabeled or disguised file is rejected even if its extension looks right.

| Accepted | Details |
|---|---|
| JPG / JPEG | Most exported scans and all phone photographs |
| PNG | Screenshots, exported ECG strips |
| WEBP | Modern web exports |
| GIF, BMP, TIFF | Legacy and scanner formats |

| Not accepted | What to do instead |
|---|---|
| DICOM (.dcm) | Export the study (or the relevant slice) as JPG or PNG from your PACS viewer, or photograph the displayed image |
| PDF reports | Take a screenshot of the image page and upload that |
| Files over 25 MB | Re-export at a lower resolution; 25 MB is the hard upload limit |

A rejected upload produces a clear error before any analysis starts, see Troubleshooting.

### 6.3 The analysis controls

Below the image preview sits a single control bar with three elements:

| Control | What it does |
|---|---|
| **Image type selector** | A dropdown identifying what kind of image this is: **X-Ray** (the default), **ECG**, **CT Scan**, **MRI**, **Clinical Photo**, or **Other**. This label is saved with the analysis and determines where the filed report appears later, imaging types (X-ray, CT, MRI, ECG and similar) are grouped under a patient's diagnostic reports, while non-imaging types are grouped with laboratory reports. |
| **Clinical context (optional)…** | A free-text field for the clinical question or relevant history, for example "62M, 3 days productive cough, fever 38.5, ?consolidation" or "post-op day 2, check line placement". Anything you type here is passed to the AI alongside the image and is also saved with the analysis for later reference. Good context measurably improves the relevance of the draft: the model will address your question rather than describing the image generically. |
| **Analyze Image** | Starts the analysis. The button reads **Analyzing…** and is disabled while a reading is in progress. |

Two practical notes on the clinical context field:

- It is treated strictly as *data about the case*. The AI is explicitly instructed that nothing in this field is an instruction to it, a deliberate safeguard against text manipulating the model's behaviour. Write clinical facts, not commands.
- Keep it concise. A line or two of focused history beats a pasted discharge summary; extremely long context can be truncated before it reaches the model.

### 6.4 Running an analysis and reading the streamed draft

Click **Analyze Image**. The image is uploaded, validated, stored securely under a randomised file name (encrypted at rest when the hospital has configured upload encryption), and handed to the vision model. Within a few seconds the draft begins to appear, streaming word by word into a card titled **AI Reference Read**.

The title line of that card carries three permanent markers:

- The badge **DRAFT · CLINICIAN REVIEW REQUIRED**, a constant reminder of the output's status.
- A small **AI provenance chip** showing the governing model's validation status (for the shipped configuration this reads **Unvalidated**). Hovering over the chip shows which model and version produced the output, what data it was trained on, and points you to **Admin → Model Governance** for the full lineage. This is Orb's "honesty layer": every AI output declares, at the point of use, exactly what produced it and whether that model has been clinically validated.
- Directly beneath the title, a fixed disclaimer paragraph: *"Decision-support reference for a clinician, not a diagnosis and not a radiologist read. Review and edit the findings below, then confirm before they enter the record."*

#### The structure of the draft

The model is instructed to produce its reading in a fixed clinical skeleton, so every Lens draft follows the same shape:

| Section | Content |
|---|---|
| **FINDINGS** | Each observation listed individually, anatomy visualised, abnormalities, devices and lines, image quality caveats |
| **IMPRESSION** | The overall clinical impression synthesised from the findings |
| **RECOMMENDATIONS** | Suggested follow-up actions, correlation with examination or labs, further imaging, specialist review |

The model is also instructed to flag critical findings prominently, so urgent observations (for example a suspected pneumothorax) are usually called out explicitly rather than buried in a list. Treat such flags as a prompt for immediate human review, not as a verified alert.

#### The mandatory investigational banner

Every completed analysis ends with a banner appended by the Orb application itself, not by the model, so it cannot be omitted, reworded, or "forgotten":

> ⚠️ INVESTIGATIONAL, NOT FOR DIAGNOSTIC USE. This is an unverified, AI-assisted image interpretation generated by a local model that has not been clinically validated. It is NOT a radiologist read. Do not make clinical decisions on this output alone; correlate with the full clinical picture and obtain a qualified human read.

This banner is part of the saved analysis text, so it travels with the report wherever it is later displayed, in the patient's diagnostic reports, in the patient portal, and in any downstream summary.

#### While it streams

- Text typically appears token by token; a full reading usually completes within thirty seconds to two minutes depending on how busy the AI queue is. Lens analyses share one AI model with the rest of Orb and queue behind live Sage conversations but ahead of background simulation activity.
- The first analysis after the system has been idle can take noticeably longer while the model loads into memory; subsequent analyses are faster.
- If you navigate away or the connection drops mid-stream, the analysis is cancelled and **nothing is saved**, there is no half-finished report in the archive. Simply re-run the analysis.

### 6.5 Reviewing and editing the draft

The streamed text lands directly in an editable text area, the draft you see *is* the working copy. Once streaming finishes:

1. Read the entire draft, including the Findings, Impression, and Recommendations sections, against the image itself.
2. Correct anything the model got wrong: delete hallucinated findings, fix laterality, adjust hedged language, add observations it missed. Edit the text in place exactly as you would edit a dictated draft.
3. Leave the investigational banner intact, it is part of the record's provenance.

Your edits become the *clinician-confirmed findings* that accompany the report when you file it (see below). Editing is not optional busywork: with an unvalidated model, the reviewed text is the only clinically meaningful version of the report, and the filing step is built to capture your version, not the machine's.

### 6.6 Filing the report to a patient record

When an analysis completes, a card titled **Assign to Patient Record** appears beneath the draft, explaining: "Link this analysis to a patient's Vigil record so it appears in their diagnostic reports."

To file the reviewed report:

1. In the field labelled **Search patient name or bed…**, start typing either the patient's name or their bed number. A live results list appears showing each matching patient's name and bed; "No matching patients" is shown if nothing matches.
2. Click the correct patient. A gold confirmation chip appears, **Selected: [patient name] · Bed [bed]**, so you can verify you have the right person before anything is written.
3. Make any final edits to the draft text above.
4. Tick the review declaration checkbox: *"I have reviewed this AI reference read, made any necessary edits, and take clinical responsibility for the findings filed to the record."*
5. Click **Confirm & File to Record**. The button is disabled (greyed out) until *both* a patient is selected *and* the declaration is ticked, this is enforced, not advisory. While filing is in progress the button reads **Filing…**.
6. A green confirmation banner appears: **✓ Report added to [patient name]'s Vigil record**. If something goes wrong, a red banner explains the failure instead.

If the image should not be linked to any patient, a teaching image, a test, a quality-review exercise, click **Skip**. The analysis is still preserved in the archive (see 6.7) but is not attached to any patient and generates no care-team notification.

#### What filing actually does

Filing is the moment the AI text crosses from "scratchpad" into "record", and Orb does three things at once:

1. **Links the analysis to the patient.** The report becomes part of that patient's imaging history.
2. **Notifies the care team in Relay.** A system message is posted automatically to the patient's case room in Messages (Relay). When you filed with reviewed text (the normal path), it is headed **Clinician-reviewed AI imaging reference** and includes the scan type, your clinical context, the note "Reviewed & filed by [your name]", and your clinician-confirmed findings (the first 1,500 characters). An analysis filed without reviewed text is instead headed **AI imaging reference (investigational, not a diagnostic read)** so its status is unmistakable.
3. **Surfaces the report across the system.** See the table below.

| Where the filed report appears | How to find it |
|---|---|
| Patient's record in **Patients (Vigil)** | Open the patient, then click **Diag. Reports** in the patient's action bar for imaging types (X-ray, CT, MRI, ECG, ultrasound, echo). Reports with non-imaging types appear under **Lab. Reports**. Both lists are searchable by content, type, or date. |
| Patient's case room in **Messages (Relay)** | The system message described above, posted at filing time |
| **Patient portal** | The patient sees their imaging reports in their own portal view of their record |
| **Sage** | When you ask Sage about the patient, recent imaging analyses are part of the context it draws on |
| Discharge documentation | Imaging history feeds the patient's discharge timeline |

One nuance worth knowing: the permanent archive keeps the *original* AI draft (with its mandatory banner) as the record of what the machine produced, while your reviewed and edited text is what goes to the care team in the Relay message. In other words, Orb preserves both what the AI said and what the clinician confirmed, an audit-friendly separation between machine output and human judgement.

### 6.7 Past analyses

Every analysis that streams to completion is saved automatically, whether or not it is ever assigned to a patient, together with the image, the image type, the clinical context, the full analysis text, the author, and a timestamp. Cancelled analyses are not saved.

- **Per patient:** the complete imaging history is available in the patient's record, newest first, as described above.
- **Hospital-wide:** the archive holds recent reads across the hospital (the general listing shows the fifty most recent), available to clinical staff with imaging-read access.

The Lens panel itself is deliberately a *capture* workspace, not a browsing archive: the assignment card appears immediately after each analysis, and that is the intended moment to file. If you clicked **Skip** and later decide the report belongs on the record, re-run the analysis on the same image and file it properly, the review-and-confirm step must accompany any filing regardless.

### 6.8 What the AI actually does: and what it does not

It is important to understand what is happening behind the streamed text, because it defines both the tool's usefulness and its limits.

Lens does **not** contain a purpose-built radiology algorithm. There is no trained fracture detector, no measurement pipeline, no bounding boxes, and no FDA/CE-cleared computer-aided-detection component. What Lens does is send your image, together with your clinical context, to a general-purpose multimodal medical language model (MedGemma 27B, the same locally hosted model that powers Sage) with instructions to describe the image in the Findings / Impression / Recommendations format. The model runs at low creativity settings with a bounded response length, so a reading is a sober, focused page.

Consequences of this design that you should internalise:

- **The model describes; it does not measure.** It cannot reliably report dimensions, angles, or densities, and it has no memory of the patient's prior imaging for comparison.
- **It can be confidently wrong.** Language models produce fluent text even when uncertain; a crisply worded finding is not evidence of a correct finding. Laterality errors, missed subtle findings, and over-called normal variants all occur.
- **Image quality matters enormously.** A phone photograph of a screen, taken at an angle with reflections, degrades the reading. Upload the cleanest export you can.
- **ECG readings are qualitative.** The model reads the ECG as a picture; never rely on it for rate, interval, or subtle ST-segment quantification.
- **The safeguards are layered, not decorative.** The structured format, the data-only fencing of your context text, the draft badge, the mandatory banner, the enforced review checkbox, and the provenance chip together form the safety envelope. None of them makes the underlying model validated.

### 6.9 Administrator note: the demo-only gate

Because Lens ships with **no clinically validated imaging model**, the entire analysis capability is controlled by an explicit deployment switch, `ORB_LENS_ENABLED`:

- In the **development/demo profile**, Lens is enabled by default, this is what you experience in the demo environment.
- In a **production profile**, Lens analysis is **disabled by default**. Any attempt to analyse an image is refused with the message: "Lens image analysis is disabled in this deployment. It has no validated imaging model and is decision-support reference only. Set ORB_LENS_ENABLED=true to enable it explicitly."
- An operator who wants Lens active in production must set `ORB_LENS_ENABLED=true` in the server's environment configuration and restart the backend. This is a deliberate, positive opt-in: an unvalidated vision model can never silently process patient images in a real deployment because someone forgot a setting. Enabling it is an institutional decision and should be documented under your local clinical-AI governance process.

The gate controls *analysis* only. Previously saved analyses remain readable in patient records regardless of the switch.

Related controls: the model powering Lens, its version, and its validation status are visible under **Admin → Model Governance**, the same place the provenance chip points. Stored images are kept under randomised, non-guessable file names, encrypted at rest when the upload-encryption key is configured, and every retrieval requires a signed-in staff session and is written to the audit trail.

### Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| "Analysis failed: … Ensure Ollama is running with medgemma:27b." | The local AI runtime is not running, or the vision model is not installed | Ask your administrator to start the AI runtime and confirm the model is loaded; then click **Analyze Image** again |
| "Lens image analysis is disabled in this deployment…" | The deployment is running with the demo-only gate closed (production default) | This is a policy setting, not a fault. An administrator must explicitly enable Lens (see 6.9) |
| "Unsupported image type. Allowed: JPG, PNG, WEBP, GIF, BMP, TIFF." | You uploaded a DICOM file, PDF, or another non-image format | Export or screenshot the image as JPG/PNG and retry |
| "Upload content is not a recognized image format." | The file's extension says image, but its contents are not a valid image (renamed or corrupted file) | Re-export the image from its source application |
| "Image exceeds maximum size of 25 MB." | The export is too large | Re-export at reduced resolution or higher compression |
| First analysis of the day is very slow | The model is loading into memory | Wait, subsequent analyses will be much faster |
| Text stalls mid-stream, then resumes | Another AI task (for example a live Sage conversation) holds the shared model | Normal queuing behaviour; the analysis will complete |
| Stream stopped and no report was saved | You navigated away or the connection dropped mid-analysis | Cancelled analyses are discarded by design; re-run the analysis |
| **Confirm & File to Record** stays greyed out | A patient is not selected, or the review declaration is not ticked | Both are required, select the patient from the search results and tick the checkbox |
| "No matching patients" in the assignment search | Name or bed doesn't match any current patient | Search is a simple contains-match on name and bed; check spelling, or search by bed number instead |
| "✗ Assignment failed: Patient not found" | The patient record was removed (for example, discharged) between search and filing | Refresh the patient list by re-running the search and select again |
| Filed report not visible under **Diag. Reports** | The image type was set to Clinical Photo or Other | Non-imaging types are grouped under **Lab. Reports** instead; check there |

### Safety & limitations

- **Lens is not a diagnostic device.** It is an investigational, decision-support reference produced by a locally hosted AI model that has **not** been clinically validated for image interpretation and has no regulatory clearance of any kind. The mandatory banner and the provenance chip state this on every reading, take it literally.
- **It is not a radiologist read and does not replace one.** Every image that clinically warrants formal reporting must still receive a qualified human read through your normal pathway. Lens output may inform your differential while you wait; it may never close it.
- **Never act on Lens output alone.** Correlate with the patient's examination, history, laboratory results, and prior imaging. Critical findings flagged by Lens require immediate human verification; equally, the *absence* of a finding in a Lens read is not evidence of absence.
- **The clinician who files a report owns it.** The review declaration, "…and take clinical responsibility for the findings filed to the record", means exactly what it says. Filing an unedited, unread AI draft to a patient record is a misuse of the tool.
- **The disclaimer cannot be removed.** The investigational banner is appended by the application itself, independent of the AI, and is stored as part of every report.
- **Patient-visible consequences.** Filed imaging reports appear in the patient portal. File only text you would be comfortable with the patient reading, and remember the report carries its investigational banner there too.
- **Privacy.** Analysis happens entirely on hospital hardware; images and reports never leave the local system. Uploaded images are stored under randomised names, encrypted at rest where configured, and access to them is authenticated and audit-logged.
- **Demo environment caveat.** In the demo deployment, simulated background activity also files imaging and laboratory entries into patient histories. Reports authored by the simulation are attributed accordingly and should not be confused with clinician-filed reads.
