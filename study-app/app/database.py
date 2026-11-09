
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
import json


# --------------------------------------------------
# DATABASE CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "study_app.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _add_column_if_missing(connection, table, column, definition):
    existing_columns = {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }

    if column not in existing_columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

def init_db():

    with get_connection() as connection:

        connection.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                page_count INTEGER DEFAULT 0,
                character_count INTEGER DEFAULT 0,
                full_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS document_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                page_text TEXT NOT NULL,

                FOREIGN KEY (document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE,

                UNIQUE(document_id, page_number)
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS document_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                key_points_json TEXT NOT NULL,
                analysis_status TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS chapter_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                chapter_title TEXT NOT NULL,
                start_page INTEGER,
                end_page INTEGER,
                summary TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                key_points_json TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (document_id)
                    REFERENCES documents(id)
                    ON DELETE CASCADE,

                UNIQUE(document_id, chapter_number)
            )
        """)

        # --------------------------------------------------
        # BACKWARD-COMPATIBLE MIGRATIONS
        # --------------------------------------------------

        _add_column_if_missing(
            connection,
            "document_analyses",
            "structure_json",
            "TEXT"
        )

        _add_column_if_missing(
            connection,
            "chapter_analyses",
            "subsections_json",
            "TEXT"
        )

        connection.commit()


# --------------------------------------------------
# DOCUMENT STORAGE
# --------------------------------------------------

def save_document(
    document_id,
    original_filename,
    stored_filename,
    file_type,
    pages,
    full_text,
    content_sha256=None,
    source_sha256=None,
):

    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as connection:

        connection.execute("""
            INSERT INTO documents (
                id, original_filename, stored_filename,
                file_type, page_count, character_count,
                full_text, created_at, content_sha256, source_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            document_id,
            original_filename,
            stored_filename,
            file_type,
            len(pages),
            len(full_text),
            full_text,
            created_at,
            content_sha256,
            source_sha256
        ))

        connection.executemany("""
            INSERT INTO document_pages (
                document_id, page_number, page_text
            )
            VALUES (?, ?, ?)
        """, [
            (
                document_id,
                page["page"],
                page["text"]
            )
            for page in pages
        ])

        connection.commit()


# --------------------------------------------------
# DOCUMENT RETRIEVAL
# --------------------------------------------------

def get_document(document_id):

    with get_connection() as connection:

        document = connection.execute("""
            SELECT *
            FROM documents
            WHERE id = ?
        """, (document_id,)).fetchone()

        return dict(document) if document else None


def _get_document_by_digest(value, column):
    if not value:
        return None
    if column not in {"content_sha256", "source_sha256"}:
        raise ValueError("Unsupported document digest column.")

    with get_connection() as connection:
        row = connection.execute(f"""
            SELECT d.id, d.original_filename, d.stored_filename,
                   d.file_type, d.page_count, d.character_count, d.created_at,
                   COALESCE(duplicate.canonical_document_id, d.id) AS canonical_id
            FROM documents AS d
            LEFT JOIN document_duplicates AS duplicate
                ON duplicate.document_id = d.id
            WHERE d.{column} = ?
            ORDER BY CASE WHEN duplicate.document_id IS NULL THEN 0 ELSE 1 END,
                     d.created_at, d.id
            LIMIT 1
        """, (value,)).fetchone()

        if row is None:
            return None

        selected = dict(row)
        canonical_id = selected.pop("canonical_id")
        if canonical_id != selected["id"]:
            canonical = connection.execute("""
                SELECT id, original_filename, stored_filename, file_type,
                       page_count, character_count, created_at
                FROM documents WHERE id = ?
            """, (canonical_id,)).fetchone()
            if canonical:
                selected = dict(canonical)
        return selected


def get_document_by_hash(content_sha256):
    """Return the preferred existing record for identical extracted text."""
    return _get_document_by_digest(content_sha256, "content_sha256")


def get_document_by_source_hash(source_sha256):
    """Return an existing record for a byte-identical uploaded source file."""
    return _get_document_by_digest(source_sha256, "source_sha256")


