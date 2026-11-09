"""Store uploaded-file hashes so identical re-uploads skip PDF/DOCX extraction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "study_app.db"
UPLOAD_DIR = ROOT / "uploads"
BACKUP_DIR = ROOT / "data" / "backups"
VERSION = 5


def connect_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def rows_digest(db: sqlite3.Connection, table: str, columns: list[str]) -> tuple[int, str]:
    quoted = ", ".join(f'"{column}"' for column in columns)
    digest = hashlib.sha256()
    count = 0
    for row in db.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid'):
        encoded = json.dumps(
            [value.hex() if isinstance(value, bytes) else value for value in row],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run() -> Path | None:
    if not DB_PATH.is_file():
        raise FileNotFoundError(DB_PATH)
    current = connect_ro(DB_PATH)
    try:
        if current.execute("SELECT 1 FROM schema_migrations WHERE version=?",
                           (VERSION,)).fetchone():
            print("Migration 5 is already recorded; no changes made.")
            return None
        if current.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone() is None:
            raise RuntimeError("Migration 4 is missing; refusing to continue.")
        columns = [row[1] for row in current.execute('PRAGMA table_info("documents")')]
        if "source_sha256" in columns:
            raise RuntimeError("source_sha256 exists but migration 5 is not recorded.")
        before = rows_digest(current, "documents", columns)
        documents = current.execute(
            "SELECT id, stored_filename FROM documents ORDER BY id"
        ).fetchall()
        if current.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed before migration.")
    finally:
        current.close()

    file_hashes: dict[str, str] = {}
    missing_files = []
    for document in documents:
        filename = document["stored_filename"]
        if not filename or Path(filename).name != filename:
            missing_files.append(document["id"])
            continue
        path = UPLOAD_DIR / filename
        try:
            resolved_path = path.resolve(strict=True)
        except OSError:
            missing_files.append(document["id"])
            continue
        if resolved_path.is_file() and resolved_path.parent == UPLOAD_DIR.resolve():
            file_hashes[document["id"]] = sha256_file(resolved_path)
        else:
            missing_files.append(document["id"])

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    backup_path = BACKUP_DIR / f"study_app_before_migration_v5_{stamp}.db"
    with backup_path.open("xb"):
        pass
    source = connect_ro(DB_PATH)
    backup_db = sqlite3.connect(backup_path)
    try:
        source.backup(backup_db)
        backup_db.commit()
    finally:
        backup_db.close()
        source.close()
    backup = connect_ro(backup_path)
    try:
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("The new pre-migration backup failed integrity check.")
        if rows_digest(backup, "documents", columns) != before:
            raise RuntimeError("The backup differs from the current document records.")
    finally:
        backup.close()

    db = sqlite3.connect(DB_PATH)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE documents ADD COLUMN source_sha256 TEXT")
        db.execute("CREATE INDEX idx_documents_source_sha256 ON documents(source_sha256)")
        db.executemany(
            "UPDATE documents SET source_sha256=? WHERE id=?",
            [(value, document_id) for document_id, value in file_hashes.items()],
        )
        db.execute("""
            INSERT INTO schema_migrations(version, description, applied_at)
            VALUES (?, ?, ?)
        """, (VERSION, "Cache source file hashes to skip repeat extraction",
              datetime.now(timezone.utc).isoformat()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    verify = connect_ro(DB_PATH)
    try:
        if rows_digest(verify, "documents", columns) != before:
            raise RuntimeError("Existing document records changed during migration.")
        for document_id, value in file_hashes.items():
            actual = verify.execute(
                "SELECT source_sha256 FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            if actual is None or actual[0] != value:
                raise RuntimeError("A stored upload hash failed verification.")
        if verify.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed after migration.")
        if not verify.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone():
            raise RuntimeError("Migration 5 was not recorded.")
    finally:
        verify.close()
    print(f"Migration 5 applied. Verified backup: {backup_path}")
    print(f"Cached hashes for {len(file_hashes)} uploaded files; {len(missing_files)} files were unavailable.")
    return backup_path


if __name__ == "__main__":
    run()
