from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
import tempfile
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.backup_restore import create_library_backup, restore_library_backup
from app.database import (
    get_analysis,
    get_document,
    get_document_pages,
    get_document_by_hash,
    get_document_by_source_hash,
    get_connection,
    init_db,
    list_documents,
    save_document,
)
from app.document_analysis import analyse_document
from app.document_processor import extract_text
from app.indexing import get_index_progress, index_document, sample_chunks, search_chunks
from app.library import (
    collection_document_ids,
    create_collection,
    delete_collection,
    delete_document,
    delete_study_material,
    list_collections,
    list_library,
    list_study_materials,
    list_unregistered_uploads,
    register_existing_upload,
    save_study_material,
    set_collection_document,
)
from app.ollama import EMBED_MODEL_NAME, check_ollama
from app.study_tools import (
    answer_question,
    generate_concept_map,
    generate_study_guide,
    generate_study_material,
)


BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = APP_DIR / "static"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx"}


class CollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)


class CollectionMembership(BaseModel):
    included: bool = True


class RegisterUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)


class DeleteDocumentRequest(BaseModel):
    confirm: bool = False


class ScopeRequest(BaseModel):
    document_ids: list[str] | None = None
    collection_id: str | None = None
    limit: int = Field(default=8, ge=1, le=20)


class SearchRequest(ScopeRequest):
    query: str = Field(min_length=1, max_length=2000)


class AskRequest(ScopeRequest):
    question: str = Field(min_length=1, max_length=4000)


class GenerateMaterialRequest(ScopeRequest):
    material_type: str
    topic: str = Field(default="", max_length=1000)
    count: int = Field(default=10, ge=3, le=50)
    quiz_format: str = Field(default="multiple_choice", pattern="^(multiple_choice|short_answer|fill_blank)$")
    difficulty: str = Field(default="balanced", pattern="^(balanced|challenging)$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with get_connection() as db:
        migrations_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        version = (
            db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if migrations_table else None
        )
    if version is None or version < 5:
        raise RuntimeError(
            "Study Desk database migrations are incomplete. Run the versioned "
            "migration scripts through app\\migrate_database_v5.py first."
        )
    yield


app = FastAPI(
    title="Personal AI Study Assistant",
    description="Private local AI study library with grounded multi-document study tools.",
    version="2.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _resolve_document_ids(document_ids: list[str] | None,
                          collection_id: str | None) -> list[str]:
    all_ids = {item["id"] for item in list_documents()}
    if document_ids is None:
        selected = set(all_ids)
    else:
        requested = set(document_ids)
        missing = requested - all_ids
        if missing:
            raise HTTPException(status_code=404,
                                detail=f"Documents not found: {sorted(missing)}")
        selected = requested
    if collection_id:
        try:
            collection_ids = set(collection_document_ids(collection_id))
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        selected &= collection_ids
    if selected:
        placeholders = ",".join("?" for _ in selected)
        with get_connection() as db:
            duplicate_links = db.execute(
                f"SELECT document_id, canonical_document_id FROM document_duplicates "
                f"WHERE document_id IN ({placeholders})",
                tuple(selected),
            ).fetchall()
        selected = (selected - {row["document_id"] for row in duplicate_links})
        selected.update(row["canonical_document_id"] for row in duplicate_links)
    return sorted(selected)


def _canonical_document_id(document_id: str) -> str:
    with get_connection() as db:
        row = db.execute(
            "SELECT canonical_document_id FROM document_duplicates WHERE document_id=?",
            (document_id,),
        ).fetchone()
    return row["canonical_document_id"] if row else document_id


def _as_http_error(error: Exception):
    if isinstance(error, LookupError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=400, detail=str(error)) from error
    raise HTTPException(status_code=500, detail="The requested operation failed.") from error


@app.get("/", response_class=FileResponse)
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health_check():
    return {"status": "running", "application": app.title, "version": app.version}


@app.get("/api/ollama-status")
def ollama_status():
    status = check_ollama()
    status["embedding_model"] = EMBED_MODEL_NAME
    return status


@app.post("/api/upload-document")
async def upload_document(file: UploadFile = File(...)):
    original_filename = (file.filename or "document").replace("\\", "/").split("/")[-1]
    extension = Path(original_filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported.")

    contents = await file.read(MAX_FILE_SIZE + 1)
    await file.close()
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Maximum upload size is 20 MB.")

    source_hash = sha256(contents).hexdigest()
    existing_file = get_document_by_source_hash(source_hash)
    if existing_file:
        return {
            "success": True,
            "duplicate": True,
            "document_id": existing_file["id"],
            "filename": existing_file["original_filename"],
            "database_saved": False,
            "message": "This exact file is already in your library; text extraction was skipped.",
        }

    temp_path = None
    final_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".incoming-", suffix=extension, dir=UPLOAD_DIR, delete=False
        ) as temp_file:
            temp_file.write(contents)
            temp_path = Path(temp_file.name)

        extraction = extract_text(str(temp_path))
        extracted_text = extraction["full_text"].strip()
        pages = extraction["pages"]
        if not extracted_text:
            raise HTTPException(
                status_code=422,
                detail="This file contains no selectable text. Image-only PDFs cannot be indexed by this app.",
            )

        content_hash = sha256(extracted_text.encode("utf-8")).hexdigest()
        existing = get_document_by_hash(content_hash)
        if existing:
            temp_path.unlink(missing_ok=True)
            temp_path = None
            return {
                "success": True,
                "duplicate": True,
                "document_id": existing["id"],
                "filename": existing["original_filename"],
                "database_saved": False,
                "message": "This document has the same extracted text as an existing library item.",
            }

        document_id = str(uuid4())
        stored_filename = document_id + extension
        final_path = UPLOAD_DIR / stored_filename
        temp_path.replace(final_path)
        temp_path = None
        save_document(
            document_id=document_id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            file_type=extraction["type"],
            pages=pages,
            full_text=extracted_text,
            content_sha256=content_hash,
            source_sha256=source_hash,
        )
        return {
            "success": True,
            "duplicate": False,
            "document_id": document_id,
            "filename": original_filename,
            "document_type": extraction["type"],
            "pages": len(pages),
            "characters_extracted": len(extracted_text),
            "database_saved": True,
            "preview": extracted_text[:1200],
            "message": "Document uploaded and saved to your local library.",
        }
    except HTTPException:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        if final_path:
            final_path.unlink(missing_ok=True)
        raise
    except Exception as error:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        if final_path:
            final_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail=f"Document processing failed: {error}",
        ) from error


