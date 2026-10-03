#!/usr/bin/env python3
"""Assemble docs/manual_sections/*.md into the single docs/ORB_TECHNICAL_MANUAL.md
with an auto-generated table of contents.

Usage (from the repo root):  python3 docs/manual_sections/_assemble.py

Edit the per-area section files in this directory, then re-run this to rebuild the
one-stop manual. 00_intro.md provides the title + section 0; 01..15 are the bodies.
"""
import re, os

SD = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SD, "..", "ORB_TECHNICAL_MANUAL.md")


def main():
    intro = open(os.path.join(SD, "00_intro.md")).read()
    head, _, rest0 = intro.partition("## 0. Start Here")
    body0 = "## 0. Start Here" + rest0
    files = [f for f in sorted(os.listdir(SD))
             if re.match(r'\d\d_.*\.md$', f) and f != "00_intro.md"]
    parts = [body0] + [open(os.path.join(SD, f)).read().strip() for f in files]
    all_sections = "\n\n---\n\n".join(parts)

    seen = {}
    def slug(t):
        s = re.sub(r'[^\w\s-]', '', t.strip().lower()).replace(' ', '-')
        s = re.sub(r'-+', '-', s).strip('-')
        n = seen.get(s, 0); seen[s] = n + 1
        return s if n == 0 else f"{s}-{n}"

    toc = ["## Table of Contents", ""]
    for line in all_sections.splitlines():
        if line.startswith('## ') and not line.startswith('###'):
            t = line[3:].strip(); toc.append(f"- [{t}](#{slug(t)})")
        elif line.startswith('### ') and not line.startswith('####'):
            t = line[4:].strip(); toc.append(f"  - [{t}](#{slug(t)})")

    final = head.rstrip() + "\n\n" + "\n".join(toc) + "\n\n---\n\n" + all_sections + "\n"
    open(OUT, "w").write(final)
    print(f"Wrote {os.path.normpath(OUT)} — {final.count(chr(10))+1:,} lines, {len(final)/1024:.0f} KB")


if __name__ == "__main__":
    main()
