#!/usr/bin/env python3
"""Render docs/USER_MANUAL.md to a print-quality PDF.

Markdown -> styled HTML -> headless Chromium print-to-PDF (Playwright).
Chromium is used because it is the only renderer already installed here (via
Playwright) and it honours the CSS paged-media features this manual needs:
running headers, page counters, and per-chapter page breaks.

    server/.venv/bin/python scripts/build_manual_pdf.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "docs" / "USER_MANUAL.md"
OUT_HTML = REPO / "docs" / "USER_MANUAL.html"
OUT_PDF = REPO / "docs" / "Orb_Hospital_OS_User_Manual.pdf"

CSS = """
@page {
  size: A4;
  margin: 20mm 18mm 18mm 18mm;
  @bottom-center { content: counter(page); }
}
:root {
  --ink: #16181d;
  --ink-soft: #4a4f5a;
  --rule: #d9dde4;
  --accent: #9a6b00;
  --accent-soft: #fdf7e8;
  --code-bg: #f5f6f8;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: var(--ink);
  margin: 0;
  -webkit-font-smoothing: antialiased;
}

/* ── Cover ── */
.cover {
  height: 247mm;              /* one full page inside the margin box */
  display: flex;
  flex-direction: column;
  justify-content: center;
  page-break-after: always;
  text-align: center;
}
.cover__mark {
  width: 74px; height: 74px; margin: 0 auto 26px;
  border-radius: 50%;
  border: 3px solid var(--accent);
  border-right-color: transparent;
  transform: rotate(-35deg);
}
.cover h1 { font-size: 30pt; margin: 0 0 6px; letter-spacing: -0.5px; }
.cover .sub { font-size: 13pt; color: var(--ink-soft); margin-bottom: 30px; }
.cover .meta { font-size: 10pt; color: var(--ink-soft); line-height: 1.9; }
.cover .note {
  margin: 34px auto 0; max-width: 118mm; padding: 12px 16px;
  border-left: 3px solid var(--accent); background: var(--accent-soft);
  font-size: 9.5pt; text-align: left; color: var(--ink-soft);
}

/* ── Headings ── */
h1 { font-size: 20pt; margin: 0 0 12px; }
h2 {
  font-size: 17pt; margin: 0 0 14px; padding-bottom: 8px;
  border-bottom: 2px solid var(--accent); color: var(--ink);
  page-break-after: avoid;
}
h3 { font-size: 12.5pt; margin: 20px 0 7px; page-break-after: avoid; }
h4 { font-size: 11pt; margin: 15px 0 5px; color: var(--ink-soft); page-break-after: avoid; }

/* Each chapter starts on a fresh page. */
h2 { page-break-before: always; }
.toc + hr + h2, h2:first-of-type { page-break-before: auto; }

p { margin: 0 0 9px; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 10px; padding-left: 20px; }
li { margin-bottom: 3px; }
strong { font-weight: 650; }

