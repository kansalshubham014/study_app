
import json
import re
from typing import Any

from app.ollama import ask_ollama


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

PAGES_PER_BATCH = 4
MAX_CHARS_PER_BATCH = 9000
MAX_AUDIT_CHARS = 10000

VALID_CONFIDENCE = {"high", "medium", "low"}
VALID_DETECTION_TYPES = {"explicit", "inferred"}


# ---------------------------------------------------------
# Common helpers
# ---------------------------------------------------------

def _clean_text(value: str) -> str:
    if not value:
        return ""

    value = re.sub(
        r"<think>.*?</think>",
        "",
        value,
        flags=re.DOTALL
    )

    value = value.replace("\u200b", "")
    value = value.replace("\ufeff", "")
    value = value.replace("\xa0", " ")

    return value.strip()


def _get_field(record: Any, key: str, default=None):
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return default


def _parse_json(response: str) -> dict:
    response = _clean_text(response)

    response = re.sub(
        r"^```(?:json)?\s*",
        "",
        response
    )
    response = re.sub(
        r"\s*```$",
        "",
        response
    )

    start = response.find("{")
    end = response.rfind("}")

    if start < 0 or end <= start:
        raise ValueError("Qwen did not return valid JSON.")

    result = json.loads(response[start:end + 1])

    if not isinstance(result, dict):
        raise ValueError("Qwen response must be a JSON object.")

    return result


def _ask_for_json(prompt: str) -> dict:
    response = ask_ollama(prompt)

    try:
        return _parse_json(response)

    except (ValueError, json.JSONDecodeError):
        retry_prompt = (
            "Return ONLY one valid JSON object. "
            "Do not include markdown, explanations or thinking text.\n\n"
            + prompt
        )

        return _parse_json(ask_ollama(retry_prompt))


def _normalise_pages(pages: list) -> list:
    normalized = []

    for page in pages:
        page_number = (
            _get_field(page, "page")
            or _get_field(page, "page_number")
        )

        page_text = (
            _get_field(page, "text")
            or _get_field(page, "page_text")
            or ""
        )

        if page_number is None:
            continue

        try:
            page_number = int(page_number)
        except (TypeError, ValueError):
            continue

        normalized.append({
            "page": page_number,
            "text": _clean_text(str(page_text))
        })

    normalized.sort(key=lambda item: item["page"])

    return normalized


def _make_batches(pages: list) -> list:
    batches = []
    current = []
    current_chars = 0

    for page in pages:
        page_size = len(page["text"])

        should_flush = (
            current
            and (
                len(current) >= PAGES_PER_BATCH
                or current_chars + page_size > MAX_CHARS_PER_BATCH
            )
        )

        if should_flush:
            batches.append(current)
            current = []
            current_chars = 0

        # A single unusually long page is split into smaller pieces.
        # The same page number is retained for every piece.
        if page_size > MAX_CHARS_PER_BATCH:
            if current:
                batches.append(current)
                current = []
                current_chars = 0

            for start in range(0, page_size, MAX_CHARS_PER_BATCH):
                batches.append([{
                    "page": page["page"],
                    "text": page["text"][
                        start:start + MAX_CHARS_PER_BATCH
                    ]
                }])
            continue

        current.append(page)
        current_chars += page_size

    if current:
        batches.append(current)

    return batches


def _format_pages(pages: list) -> str:
    return "\n\n".join(
        f"[PAGE {page['page']}]\n{page['text']}"
        for page in pages
    )


def _confidence(value: Any) -> str:
    value = str(value or "low").lower()
    return value if value in VALID_CONFIDENCE else "low"


def _detection_type(value: Any) -> str:
    value = str(value or "inferred").lower()

    return (
        value
        if value in VALID_DETECTION_TYPES
        else "inferred"
    )


def _title_key(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value).strip())
    return value.casefold()


def _confidence_rank(value: str) -> int:
    return {
        "low": 1,
        "medium": 2,
        "high": 3
    }.get(value, 0)


def _progress(message: str):
    print(f"[Structure Detection] {message}", flush=True)


