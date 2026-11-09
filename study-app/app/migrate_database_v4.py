"""Allow saved interactive concept maps without changing existing study sets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "study_app.db"
BACKUP_DIR = ROOT / "data" / "backups"
VERSION = 4


def connect_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def digest(db: sqlite3.Connection) -> tuple[int, str]:
    value = hashlib.sha256()
    count = 0
    for row in db.execute("SELECT * FROM study_materials ORDER BY rowid"):
        encoded = json.dumps(list(row), ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        value.update(len(encoded).to_bytes(8, "big"))
        value.update(encoded)
        count += 1
    return count, value.hexdigest()


def run() -> Path | None:
    if not DB_PATH.is_file():
        raise FileNotFoundError(DB_PATH)
    current = connect_ro(DB_PATH)
    try:
        if current.execute("SELECT 1 FROM schema_migrations WHERE version=?",
                           (VERSION,)).fetchone():
            print("Migration 4 is already recorded; no changes made.")
            return None
        if current.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone() is None:
            raise RuntimeError("Migration 3 is missing; refusing to continue.")
        before = digest(current)
        if current.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed before migration.")
    finally:
        current.close()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    backup_path = BACKUP_DIR / f"study_app_before_migration_v4_{stamp}.db"
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
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or digest(backup) != before:
            raise RuntimeError("The new pre-migration backup failed verification.")
    finally:
        backup.close()

    db = sqlite3.connect(DB_PATH)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE study_materials RENAME TO study_materials_v3")
        db.execute("""
            CREATE TABLE study_materials (
                id TEXT PRIMARY KEY,
                material_type TEXT NOT NULL
                    CHECK (material_type IN ('flashcards', 'quiz', 'study_guide', 'concept_map')),
                title TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            INSERT INTO study_materials
                (id, material_type, title, scope_json, content_json, created_at)
            SELECT id, material_type, title, scope_json, content_json, created_at
            FROM study_materials_v3
        """)
        db.execute("DROP TABLE study_materials_v3")
        db.execute("CREATE INDEX idx_study_materials_created ON study_materials(created_at DESC)")
        db.execute("""
            INSERT INTO schema_migrations(version, description, applied_at)
            VALUES (?, ?, ?)
        """, (VERSION, "Persist interactive source-grounded concept maps",
              datetime.now(timezone.utc).isoformat()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    verify = connect_ro(DB_PATH)
    try:
        if digest(verify) != before:
            raise RuntimeError("Existing saved study sets changed during migration.")
        if verify.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed after migration.")
        if not verify.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone():
            raise RuntimeError("Migration 4 was not recorded.")
    finally:
        verify.close()
    print(f"Migration 4 applied. Verified backup: {backup_path}")
    return backup_path


if __name__ == "__main__":
    run()