@app.get("/api/documents")
def get_all_documents():
    documents = list_documents()
    return {"success": True, "total_documents": len(documents), "documents": documents}


@app.get("/api/library")
def get_library():
    return {
        "success": True,
        "documents": list_library(),
        "collections": list_collections(),
        "unregistered_uploads": list_unregistered_uploads(),
    }


@app.post("/api/library/backup")
def create_library_backup_download():
    try:
        backup_path = create_library_backup()
        return {"success": True,
                "download_url": f"/api/library/backup/{backup_path.name}"}
    except Exception as error:
        raise HTTPException(status_code=500,
                            detail=f"Could not create a library backup: {error}") from error


@app.get("/api/library/backup/{filename}")
def download_library_backup_file(filename: str):
    if (Path(filename).name != filename or not filename.startswith("study_desk_backup_")
            or Path(filename).suffix.lower() != ".zip"):
        raise HTTPException(status_code=404, detail="Backup not found.")
    backup_path = Path(__file__).resolve().parent.parent / "data" / "backups" / filename
    if not backup_path.is_file():
        raise HTTPException(status_code=404, detail="Backup not found.")
    return FileResponse(backup_path, media_type="application/zip", filename=filename)


@app.post("/api/library/restore")
async def restore_library(file: UploadFile = File(...)):
    if Path(file.filename or "").suffix.lower() != ".zip":
        await file.close()
        raise HTTPException(status_code=400, detail="Choose a Study Desk backup .zip file.")
    try:
        return await restore_library_backup(file)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500,
                            detail=f"Could not restore the library backup: {error}") from error


@app.get("/api/collections")
def get_collections():
    return {"success": True, "collections": list_collections()}


@app.post("/api/uploads/register")
def register_upload(payload: RegisterUploadRequest):
    try:
        return {"success": True, **register_existing_upload(payload.filename)}
    except Exception as error:
        _as_http_error(error)


