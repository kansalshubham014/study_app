"""Add persisted flashcard and quiz storage to the Study App database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "study_app.db"
BACKUP_DIR = ROOT / "data" / "backups"
VERSION = 2


def connect_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def snapshot(db: sqlite3.Connection) -> dict:
    result = {}
    for table in ("documents", "document_pages", "document_analyses",
                  "chapter_analyses", "document_chunks", "document_duplicates",
                  "collections", "folders"):
        if table not in tables(db):
            continue
        columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
        selected = ", ".join(f'"{column}"' for column in columns)
        digest = hashlib.sha256()
        count = 0
        for row in db.execute(f'SELECT {selected} FROM "{table}" ORDER BY rowid'):
            encoded = json.dumps(
                [value.hex() if isinstance(value, bytes) else value for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
        result[table] = (count, digest.hexdigest())
    return result


def run() -> Path | None:
    if not DB_PATH.is_file():
        raise FileNotFoundError(DB_PATH)
    check = connect_ro(DB_PATH)
    try:
        if "schema_migrations" not in tables(check):
            raise RuntimeError("Migration 1 is missing; run the base migration first.")
        if check.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone() is None:
            raise RuntimeError("Migration 1 is not recorded; refusing to continue.")
        if check.execute("SELECT 1 FROM schema_migrations WHERE version=?", (VERSION,)).fetchone():
            print("Migration 2 is already recorded; no changes made.")
            return None
        before = snapshot(check)
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed.")
    finally:
        check.close()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    backup_path = BACKUP_DIR / f"study_app_before_migration_v2_{stamp}.db"
    with backup_path.open("xb"):
        pass
    source = connect_ro(DB_PATH)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()

    backup = connect_ro(backup_path)
    try:
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("New pre-migration backup failed integrity check.")
        if snapshot(backup) != before:
            raise RuntimeError("New backup differs from the pre-migration database.")
    finally:
        backup.close()

    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE study_materials (
                id TEXT PRIMARY KEY,
                material_type TEXT NOT NULL
                    CHECK (material_type IN ('flashcards', 'quiz')),
                title TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX idx_study_materials_created ON study_materials(created_at DESC)")
        db.execute("""
            INSERT INTO schema_migrations(version, description, applied_at)
            VALUES (?, ?, ?)
        """, (VERSION, "Persist generated flashcards and quizzes",
              datetime.now(timezone.utc).isoformat()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    verify = connect_ro(DB_PATH)
    try:
        if snapshot(verify) != before:
            raise RuntimeError("Existing records changed during migration 2.")
        if verify.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed after migration 2.")
        if verify.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign-key check failed after migration 2.")
    finally:
        verify.close()

    print(f"Migration 2 applied. Verified backup: {backup_path}")
    return backup_path


if __name__ == "__main__":
    run()
