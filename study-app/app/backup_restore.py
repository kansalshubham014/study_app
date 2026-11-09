"""Verified local backups for the Study Desk database and uploaded files."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "study_app.db"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
BACKUP_DIR = DATA_DIR / "backups"
MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_CONTENT_BYTES = 4 * 1024 * 1024 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _copy_database(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(target_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def _verify_database(path: Path, required_uploads: set[str] | None = None) -> int:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("The backup database failed its integrity check.")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("The backup database contains foreign-key errors.")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"schema_migrations", "documents", "document_pages", "collections"} <= tables:
            raise ValueError("This archive does not contain a compatible Study Desk database.")
        version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version is None or version < 2:
            raise ValueError("This database is an older Study Desk version and cannot be restored here.")
        stored_files = {
            row[0] for row in db.execute(
                "SELECT stored_filename FROM documents WHERE stored_filename != ''"
            )
        }
        if required_uploads is not None and not stored_files <= required_uploads:
            raise ValueError("The backup is missing one or more uploaded files referenced by the library.")
        return db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        db.close()


def create_library_backup() -> Path:
    if not DATABASE_PATH.is_file():
        raise FileNotFoundError("The Study Desk database was not found.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    temp_dir = Path(tempfile.mkdtemp(prefix="study_backup_", dir=BACKUP_DIR))
    database_copy = temp_dir / "study_app.db"
    archive_temp = BACKUP_DIR / f".study_desk_backup_{stamp}.tmp"
    archive_path = BACKUP_DIR / f"study_desk_backup_{stamp}.zip"
    try:
        _copy_database(DATABASE_PATH, database_copy)
        upload_files = sorted(
            path for path in UPLOAD_DIR.iterdir()
            if path.is_file() and not path.is_symlink()
            and path.suffix.lower() in {".pdf", ".docx"}
            and not path.name.startswith(".incoming-")
        ) if UPLOAD_DIR.is_dir() else []
        _verify_database(database_copy, {path.name for path in upload_files})
        manifest = {
            "format": "study-desk-backup",
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "uploaded_files": [path.name for path in upload_files],
        }
        with zipfile.ZipFile(archive_temp, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.write(database_copy, "database/study_app.db")
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for path in upload_files:
                archive.write(path, f"uploads/{path.name}")
        os.replace(archive_temp, archive_path)
        return archive_path
    finally:
        archive_temp.unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)


def _safe_archive_members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, dict]:
    infos = archive.infolist()
    if not infos or len(infos) > 10002:
        raise ValueError("The backup archive has an invalid number of files.")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ValueError("The backup archive contains duplicate paths.")
    for info in infos:
        path = Path(info.filename)
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError("The backup uses an unsupported compression format.")
        if info.flag_bits & 1:
            raise ValueError("Password-protected backup archives are not supported.")
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
            raise ValueError("The backup archive contains an unsafe file path.")
        if info.file_size > MAX_ARCHIVE_CONTENT_BYTES:
            raise ValueError("A file in the backup is too large to restore.")
        unix_mode = info.external_attr >> 16
        if unix_mode and (unix_mode & 0o170000) == 0o120000:
            raise ValueError("The backup archive contains an unsupported link.")
    if sum(info.file_size for info in infos) > MAX_ARCHIVE_CONTENT_BYTES:
        raise ValueError("The backup archive is too large to restore.")
    if "manifest.json" not in names or "database/study_app.db" not in names:
        raise ValueError("This is not a complete Study Desk backup.")
    manifest_info = archive.getinfo("manifest.json")
    if manifest_info.file_size > 4 * 1024 * 1024:
        raise ValueError("The backup manifest is too large.")
    manifest = json.loads(archive.read("manifest.json"))
    if not isinstance(manifest, dict):
        raise ValueError("The backup manifest is invalid.")
    if manifest.get("format") != "study-desk-backup" or manifest.get("version") != 1:
        raise ValueError("This backup format is not supported.")
    manifest_uploads = manifest.get("uploaded_files")
    if not isinstance(manifest_uploads, list) or any(
            not isinstance(filename, str) for filename in manifest_uploads):
        raise ValueError("The backup manifest has an invalid uploaded-file list.")
    upload_names = []
    for name in names:
        if name in {"manifest.json", "database/study_app.db"}:
            continue
        if not name.startswith("uploads/"):
            raise ValueError("The backup contains an unexpected file.")
        filename = name.removeprefix("uploads/")
        if (not filename or Path(filename).name != filename
                or filename.endswith((" ", "."))
                or any(ord(char) < 32 or char in '<>:"|?*' for char in filename)
                or Path(filename).suffix.lower() not in {".pdf", ".docx"}):
            raise ValueError("The backup contains an invalid uploaded file name.")
        upload_names.append(filename)
    if sorted(manifest_uploads) != sorted(upload_names):
        raise ValueError("The backup manifest does not match its uploaded files.")
    database_info = archive.getinfo("database/study_app.db")
    return database_info, manifest


async def restore_library_backup(upload: UploadFile) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="study_restore_", dir=DATA_DIR))
    archive_path = stage / "uploaded_backup.zip"
    stage_db = stage / "study_app.db"
    stage_uploads = stage / "uploads"
    rollback_uploads = DATA_DIR / f".uploads_before_restore_{uuid4().hex}"
    bytes_written = 0
    try:
        with archive_path.open("wb") as target:
            while chunk := await upload.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_BACKUP_BYTES:
                    raise ValueError("The uploaded backup exceeds the 2 GB limit.")
                target.write(chunk)
        if bytes_written == 0:
            raise ValueError("Choose a Study Desk backup file first.")

        stage_uploads.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            database_info, manifest = _safe_archive_members(archive)
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError("The backup archive is damaged and failed its checksum check.")
            with archive.open(database_info) as source, stage_db.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            for filename in manifest["uploaded_files"]:
                with archive.open(f"uploads/{filename}") as source:
                    with (stage_uploads / filename).open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
        restored_documents = _verify_database(stage_db, set(manifest["uploaded_files"]))
        if not DATABASE_PATH.is_file():
            raise FileNotFoundError("The current Study Desk database was not found; restore was stopped.")
        safety_backup = create_library_backup()

        if UPLOAD_DIR.exists():
            os.replace(UPLOAD_DIR, rollback_uploads)
        try:
            os.replace(stage_uploads, UPLOAD_DIR)
            os.replace(stage_db, DATABASE_PATH)
        except Exception:
            if UPLOAD_DIR.exists():
                shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
            if rollback_uploads.exists():
                os.replace(rollback_uploads, UPLOAD_DIR)
            raise
        shutil.rmtree(rollback_uploads, ignore_errors=True)
        return {
            "success": True,
            "restored_documents": restored_documents,
            "restored_uploads": len(manifest["uploaded_files"]),
            "safety_backup": safety_backup.name,
        }
    except (zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise ValueError("The selected file is not a valid Study Desk backup.") from error
    finally:
        await upload.close()
        shutil.rmtree(stage, ignore_errors=True)