def _clean_section(item: dict, level: int) -> dict | None:
    if not isinstance(item, dict):
        return None

    title = str(item.get("title", "")).strip()

    try:
        start_page = int(item.get("start_page"))
    except (TypeError, ValueError):
        return None

    if not title:
        return None

    return {
        "title": title[:180],
        "start_page": start_page,
        "level": level,
        "parent_title": (
            str(item.get("parent_title", "")).strip()
            if level == 2 else None
        ),
        "detection_type": _detection_type(
            item.get("detection_type")
        ),
        "confidence": _confidence(item.get("confidence")),
        "boundary_evidence": str(
            item.get("boundary_evidence", "")
        ).strip()[:500]
    }


# ---------------------------------------------------------
# Pass 1: Main section detection
# ---------------------------------------------------------

def _detect_main_sections(
    batch: list,
    previous_context: str = ""
) -> dict:
    first_page = batch[0]["page"]
    last_page = batch[-1]["page"]

    prompt = f"""
You are analysing the structure of a document.

TASK: Identify MAIN SECTIONS ONLY.

The document may be a textbook, report, manual, research paper,
business document, training material, or another document type.

Do not assume a particular document type or heading style.

PREVIOUS CONTEXT:
{previous_context or "No previous context."}

PAGES:
{_format_pages(batch)}

RULES:

1. Identify only genuine top-level sections.
2. Do not return subsections in this pass.
3. A main section must represent a meaningful change in
   subject or a clearly identifiable major document heading.
4. Do not treat every bold heading, example, definition,
   bullet, or minor topic as a main section.
5. Numbering alone does not determine hierarchy.
6. A table of contents entry alone is insufficient evidence.
   Prefer evidence from actual section content.
7. Do not duplicate a section that started in an earlier batch.
8. Only report starts visible on pages {first_page} to {last_page}.
9. Do not invent titles or page numbers.
10. If no new main section begins in this batch, return [].
11. Do not create an artificial introduction section.

Return ONLY JSON:

{{
  "sections": [
    {{
      "title": "Main section title",
      "start_page": 2,
      "detection_type": "explicit",
      "confidence": "high",
      "boundary_evidence": "Evidence visible in the supplied pages"
    }}
  ],
  "batch_notes": "Brief structural observation"
}}
"""

    result = _ask_for_json(prompt)
    raw_sections = result.get("sections", [])

    if not isinstance(raw_sections, list):
        raw_sections = []

    sections = []

    for item in raw_sections:
        section = _clean_section(item, 1)

        if section is None:
            continue

        if first_page <= section["start_page"] <= last_page:
            sections.append(section)

    return {
        "sections": sections,
        "batch_notes": str(
            result.get("batch_notes", "")
        ).strip()
    }


# ---------------------------------------------------------
# Pass 2: Global subsection detection
# ---------------------------------------------------------

def _detect_subsections_batch(
    batch: list,
    candidate_parents: list
) -> list:
    if not candidate_parents:
        return []

    first_page = batch[0]["page"]
    last_page = batch[-1]["page"]

    parent_context = [
        {
            "title": parent["title"],
            "start_page": parent["start_page"],
            "end_page": parent["end_page"]
        }
        for parent in candidate_parents
    ]

    prompt = f"""
You are analysing the internal hierarchy of a document.

TASK: Identify genuine LEVEL 2 SUBSECTIONS.

DOCUMENT PAGES:
{_format_pages(batch)}

ESTABLISHED MAIN SECTIONS:
{json.dumps(parent_context, ensure_ascii=False)}

RULES:

1. Identify only meaningful internal divisions of a main section.
2. Every subsection MUST belong to exactly one listed parent.
3. Return the exact parent title in parent_title.
4. Do not create another main section in this pass.
5. Do not treat examples, bullets, definitions or ordinary
   paragraphs as subsections.
6. Use the document's actual headings and topic transitions.
7. Do not invent titles, parent names or page numbers.
8. Only report subsection starts visible on pages
   {first_page} to {last_page}.
9. The start page must fall within the parent's page range.
10. Do not repeat a subsection that began on an earlier page.
11. If no genuine subsection begins in this batch, return [].
12. A heading belonging to a different main section must
    not be assigned to the current parent.

Return ONLY JSON:

{{
  "subsections": [
    {{
      "title": "Subsection title",
      "parent_title": "Exact main section title",
      "start_page": 3,
      "detection_type": "explicit",
      "confidence": "high",
      "boundary_evidence": "Evidence from the supplied page"
    }}
  ]
}}
"""

    result = _ask_for_json(prompt)
    raw_items = result.get("subsections", [])

    if not isinstance(raw_items, list):
        return []

    valid_parents = {
        _title_key(parent["title"]): parent
        for parent in candidate_parents
    }

    detected = []

    for item in raw_items:
        section = _clean_section(item, 2)

        if section is None:
            continue

        if not first_page <= section["start_page"] <= last_page:
            continue

        parent_key = _title_key(section["parent_title"])

        parent = valid_parents.get(parent_key)

        # Do not guess a parent when multiple candidates exist.
        if parent is None:
            continue

        if not (
            parent["start_page"]
            <= section["start_page"]
            <= parent["end_page"]
        ):
            continue

        detected.append(section)

    return detected