/* ── Tables ── */
table {
  width: 100%; border-collapse: collapse; margin: 12px 0 16px;
  font-size: 9.5pt; page-break-inside: avoid;
}
th {
  text-align: left; background: #eef0f4; border-bottom: 1.5px solid var(--rule);
  padding: 7px 9px; font-weight: 650;
}
td { padding: 6px 9px; border-bottom: 1px solid var(--rule); vertical-align: top; }
tr:nth-child(even) td { background: #fafbfc; }

/* ── Code ── */
code {
  font-family: "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 9pt; background: var(--code-bg); padding: 1px 4px;
  border-radius: 3px; color: #1d3b53;
}
pre {
  background: var(--code-bg); border: 1px solid var(--rule); border-radius: 5px;
  padding: 10px 12px; overflow-x: auto; page-break-inside: avoid; margin: 10px 0 14px;
}
pre code { background: none; padding: 0; font-size: 8.6pt; line-height: 1.45; }

blockquote {
  margin: 12px 0; padding: 9px 14px;
  border-left: 3px solid var(--accent); background: var(--accent-soft);
  color: var(--ink-soft);
}
blockquote p:last-child { margin-bottom: 0; }

hr { border: 0; border-top: 1px solid var(--rule); margin: 22px 0; }

/* ── Table of contents ── */
.toc { page-break-after: always; }
.toc h2 {
  page-break-before: auto;   /* the TOC heading must not push a blank page */
  border-bottom-color: var(--rule);
}
.toc ul { list-style: none; padding-left: 0; margin: 0; }
.toc ul ul { padding-left: 16px; }
.toc li { margin-bottom: 2px; font-size: 9.8pt; }
.toc > ul > li { margin-top: 8px; font-weight: 600; font-size: 10.5pt; }
.toc > ul > li > ul > li { font-weight: 400; color: var(--ink-soft); }
.toc a { color: inherit; text-decoration: none; }

/* ── Figures ──
   Screenshots are 1440x900 captures of the real app. Constrain by width and
   keep each image with the italic caption that follows it. */
img {
  display: block;
  width: 100%;
  max-width: 165mm;
  margin: 14px auto 6px;
  border: 1px solid var(--rule);
  border-radius: 4px;
  page-break-inside: avoid;
}
/* python-markdown wraps a lone image in <p>; the caption is the next <p><em>. */
p > img { margin-bottom: 4px; }
p > em:only-child {
  display: block;
  text-align: center;
  font-size: 8.8pt;
  color: var(--ink-soft);
  margin: 0 auto 16px;
  max-width: 165mm;
  page-break-before: avoid;
}
"""


def gfm_anchor(title: str, seen: dict) -> str:
    """GitHub-flavoured heading slug, with GitHub's duplicate suffixes.

    Must stay byte-identical to the slugs the manual's own Table of Contents
    was generated with, otherwise every internal link in the PDF is dead.
    """
    a = re.sub(r"[^\w\s-]", "", title.lower())
    a = re.sub(r"[\s_]+", "-", a).strip("-")
    n = seen.get(a, 0)
    seen[a] = n + 1
    return a if n == 0 else f"{a}-{n}"


def add_heading_ids(html: str) -> str:
    """Give every h2/h3 an id matching its TOC link.

    python-markdown's `toc` extension is not used: its de-duplication suffix
    (`_1`) differs from GitHub's (`-1`), which would silently break links on
    the repeated "Troubleshooting" / "Safety & limitations" headings.
    """
    seen: dict = {}

    def repl(m):
        level, inner = m.group(1), m.group(2)
        text = re.sub(r"<[^>]+>", "", inner)                 # strip <em>, <code>…
        text = re.sub(r"&[a-zA-Z]+;|&#\d+;", "", text)        # …and entities
        return f'<h{level} id="{gfm_anchor(text, seen)}">{inner}</h{level}>'

    return re.sub(r"<h([23])>(.*?)</h\1>", repl, html, flags=re.S)


def build_html() -> str:
    text = SRC.read_text(encoding="utf-8")

    # The assembled manual is: title block, `---`, "## Table of Contents" + list,
    # `---`, then the chapters. Anchor the split on the TOC heading itself — the
    # naive "first \n---\n\n## " marker matches the rule BEFORE the TOC, which
    # dropped the whole contents list into the body (dead .toc styling, an
    # orphan blank page, and default-blue links).
    toc_start = text.find("## Table of Contents")
    if toc_start == -1:
        toc_md, body = "", text
    else:
        after = text.index("\n---\n", toc_start)   # rule between TOC and ch. 1
        toc_md = text[toc_start:after]
        body = text[after + len("\n---\n") :]

    md = markdown.Markdown(extensions=["tables", "fenced_code", "attr_list", "sane_lists"])
    toc_html = md.convert(toc_md)
    md.reset()
    body_html = add_heading_ids(md.convert(body))

    cover = """
<div class="cover">
  <div class="cover__mark"></div>
  <h1>Orb Hospital OS</h1>
  <div class="sub">User Manual</div>
  <div class="meta">
    The local-first, AI-native hospital operating system<br>
    Edition: July 2026 &middot; 15 chapters<br>
    For clinicians, nurses, pharmacists, administrators, and patients
  </div>
  <div class="note">
    All AI output in Orb is decision support for a qualified clinician &mdash; never
    autonomous care. Every model runs on hospital hardware; no patient data leaves
    the building.
  </div>
</div>
"""
    # <base> so the chapters' `assets/manual/…` image paths resolve when the
    # HTML is opened from docs/ (and from the file:// URL Chromium prints).
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<base href="{(REPO / 'docs').as_uri()}/">
<title>Orb Hospital OS — User Manual</title>
<style>{CSS}</style></head>
<body>
{cover}
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

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed in this interpreter — run the render step "
              "with the repo's node Playwright instead (see scripts/render_pdf.mjs)",
              file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(OUT_HTML.as_uri(), wait_until="networkidle")
        page.pdf(
            path=str(OUT_PDF),
            format="A4",
            print_background=True,
            margin={"top": "20mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
            display_header_footer=True,
            header_template='<div style="font-size:7pt;color:#8a8f99;width:100%;padding:0 18mm;">'
                            '<span style="float:right">Orb Hospital OS — User Manual</span></div>',
            footer_template='<div style="font-size:8pt;color:#8a8f99;width:100%;text-align:center;">'
                            '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
        )
        browser.close()
    print(f"PDF written: {OUT_PDF} ({OUT_PDF.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