@app.get("/api/documents/{document_id}")
def get_document_details(document_id: str):
    document = get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"success": True, "document": document,
            "pages": get_document_pages(document_id)}


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: str):
    document = get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    stored_filename = document["stored_filename"] or ""
    if Path(stored_filename).name != stored_filename or stored_filename in {".", ".."}:
        raise HTTPException(status_code=404, detail="The stored source filename is invalid.")
    path = UPLOAD_DIR / stored_filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The uploaded source file is missing.")
    media_type = (
        "application/pdf" if path.suffix.lower() == ".pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return FileResponse(path, media_type=media_type)


@app.delete("/api/documents/{document_id}")
def remove_document(document_id: str, payload: DeleteDocumentRequest):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirm document deletion first.")
    try:
        return delete_document(document_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500,
                            detail="The document could not be deleted.") from error


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: str):
    document = get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    path = UPLOAD_DIR / document["stored_filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The uploaded source file is missing.")
    media_type = (
        "application/pdf" if path.suffix.lower() == ".pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return FileResponse(path, media_type=media_type)


@app.post("/api/documents/{document_id}/analyze")
def analyze_saved_document(document_id: str):
    if get_document(document_id) is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    document_id = _canonical_document_id(document_id)
    try:
        return analyse_document(document_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502,
                            detail=f"Document analysis failed: {error}") from error


@app.get("/api/documents/{document_id}/analysis")
def get_saved_document_analysis(document_id: str):
    if get_document(document_id) is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    analysis = get_analysis(document_id)
    if analysis is None:
        canonical_id = _canonical_document_id(document_id)
        if canonical_id != document_id:
            document_id = canonical_id
            analysis = get_analysis(document_id)
    if analysis is None:
        raise HTTPException(status_code=404,
                            detail="No saved analysis found. Analyze the document first.")
    return {"success": True, "document_id": document_id, "analysis": analysis}


@app.post("/api/collections")
def add_collection(payload: CollectionRequest):
    try:
        return {"success": True,
                "collection": create_collection(payload.name, payload.description)}
    except Exception as error:
        _as_http_error(error)


@app.delete("/api/collections/{collection_id}")
def remove_collection(collection_id: str):
    try:
        return {"success": True, **delete_collection(collection_id)}
    except Exception as error:
        _as_http_error(error)


@app.put("/api/collections/{collection_id}/documents/{document_id}")
def update_collection_membership(collection_id: str, document_id: str,
                                 payload: CollectionMembership):
    try:
        set_collection_document(collection_id, document_id, payload.included)
        return {"success": True}
    except Exception as error:
        _as_http_error(error)


@app.post("/api/documents/{document_id}/index")
def index_saved_document(document_id: str):
    try:
        return index_document(document_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/documents/{document_id}/index-progress")
def document_index_progress(document_id: str):
    try:
        return get_index_progress(document_id)
    except Exception as error:
        raise HTTPException(status_code=500,
                            detail=f"Could not read indexing progress: {error}") from error


@app.post("/api/search")
def search_library(payload: SearchRequest):
    document_ids = _resolve_document_ids(payload.document_ids, payload.collection_id)
    return {"success": True, **search_chunks(payload.query, document_ids, payload.limit)}


@app.post("/api/ask")
def ask_library(payload: AskRequest):
    document_ids = _resolve_document_ids(payload.document_ids, payload.collection_id)
    retrieval = search_chunks(payload.question, document_ids, payload.limit)
    if not retrieval["results"]:
        raise HTTPException(
            status_code=404,
            detail="No matching indexed passages. Index the selected documents first.",
        )
    try:
        answer = answer_question(payload.question, retrieval["results"])
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Local AI request failed: {error}") from error
    return {"success": True, **answer,
            "retrieval_mode": retrieval["mode"],
            "embedding_warning": retrieval["embedding_error"]}


@app.post("/api/study-materials/generate")
def generate_material(payload: GenerateMaterialRequest):
    if payload.material_type not in {"flashcards", "quiz", "study_guide", "concept_map"}:
        raise HTTPException(status_code=400,
                            detail="Choose flashcards, a quiz, a study guide, or a concept map.")
    document_ids = _resolve_document_ids(payload.document_ids, payload.collection_id)
    if payload.topic.strip():
        retrieval = search_chunks(payload.topic, document_ids, max(payload.limit, 12))
        source_results = retrieval["results"]
    else:
        source_results = sample_chunks(document_ids, 12)
        retrieval = {"mode": "representative passages", "embedding_error": None}
    if not source_results:
        raise HTTPException(
            status_code=404,
            detail="No matching indexed passages. Index the selected documents first.",
        )
    try:
        if payload.material_type == "study_guide":
            content = generate_study_guide(payload.topic, source_results)
        elif payload.material_type == "concept_map":
            content = generate_concept_map(payload.topic, source_results)
        else:
            content = generate_study_material(
                payload.material_type, payload.topic, source_results, payload.count,
                payload.quiz_format, payload.difficulty,
            )
        title = content["title"]
        material = save_study_material(
            payload.material_type,
            title,
            {"document_ids": document_ids, "collection_id": payload.collection_id},
            content,
        )
        return {"success": True, "material": material,
                "retrieval_mode": retrieval["mode"]}
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Local AI request failed: {error}") from error


@app.get("/api/study-materials")
def get_study_materials(limit: int = 50):
    return {"success": True, "materials": list_study_materials(limit)}


@app.delete("/api/study-materials/{material_id}")
def remove_study_material(material_id: str):
    try:
        return {"success": True, **delete_study_material(material_id)}
    except Exception as error:
        _as_http_error(error)