# ---------------------------------------------------------
# Global audit: Review, do not overwrite blindly
# ---------------------------------------------------------

def _build_page_map(pages: list) -> str:
    parts = []

    for page in pages:
        text = re.sub(r"\s+", " ", page["text"]).strip()

        # Keep a compact preview from every page.
        preview = text[:200]

        parts.append(
            f"Page {page['page']}: {preview}"
        )

    result = "\n".join(parts)

    if len(result) <= MAX_AUDIT_CHARS:
        return result

    # Preserve coverage across the whole document rather than
    # simply truncating the end of the document.
    if len(parts) == 1:
        return parts[0][:MAX_AUDIT_CHARS]

    selected = []
    step = max(1, len(parts) // 40)

    for index in range(0, len(parts), step):
        selected.append(parts[index])

    return "\n".join(selected)[:MAX_AUDIT_CHARS]


def _audit_main_sections(
    pages: list,
    chapters: list
) -> dict:
    page_map = _build_page_map(pages)

    candidate_sections = [
        {
            "title": item["title"],
            "start_page": item["start_page"],
            "confidence": item["confidence"]
        }
        for item in chapters
    ]

    prompt = f"""
You are an independent document structure auditor.

Review the proposed MAIN SECTION boundaries against this
document-wide page overview.

DOCUMENT PAGE MAP:
{page_map}

PROPOSED MAIN SECTIONS:
{json.dumps(candidate_sections, ensure_ascii=False)}

Find possible:
- Duplicate main sections.
- Missing major topics.
- Incorrect section start pages.
- Subsections incorrectly classified as main sections.
- Main sections incorrectly classified as subsections.

RULES:

1. Do not invent sections.
2. Do not use external knowledge.
3. Preserve valid existing sections.
4. If uncertain, report uncertainty rather than guessing.
5. Only report evidence visible in the page map.
6. Do not silently remove a valid section.
7. Do not assume that every page starts a new section.

Return ONLY JSON:

{{
  "issues": [
    {{
      "issue": "Description of possible structural issue",
      "related_title": "Existing section title or empty",
      "suggested_action": "Review, retain or investigate",
      "confidence": "medium",
      "evidence": "Evidence from page map"
    }}
  ],
  "audit_notes": "Overall audit observations"
}}
"""

    result = _ask_for_json(prompt)

    issues = result.get("issues", [])

    if not isinstance(issues, list):
        issues = []

    return {
        "issues": [
            item for item in issues
            if isinstance(item, dict)
        ],
        "audit_notes": str(
            result.get("audit_notes", "")
        ).strip()
    }


# ---------------------------------------------------------
# Structural validation
# ---------------------------------------------------------

def _merge_duplicate_sections(sections: list) -> list:
    merged = {}

    for section in sections:
        key = (
            section["start_page"],
            _title_key(section["title"]),
            _title_key(section.get("parent_title") or "")
        )

        existing = merged.get(key)

        if existing is None:
            merged[key] = section
            continue

        if (
            _confidence_rank(section["confidence"])
            > _confidence_rank(existing["confidence"])
        ):
            merged[key] = section

    result = list(merged.values())

    result.sort(
        key=lambda item: (
            item["start_page"],
            _title_key(item["title"])
        )
    )

    return result


def _validate_main_sections(
    sections: list,
    first_page: int,
    last_page: int
) -> list:
    valid = []

    for section in _merge_duplicate_sections(sections):
        if not first_page <= section["start_page"] <= last_page:
            continue

        if not section["title"].strip():
            continue

        valid.append(section)

    # One main-section boundary per page.
    by_page = {}

    for section in valid:
        page = section["start_page"]
        existing = by_page.get(page)

        if existing is None or (
            _confidence_rank(section["confidence"])
            > _confidence_rank(existing["confidence"])
        ):
            by_page[page] = section

    return sorted(
        by_page.values(),
        key=lambda item: item["start_page"]
    )


# ---------------------------------------------------------
# Build final hierarchy
# ---------------------------------------------------------

def _build_chapters(
    main_sections: list,
    all_pages: list,
    detected_subsections: list
) -> list:
    if not main_sections:
        return []

    last_page = all_pages[-1]["page"]
    chapters = []

    for index, section in enumerate(main_sections):
        start_page = section["start_page"]

        if index + 1 < len(main_sections):
            end_page = main_sections[index + 1]["start_page"] - 1
        else:
            end_page = last_page

        if end_page < start_page:
            raise ValueError(
                f"Invalid range for section: {section['title']}"
            )

        chapter = {
            "chapter_number": index + 1,
            "title": section["title"],
            "start_page": start_page,
            "end_page": end_page,
            "level": 1,
            "detection_type": section["detection_type"],
            "confidence": section["confidence"],
            "boundary_evidence": section["boundary_evidence"],
            "subsections": []
        }

        children = [
            child for child in detected_subsections
            if (
                _title_key(child.get("parent_title", ""))
                == _title_key(section["title"])
                and start_page <= child["start_page"] <= end_page
            )
        ]

        children = _merge_duplicate_sections(children)

        # Keep one subsection boundary per page.
        unique_children = {}

        for child in children:
            page = child["start_page"]
            existing = unique_children.get(page)

            if existing is None or (
                _confidence_rank(child["confidence"])
                > _confidence_rank(existing["confidence"])
            ):
                unique_children[page] = child

        children = sorted(
            unique_children.values(),
            key=lambda item: item["start_page"]
        )

        for child_index, child in enumerate(children):
            if child_index + 1 < len(children):
                child_end = (
                    children[child_index + 1]["start_page"] - 1
                )
            else:
                child_end = end_page

            child_end = max(
                child["start_page"],
                min(child_end, end_page)
            )

            chapter["subsections"].append({
                "title": child["title"],
                "start_page": child["start_page"],
                "end_page": child_end,
                "level": 2,
                "detection_type": child["detection_type"],
                "confidence": child["confidence"],
                "boundary_evidence": child["boundary_evidence"]
            })

        chapters.append(chapter)

    # Validate main section ranges.
    previous_end = None

    for chapter in chapters:
        if not (
            chapter["start_page"]
            <= chapter["end_page"]
            <= last_page
        ):
            raise ValueError(
                f"Invalid chapter range: {chapter['title']}"
            )

        if previous_end is not None:
            if chapter["start_page"] != previous_end + 1:
                raise ValueError(
                    "Main chapter ranges are not contiguous."
                )

        previous_end = chapter["end_page"]

        for child in chapter["subsections"]:
            if not (
                chapter["start_page"]
                <= child["start_page"]
                <= child["end_page"]
                <= chapter["end_page"]
            ):
                raise ValueError(
                    f"Invalid subsection range: {child['title']}"
                )

    return chapters


# ---------------------------------------------------------
# Main public function
# ---------------------------------------------------------

def detect_document_structure(pages: list) -> dict:
    """
    Automatic document structure detection.

    Pass 1:
        Detect main sections in sequential page batches.

    Pass 2:
        Detect subsections in document-wide page batches
        and map each subsection to an established parent.

    Audit:
        Independently review main-section boundaries.
        Audit suggestions do not overwrite validated results.

    Final:
        Validate and return nested chapters and subsections.

    No human approval is required.
    """

    normalized_pages = _normalise_pages(pages)

    if not normalized_pages:
        return {
            "success": False,
            "detection_status": "no_text",
            "total_sections": 0,
            "total_subsections": 0,
            "sections": [],
            "error": "No readable page text was supplied."
        }

    first_page = normalized_pages[0]["page"]
    last_page = normalized_pages[-1]["page"]
    batches = _make_batches(normalized_pages)

    try:
        # ---------------------------------------------
        # PASS 1: Main sections
        # ---------------------------------------------

        detected_main = []
        batch_notes = []
        previous_context = ""

        _progress(
            f"Pass 1 started: {len(batches)} page batches."
        )

        for batch_number, batch in enumerate(batches, start=1):
            _progress(
                f"Pass 1 — batch {batch_number}/{len(batches)} "
                f"(pages {batch[0]['page']}-{batch[-1]['page']})."
            )

            result = _detect_main_sections(
                batch,
                previous_context
            )

            detected_main.extend(result["sections"])

            batch_notes.append({
                "batch": batch_number,
                "start_page": batch[0]["page"],
                "end_page": batch[-1]["page"],
                "notes": result["batch_notes"]
            })

            recent_titles = [
                item["title"] for item in result["sections"]
            ]

            if recent_titles:
                previous_context = (
                    "Recently detected main sections: "
                    + "; ".join(recent_titles[-5:])
                )

        main_sections = _validate_main_sections(
            detected_main,
            first_page,
            last_page
        )

        if not main_sections:
            return {
                "success": True,
                "detection_status": "structure_not_detected",
                "total_sections": 0,
                "total_subsections": 0,
                "sections": [],
                "batch_notes": batch_notes,
                "message": (
                    "No reliable main sections were detected. "
                    "The document remains one complete source. "
                    "No artificial chapter boundaries were created."
                )
            }

        # Calculate provisional parent ranges.
        parents = []

        for index, section in enumerate(main_sections):
            if index + 1 < len(main_sections):
                end_page = (
                    main_sections[index + 1]["start_page"] - 1
                )
            else:
                end_page = last_page

            parents.append({
                **section,
                "end_page": end_page
            })

        # ---------------------------------------------
        # PASS 2: Subsections in global batches
        # ---------------------------------------------

        detected_subsections = []

        _progress(
            f"Pass 2 started: {len(batches)} page batches."
        )

        for batch_number, batch in enumerate(batches, start=1):
            batch_first = batch[0]["page"]
            batch_last = batch[-1]["page"]

            candidate_parents = [
                parent for parent in parents
                if (
                    parent["start_page"] <= batch_last
                    and parent["end_page"] >= batch_first
                )
            ]

            if not candidate_parents:
                continue

            _progress(
                f"Pass 2 — batch {batch_number}/{len(batches)} "
                f"(pages {batch_first}-{batch_last})."
            )

            children = _detect_subsections_batch(
                batch,
                candidate_parents
            )

            detected_subsections.extend(children)

        detected_subsections = _merge_duplicate_sections(
            detected_subsections
        )

        # ---------------------------------------------
        # GLOBAL AUDIT
        # ---------------------------------------------

        _progress("Global structural audit started.")

        audit_status = "completed"
        audit_notes = ""
        audit_issues = []

        try:
            audit_result = _audit_main_sections(
                normalized_pages,
                main_sections
            )

            audit_notes = audit_result["audit_notes"]
            audit_issues = audit_result["issues"]

        except Exception as error:
            audit_status = "audit_failed_original_preserved"
            audit_notes = str(error)

        # Audit is advisory only. It never silently replaces
        # the original validated section boundaries.

        # ---------------------------------------------
        # BUILD FINAL HIERARCHY
        # ---------------------------------------------

        chapters = _build_chapters(
            main_sections,
            normalized_pages,
            detected_subsections
        )

        total_subsections = sum(
            len(chapter["subsections"])
            for chapter in chapters
        )

        first_detected_page = chapters[0]["start_page"]

        unassigned_pages = [
            page["page"]
            for page in normalized_pages
            if page["page"] < first_detected_page
        ]

        _progress(
            f"Completed: {len(chapters)} main sections, "
            f"{total_subsections} subsections."
        )

        return {
            "success": True,
            "detection_status": "sections_detected",
            "detection_method": "two_pass_global_batch_detection",
            "audit_status": audit_status,
            "total_sections": len(chapters),
            "total_subsections": total_subsections,
            "document_start_page": first_page,
            "document_end_page": last_page,
            "unassigned_pages_before_first_section": unassigned_pages,
            "sections": chapters,
            "batch_notes": batch_notes,
            "audit_notes": audit_notes,
            "audit_issues": audit_issues
        }

    except Exception as error:
        _progress(f"Detection failed: {error}")

        return {
            "success": False,
            "detection_status": "detection_failed",
            "total_sections": 0,
            "total_subsections": 0,
            "sections": [],
            "error": str(error)
        }
