"""Guideline Corpus Loader — bulk-ingest clinical guideline files into Sage's RAG store.

Sage's retrieval-augmented generation currently draws on ~19 hand-authored documents
hard-coded as ``services.clinical_knowledge.CLINICAL_DOCUMENTS``. That list does not
scale: adding a guideline means editing Python. This module lets an operator drop
``.md`` / ``.txt`` guideline files into a directory and load them into the SAME SQLite
store that ``clinical_knowledge`` uses, so they become retrievable through
``clinical_knowledge.search_guidelines()`` exactly like the built-in corpus.

Store reused (identical table names + DDL to ``clinical_knowledge.init_knowledge_base``)
so nothing new needs to learn about them:

  * ``clinical_knowledge``      — chunk metadata + text (id, document_id, chunk_index, …)
  * ``clinical_knowledge_fts``  — FTS5 lexical/BM25 index (external-content, content_rowid=id)
  * ``clinical_knowledge_vec``  — sqlite-vec ``vec0`` dense-embedding index (float[EMBEDDING_DIM])

We additionally maintain one loader-owned bookkeeping table, ``corpus_documents``,
recording one row per loaded source file (version, loaded_at, chunk count, embed mode)
for idempotency + provenance without touching the shared retrieval schema.

Graceful degradation (embedded vs lexical-only)
-----------------------------------------------
The real BGE-M3 embedder (``FlagEmbedding``) is a ~2 GB optional dependency. When it is
installed we compute + store a dense vector per chunk (embedded mode). When it is NOT
installed we still index every chunk in FTS5 and store it WITHOUT a vector row
(lexical-only mode) — ``search_guidelines`` then retrieves it via its BM25/lexical path.
The active mode is logged and returned in the result.

Idempotency + versioning
-------------------------
Each source file maps to one logical document (``document_id``: front-matter override,
else a slug of the filename). Re-loading a file DELETES that document's existing chunks
(base row + FTS entry + vector) and re-inserts fresh ones, so re-runs upsert/replace
rather than duplicating. A ``version`` and ``loaded_at`` are recorded per document.

Metadata may be supplied via YAML front-matter at the top of the file, or via a JSON
sidecar (``<stem>.meta.json`` / ``<stem>.json``). Recognised keys: title, source,
source_type, source_origin, trust_tier (1=hospital protocol, 2=national guideline,
3=reference), version, category, specialty, effective_date, document_id, is_synthetic.

NOTE: ``clinical_knowledge.init_knowledge_base()`` rebuilds (DROPs + recreates) these
tables when its own corpus fingerprint changes. Run this loader AFTER boot to extend
the corpus; a fingerprint-unchanged boot keeps loaded docs intact (see that module's
fast-path). This loader never edits clinical_knowledge.py — it only reuses its tables.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config import EMBEDDING_DIM
from database import db_session

logger = logging.getLogger("orb.corpus_loader")

# Chunking: heading/paragraph aware, ~500-1000 char target with character overlap.
CHUNK_TARGET_CHARS = 900
CHUNK_OVERLAP_CHARS = 150

_SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt")

# trust_tier -> a sensible default source_type when the file omits one.
_TIER_SOURCE_TYPE = {1: "hospital_sop", 2: "national_guideline", 3: "reference"}


# ─────────────────────────────────────────────────────────────────────────────
# Embedding availability (best-effort; never loads the 2 GB model just to check)
# ─────────────────────────────────────────────────────────────────────────────
def _real_embedder_available() -> bool:
    """True only if the REAL BGE-M3 embedder can be used.

    ``clinical_knowledge._get_embedder`` falls back to a zero-vector mock when
    ``FlagEmbedding`` is missing or ``ORB_SKIP_KNOWLEDGE_BASE`` is set — zero
    vectors are useless for dense retrieval, so we treat those cases as
    "embeddings unavailable" and store chunks lexical-only.
    """
    if os.getenv("ORB_SKIP_KNOWLEDGE_BASE", "").lower() in ("1", "true", "yes"):
        return False
    return importlib.util.find_spec("FlagEmbedding") is not None


def _embed_composites(texts: List[str]) -> List[bytes]:
    """Embed chunk composite texts, reusing clinical_knowledge's embedder.

    Only called when :func:`_real_embedder_available` is True.
    """
    from services import clinical_knowledge as ck

    # Defensive: ck._get_embedder() references a module-global ``os`` that is only
    # bound as a side effect of init_knowledge_base() having run. We may run
    # standalone (before boot), so ensure the name exists WITHOUT editing that file.
    if not hasattr(ck, "os"):
        ck.os = os  # type: ignore[attr-defined]

    return ck._embed_texts(texts)


# ─────────────────────────────────────────────────────────────────────────────
# Metadata parsing (YAML front-matter + JSON sidecar)
# ─────────────────────────────────────────────────────────────────────────────
_FRONT_MATTER_RE = re.compile(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split leading ``---`` YAML front-matter from the body. Returns (meta, body)."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: Dict[str, Any] = {}
    try:
        import yaml  # PyYAML is available in the runtime; degrade if not.

        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            meta = parsed
    except Exception:
        meta = _parse_simple_kv(raw)
    # Only treat the leading region as front-matter if it actually parsed to
    # metadata. Otherwise a document that merely opens with a '---' rule (and has
    # another '---' later) would have its real body silently truncated.
    if not meta:
        return {}, text
    return meta, body


def _parse_simple_kv(raw: str) -> Dict[str, Any]:
    """Minimal ``key: value`` fallback parser when PyYAML is unavailable."""
    meta: Dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta


def _load_sidecar(path: Path) -> Dict[str, Any]:
    """Load a JSON metadata sidecar if one sits next to the file."""
    candidates = [
        path.with_name(path.stem + ".meta.json"),
        path.with_name(path.stem + ".json"),
        Path(str(path) + ".meta.json"),
    ]
    for cand in candidates:
        if cand.exists() and cand != path:
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as e:  # pragma: no cover - corrupt sidecar
                logger.warning("Could not parse sidecar %s: %s", cand, e)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Chunking (heading + paragraph aware, char-target with overlap)
# ─────────────────────────────────────────────────────────────────────────────
def _first_heading(body: str) -> Optional[str]:
    for line in body.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            return m.group(1).strip()
        if line.strip():
            return None
    return None


def _split_sections(body: str) -> List[Tuple[Optional[str], str]]:
    """Break a document into (heading, section_body) pairs on markdown headings."""
    sections: List[Tuple[Optional[str], str]] = []
    heading: Optional[str] = None
    buf: List[str] = []
    for line in body.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            if heading is not None or "".join(buf).strip():
                sections.append((heading, "\n".join(buf).strip()))
            heading = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if heading is not None or "".join(buf).strip():
        sections.append((heading, "\n".join(buf).strip()))
    return sections


def _word_wrap(text: str, target: int) -> List[str]:
    """Hard-wrap an over-long unit on word boundaries."""
    words = text.split()
    out, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > target:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


def _chunk_text(text: str, target: int, overlap: int) -> List[str]:
    """Pack paragraphs into ~``target``-char chunks with a small trailing overlap."""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []
    if len(text) <= target:
        return [text]

    # Break into units no larger than target (paragraphs, then sentences, then words).
    units: List[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        if len(para) <= target:
            units.append(para)
            continue
        for sent in re.split(r"(?<=[.;])\s+", para):
            sent = sent.strip()
            if not sent:
                continue
            units.extend([sent] if len(sent) <= target else _word_wrap(sent, target))

    chunks: List[str] = []
    buf = ""
    for unit in units:
        if buf and len(buf) + 2 + len(unit) > target:
            chunks.append(buf)
            tail = buf[-overlap:]
            sp = tail.find(" ")
            tail = tail[sp + 1:] if sp != -1 else ""
            buf = f"{tail} {unit}".strip() if tail else unit
        else:
            buf = f"{buf}\n\n{unit}".strip() if buf else unit
    if buf:
        chunks.append(buf)
    return chunks


def chunk_markdown(
    body: str,
    target: int = CHUNK_TARGET_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> List[str]:
    """Split a document body into overlapping chunks, prefixing each with its heading."""
    sections = _split_sections(body) or [(None, body)]
    chunks: List[str] = []
    for heading, sec_body in sections:
        pieces = _chunk_text(sec_body, target, overlap)
        if not pieces and heading:
            pieces = [heading]  # heading-only section still worth indexing
        for piece in pieces:
            prefix = f"{heading}\n\n" if heading else ""
            content = f"{prefix}{piece}".strip()
            if content:
                chunks.append(content)
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Store schema (mirrors clinical_knowledge.init_knowledge_base exactly)
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_store(db) -> Tuple[bool, bool]:
    """Create the shared retrieval tables (if absent) + loader bookkeeping table.

    Returns (fts_available, vec_available).
    """
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS clinical_knowledge (
            id INTEGER PRIMARY KEY,
            document_id TEXT,
            chunk_index INTEGER,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'external_guideline',
            source_origin TEXT NOT NULL DEFAULT 'external',
            trust_tier INTEGER NOT NULL DEFAULT 3,
            specialty TEXT,
            effective_date TEXT,
            version TEXT,
            is_synthetic BOOLEAN DEFAULT 0
        )
        """
    )

    fts_available = False
    try:
        db.enable_load_extension(True)
        cur.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS clinical_knowledge_fts USING fts5(
                title, content, source, category, specialty,
                content=clinical_knowledge, content_rowid=id
            )
            """
        )
        fts_available = True
    except Exception as e:
        logger.warning("FTS5 unavailable (%s); lexical search will use LIKE fallback", e)

    vec_available = False
    try:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        cur.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS clinical_knowledge_vec USING vec0(
                embedding float[{EMBEDDING_DIM}]
            )
            """
        )
        vec_available = True
    except Exception as e:
        logger.warning("sqlite-vec unavailable (%s); dense retrieval disabled", e)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS corpus_documents (
            document_id TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            trust_tier INTEGER,
            version TEXT,
            source_path TEXT,
            content_hash TEXT,
            chunk_count INTEGER DEFAULT 0,
            embedded INTEGER DEFAULT 0,
            loaded_at TEXT,
            approval_status TEXT NOT NULL DEFAULT 'draft',
            approved_by TEXT,
            approved_at TEXT
        )
        """
    )
    # Migration: older DBs created before the approval workflow lack these columns.
    # Add them in-place (loader owns this table — no touching database.py).
    _existing = {r[1] for r in cur.execute("PRAGMA table_info(corpus_documents)").fetchall()}
    for col, ddl in (
        ("approval_status", "approval_status TEXT NOT NULL DEFAULT 'draft'"),
        ("approved_by", "approved_by TEXT"),
        ("approved_at", "approved_at TEXT"),
    ):
        if col not in _existing:
            cur.execute(f"ALTER TABLE corpus_documents ADD COLUMN {ddl}")
    return fts_available, vec_available


def _delete_document(db, document_id: str, fts: bool, vec: bool) -> None:
    """Remove every chunk of a document from base + FTS + vec tables (idempotent replace).

    The FTS table is external-content, so rows are removed with the special
    'delete' command supplying the OLD indexed values (a plain base-table DELETE
    would leave the FTS index stale).
    """
    rows = db.execute(
        "SELECT id, title, content, source, category, specialty "
        "FROM clinical_knowledge WHERE document_id = ?",
        (document_id,),
    ).fetchall()
    for rid, title, content, source, category, specialty in rows:
        if fts:
            try:
                db.execute(
                    "INSERT INTO clinical_knowledge_fts"
                    "(clinical_knowledge_fts, rowid, title, content, source, category, specialty) "
                    "VALUES ('delete', ?, ?, ?, ?, ?, ?)",
                    (rid, title, content, source, category, specialty),
                )
            except Exception as e:  # pragma: no cover
                logger.warning("FTS delete failed for rowid %s: %s", rid, e)
        if vec:
            try:
                db.execute("DELETE FROM clinical_knowledge_vec WHERE rowid = ?", (rid,))
            except Exception as e:  # pragma: no cover
                logger.warning("vec delete failed for rowid %s: %s", rid, e)
        db.execute("DELETE FROM clinical_knowledge WHERE id = ?", (rid,))
    db.execute("DELETE FROM corpus_documents WHERE document_id = ?", (document_id,))


# ─────────────────────────────────────────────────────────────────────────────
# Metadata resolution
# ─────────────────────────────────────────────────────────────────────────────
_SEED_IDS_CACHE: Optional[set] = None


def _seed_document_ids() -> set:
    """Document ids of the built-in seed corpus (so loader ids can't collide with
    and delete them). Cached; degrades to empty set if introspection fails."""
    global _SEED_IDS_CACHE
    if _SEED_IDS_CACHE is None:
        try:
            import services.clinical_knowledge as ck
            _SEED_IDS_CACHE = {
                str(d.get("document_id")) for d in getattr(ck, "CLINICAL_DOCUMENTS", [])
                if d.get("document_id")
            }
        except Exception:
            _SEED_IDS_CACHE = set()
    return _SEED_IDS_CACHE


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-").upper()
    return s or "DOC"


def _resolve_metadata(
    path: Path, meta: Dict[str, Any], body: str, default_trust_tier: int
) -> Dict[str, Any]:
    stem = path.stem
    try:
        trust_tier = int(meta.get("trust_tier", default_trust_tier) or default_trust_tier)
    except (TypeError, ValueError):
        trust_tier = default_trust_tier

    title = (
        str(meta.get("title")).strip()
        if meta.get("title")
        else (_first_heading(body) or stem.replace("-", " ").replace("_", " ").title())
    )
    document_id = str(meta.get("document_id") or _slug(stem))
    source = str(meta.get("source") or title)
    source_type = str(meta.get("source_type") or _TIER_SOURCE_TYPE.get(trust_tier, "external_guideline"))
    source_origin = str(meta.get("source_origin") or ("demo_local" if trust_tier == 1 else "external"))
    return {
        "document_id": document_id,
        "title": title,
        "source": source,
        "source_type": source_type,
        "source_origin": source_origin,
        "trust_tier": trust_tier,
        "category": str(meta.get("category") or "Clinical Guideline"),
        "specialty": (str(meta["specialty"]) if meta.get("specialty") else None),
        "effective_date": (str(meta["effective_date"]) if meta.get("effective_date") else None),
        "version": str(meta.get("version", "v1")),
        "is_synthetic": bool(meta.get("is_synthetic", False)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def load_file(db, path: Path, default_trust_tier: int, fts: bool, vec: bool, embed: bool) -> Dict[str, Any]:
    """Parse + chunk + (re-)insert a single guideline file. Returns a per-file summary."""
    text = path.read_text(encoding="utf-8")
    front_meta, body = _parse_front_matter(text)
    combined: Dict[str, Any] = {}
    combined.update(_load_sidecar(path))  # sidecar first…
    combined.update(front_meta)           # …front-matter wins
    md = _resolve_metadata(path, combined, body, default_trust_tier)

    # Safety: never let a loader document_id collide with a built-in seed
    # document id — _delete_document deletes by id from the SHARED store, so a
    # collision would wipe the seed corpus. Namespace any collision.
    if md["document_id"] in _seed_document_ids():
        md["document_id"] = f"CORPUS-{md['document_id']}"

    # Preserve an existing approval decision across an idempotent re-load: capture
    # the prior approval row BEFORE _delete_document wipes the bookkeeping entry, so
    # re-loading an unchanged approved doc does not silently revert it to 'draft'.
    prior_approval = _get_approval_row(db, md["document_id"])

    # Chunk AND embed BEFORE the first write. _delete_document's DELETEs open
    # sqlite's implicit write transaction; embedding after them parked the WAL
    # write lock for the whole (GPU-contended, potentially minutes-long) BGE-M3
    # pass, starving every other writer in the server process.
    chunks = chunk_markdown(body)

    embeddings: Optional[List[bytes]] = None
    if chunks and embed and vec:
        composites = [
            f"Title: {md['title']}\nCategory: {md['category']}\nSource: {md['source']}\nContent: {c}"
            for c in chunks
        ]
        try:
            embeddings = _embed_composites(composites)
        except Exception as e:
            logger.warning("Embedding failed for %s (%s); storing lexical-only", path.name, e)
            embeddings = None

    # Idempotent replace: clear any prior copy of this document FIRST (before the
    # empty-chunk check) so re-loading a now-empty file still purges its old chunks.
    _delete_document(db, md["document_id"], fts, vec)

    if not chunks:
        logger.warning("No content chunks produced for %s; skipping", path.name)
        return {"document_id": md["document_id"], "file": path.name, "chunks": 0, "embedded": False}

    doc_embedded = embeddings is not None
    for idx, chunk in enumerate(chunks):
        cur = db.execute(
            """
            INSERT INTO clinical_knowledge
                (document_id, chunk_index, category, title, content, source, source_type,
                 source_origin, trust_tier, specialty, effective_date, version, is_synthetic)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                md["document_id"], idx, md["category"], md["title"], chunk, md["source"],
                md["source_type"], md["source_origin"], md["trust_tier"], md["specialty"],
                md["effective_date"], md["version"], int(md["is_synthetic"]),
            ),
        )
        rowid = cur.lastrowid
        if doc_embedded and vec:
            db.execute(
                "INSERT INTO clinical_knowledge_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, embeddings[idx]),
            )
        if fts:
            db.execute(
                "INSERT INTO clinical_knowledge_fts (rowid, title, content, source, category, specialty) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rowid, md["title"], chunk, md["source"], md["category"], md["specialty"]),
            )

    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    # Approval status: keep any prior decision on reload; otherwise apply the
    # per-source default (tier-1 hospital protocols are pre-approved; everything
    # else the loader adds starts as 'draft' pending human sign-off).
    if prior_approval is not None:
        approval_status, approved_by, approved_at = prior_approval
    else:
        approval_status = _default_approval_status(md)
        approved_by = "loader:auto" if approval_status == "approved" else None
        approved_at = datetime.now(timezone.utc).isoformat() if approval_status == "approved" else None
    db.execute(
        """
        INSERT OR REPLACE INTO corpus_documents
            (document_id, title, source, trust_tier, version, source_path, content_hash,
             chunk_count, embedded, loaded_at, approval_status, approved_by, approved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            md["document_id"], md["title"], md["source"], md["trust_tier"], md["version"],
            str(path), content_hash, len(chunks), int(doc_embedded),
            datetime.now(timezone.utc).isoformat(), approval_status, approved_by, approved_at,
        ),
    )
    return {
        "document_id": md["document_id"],
        "file": path.name,
        "title": md["title"],
        "trust_tier": md["trust_tier"],
        "version": md["version"],
        "chunks": len(chunks),
        "embedded": doc_embedded,
        "approval_status": approval_status,
    }


def load_directory(path, default_trust_tier: int = 3) -> Dict[str, Any]:
    """Load every ``.md``/``.txt`` guideline file in ``path`` into the RAG store.

    Args:
        path: directory containing guideline files.
        default_trust_tier: trust tier applied to files that don't declare one
            (1=hospital protocol, 2=national guideline, 3=reference).

    Returns a summary dict: mode ("embedded"/"lexical"), files loaded, total chunks,
    per-file details, and a post-load :func:`corpus_stats` snapshot.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise NotADirectoryError(f"Corpus path is not a directory: {directory}")

    files = sorted(
        p for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower() in _SUPPORTED_SUFFIXES
        and not p.name.endswith((".meta.json", ".meta.md"))
    )

    embed = _real_embedder_available()
    mode = "embedded" if embed else "lexical"
    logger.info(
        "Loading %d guideline file(s) from %s in %s mode%s",
        len(files), directory, mode,
        "" if embed else " (BGE-M3/FlagEmbedding unavailable — FTS lexical index only)",
    )

    per_file: List[Dict[str, Any]] = []
    # One SHORT transaction per file instead of a single directory-wide one:
    # a whole-directory transaction held the write lock across every file's
    # embedding, wedging concurrent server writers for the full load.
    # _ensure_store must run on EVERY connection — it loads the sqlite-vec
    # extension, which is per-connection state; without it the vec0 virtual
    # table errors "no such module: vec0" on the fresh per-file sessions.
    with db_session() as db:
        fts, vec = _ensure_store(db)
    for f in files:
        try:
            with db_session() as db:
                fts, vec = _ensure_store(db)
                per_file.append(load_file(db, f, default_trust_tier, fts, vec, embed))
        except Exception as e:
            logger.error("Failed to load %s: %s", f.name, e)
            per_file.append({"file": f.name, "chunks": 0, "embedded": False, "error": str(e)})

    total_chunks = sum(d.get("chunks", 0) for d in per_file)
    loaded_ok = [d for d in per_file if d.get("chunks", 0) > 0]
    logger.info(
        "Loaded %d/%d file(s), %d chunk(s) in %s mode (fts=%s, vec=%s)",
        len(loaded_ok), len(files), total_chunks, mode, fts, vec,
    )
    return {
        "directory": str(directory),
        "mode": mode,
        "fts_available": fts,
        "vec_available": vec,
        "files_found": len(files),
        "files_loaded": len(loaded_ok),
        "chunks_loaded": total_chunks,
        "documents": per_file,
        "stats": corpus_stats(),
    }


def _table_exists(db, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def corpus_stats() -> Dict[str, Any]:
    """Return corpus counts: documents, chunks, embedded vs lexical-only.

    Counts reflect the entire ``clinical_knowledge`` store (built-in seed + loaded).
    A chunk is "embedded" when it has a row in the vector index; otherwise it is
    lexical-only (retrievable through FTS/BM25).
    """
    stats: Dict[str, Any] = {
        "documents": 0,
        "chunks": 0,
        "embedded_chunks": 0,
        "lexical_only_chunks": 0,
        "loader_documents": 0,
        "mode": "empty",
    }
    with db_session() as db:
        if not _table_exists(db, "clinical_knowledge"):
            return stats
        stats["chunks"] = db.execute("SELECT COUNT(*) FROM clinical_knowledge").fetchone()[0]
        stats["documents"] = db.execute(
            "SELECT COUNT(DISTINCT document_id) FROM clinical_knowledge"
        ).fetchone()[0]

        embedded = 0
        # Count vectors via the vec0 SHADOW table (plain storage, one row per
        # vector) — counting the virtual table itself raises "no such module:
        # vec0" on any connection without the sqlite-vec extension loaded,
        # which silently zeroed this stat (store looked "lexical" while fully
        # embedded). db_session() connections do not load extensions.
        if _table_exists(db, "clinical_knowledge_vec_rowids"):
            try:
                embedded = db.execute(
                    "SELECT COUNT(*) FROM clinical_knowledge_vec_rowids"
                ).fetchone()[0]
            except Exception:  # pragma: no cover
                embedded = 0
        elif _table_exists(db, "clinical_knowledge_vec"):
            try:
                embedded = db.execute("SELECT COUNT(*) FROM clinical_knowledge_vec").fetchone()[0]
            except Exception:  # pragma: no cover
                embedded = 0
        stats["embedded_chunks"] = embedded
        stats["lexical_only_chunks"] = max(0, stats["chunks"] - embedded)

        if _table_exists(db, "corpus_documents"):
            stats["loader_documents"] = db.execute(
                "SELECT COUNT(*) FROM corpus_documents"
            ).fetchone()[0]

        if stats["chunks"] == 0:
            stats["mode"] = "empty"
        elif embedded >= stats["chunks"]:
            stats["mode"] = "embedded"
        elif embedded == 0:
            stats["mode"] = "lexical"
        else:
            stats["mode"] = "mixed"
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Approval workflow (draft -> approved -> retired)
# ─────────────────────────────────────────────────────────────────────────────
# Loader-added documents start as 'draft'. A reviewer promotes them to 'approved'
# (via :func:`approve_document`); they can later be 'retired'. Tier-1 hospital
# protocols are pre-approved on load. Retrieval can restrict to approved documents
# with ``search_guidelines(..., approved_only=True)``.
_APPROVAL_STATUSES = ("draft", "approved", "retired")


def _default_approval_status(md: Dict[str, Any]) -> str:
    """Per-source default status for a freshly-loaded document.

    Tier-1 hospital protocols are governed locally and trusted, so they load
    pre-approved. Everything else the loader ingests starts as 'draft' and must be
    signed off before it can be surfaced under ``approved_only`` retrieval.
    """
    if int(md.get("trust_tier", 3) or 3) == 1 and str(md.get("source_type", "")).startswith("hospital"):
        return "approved"
    return "draft"


def _get_approval_row(db, document_id: str):
    """Return (approval_status, approved_by, approved_at) for a doc, or None.

    Tolerates a pre-migration table that lacks the approval columns.
    """
    if not _table_exists(db, "corpus_documents"):
        return None
    cols = {r[1] for r in db.execute("PRAGMA table_info(corpus_documents)").fetchall()}
    if "approval_status" not in cols:
        return None
    row = db.execute(
        "SELECT approval_status, approved_by, approved_at FROM corpus_documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def set_document_status(document_id: str, status: str, actor: Optional[str] = None) -> Dict[str, Any]:
    """Set a document's approval_status. `status` must be one of _APPROVAL_STATUSES.

    Returns a summary dict with ``updated`` True/False. Raises ValueError for an
    unknown status.
    """
    if status not in _APPROVAL_STATUSES:
        raise ValueError(f"Invalid approval status {status!r}; expected one of {_APPROVAL_STATUSES}")
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        _ensure_bookkeeping(db)
        exists = db.execute(
            "SELECT 1 FROM corpus_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if not exists:
            return {"document_id": document_id, "updated": False, "reason": "not found"}
        if status == "approved":
            db.execute(
                "UPDATE corpus_documents SET approval_status = ?, approved_by = ?, approved_at = ? "
                "WHERE document_id = ?",
                (status, actor, now, document_id),
            )
        else:
            db.execute(
                "UPDATE corpus_documents SET approval_status = ? WHERE document_id = ?",
                (status, document_id),
            )
        db.commit()
    return {"document_id": document_id, "updated": True, "approval_status": status, "actor": actor}


def approve_document(document_id: str, approver: str) -> Dict[str, Any]:
    """Approve a loaded document so it is surfaced under ``approved_only`` retrieval.

    Records the approver and an approval timestamp. Returns a summary dict.
    """
    return set_document_status(document_id, "approved", actor=approver)


def retire_document(document_id: str, actor: Optional[str] = None) -> Dict[str, Any]:
    """Retire a document (excluded from ``approved_only`` retrieval)."""
    return set_document_status(document_id, "retired", actor=actor)


def list_documents(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List loader-tracked documents, optionally filtered by approval_status.

    Returns one dict per row in ``corpus_documents`` (empty list if the table does
    not exist yet). `status`, if given, must be a valid approval status.
    """
    if status is not None and status not in _APPROVAL_STATUSES:
        raise ValueError(f"Invalid approval status {status!r}; expected one of {_APPROVAL_STATUSES}")
    out: List[Dict[str, Any]] = []
    with db_session() as db:
        if not _table_exists(db, "corpus_documents"):
            return out
        _ensure_bookkeeping(db)
        cols = [r[1] for r in db.execute("PRAGMA table_info(corpus_documents)").fetchall()]
        sql = "SELECT * FROM corpus_documents"
        params: Tuple = ()
        if status is not None:
            sql += " WHERE approval_status = ?"
            params = (status,)
        sql += " ORDER BY loaded_at DESC"
        for row in db.execute(sql, params).fetchall():
            out.append(dict(zip(cols, row)))
    return out


def _ensure_bookkeeping(db) -> None:
    """Ensure corpus_documents exists with the approval columns (migration-safe).

    A lightweight subset of _ensure_store for approval APIs that may run before any
    load (so they never touch the shared retrieval/vec tables).
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS corpus_documents (
            document_id TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            trust_tier INTEGER,
            version TEXT,
            source_path TEXT,
            content_hash TEXT,
            chunk_count INTEGER DEFAULT 0,
            embedded INTEGER DEFAULT 0,
            loaded_at TEXT,
            approval_status TEXT NOT NULL DEFAULT 'draft',
            approved_by TEXT,
            approved_at TEXT
        )
        """
    )
    existing = {r[1] for r in db.execute("PRAGMA table_info(corpus_documents)").fetchall()}
    for col, ddl in (
        ("approval_status", "approval_status TEXT NOT NULL DEFAULT 'draft'"),
        ("approved_by", "approved_by TEXT"),
        ("approved_at", "approved_at TEXT"),
    ):
        if col not in existing:
            db.execute(f"ALTER TABLE corpus_documents ADD COLUMN {ddl}")