def get_document_pages(document_id):

    with get_connection() as connection:

        pages = connection.execute("""
            SELECT page_number, page_text
            FROM document_pages
            WHERE document_id = ?
            ORDER BY page_number
        """, (document_id,)).fetchall()

        return [dict(page) for page in pages]


def list_documents():

    with get_connection() as connection:

        documents = connection.execute("""
            SELECT
                id,
                original_filename,
                file_type,
                page_count,
                character_count,
                created_at
            FROM documents
            ORDER BY created_at DESC
        """).fetchall()

        return [dict(document) for document in documents]


# --------------------------------------------------
# SAVE AI ANALYSIS
# --------------------------------------------------

def save_analysis_results(
    document_id,
    document_analysis,
    chapter_analyses,
    structure=None
):

    created_at = datetime.now(timezone.utc).isoformat()

    structure_json = (
        json.dumps(structure, ensure_ascii=False)
        if structure is not None
        else None
    )

    with get_connection() as connection:

        connection.execute("""
            INSERT INTO document_analyses (
                document_id,
                summary,
                topics_json,
                key_points_json,
                analysis_status,
                created_at,
                structure_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(document_id) DO UPDATE SET
                summary = excluded.summary,
                topics_json = excluded.topics_json,
                key_points_json = excluded.key_points_json,
                analysis_status = excluded.analysis_status,
                created_at = excluded.created_at,
                structure_json = excluded.structure_json
        """, (
            document_id,
            document_analysis["summary"],
            json.dumps(
                document_analysis["topics"],
                ensure_ascii=False
            ),
            json.dumps(
                document_analysis["key_points"],
                ensure_ascii=False
            ),
            "completed",
            created_at,
            structure_json
        ))

        # Replace previous chapter results for this document.
        connection.execute("""
            DELETE FROM chapter_analyses
            WHERE document_id = ?
        """, (document_id,))

        for chapter in chapter_analyses:

            connection.execute("""
                INSERT INTO chapter_analyses (
                    document_id,
                    chapter_number,
                    chapter_title,
                    start_page,
                    end_page,
                    summary,
                    topics_json,
                    key_points_json,
                    created_at,
                    subsections_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                document_id,
                chapter["chapter_number"],
                chapter["chapter_title"],
                chapter.get("start_page"),
                chapter.get("end_page"),
                chapter["summary"],
                json.dumps(
                    chapter["topics"],
                    ensure_ascii=False
                ),
                json.dumps(
                    chapter["key_points"],
                    ensure_ascii=False
                ),
                created_at,
                json.dumps(
                    chapter.get("subsections", []),
                    ensure_ascii=False
                )
            ))

        connection.commit()


# --------------------------------------------------
# RETRIEVE AI ANALYSIS
# --------------------------------------------------

def get_analysis(document_id):

    with get_connection() as connection:

        document_analysis = connection.execute("""
            SELECT *
            FROM document_analyses
            WHERE document_id = ?
        """, (document_id,)).fetchone()

        chapters = connection.execute("""
            SELECT *
            FROM chapter_analyses
            WHERE document_id = ?
            ORDER BY chapter_number
        """, (document_id,)).fetchall()

        if document_analysis is None:
            return None

        result = dict(document_analysis)

        result["topics"] = json.loads(
            result.pop("topics_json")
        )

        result["key_points"] = json.loads(
            result.pop("key_points_json")
        )

        structure_json = result.pop("structure_json", None)

        result["structure"] = (
            json.loads(structure_json)
            if structure_json
            else None
        )

        chapter_results = []

        for chapter in chapters:

            item = dict(chapter)

            item["topics"] = json.loads(
                item.pop("topics_json")
            )

            item["key_points"] = json.loads(
                item.pop("key_points_json")
            )

            subsections_json = item.pop(
                "subsections_json",
                None
            )

            item["subsections"] = (
                json.loads(subsections_json)
                if subsections_json
                else []
            )

            chapter_results.append(item)

        result["chapters"] = chapter_results

        return result
