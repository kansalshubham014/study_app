
from pathlib import Path
import pymupdf
from docx import Document

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx"}


def extract_text(file_path: str) -> dict:
    path = Path(file_path)
    extension = path.suffix.lower()

    if extension == ".pdf":
        document = pymupdf.open(path)

        pages = []
        for page_number, page in enumerate(document, start=1):
            pages.append({"page": page_number, "text": page.get_text()})

        document.close()

        return {
            "filename": path.name,
            "type": "PDF",
            "pages": pages,
            "full_text": "\n".join(
                page["text"] for page in pages
            )
        }

    elif extension == ".docx":
        document = Document(path)

        paragraphs = [
            paragraph.text
            for paragraph in document.paragraphs
            if paragraph.text.strip()
        ]

        text = "\n".join(paragraphs)

        pages = ([{"page": 1, "text": text}] if text.strip() else [])

        return {
            "filename": path.name,
            "type": "DOCX",
            "pages": pages,
            "full_text": text
        }

    else:
        raise ValueError("Only PDF and DOCX files are supported.")
