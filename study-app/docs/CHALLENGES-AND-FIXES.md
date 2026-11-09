# Challenges and fixes applied

This log summarizes the main product and implementation problems addressed in Study Desk. It describes the current code and database state; it is not a test report.

## Applied fixes

| Challenge | Fix applied | Current result |
|---|---|---|
| Re-uploading an identical PDF/DOCX repeated text extraction. | Added a `source_sha256` field and index in migration 5. The upload endpoint checks the raw file hash before parsing. | Byte-identical files are recognized early. If bytes differ, extracted-text hash detection still handles duplicates. |
| Re-indexing unchanged documents did unnecessary work. | Added content/chunker-version checks, chunk reuse, and reuse of existing embeddings; embeddings are processed in batches. | Unchanged indexed content can reuse saved chunks and completed embeddings. |
| Search and generated study materials needed page-level grounding. | Store extracted pages and indexed chunks with page ranges; return filenames, excerpts, and page references from retrieval. | Search, answers, quiz items, guide sections, and concept nodes can link back to source pages. |
| Answers could refer to a source label that was not supplied or a page outside the retrieved range. | Validate answer citation IDs and page ranges and return a citation warning when a citation is not verifiable. | The user can inspect source links and see a warning when the answer’s inline citation needs review. |
| Quiz questions were generic, uneven in format, and could expose internal IDs such as `S3`. | Added type-specific prompts, distinct-question guidance, selectable type/difficulty/count, source-label cleanup, and a distributed passage sample. Larger quizzes are generated in batches of up to 10. | Learners choose multiple choice, short answer, or fill in the blank and 10–50 questions. Visible source links use filename/page labels. |
| Quiz review stopped at showing whether a choice was right. | Added hints, explanations, result counts, and a review path for missed questions. | A learner can retry only the questions they missed. Short answers remain self-assessed. |
| Study controls were crowded together. | Grouped flashcards, quizzes, study guides, and concept maps in separate responsive cards; kept quiz count visible in the quiz card; shortened the library hero. | Common study actions are separated by purpose and adapt to narrower screens. |
| A folder feature was confusing and overlapped with collections. | Removed folder controls from the active interface; retained collections as the supported way to group documents. | Collections are visible and supported. Legacy folder schema from an earlier migration may remain in the database. |
| Document deletion needed clearer consequences and a recovery path. | Added confirmation, names affected source files, backs up the database before deletion, removes explicit FTS/index/study references, and promotes a surviving duplicate when needed. | Deleting one document does not remove a shared source file still used by another library record. |
| A user needed a way to move or restore the whole library. | Added a ZIP backup containing the database and uploads, validation of restore contents, and a safety backup before replacement. | Backup/restore is available from the Library view. |
| Local AI or embeddings may be unavailable. | Added a sidebar status indicator and keyword-search fallback. | The app identifies unavailable Qwen/embedding models and can still provide keyword retrieval. |
| OCR added a separate dependency and setup burden. | Removed OCR integration, status UI, Tesseract configuration, and setup instructions at the user’s request. | Image-only PDFs are unsupported; selectable text PDFs and DOCX remain supported. |

## Database migration history

The current copied project database is at migration version 5. Each migration script creates and verifies a timestamped database backup before changing the schema.

| Version | Purpose |
|---|---|
| 1 | Multi-document study foundation, including collections, duplicate tracking, chunk/index tables, processing records, and supporting schema. |
| 2 | Persist generated flashcard and quiz sets. |
| 3 | Permit saved study guides. |
| 4 | Permit saved concept maps. |
| 5 | Store source-file hashes to recognize byte-identical re-uploads before text extraction. |

Migration scripts are in `app/migrate_database*.py`. For a fresh database, follow the ordered commands in the root `README.md`. Do not skip a version.

## Current limitations and follow-up notes

- The app does not perform OCR. Image-only PDFs will fail extraction and cannot be indexed.
- Short-answer grading is self-assessment; fill-in-the-blank uses normalized string comparison and can reject a valid synonym.
- 50-question quizzes require multiple local model calls and can take longer than smaller quizzes.
- The GET route for `/api/documents/{document_id}/file` is duplicated in `app/main.py`. The earlier handler validates the stored filename; a later duplicate handler should be removed so the route is declared once.
- No automated test suite is present in the current project tree. The migration scripts perform their own backup and database-integrity checks, but application behavior still needs manual review when changes are made.
