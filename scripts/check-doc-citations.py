#!/usr/bin/env python3
"""Verify every `path:line` citation in the documentation still points at the truth.

WHY THIS EXISTS
---------------
`docs/guide/` is ~48,000 lines of technical manual whose entire value is that you can
follow a citation to the exact function that does the thing. On 2026-08-08 an audit found
**502 of 1,108 symbol anchors stale — 45%** — because nine features had landed the day
before and shifted line numbers throughout. None of the prose was wrong. Every line number
was.

A manual that is 45% wrong about line numbers is exactly as untrustworthy as one that is
100% wrong, because a reader cannot tell which half they are holding. That is the failure
this guards against, and it will recur every time a batch of features lands.

TWO CHECKS, AND THE SECOND IS THE ONE THAT MATTERS
--------------------------------------------------
1. RESOLUTION — does the file exist, and does the line exist inside it?
   Cheap, and catches deleted or renamed files. It is also WEAK: a citation can resolve
   perfectly and still point at a blank line thirty lines from the function it claims.

2. SYMBOL ANCHORS — for a heading that names a backticked symbol AND cites a line, does
   that symbol actually appear within a few lines of it?
   This is the check with teeth. It is what measured the 45%.

Only BACKTICKED identifiers are treated as symbol claims. An earlier version of this check
matched the first word of any heading and reported 137 stale anchors — of which 129 were
prose ("Rate limiting", "Module overview", "Response shape"). Tightening it to backticks
took the real number to 8. A checker that cries wolf gets disabled by the first engineer it
inconveniences, so it is deliberately conservative: it would rather miss a stale anchor than
flag a good one.

USAGE
    python3 scripts/check-doc-citations.py            # check, exit 1 on failure
    python3 scripts/check-doc-citations.py --fix      # rewrite line numbers it can resolve
    python3 scripts/check-doc-citations.py --quiet    # totals only

Exit code 0 = clean, 1 = stale citations found, 2 = the checker itself could not run.
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where documentation lives. Add new doc trees here.
DOC_GLOBS = [
    "docs/guide/*.md",
    "docs/guide/screen-map/*.md",
    "docs/MANUAL_PLAIN_ENGLISH.md",
]

#: Files that legitimately contain broken citations.
#: The citation audit QUOTES stale citations as examples of what it found — flagging those
#: would make the checker permanently red for doing its job correctly.
DOC_EXCLUDE = {"_CITATION_AUDIT.md"}

#: Directories that are never citation targets.
#: docs/archive holds pre-change snapshots of source files. Without this, a bare-basename
#: citation (`client.ts:965`) could resolve to a stale archived copy and pass while the
#: live file has moved on.
SKIP_DIRS = ("/node_modules", "/.git", "/.venv", "__pycache__", "/dist", "/build", "/.next",
             "/docs/archive", "/.claude")

#: A citation: some/path.ext:123 or some/path.ext:123-456
CITATION = re.compile(
    r"([A-Za-z0-9_./\-]+\.(?:py|ts|tsx|css|json|js|mjs|sh)):(\d+)(?:-(\d+))?"
)

#: A symbol anchor: a heading whose FIRST token is a backticked identifier, followed by a
#: citation. `#### \`get_current_user\` — \`server/auth.py:416\``
SYMBOL_ANCHOR = re.compile(
    r"^#{2,5}\s+`([A-Za-z_][A-Za-z0-9_.]*)`[^\n]*?[`\s(]"
    r"([A-Za-z0-9_./\-]+\.(?:py|ts|tsx)):(\d+)"
)

#: Tokens that look like identifiers but are not symbols. File extensions arrive here when a
#: heading backticks a whole filename (`test_audit.py`); dunders are language furniture.
NOT_SYMBOLS = {"py", "ts", "tsx", "css", "json", "js", "mjs", "sh", "__main__", "__init__"}

#: How far from the cited line the symbol may sit and still count. A definition often has
#: decorators or a docstring above it, and citations legitimately point at either.
ANCHOR_WINDOW = 5


def build_basename_index() -> dict[str, list[str]]:
    """Map bare filenames to full paths, so `audit_service.py:159` shorthand resolves."""
    idx: dict[str, list[str]] = collections.defaultdict(list)
    for base, dirs, files in os.walk(ROOT):
        if any(s in base for s in SKIP_DIRS):
            dirs[:] = []
            continue
        for f in files:
            idx[f].append(os.path.join(base, f))
    return idx


def resolve(path: str, idx: dict[str, list[str]]) -> str | None:
    """Find the real file a citation refers to.

    Tries repo-relative, then server-relative (chapters often cite `services/x.py` from
    inside `server/`), then a unique basename match.
    """
    for candidate in (os.path.join(ROOT, path), os.path.join(ROOT, "server", path)):
        if os.path.isfile(candidate):
            return candidate
    hits = idx.get(os.path.basename(path), [])
    return hits[0] if hits else None


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def find_symbol(src: list[str], symbol: str) -> int | None:
    """The most likely DEFINITION line for `symbol`, 1-indexed, or None.

    Prefers a definition over a mention — a citation should point at where a thing IS, not
    at one of fifty places it is called.
    """
    definition = re.compile(
        rf"^\s*(?:async\s+def|def|class)\s+{re.escape(symbol)}\b"
        rf"|^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class|interface|type)\s+{re.escape(symbol)}\b"
        rf"|^\s*(?:const|let|var|function|class)\s+{re.escape(symbol)}\b"
        rf"|^{re.escape(symbol)}\s*[:=]"
    )
    for i, line in enumerate(src, 1):
        if definition.match(line):
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fix", action="store_true",
                    help="rewrite stale line numbers where the symbol can be located")
    ap.add_argument("--quiet", action="store_true", help="totals only")
    args = ap.parse_args()

    import glob
    docs: list[str] = []
    for pattern in DOC_GLOBS:
        docs.extend(glob.glob(os.path.join(ROOT, pattern)))
    docs = [d for d in sorted(set(docs)) if os.path.basename(d) not in DOC_EXCLUDE]

    if not docs:
        print("check-doc-citations: no documentation found — is DOC_GLOBS correct?",
              file=sys.stderr)
        return 2

    idx = build_basename_index()

    total = resolved = 0
    unresolved: list[str] = []
    anchors = anchors_ok = 0
    stale: list[tuple[str, int, str, str, int, int | None]] = []

    for doc in docs:
        rel = os.path.relpath(doc, ROOT)
        lines = read_lines(doc)

        # ── Check 1: every citation resolves and is in range ──
        for lineno, text in enumerate(lines, 1):
            for m in CITATION.finditer(text):
                path, start = m.group(1), int(m.group(2))
                total += 1
                target = resolve(path, idx)
                if target is None:
                    unresolved.append(f"{rel}:{lineno}  no such file: {path}")
                    continue
                n = len(read_lines(target))
                if start < 1 or start > n:
                    unresolved.append(
                        f"{rel}:{lineno}  {path}:{start} out of range (file has {n} lines)")
                    continue
                resolved += 1

        # ── Check 2: symbol anchors point at their symbol ──
        for lineno, text in enumerate(lines, 1):
            m = SYMBOL_ANCHOR.match(text.strip())
            if not m:
                continue
            symbol = m.group(1).split(".")[-1]
            if symbol in NOT_SYMBOLS:
                continue
            path, cited = m.group(2), int(m.group(3))
            target = resolve(path, idx)
            if target is None:
                continue  # already counted as unresolved above
            src = read_lines(target)
            if cited > len(src):
                continue
            anchors += 1
            window = "\n".join(src[max(0, cited - ANCHOR_WINDOW - 1): cited + ANCHOR_WINDOW])
            if symbol in window:
                anchors_ok += 1
            else:
                stale.append((rel, lineno, symbol, path, cited, find_symbol(src, symbol)))

    # ── Optional repair ──
    fixed = 0
    if args.fix and stale:
        by_doc: dict[str, list] = collections.defaultdict(list)
        for rel, lineno, symbol, path, cited, actual in stale:
            if actual is not None:
                by_doc[rel].append((lineno, path, cited, actual))
        for rel, edits in by_doc.items():
            full = os.path.join(ROOT, rel)
            lines = read_lines(full)
            for lineno, path, cited, actual in edits:
                old, new = f"{path}:{cited}", f"{path}:{actual}"
                if old in lines[lineno - 1]:
                    lines[lineno - 1] = lines[lineno - 1].replace(old, new)
                    fixed += 1
            with open(full, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    # ── Report ──
    pct_res = (resolved / total * 100) if total else 100.0
    pct_anc = (anchors_ok / anchors * 100) if anchors else 100.0
    print(f"citations   {resolved}/{total} resolve and are in range  ({pct_res:.2f}%)")
    print(f"anchors     {anchors_ok}/{anchors} point at their symbol    ({pct_anc:.1f}%)")

    if not args.quiet and unresolved:
        print(f"\nUNRESOLVED ({len(unresolved)}):")
        for u in unresolved[:40]:
            print(f"  {u}")
        if len(unresolved) > 40:
            print(f"  … and {len(unresolved) - 40} more")

    remaining = [s for s in stale if not (args.fix and s[5] is not None)]
    if not args.quiet and remaining:
        print(f"\nSTALE ANCHORS ({len(remaining)}):")
        for rel, lineno, symbol, path, cited, actual in remaining[:40]:
            hint = f"  → looks like {path}:{actual}" if actual else "  → symbol not found in file"
            print(f"  {rel}:{lineno}  `{symbol}` cited at {path}:{cited}{hint}")
        if len(remaining) > 40:
            print(f"  … and {len(remaining) - 40} more")

    if fixed:
        print(f"\nfixed {fixed} line number(s). Re-run to confirm, and READ THE DIFF — a symbol "
              f"can move because the code changed, in which case the PROSE needs updating too, "
              f"not just the number.")

    if unresolved or remaining:
        print(f"\nFAILED. A citation pointing at the wrong line is worse than no citation: it "
              f"reads as authoritative. Fix with --fix where the symbol simply moved, by hand "
              f"where the code actually changed.")
        return 1

    print("\nOK — every citation resolves and every symbol anchor is correct.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
