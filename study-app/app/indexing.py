"""Chunk documents for local full-text and optional semantic retrieval."""

import hashlib
import json
import math
import re
import sqlite3
import struct
from functools import lru_cache
from datetime import datetime, timezone
from uuid import uuid4

from app.database import get_connection, get_document, get_document_pages
from app.ollama import EMBED_MODEL_NAME, embed_texts


CHUNKER_VERSION = "page-char-v1"
MAX_CHUNK_CHARS = 1500
OVERLAP_CHARS = 180
EMBED_BATCH_SIZE = 16


def _now():
    return datetime.now(timezone.utc).isoformat()


def _split_page(page_number: int, text: str) -> list[dict]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + MAX_CHUNK_CHARS)
        if end < len(text):
            boundary = max(text.rfind(" ", start + int(MAX_CHUNK_CHARS * 0.65), end),
                           text.rfind(".", start + int(MAX_CHUNK_CHARS * 0.65), end),
                           text.rfind(";", start + int(MAX_CHUNK_CHARS * 0.65), end))
            if boundary > start:
                end = boundary + 1
        content = text[start:end].strip()
        if content:
            chunks.append({"page": page_number, "content": content})
        if end >= len(text):
            break
        start = max(start + 1, end - OVERLAP_CHARS)
        while start < len(text) and text[start].isspace():
            start += 1
    return chunks


def _make_chunks(pages: list[dict]) -> list[dict]:
    chunks = []
    for page in pages:
        page_number = int(page.get("page_number", page.get("page", 0)) or 0)
        text = page.get("page_text", page.get("text", "")) or ""
        if page_number < 1:
            continue
        chunks.extend(_split_page(page_number, text))
    for index, chunk in enumerate(chunks):
        chunk["chunk_index"] = index
        chunk["content_sha256"] = hashlib.sha256(
            chunk["content"].encode("utf-8")
        ).hexdigest()
    return chunks


def _pack_vector(vector: list[float]) -> bytes:
    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("Embedding contains invalid numeric values.")
    return struct.pack("<" + "f" * len(values), *values)


def unpack_vector(blob: bytes, dimensions: int) -> list[float]:
    if dimensions <= 0 or len(blob) != dimensions * 4:
        return []
    return list(struct.unpack("<" + "f" * dimensions, blob))


@lru_cache(maxsize=128)
def _cached_query_embedding(query: str, model: str) -> tuple[float, ...]:
    """Reuse recent query vectors in memory; document data remains unchanged."""
    return tuple(embed_texts([query], model=model)[0])


def _create_or_resume_chunks(document_id: str, chunks: list[dict]) -> list[dict]:
    now = _now()
    with get_connection() as db:
        existing = db.execute("""
            SELECT id, chunk_index, content_sha256, start_page, end_page,
                   metadata_json
            FROM document_chunks WHERE document_id=? ORDER BY chunk_index
        """, (document_id,)).fetchall()
        if existing:
            if len(existing) != len(chunks):
                raise RuntimeError(
                    "A previous chunk set differs from the current document. "
                    "It was left untouched; use a future explicit reindex action."
                )
            resolved = []
            for row, chunk in zip(existing, chunks):
                metadata = json.loads(row["metadata_json"] or "{}")
                if (row["content_sha256"] != chunk["content_sha256"]
                        or row["start_page"] != chunk["page"]
                        or row["end_page"] != chunk["page"]
                        or metadata.get("chunker") != CHUNKER_VERSION):
                    raise RuntimeError(
                        "A previous chunk differs from the current document. "
                        "No existing chunk data was changed."
                    )
                resolved.append({**chunk, "id": row["id"]})
        else:
            resolved = []
            inserts = []
            for chunk in chunks:
                chunk_id = str(uuid4())
                inserts.append((
                    chunk_id, document_id, chunk["chunk_index"], chunk["page"],
                    chunk["page"], chunk["content"], chunk["content_sha256"],
                    len(chunk["content"].split()),
                    json.dumps({"chunker": CHUNKER_VERSION, "page": chunk["page"]}),
                    now, now,
                ))
                resolved.append({**chunk, "id": chunk_id})
            db.executemany("""
                    INSERT INTO document_chunks(
                        id, document_id, chunk_index, start_page, end_page,
                        content, content_sha256, token_count, status,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """, inserts)

        indexed_ids = {row[0] for row in db.execute(
            "SELECT chunk_id FROM chunk_fts WHERE document_id=?", (document_id,)
        )}
        fts_inserts = [
            (chunk["id"], document_id, chunk["content"])
            for chunk in resolved if chunk["id"] not in indexed_ids
        ]
        if fts_inserts:
            db.executemany("""
                INSERT INTO chunk_fts(chunk_id, document_id, content)
                VALUES (?, ?, ?)
            """, fts_inserts)
        db.executemany("""
            UPDATE document_chunks SET status='completed', updated_at=?
            WHERE id=?
        """, [(now, chunk["id"]) for chunk in resolved])
    return resolved


