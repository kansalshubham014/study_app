"""Database operations for collections and saved study material."""

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.database import get_connection, get_document_by_hash, save_document
from app.document_processor import extract_text


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = PROJECT_ROOT / "uploads"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"


def _now():
    return datetime.now(timezone.utc).isoformat()


def list_library():
    with get_connection() as db:
        documents = [dict(row) for row in db.execute("""
            SELECT d.id, d.original_filename, d.file_type, d.page_count,
                   d.character_count, d.created_at, d.content_sha256,
                   dd.canonical_document_id AS duplicate_of,
                   EXISTS(
                       SELECT 1 FROM document_analyses a WHERE a.document_id=d.id
                   ) OR EXISTS(
                       SELECT 1 FROM document_analyses a
                       WHERE a.document_id=dd.canonical_document_id
                   ) AS has_analysis,
                   (SELECT COUNT(*) FROM document_chunks c
                    WHERE c.document_id = d.id) AS chunk_count,
                   (SELECT COUNT(*) FROM chunk_embeddings e
                    JOIN document_chunks c ON c.id = e.chunk_id
                    WHERE c.document_id = d.id) AS embedded_chunk_count
            FROM documents d
            LEFT JOIN document_duplicates dd ON dd.document_id=d.id
            ORDER BY d.created_at DESC, d.id
        """)]
        collection_rows = db.execute("""
            SELECT cd.document_id, c.id, c.name
            FROM collection_documents cd JOIN collections c ON c.id = cd.collection_id
            ORDER BY lower(c.name), c.id
        """).fetchall()

    collections = {}
    for row in collection_rows:
        collections.setdefault(row["document_id"], []).append(
            {"id": row["id"], "name": row["name"]}
        )
    for document in documents:
        document["collections"] = collections.get(document["id"], [])
        document["indexed"] = document["chunk_count"] > 0
    return documents


def list_unregistered_uploads():
    with get_connection() as db:
        registered = {row[0] for row in db.execute(
            "SELECT stored_filename FROM documents"
        )}
    if not UPLOAD_DIR.exists():
        return []
    return [
        {"filename": path.name, "size": path.stat().st_size,
         "extension": path.suffix.lower()}
        for path in sorted(UPLOAD_DIR.iterdir(), key=lambda item: item.name.lower())
        if path.is_file() and path.suffix.lower() in {".pdf", ".docx"}
        and path.name not in registered
        and not path.name.startswith(".incoming-")
    ]


def register_existing_upload(filename: str):
    if not filename or Path(filename).name != filename:
        raise ValueError("Choose an uploaded file by its filename.")
    path = UPLOAD_DIR / filename
    if path.suffix.lower() not in {".pdf", ".docx"} or not path.is_file():
        raise LookupError("Uploaded file not found.")
    source_digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            source_digest.update(block)
    extraction = extract_text(str(path))
    full_text = extraction["full_text"].strip()
    if not full_text:
        raise ValueError("The uploaded file contains no selectable text.")
    digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    canonical = get_document_by_hash(digest)
    candidate_id = path.stem
    try:
        from uuid import UUID
        UUID(candidate_id)
    except (ValueError, TypeError, AttributeError):
        candidate_id = str(uuid4())
    with get_connection() as db:
        id_exists = db.execute("SELECT 1 FROM documents WHERE id=?", (candidate_id,)).fetchone()
    if id_exists:
        candidate_id = str(uuid4())

    save_document(
        document_id=candidate_id,
        original_filename=path.name,
        stored_filename=path.name,
        file_type=extraction["type"],
        pages=extraction["pages"],
        full_text=full_text,
        content_sha256=digest,
        source_sha256=source_digest.hexdigest(),
    )
    if canonical and canonical["id"] != candidate_id:
        with get_connection() as db:
            db.execute("""
                INSERT INTO document_duplicates(
                    document_id, canonical_document_id, content_sha256,
                    detection_method, created_at
                ) VALUES (?, ?, ?, 'identical_extracted_text', ?)
            """, (candidate_id, canonical["id"], digest, _now()))
    return {
        "document_id": candidate_id,
        "filename": path.name,
        "duplicate_of": canonical["id"] if canonical else None,
        "pages": len(extraction["pages"]),
    }


