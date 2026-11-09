# Study Desk — Product Requirements Document

**Status:** Current product baseline  
**Updated:** 2026-11-09  
**Product:** Study Desk, a local AI study library for Windows

## 1. Product summary

Study Desk lets one learner organize course files, search their contents, ask questions grounded in retrieved passages, and turn those passages into revision materials. Uploaded files and study records stay in the local project. AI text generation and optional embeddings use the Ollama service configured on the same computer.

## 2. Problem and goals

Course information is spread across documents, and finding useful explanations or building revision material takes time. Study Desk should make it straightforward to:

- Keep course PDFs and DOCX files together in a private local library.
- Find relevant passages across a document set or collection.
- Ask questions and see the source pages behind an answer.
- Generate reusable quizzes, flashcards, study guides, and concept maps.
- Protect the library with backups and recoverable deletion.

## 3. Intended user

A student or independent learner using a Windows computer who has course documents in PDF or DOCX format and wants local, source-grounded study tools.

## 4. Product principles

1. **Local by default.** Store files and study history in the project’s SQLite database and uploads folder. Send model requests to the configured Ollama service.
2. **Show evidence.** Answers and generated study items should link back to source documents and page ranges.
3. **Preserve user work.** Back up before destructive document removal or library restore, and identify duplicate files without silently replacing records.
4. **Make common tasks clear.** Keep library management, asking/searching, and study materials distinct in the interface.

## 5. Scope

### Included

- Upload PDF and DOCX files up to 20 MB each; extract selectable text and retain page references.
- Detect byte-identical uploads before parsing and detect different files with identical extracted text afterward.
- Browse documents, filter the library, view source files, and organize documents in collections.
- Index documents for SQLite FTS5 keyword search and optional local semantic search using Ollama embeddings.
- Ask questions against selected documents, a collection, or the whole library; show citations to source pages.
- Generate saved flashcards, quizzes, study guides, and concept maps from indexed passages.
- Select quiz format (multiple choice, short answer, or fill in the blank), difficulty, and 10, 15, 20, 25, or 50 questions.
- Review quiz explanations, retry missed questions, and access cited source pages.
- Analyze an individual document and display saved document/chapter analysis.
- Delete documents with confirmation and a pre-delete database backup; preserve a shared upload file while another record uses it.
- Download a library ZIP backup and restore a validated Study Desk backup with a safety backup of the current library.
- Display whether Ollama and its configured models are available.

### Not included

- OCR or indexing of image-only/scanned PDFs. The current extractor reads embedded PDF text.
- Cloud storage, account login, multi-user collaboration, or cloud model APIs.
- Native mobile applications or real-time synchronization across devices.
- Automatic grading of free-text short answers; learners compare their response to the answer key and self-mark.

## 6. Functional requirements

### Library and ingestion

- Accept PDF and DOCX uploads only, with a 20 MB per-file limit.
- Extract page text and keep the original uploaded file.
- Skip extraction when the uploaded file’s SHA-256 matches a registered source file.
- If file bytes differ, continue with extracted-text duplicate detection so reformatted copies can still be recognized.
- Allow the user to register an upload already present in the uploads directory without moving it.
- Provide collection creation, assignment, removal, and deletion independently from document deletion.

### Indexing and retrieval

- Split extracted page text into chunks and index it in FTS5.
- Reuse unchanged chunks and embeddings when the stored content hash and chunker version match.
- Provide progress and an error state for indexing.
- Use keyword retrieval when embeddings are unavailable; use hybrid keyword and embedding retrieval when configured embeddings are available.
- Include document names, page ranges, and excerpts with retrieved passages.

### Questions and study materials

- Answer questions only from retrieved passages; expose citations to those passages.
- Generate material only from the selected scope and retrieved passages.
- Keep source IDs as internal metadata; show understandable source links in quiz items using filenames and pages.
- Let the user choose quiz type, difficulty, and question count. Generate large quizzes in batches of up to 10 items and avoid repeating earlier question stems.
- Keep generated material saved in the local database until the user deletes it or removes a document it references.
- Provide hints, explanations, result counts, and a retry focused on missed quiz items.

### Safety and recovery

- Require explicit confirmation before deleting a document.
- Create a verified database backup before document deletion.
- Include the current database and registered uploaded files in a downloadable ZIP backup.
- Validate archive paths, database integrity/schema, and required uploaded files before restore.
- Create a safety backup of the current library before replacing it during restore.

## 7. Non-functional requirements

- Run locally on Windows through a loopback-only web server (`127.0.0.1:8000` by default).
- Keep the interface usable on small windows and provide keyboard focus visibility and status/error announcements.
- Continue to provide keyword search if Ollama or the embedding model is unavailable.
- Avoid remote font or JavaScript dependencies in the UI.
- Keep database schema changes versioned and create/verify backups before applying migrations.

## 8. Success measures

- A learner can add, index, find, view, and remove documents without losing unrelated library data.
- Search results and answers link to the relevant source file and page.
- Re-uploading the same file skips parsing; a different file with identical text is still detected.
- A user can create and reopen each supported study-material type from indexed sources.
- A downloaded backup can be restored, and the current library is backed up before restore.
- Core library and keyword-search functions remain available when local AI models are offline.

## 9. Assumptions and dependencies

- Python dependencies are installed in `.venv` from `requirements.txt`.
- Ollama is installed and running for generation; the default text model is `qwen3-personal:latest`.
- `nomic-embed-text` is optional and enables semantic retrieval when installed in Ollama.
- The learner supplies supported documents and indexes them before asking questions or generating materials.

## 10. Current known limitations

- Scanned or image-only PDFs are not searchable because OCR was removed from the project.
- Local model quality, speed, and maximum practical quiz size depend on the installed model and computer hardware.
- Short-answer quiz results are self-assessed. Fill-in-the-blank checking normalizes text but may not accept every valid alternative wording.
- The API currently declares the document-file GET route twice in `app/main.py`; consolidate it as a maintenance cleanup.
