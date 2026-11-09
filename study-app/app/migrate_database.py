"""Prepare the Study App database for multi-document study features.

Running this file without --apply performs a read-only preflight. The database
is changed only when --apply is supplied explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "study_app.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
MIGRATION_VERSION = 1
MIGRATION_DESCRIPTION = "Multi-document study architecture foundation"

CANONICAL_DOCUMENT_ID = "626f66e9-7d17-4782-9798-0238c6ac024a"
DUPLICATE_DOCUMENT_ID = "032e79ff-fe7f-4192-89b0-bb6e3c9c8de6"

REQUIRED_COLUMNS = {
    "documents": {"id", "full_text"},
    "document_pages": {"id", "document_id", "page_number", "page_text"},
    "document_analyses": {"id", "document_id"},
    "chapter_analyses": {"id", "document_id"},
}


def connect_read_only(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def fts5_available() -> bool:
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


def _json_value(value):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def table_snapshot(db: sqlite3.Connection, table: str,
                   columns: list[str] | None = None) -> tuple[int, str]:
    columns = columns or sorted(table_columns(db, table))
    quoted = ", ".join(f'"{column}"' for column in columns)
    rows = db.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid')
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        encoded = json.dumps(
            [_json_value(value) for value in row],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def capture_existing_tables(db: sqlite3.Connection):
    return {
        table: (sorted(table_columns(db, table)),
                table_snapshot(db, table))
        for table in REQUIRED_COLUMNS
    }


def preflight(db: sqlite3.Connection) -> dict:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    missing_tables = [table for table in REQUIRED_COLUMNS
                      if not table_exists(db, table)]
    if missing_tables:
        raise RuntimeError(f"Required tables are missing: {missing_tables}")

    for table, required in REQUIRED_COLUMNS.items():
        missing = required - table_columns(db, table)
        if missing:
            raise RuntimeError(f"{table} is missing columns: {sorted(missing)}")

    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"Database integrity check failed: {integrity}")
    fk_errors = db.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise RuntimeError(f"Database has foreign-key violations: {fk_errors}")

    docs = {
        row["id"]: row["full_text"] or ""
        for row in db.execute(
            "SELECT id, full_text FROM documents WHERE id IN (?, ?)",
            (CANONICAL_DOCUMENT_ID, DUPLICATE_DOCUMENT_ID),
        )
    }
    has_reviewed_pair = set(docs) == {CANONICAL_DOCUMENT_ID, DUPLICATE_DOCUMENT_ID}
    canonical_hash = None
    if has_reviewed_pair:
        canonical_hash = hashlib.sha256(
            docs[CANONICAL_DOCUMENT_ID].encode("utf-8")
        ).hexdigest()
        duplicate_hash = hashlib.sha256(
            docs[DUPLICATE_DOCUMENT_ID].encode("utf-8")
        ).hexdigest()
        if canonical_hash != duplicate_hash:
            raise RuntimeError("The reviewed document pair no longer has identical text.")

    already_applied = False
    if table_exists(db, "schema_migrations"):
        already_applied = db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone() is not None

    if not fts5_available():
        raise RuntimeError("This Python SQLite build does not provide FTS5.")

    counts = {
        table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in REQUIRED_COLUMNS
    }
    return {
        "already_applied": already_applied,
        "has_reviewed_pair": has_reviewed_pair,
        "counts": counts,
        "snapshots": capture_existing_tables(db),
        "canonical_hash": canonical_hash,
    }


def create_verified_backup(expected_snapshots) -> Path:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    backup_path = BACKUP_DIR / f"study_app_before_migration_{stamp}.db"

    # Exclusive creation prevents a timestamp collision from overwriting a backup.
    with backup_path.open("xb"):
        pass
    source = connect_read_only(DB_PATH)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()

    backup = connect_read_only(backup_path)
    try:
        integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"New backup failed integrity check: {integrity}")
        actual = capture_existing_tables(backup)
        if actual != expected_snapshots:
            raise RuntimeError("New backup does not match the pre-migration database.")
    finally:
        backup.close()
    return backup_path


def create_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)

    if "content_sha256" not in table_columns(db, "documents"):
        db.execute("ALTER TABLE documents ADD COLUMN content_sha256 TEXT")

    db.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            parent_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (parent_id) REFERENCES folders(id) ON DELETE RESTRICT,
            UNIQUE (parent_id, name)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS collections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS collection_documents (
            collection_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (collection_id, document_id),
            FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS document_folders (
            document_id TEXT NOT NULL,
            folder_id TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (document_id, folder_id),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
            FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            start_page INTEGER NOT NULL,
            end_page INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            token_count INTEGER,
            summary TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (document_id, chunk_index),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
            CHECK (start_page >= 1),
            CHECK (end_page >= start_page),
            CHECK (status IN ('pending', 'processing', 'completed', 'failed'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_document_chunks_document ON document_chunks(document_id, chunk_index)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_document_chunks_status ON document_chunks(status)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS chunk_embeddings (
            chunk_id TEXT PRIMARY KEY,
            model_name TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chunk_id) REFERENCES document_chunks(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS processing_runs (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            pipeline_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            config_json TEXT,
            error_message TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
            CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_processing_runs_document ON processing_runs(document_id, created_at)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS processing_run_chunks (
            run_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            started_at TEXT,
            completed_at TEXT,
            PRIMARY KEY (run_id, chunk_id),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (chunk_id) REFERENCES document_chunks(id) ON DELETE CASCADE,
            CHECK (status IN ('pending', 'processing', 'completed', 'failed'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS document_duplicates (
            document_id TEXT PRIMARY KEY,
            canonical_document_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            detection_method TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (document_id != canonical_document_id),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
            FOREIGN KEY (canonical_document_id) REFERENCES documents(id) ON DELETE RESTRICT
        )
    """)
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts
        USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, content)
    """)


def apply_migration(preflight_result: dict) -> Path:
    backup_path = create_verified_backup(preflight_result["snapshots"])
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        create_schema(db)

        documents = db.execute("SELECT id, full_text FROM documents").fetchall()
        for document in documents:
            content = document["full_text"] or ""
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            db.execute(
                "UPDATE documents SET content_sha256 = ? WHERE id = ?",
                (digest, document["id"]),
            )

        if preflight_result["has_reviewed_pair"]:
            existing_link = db.execute(
                "SELECT canonical_document_id, content_sha256 FROM document_duplicates WHERE document_id = ?",
                (DUPLICATE_DOCUMENT_ID,),
            ).fetchone()
            canonical = db.execute(
                "SELECT content_sha256 FROM documents WHERE id = ?",
                (CANONICAL_DOCUMENT_ID,),
            ).fetchone()
            duplicate = db.execute(
                "SELECT content_sha256 FROM documents WHERE id = ?",
                (DUPLICATE_DOCUMENT_ID,),
            ).fetchone()
            if not canonical or not duplicate or canonical[0] != duplicate[0]:
                raise RuntimeError("The reviewed duplicate pair changed; refusing to link it.")
            if existing_link and (
                existing_link["canonical_document_id"] != CANONICAL_DOCUMENT_ID
                or existing_link["content_sha256"] != canonical[0]
            ):
                raise RuntimeError("An incompatible duplicate relationship already exists.")
            if not existing_link:
                db.execute("""
                    INSERT INTO document_duplicates (
                        document_id, canonical_document_id, content_sha256,
                        detection_method, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                """, (
                    DUPLICATE_DOCUMENT_ID,
                    CANONICAL_DOCUMENT_ID,
                    canonical[0],
                    "identical_extracted_text",
                    datetime.now(timezone.utc).isoformat(),
                ))

        db.execute(
            "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
            (MIGRATION_VERSION, MIGRATION_DESCRIPTION,
             datetime.now(timezone.utc).isoformat()),
        )

        for table, (columns, expected) in preflight_result["snapshots"].items():
            actual = table_snapshot(db, table, columns)
            if actual != expected:
                raise RuntimeError(f"Existing records in {table} changed during migration.")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed after migration.")
        fk_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign-key check failed after migration: {fk_errors}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a verified backup and apply the migration. Omit for read-only preflight.",
    )
    args = parser.parse_args()

    try:
        db = connect_read_only(DB_PATH)
        try:
            result = preflight(db)
        finally:
            db.close()

        if result["already_applied"]:
            print(f"Migration {MIGRATION_VERSION} is already recorded; no changes made.")
            return 0

        print("Read-only preflight passed.")
        print(f"Existing row counts: {result['counts']}")
        print("Existing document and analysis records will be retained.")
        print("Uploaded files are not read, moved, renamed, or deleted by this migration.")
        if not args.apply:
            print("No changes made. Pass --apply only after reviewing and approving this script.")
            return 0

        backup_path = apply_migration(result)
        print("Migration completed successfully.")
        print(f"Verified pre-migration backup: {backup_path}")
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
