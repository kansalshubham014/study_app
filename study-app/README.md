# Study Desk

A private study library that runs on your Windows computer. It stores uploaded documents and study data in the local SQLite database, searches passages across selected documents or collections, and can use Ollama for grounded answers, flashcards, quizzes, and optional semantic search.

## Included features

- PDF and DOCX uploads, extracted text, page-level source references, and duplicate detection.
- Persistent document library and study collections.
- SQLite FTS5 keyword search across selected documents or collections.
- Resumable chunk indexing and optional Ollama embeddings for hybrid retrieval.
- Byte-identical re-uploads skip document parsing; text-based duplicate detection still catches equivalent files with different bytes.
- Local AI questions with the retrieved passages shown as sources.
- Saved flashcard sets and selectable multiple-choice, short-answer, or fill-in-the-blank quizzes with difficulty, count, hints, explanations, and source links.
- Source-linked study guides with key ideas, terms, and review questions.
- Interactive concept maps that connect source-backed ideas.
- Existing single-document analysis and chapter summaries.
- A local interface with no remote fonts or JavaScript dependencies.
- Per-document deletion with a confirmation step and a verified database backup.
- Downloadable library backups that include the database and uploaded files, plus validated restore with an automatic safety backup.

## Start on Windows

Use Python 3.10 or later.

Open PowerShell in this folder and run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If this is a fresh database, initialize the original tables first, then apply the versioned migrations:

```powershell
.\.venv\Scripts\python.exe -c "from app.database import init_db; init_db()"
.\.venv\Scripts\python.exe app\migrate_database.py --apply
.\.venv\Scripts\python.exe app\migrate_database_v2.py
.\.venv\Scripts\python.exe app\migrate_database_v3.py
.\.venv\Scripts\python.exe app\migrate_database_v4.py
.\.venv\Scripts\python.exe app\migrate_database_v5.py
```

Each migration creates and verifies a timestamped backup before changing the database. The copied project has migrations 1 through 5 applied. Rerunning the scripts reports when a migration is already recorded.

Copy `.env.example` to `.env` if you want to change the local Ollama settings. The defaults are `qwen3-personal:latest` for text generation and `nomic-embed-text` for embeddings. Keyword search works without an embedding model; hybrid search becomes available after the configured embedding model is installed in Ollama.

Start the app with:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or double-click `run_study_app.bat`. It starts the local server if needed, waits for it to respond, and opens Study Desk in your default browser. The interactive API reference is at `/docs`.

## First use

1. Add PDF or DOCX files in **Library**.
2. Select the files to use together and choose **Index selected**. Indexing prepares local keyword search and attempts embeddings if the configured Ollama embedding model is available.
3. Create a collection if you want a reusable subset, then add selected files to it with the plus button.
4. Open **Ask your library**. Select files in the Library first, or choose a collection. With no selection, the entire library is in scope.
5. Search for passages, ask a question, or generate a study guide, concept map, flashcards, or a quiz. Generated sets are saved under **Study materials**.

The **Files to add to your library** panel lists uploads that exist on disk but do not have a database record. Registering one reads it in place and preserves the original file. If it duplicates an existing document, both records remain and are linked as duplicates.

Each document now has a clear **View document** action and a compact **More options** menu for collection assignment, indexing, analysis, and deletion. Select one or more documents and use **Delete selected** in the top bar, or use **Delete document** in a row's menu. The confirmation names each uploaded source file that may be removed. Deletion removes the database record, extracted pages, analysis, index, saved study sets that reference the document, and uploaded source file. The app creates a database backup in `data/backups/` first; that backup retains the pre-delete extracted text until the backup file is removed. If the selected document is the canonical copy for duplicates, one duplicate is promoted so the other records remain usable.

Use **Backup and restore** in the Library to download a ZIP containing the database and uploaded PDFs/DOCX files. Restore only ZIP files created by Study Desk; the app checks their paths, database integrity, schema version, and required source files. Before replacing the current library, it creates a safety backup in `data/backups/`.

## Local data

- Database: `data/study_app.db`
- Uploaded documents: `uploads/`
- Migration backups: `data/backups/`

## Project documentation

- Product requirements (PRD): [Markdown](docs/PRD.md) · [PDF](docs/PRD.pdf) · [Word](docs/PRD.docx)
- Challenges and fixes: [Markdown](docs/CHALLENGES-AND-FIXES.md) · [PDF](docs/CHALLENGES-AND-FIXES.pdf) · [Word](docs/CHALLENGES-AND-FIXES.docx)
- Codebase guide: [Markdown](docs/CODEBASE-GUIDE.md) · [PDF](docs/CODEBASE-GUIDE.pdf) · [Word](docs/CODEBASE-GUIDE.docx)

The app binds to `127.0.0.1` by default. Do not change the host to `0.0.0.0` unless you intend to expose the local service to other devices on your network.
