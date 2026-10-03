#!/usr/bin/env python3
"""Insert screenshot figures into the generated manual chapters.

Each figure is placed immediately after its chapter's `## ` heading, so the
reader sees the screen before reading how to drive it. Idempotent: a chapter
that already carries its figure is skipped, so this can run after every
regeneration of docs/manual_generated/.

    server/.venv/bin/python scripts/insert_manual_figures.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GEN = REPO / "docs" / "manual_generated"

# chapter file stem -> [(image file, caption), ...]
FIGURES: dict[str, list[tuple[str, str]]] = {
    "02-getting-started": [
        ("clinician-login.png", "The clinician sign-in screen. Patients use the same address and are routed to the Bridge portal after signing in."),
    ],
    "03-vigil": [
        ("vigil-ward-board.png", "The Vigil patient board. The metric tiles count your own department; the Sage brief beneath them is hospital-wide."),
    ],
    "04-sage": [
        ("sage-assistant.png", "The Sage tab. Suggestion cards start a query; answers carry governance badges and cited sources."),
    ],
    "05-scribe": [
        ("scribe-documentation.png", "Scribe. Type or dictate, then structure the transcript into a SOAP note, discharge summary, or I-PASS handoff."),
    ],
    "06-lens": [
        ("lens-image-analysis.png", "Lens. Drag in an X-ray, ECG, CT, or clinical photo; the draft that returns is always for clinician review."),
    ],
    "07-relay": [
        ("relay-case-rooms.png", "Relay case rooms, one per patient, sorted by recency or urgency with unread counts and criticality badges."),
    ],
    "08-orders-emar": [
        ("orders-cpoe.png", "Provider order entry. Codes are validated against LOINC, SNOMED, or ICD-10 before the order can be created."),
    ],
    "09-helix-pharmacy": [
        ("helix-pharmacy.png", "The Helix pharmacy workspace: catalogue, inventory lots, and the pending verification queue."),
    ],
    "10-forecast-command": [
        ("forecast-census.png", "Forecast. The provenance badge states plainly when a projection is heuristic rather than a trained model."),
        ("command-center.png", "The Command Center: house-wide census, acuity heatmap, active deteriorations, and today's operating room list."),
        ("what-if-simulator.png", "The What-If Simulator models surges, bed closures, and discharge acceleration against the live baseline."),
    ],
    "12-bridge": [
        ("bridge-patient-portal.png", "The Bridge patient portal presents the same clinical record in plain language."),
    ],
    "13-admin": [
        ("admin-panel.png", "The administration workspace: user accounts, audit trail, model governance, and compliance."),
        ("ai-model-configure.png", "The AI model panel, opened from the header chip. Administrators can load or unload each local tier."),
        ("ai-model-unload-confirm.png", "Unloading asks for confirmation and lists exactly which clinical features stop working until the model is loaded again."),
    ],
    "14-pulse-integrations": [
        ("appointments.png", "The Appointments board: scheduled follow-ups, medication reviews, and post-discharge tasks."),
    ],
    "15-troubleshooting": [
        ("revenue-integrity.png", "Revenue Integrity analyses coding and documentation-improvement opportunities for a patient."),
    ],
}


def figure_md(img: str, caption: str) -> str:
    # Relative to docs/, so the path resolves both in the assembled markdown and
    # in the HTML the PDF builder renders from docs/.
    return f"![{caption}](assets/manual/{img})\n\n*{caption}*\n"


def main() -> int:
    changed = 0
    for stem, figs in FIGURES.items():
        path = GEN / f"{stem}.md"
        if not path.exists():
            print(f"  ! missing chapter {stem}.md — skipped")
            continue
        text = path.read_text(encoding="utf-8")
        block = "\n".join(figure_md(img, cap) for img, cap in figs)
        if any(img in text for img, _ in figs):
            continue  # already carries its figures
        # Insert after the chapter heading (and any immediately following blank line).
        m = re.search(r"^(## .+?\n)", text, re.M)
        if not m:
            print(f"  ! no '## ' heading in {stem}.md — skipped")
            continue
        insert_at = m.end()
        text = text[:insert_at] + "\n" + block + text[insert_at:]
        path.write_text(text, encoding="utf-8")
        changed += 1
        print(f"  + {stem}.md ({len(figs)} figure{'s' if len(figs) > 1 else ''})")
    print(f"{changed} chapter(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