def _backup_before_document_delete(document_id: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    safe_id = "".join(char for char in document_id if char.isalnum() or char in "-_")[:80]
    backup_path = BACKUP_DIR / f"study_app_before_delete_{safe_id or 'document'}_{stamp}.db"
    with backup_path.open("xb"):
        pass
    source = sqlite3.connect(f"file:{(PROJECT_ROOT / 'data' / 'study_app.db').as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()

    check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Pre-delete backup failed integrity check: {integrity}")
        foreign_key_errors = check.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError("Pre-delete backup contains foreign-key violations.")
    finally:
        check.close()
    return backup_path


def delete_document(document_id: str) -> dict:
    """Delete one library item, preserving other duplicate records."""
    with get_connection() as db:
        existing = db.execute(
            "SELECT id FROM documents WHERE id=?", (document_id,)
        ).fetchone()
    if existing is None:
        raise LookupError("Document not found.")

    backup_path = _backup_before_document_delete(document_id)
    removed_materials = 0
    promoted_duplicate_id = None
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        document = db.execute(
            "SELECT id, stored_filename, content_sha256 FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise LookupError("Document no longer exists.")

        duplicates = db.execute("""
            SELECT document_id, content_sha256
            FROM document_duplicates
            WHERE canonical_document_id=?
            ORDER BY created_at, document_id
        """, (document_id,)).fetchall()
        if duplicates:
            if any(row["content_sha256"] != document["content_sha256"] for row in duplicates):
                raise RuntimeError("Duplicate records have inconsistent hashes; deletion was stopped.")
            promoted_duplicate_id = duplicates[0]["document_id"]
            # Promote one duplicate to canonical and point any remaining records to it.
            db.execute(
                "DELETE FROM document_duplicates WHERE document_id=?",
                (promoted_duplicate_id,),
            )
            db.execute("""
                UPDATE document_duplicates SET canonical_document_id=?
                WHERE canonical_document_id=?
            """, (promoted_duplicate_id, document_id))

        material_rows = db.execute(
            "SELECT id, scope_json, content_json FROM study_materials"
        ).fetchall()
        material_ids = []
        for row in material_rows:
            try:
                scope = json.loads(row["scope_json"] or "{}")
                content = json.loads(row["content_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            scoped_ids = scope.get("document_ids", []) if isinstance(scope, dict) else []
            if not isinstance(scoped_ids, list):
                scoped_ids = []
            source_rows = content.get("sources", []) if isinstance(content, dict) else []
            if not isinstance(source_rows, list):
                source_rows = []
            source_ids = [
                source.get("document_id")
                for source in source_rows
                if isinstance(source, dict)
            ]
            if document_id in scoped_ids or document_id in source_ids:
                material_ids.append(row["id"])
        if material_ids:
            db.executemany("DELETE FROM study_materials WHERE id=?",
                           [(material_id,) for material_id in material_ids])
            removed_materials = len(material_ids)

        # FTS5 is not linked by a foreign key, so remove its rows explicitly.
        db.execute("DELETE FROM chunk_fts WHERE document_id=?", (document_id,))
        db.execute("DELETE FROM documents WHERE id=?", (document_id,))
        db.commit()

    shared_file = False
    with get_connection() as db:
        shared_file = db.execute(
            "SELECT 1 FROM documents WHERE stored_filename=? LIMIT 1",
            (document["stored_filename"],),
        ).fetchone() is not None

    file_deleted = False
    file_warning = None
    stored_filename = document["stored_filename"] or ""
    if not shared_file and stored_filename and Path(stored_filename).name == stored_filename:
        source_path = UPLOAD_DIR / stored_filename
        try:
            if source_path.is_file() or source_path.is_symlink():
                source_path.unlink()
            file_deleted = True
        except OSError as error:
            file_warning = str(error)
    elif shared_file:
        file_warning = "The source file remains because another library record uses it."
    elif stored_filename:
        file_warning = "The source file was left untouched because its stored name was unsafe."

    return {
        "success": True,
        "document_id": document_id,
        "file_deleted": file_deleted,
        "removed_study_materials": removed_materials,
        "promoted_duplicate_id": promoted_duplicate_id,
        "backup_path": str(backup_path),
        "warning": file_warning,
    }


def list_collections():
    with get_connection() as db:
        rows = db.execute("""
            SELECT c.id, c.name, c.description, c.created_at, c.updated_at,
                   COUNT(cd.document_id) AS document_count
            FROM collections c
            LEFT JOIN collection_documents cd ON cd.collection_id = c.id
            GROUP BY c.id
            ORDER BY lower(c.name), c.id
        """).fetchall()
        return [dict(row) for row in rows]


def create_collection(name: str, description: str = ""):
    name = name.strip()
    description = description.strip()
    if not name or len(name) > 100:
        raise ValueError("Collection names must contain 1 to 100 characters.")
    if len(description) > 1000:
        raise ValueError("Collection descriptions can be at most 1000 characters.")
    collection_id = str(uuid4())
    now = _now()
    with get_connection() as db:
        if db.execute("SELECT 1 FROM collections WHERE lower(name)=lower(?)", (name,)).fetchone():
            raise ValueError("A collection with that name already exists.")
        db.execute("""
            INSERT INTO collections(id, name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (collection_id, name, description, now, now))
    return {"id": collection_id, "name": name, "description": description,
            "created_at": now, "updated_at": now, "document_count": 0}


def delete_collection(collection_id: str) -> dict:
    with get_connection() as db:
        collection = db.execute(
            "SELECT id, name FROM collections WHERE id=?", (collection_id,)
        ).fetchone()
        if collection is None:
            raise LookupError("Collection not found.")
        document_count = db.execute(
            "SELECT COUNT(*) FROM collection_documents WHERE collection_id=?",
            (collection_id,),
        ).fetchone()[0]
        db.execute("DELETE FROM collections WHERE id=?", (collection_id,))
    return {"id": collection_id, "name": collection["name"],
            "removed_document_assignments": document_count}


def set_collection_document(collection_id: str, document_id: str, included: bool):
    with get_connection() as db:
        if db.execute("SELECT 1 FROM collections WHERE id=?", (collection_id,)).fetchone() is None:
            raise LookupError("Collection not found.")
        if db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone() is None:
            raise LookupError("Document not found.")
        if included:
            db.execute("""
                INSERT OR IGNORE INTO collection_documents(collection_id, document_id, added_at)
                VALUES (?, ?, ?)
            """, (collection_id, document_id, _now()))
        else:
            db.execute("DELETE FROM collection_documents WHERE collection_id=? AND document_id=?",
                       (collection_id, document_id))


def collection_document_ids(collection_id: str) -> list[str]:
    with get_connection() as db:
        if db.execute("SELECT 1 FROM collections WHERE id=?", (collection_id,)).fetchone() is None:
            raise LookupError("Collection not found.")
        return [row[0] for row in db.execute(
            "SELECT document_id FROM collection_documents WHERE collection_id=? ORDER BY added_at, document_id",
            (collection_id,),
        )]


def save_study_material(material_type: str, title: str, scope: dict, content: dict):
    material_id = str(uuid4())
    created_at = _now()
    with get_connection() as db:
        db.execute("""
            INSERT INTO study_materials(id, material_type, title, scope_json, content_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (material_id, material_type, title,
              json.dumps(scope, ensure_ascii=False),
              json.dumps(content, ensure_ascii=False), created_at))
    return {"id": material_id, "material_type": material_type, "title": title,
            "scope": scope, "content": content, "created_at": created_at}


def delete_study_material(material_id: str) -> dict:
    with get_connection() as db:
        row = db.execute(
            "SELECT id, title FROM study_materials WHERE id=?", (material_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Saved study set not found.")
        db.execute("DELETE FROM study_materials WHERE id=?", (material_id,))
    return {"id": material_id, "title": row["title"]}


def list_study_materials(limit: int = 50):
    with get_connection() as db:
        rows = db.execute("""
            SELECT id, material_type, title, scope_json, content_json, created_at
            FROM study_materials ORDER BY created_at DESC, id LIMIT ?
        """, (max(1, min(limit, 100)),)).fetchall()
        return [{"id": row["id"], "material_type": row["material_type"],
                 "title": row["title"], "scope": json.loads(row["scope_json"]),
                 "content": json.loads(row["content_json"]),
                 "created_at": row["created_at"]} for row in rows]