def _reuse_unchanged_chunks(document_id: str, content_sha256: str | None) -> list[dict] | None:
    """Reuse saved passages when the document and chunker match the last run."""
    if not content_sha256:
        return None
    with get_connection() as db:
        run = db.execute("""
            SELECT config_json FROM processing_runs
            WHERE document_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1
        """, (document_id,)).fetchone()
        if run is None:
            return None
        try:
            config = json.loads(run["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if (config.get("content_sha256") != content_sha256
                or config.get("chunker_version") != CHUNKER_VERSION):
            return None
        rows = db.execute("""
            SELECT id, chunk_index, start_page, content, content_sha256
            FROM document_chunks WHERE document_id=? ORDER BY chunk_index
        """, (document_id,)).fetchall()
        if len(rows) != config.get("chunk_count") or not rows:
            return None
        fts_count = db.execute(
            "SELECT COUNT(*) FROM chunk_fts WHERE document_id=?", (document_id,)
        ).fetchone()[0]
        if fts_count != len(rows):
            return None
    return [{
        "id": row["id"],
        "chunk_index": row["chunk_index"],
        "page": row["start_page"],
        "content": row["content"],
        "content_sha256": row["content_sha256"],
    } for row in rows]


def index_document(document_id: str, embedding_model: str | None = None) -> dict:
    document = get_document(document_id)
    if document is None:
        raise LookupError("Document not found.")
    with get_connection() as db:
        duplicate = db.execute(
            "SELECT canonical_document_id FROM document_duplicates WHERE document_id=?",
            (document_id,),
        ).fetchone()
    if duplicate:
        return index_document(duplicate["canonical_document_id"], embedding_model)
    resolved = _reuse_unchanged_chunks(document_id, document.get("content_sha256"))
    if resolved is None:
        pages = get_document_pages(document_id)
        if not pages and document.get("full_text"):
            pages = [{"page_number": 1, "page_text": document["full_text"]}]
        chunks = _make_chunks(pages)
        if not chunks:
            raise ValueError("No extracted text is available to index.")
        resolved = _create_or_resume_chunks(document_id, chunks)
    model = embedding_model or EMBED_MODEL_NAME
    run_id = str(uuid4())
    now = _now()

    with get_connection() as db:
        embedded_ids = {row[0] for row in db.execute("""
            SELECT c.id
            FROM document_chunks c
            JOIN chunk_embeddings e ON e.chunk_id=c.id
            WHERE c.document_id=? AND e.model_name=?
        """, (document_id, model))}
        missing = [chunk for chunk in resolved if chunk["id"] not in embedded_ids]
        db.execute("""
            INSERT INTO processing_runs(
                id, document_id, pipeline_version, status, config_json, created_at
            ) VALUES (?, ?, ?, 'processing', ?, ?)
        """, (run_id, document_id, CHUNKER_VERSION,
              json.dumps({
                  "embedding_model": model,
                  "chunk_count": len(resolved),
                  "content_sha256": document.get("content_sha256"),
                  "chunker_version": CHUNKER_VERSION,
              }), now))
        if missing:
            db.executemany("""
                INSERT INTO processing_run_chunks(run_id, chunk_id, status, attempts, started_at)
                VALUES (?, ?, 'processing', 1, ?)
            """, [(run_id, chunk["id"], now) for chunk in missing])

    embedding_error = None
    embedded_count = 0
    for offset in range(0, len(missing), EMBED_BATCH_SIZE):
        batch = missing[offset:offset + EMBED_BATCH_SIZE]
        try:
            vectors = embed_texts([item["content"] for item in batch], model=model)
            if len(vectors) != len(batch):
                raise RuntimeError("Embedding count did not match the chunk batch.")
            packed = [_pack_vector(vector) for vector in vectors]
            dimensions = [len(vector) for vector in vectors]
            if len(set(dimensions)) != 1:
                raise RuntimeError("Embedding dimensions changed within the batch.")
            completed_at = _now()
            with get_connection() as db:
                db.executemany("""
                    INSERT INTO chunk_embeddings(
                        chunk_id, model_name, dimensions, embedding, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        model_name=excluded.model_name,
                        dimensions=excluded.dimensions,
                        embedding=excluded.embedding,
                        created_at=excluded.created_at
                """, [
                    (item["id"], model, len(vector), blob, completed_at)
                    for item, vector, blob in zip(batch, vectors, packed)
                ])
                db.executemany("""
                    UPDATE processing_run_chunks SET status='completed', completed_at=?
                    WHERE run_id=? AND chunk_id=?
                """, [(completed_at, run_id, item["id"]) for item in batch])
            embedded_count += len(batch)
        except Exception as error:
            embedding_error = str(error)
            failed_at = _now()
            with get_connection() as db:
                db.executemany("""
                    UPDATE processing_run_chunks
                    SET status='failed', error_message=?, completed_at=?
                    WHERE run_id=? AND chunk_id=?
                """, [
                    (embedding_error[:1000], failed_at, run_id, item["id"])
                    for item in missing[offset:]
                ])
            break

    with get_connection() as db:
        run_status = "failed" if embedding_error else "completed"
        db.execute("""
            UPDATE processing_runs SET status=?, error_message=?, completed_at=?
            WHERE id=?
        """, (run_status, embedding_error, _now(), run_id))
        embedded_total = db.execute("""
            SELECT COUNT(*) FROM chunk_embeddings e
            JOIN document_chunks c ON c.id=e.chunk_id
            WHERE c.document_id=? AND e.model_name=?
        """, (document_id, model)).fetchone()[0]

    return {
        "success": True,
        "document_id": document_id,
        "chunks_indexed": len(resolved),
        "embedded_chunks": embedded_total,
        "embedding_model": model,
        "mode": "hybrid" if embedded_total else "keyword",
        "warning": embedding_error,
        "resumable": bool(embedding_error),
    }


def get_index_progress(document_id: str) -> dict:
    """Read progress for the latest indexing run without changing stored data."""
    with get_connection() as db:
        duplicate = db.execute(
            "SELECT canonical_document_id FROM document_duplicates WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if duplicate:
            document_id = duplicate["canonical_document_id"]
        run = db.execute("""
            SELECT id, status, error_message, created_at
            FROM processing_runs
            WHERE document_id=?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
        """, (document_id,)).fetchone()
        if run is None:
            return {"status": "not_started", "document_id": document_id,
                    "completed_chunks": 0, "failed_chunks": 0, "total_chunks": 0}
        counts = db.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
            FROM processing_run_chunks WHERE run_id=?
        """, (run["id"],)).fetchone()
    return {
        "status": run["status"],
        "document_id": document_id,
        "completed_chunks": counts["completed"] or 0,
        "failed_chunks": counts["failed"] or 0,
        "total_chunks": counts["total"] or 0,
        "error": run["error_message"],
    }


def _fts_query(query: str) -> str:
    tokens = list(dict.fromkeys(re.findall(r"[\w]+", query, flags=re.UNICODE)))[:12]
    return " OR ".join(f'"{token}"*' for token in tokens)


def _excerpt(text: str, query: str, limit: int = 480) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
    lowered = text.casefold()
    positions = [lowered.find(term.casefold()) for term in terms if lowered.find(term.casefold()) >= 0]
    start = max(0, (min(positions) if positions else 0) - limit // 3)
    end = min(len(text), start + limit)
    excerpt = text[start:end]
    if start:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt += "…"
    return excerpt


def search_chunks(query: str, document_ids: list[str], limit: int = 8,
                  embedding_model: str | None = None) -> dict:
    query = query.strip()
    document_ids = list(dict.fromkeys(document_ids))
    if not query:
        raise ValueError("Enter a search question or phrase.")
    if not document_ids:
        return {"results": [], "mode": "keyword", "embedding_error": None}
    limit = max(1, min(int(limit), 20))
    placeholders = ",".join("?" for _ in document_ids)
    model = embedding_model or EMBED_MODEL_NAME
    fts_query = _fts_query(query)

    with get_connection() as db:
        lexical_rows = []
        if fts_query:
            lexical_rows = db.execute(f"""
                SELECT c.id AS chunk_id, c.document_id, c.chunk_index,
                       c.start_page, c.end_page, c.content,
                       d.original_filename,
                       bm25(chunk_fts) AS rank
                FROM chunk_fts
                JOIN document_chunks c ON c.id=chunk_fts.chunk_id
                JOIN documents d ON d.id=c.document_id
                WHERE chunk_fts MATCH ? AND c.document_id IN ({placeholders})
                ORDER BY rank LIMIT ?
            """, (fts_query, *document_ids, limit * 4)).fetchall()
        vector_rows = db.execute(f"""
            SELECT c.id AS chunk_id, c.document_id, e.dimensions, e.embedding
            FROM chunk_embeddings e
            JOIN document_chunks c ON c.id=e.chunk_id
            WHERE e.model_name=? AND c.document_id IN ({placeholders})
        """, (model, *document_ids)).fetchall()

    vector_results = []
    embedding_error = None
    if vector_rows:
        try:
            query_vector = _pack_vector(list(_cached_query_embedding(query, model)))
            dimensions = len(query_vector) // 4
            query_values = unpack_vector(query_vector, dimensions)
            qnorm = math.sqrt(sum(value * value for value in query_values)) or 1.0
            for row in vector_rows:
                if row["dimensions"] != dimensions:
                    continue
                values = unpack_vector(row["embedding"], row["dimensions"])
                norm = math.sqrt(sum(value * value for value in values)) or 1.0
                cosine = sum(a * b for a, b in zip(query_values, values)) / (qnorm * norm)
                vector_results.append((cosine, row["chunk_id"], row["document_id"]))
            vector_results.sort(key=lambda item: item[0], reverse=True)
        except Exception as error:
            embedding_error = str(error)

    ranked = {}
    for rank, row in enumerate(lexical_rows, start=1):
        ranked[row["chunk_id"]] = {
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "score": 1 / (60 + rank),
            "lexical_rank": rank,
        }
    for rank, (cosine, chunk_id, document_id) in enumerate(vector_results, start=1):
        item = ranked.setdefault(chunk_id, {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "score": 0,
            "lexical_rank": None,
        })
        item["score"] += 1 / (60 + rank)
        item["semantic_score"] = round(cosine, 4)
    candidates = sorted(ranked.values(), key=lambda item: item["score"], reverse=True)
    ordered = []
    represented = set()
    for item in candidates:
        document_id = item["document_id"]
        if document_id not in represented:
            ordered.append(item)
            represented.add(document_id)
            if len(ordered) >= limit:
                break
    if len(ordered) < limit:
        selected_chunks = {item["chunk_id"] for item in ordered}
        ordered.extend(
            item for item in candidates
            if item["chunk_id"] not in selected_chunks
        )
        ordered = ordered[:limit]
    selected_ids = [item["chunk_id"] for item in ordered]
    selected_placeholders = ",".join("?" for _ in selected_ids)
    with get_connection() as db:
        selected_rows = db.execute(f"""
            SELECT c.id AS chunk_id, c.document_id, c.start_page, c.end_page,
                   c.content, d.original_filename
            FROM document_chunks c
            JOIN documents d ON d.id=c.document_id
            WHERE c.id IN ({selected_placeholders})
        """, selected_ids).fetchall() if selected_ids else []
    selected_by_id = {row["chunk_id"]: row for row in selected_rows}
    results = []
    for item in ordered:
        row = selected_by_id.get(item["chunk_id"])
        if row is None:
            continue
        results.append({
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "filename": row["original_filename"],
            "start_page": row["start_page"],
            "end_page": row["end_page"],
            "content": row["content"],
            "excerpt": _excerpt(row["content"], query),
            "score": round(item["score"], 6),
            "semantic_score": item.get("semantic_score"),
        })
    return {
        "results": results,
        "mode": "hybrid" if vector_results else "keyword",
        "embedding_error": embedding_error,
    }


def sample_chunks(document_ids: list[str], limit: int = 12) -> list[dict]:
    """Select representative early passages from every selected document."""
    document_ids = list(dict.fromkeys(document_ids))
    if not document_ids:
        return []
    limit = max(1, min(int(limit), 20))
    placeholders = ",".join("?" for _ in document_ids)
    with get_connection() as db:
        rows = db.execute(f"""
            SELECT chunk_id, document_id, chunk_index, start_page, end_page,
                   content, original_filename
            FROM (
                SELECT c.id AS chunk_id, c.document_id, c.chunk_index,
                       c.start_page, c.end_page, c.content, d.original_filename,
                       ROW_NUMBER() OVER (
                           PARTITION BY c.document_id ORDER BY c.chunk_index
                       ) AS document_position,
                       COUNT(*) OVER (PARTITION BY c.document_id) AS document_total
                FROM document_chunks c
                JOIN documents d ON d.id=c.document_id
                WHERE c.document_id IN ({placeholders})
            )
            WHERE document_position IN (
                1,
                MAX(1, CAST((document_total + 2) / 3 AS INTEGER)),
                MAX(1, CAST((2 * document_total + 2) / 3 AS INTEGER)),
                document_total
            )
            ORDER BY document_position, document_id
            LIMIT ?
        """, (*document_ids, limit)).fetchall()
    return [{
        "chunk_id": row["chunk_id"],
        "document_id": row["document_id"],
        "filename": row["original_filename"],
        "start_page": row["start_page"],
        "end_page": row["end_page"],
        "content": row["content"],
        "excerpt": _excerpt(row["content"], ""),
        "score": 0,
        "semantic_score": None,
    } for row in rows]
