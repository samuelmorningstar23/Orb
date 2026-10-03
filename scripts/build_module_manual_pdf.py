#!/usr/bin/env python3
"""Render docs/MODULE_MANUAL.md to print-quality HTML (docs/MODULE_MANUAL.html).

Sibling of build_manual_pdf.py, for the module manual (per module:
architecture + clinical claim). Reuses that script's CSS and heading-anchor
logic so both PDFs share one look. The PDF render step runs through the
repo's node Playwright:

    server/.venv/bin/python scripts/build_module_manual_pdf.py
    node scripts/render_pdf.mjs docs/MODULE_MANUAL.html docs/Orb_Module_Manual.pdf "Orb Hospital OS — Module Manual"
"""
from __future__ import annotations

import sys
from pathlib import Path

import markdown

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from build_manual_pdf import CSS, add_heading_ids  # noqa: E402

SRC = REPO / "docs" / "MODULE_MANUAL.md"
OUT_HTML = REPO / "docs" / "MODULE_MANUAL.html"

COVER = """
<div class="cover">
  <div class="cover__mark"></div>
  <h1>Orb Hospital OS</h1>
  <div class="sub">Module Manual — Architecture &amp; Clinical Claims</div>
  <div class="meta">
    Every module: what it is built from, and how it is meant to<br>
    make the clinician's day easier &middot; Edition: August 2026
  </div>
  <div class="note">
    Orb is pre-pilot. Every claim in this manual is a design claim pending
    shadow-mode validation with real patients; where a module today runs on
    simulated data or an unvalidated model, its Status line says so. All AI
    output is decision support for a qualified clinician &mdash; never
    autonomous care.
  </div>
</div>
"""


def build_html() -> str:
    text = SRC.read_text(encoding="utf-8")

    toc_start = text.find("## Table of Contents")
    if toc_start == -1:
        toc_md, body = "", text
    else:
        after = text.index("\n---\n", toc_start)
        toc_md = text[toc_start:after]
        body = text[after + len("\n---\n") :]

    md = markdown.Markdown(extensions=["tables", "fenced_code", "attr_list", "sane_lists"])
    toc_html = md.convert(toc_md)
    md.reset()
    body_html = add_heading_ids(md.convert(body))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<base href="{(REPO / 'docs').as_uri()}/">
<title>Orb Hospital OS — Module Manual</title>
<style>{CSS}</style></head>
<body>
{COVER}
<section class="toc">{toc_html}</section>
{body_html}
</body></html>"""


def main() -> int:
    if not SRC.exists():
        print(f"missing {SRC}", file=sys.stderr)
        return 1
    html = build_html()
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"HTML written: {OUT_HTML} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
