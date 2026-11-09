# Study Desk

A local-first study library for asking questions about your course documents and creating revision materials with AI running through Ollama.

Study Desk runs on your computer. It stores documents and study records in a local SQLite database, searches indexed passages, and links results back to source pages.

## Features

- Import PDF and DOCX study documents and view their extracted pages.
- Search across selected documents, collections, or the full library.
- Ask questions grounded in retrieved passages, with source links and page references.
- Generate flashcards, quizzes, study guides, and concept maps from indexed material.
- Choose quiz format, difficulty, and question count.
- Organize documents into collections and find duplicate uploads.
- Resume indexing and reuse existing chunks and embeddings when content is unchanged.
- Back up and restore the local library; confirm document deletion and make a database backup first.
- Continue using keyword search when Ollama or its embedding model is unavailable.

## Technology

- Python, FastAPI, and Uvicorn
- SQLite with FTS5 full-text search
- Plain HTML, CSS, and JavaScript
- Ollama for local text generation and optional embeddings

## Requirements

- Windows
- Python 3.10 or later
- Ollama for AI answers and study material generation (optional for keyword search)

The default model settings are `qwen3-personal:latest` for text generation and `nomic-embed-text` for embeddings. Change them in `.env` to match the models available in your Ollama installation.

## Setup

Open PowerShell in the project folder and create the Python environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For a new, empty database, initialize it and apply migrations in order:

```powershell
.\.venv\Scripts\python.exe -c "from app.database import init_db; init_db()"
.\.venv\Scripts\python.exe app\migrate_database.py --apply
.\.venv\Scripts\python.exe app\migrate_database_v2.py
.\.venv\Scripts\python.exe app\migrate_database_v3.py
.\.venv\Scripts\python.exe app\migrate_database_v4.py
.\.venv\Scripts\python.exe app\migrate_database_v5.py
```

Copy `.env.example` to `.env` if you need to change Ollama settings:

```powershell
Copy-Item .env.example .env
```

Keep `.env` private. Before publishing a repository, make sure it does not include personal documents, database files, backups, or your Python environment.

## Run

Double-click `run_study_app.bat`. It starts the local server if needed and opens Study Desk in your default browser. The app is available at [http://127.0.0.1:8000](http://127.0.0.1:8000), and the interactive API reference is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

You can also start it from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## First use

1. Add PDF or DOCX files in **Library**.
2. Select documents and choose **Index selected**.
3. Optionally create a collection and add documents to it.
4. Open **Ask your library**, choose a scope, then search, ask a question, or create study materials.
5. Reopen saved materials from **Study materials**.

## Local data and privacy

- SQLite database: `data/study_app.db`
- Uploaded documents: `uploads/`
- Backups: `data/backups/`

The web app binds to `127.0.0.1` by default, so it is available only from the same computer. Model requests are sent to the Ollama service configured for the app. The application does not use a cloud AI API by default. If you change Ollama's address, requests may go to another machine or service.

## Supported documents and limitations

- PDF and DOCX files up to 20 MB each.
- PDF extraction reads embedded/selectable text. OCR has been removed, so scanned or image-only PDFs are not searchable.
- Ollama availability, model quality, response time, and practical quiz size depend on your installed model and computer.
- Short-answer quiz responses are self-assessed. Fill-in-the-blank checks may not accept every valid alternative wording.

- [Codebase Guide](docs/CODEBASE-GUIDE.md) ([PDF](docs/CODEBASE-GUIDE.pdf), [Word](docs/CODEBASE-GUIDE.docx))

## License

No license file is currently included. Add a license before granting permission for others to reuse, modify, or distribute this project.
