
import json
import re
from typing import Any

from app.database import (
    get_document,
    get_document_pages,
    save_analysis_results,
)
from app.structure_detector import detect_document_structure
from app.grounding import (
    generate_grounded_content,
    verify_grounding,
)
from app.ollama import ask_ollama


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

CHUNK_SIZE = 12000
MAX_TOPICS = 20
MAX_KEY_POINTS = 25

JSON_REPAIR_ATTEMPTS = 1

MAX_GROUP_CHARS = 7000
MAX_GROUP_SUMMARY_CHARS = 1000
MAX_FINAL_SYNTHESIS_CHARS = 15000


# ---------------------------------------------------------
# GENERAL HELPERS
# ---------------------------------------------------------

def _get_field(data: Any, key: str, default=None):
    if isinstance(data, dict):
        return data.get(key, default)

    return getattr(data, key, default)


def _clean_string_list(value, limit=None):
    if not isinstance(value, list):
        return []

    cleaned = []

    for item in value:
        if isinstance(item, str):
            item = item.strip()

        elif isinstance(item, dict):
            item = (
                item.get("topic")
                or item.get("point")
                or item.get("text")
                or item.get("title")
                or ""
            )
            item = str(item).strip()

        else:
            continue

        if item and item not in cleaned:
            cleaned.append(item)

    return cleaned[:limit] if limit else cleaned


def _clean_model_response(response: str) -> str:
    if not isinstance(response, str):
        response = str(response)

    response = response.strip()

    response = re.sub(
        r"<think>.*?</think>",
        "",
        response,
        flags=re.DOTALL,
    ).strip()

    response = re.sub(
        r"^```(?:json)?\s*",
        "",
        response,
        flags=re.IGNORECASE,
    )

    response = re.sub(r"\s*```$", "", response)

    return response.strip()


def _parse_generated_json(response: str) -> dict:
    cleaned = _clean_model_response(response)

    try:
        parsed = json.loads(cleaned)

        if isinstance(parsed, dict):
            return parsed

    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start:end + 1])

            if isinstance(parsed, dict):
                return parsed

        except json.JSONDecodeError:
            pass

    raise ValueError(
        "Qwen returned invalid JSON. "
        f"Response preview: {cleaned[:1000]}"
    )


def _repair_generated_json(
    original_response: str,
    task_description: str,
) -> str:
    repair_prompt = f"""
You are a JSON formatting repair assistant.

Convert the supplied response into valid JSON.

Rules:
- Return one valid JSON object only.
- Do not use Markdown code fences.
- Do not add explanations.
- Preserve the original meaning and information.
- Do not invent missing information.
- Correct commas, quotation marks, brackets and JSON syntax.
- Do not remove useful content.

Expected task:
{task_description}

Response to repair:
{original_response}
"""

    return ask_ollama(
        repair_prompt,
        timeout=300,
        options={"temperature": 0.1},
    )


def _extract_generated_text(result: Any) -> str:
    """
    Read the actual dictionary returned by
    generate_grounded_content().
    """

    if not isinstance(result, dict):
        raise RuntimeError(
            "Unexpected response from generate_grounded_content()."
        )

    if not result.get("success"):
        raise RuntimeError(
            result.get("error")
            or "Qwen content generation failed."
        )

    content = result.get("content")

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            "Qwen generation succeeded but returned empty content."
        )

    return content


def _generate_json(
    task,
    passages,
    output_instructions="",
):
    """
    Generate JSON using the document-first grounding policy.

    Verification is advisory. Invalid JSON receives one
    formatting repair attempt.
    """

    result = generate_grounded_content(
        task=task,
        passages=passages,
        output_instructions=output_instructions,
        verify=False,
    )

    response_text = _extract_generated_text(result)

    try:
        parsed = _parse_generated_json(response_text)

        return {
            "data": parsed,
            "json_repaired": False,
            "original_response": response_text,
        }

    except ValueError:
        if JSON_REPAIR_ATTEMPTS <= 0:
            raise

    repaired_response = _repair_generated_json(
        response_text,
        task,
    )

    try:
        parsed = _parse_generated_json(repaired_response)

        return {
            "data": parsed,
            "json_repaired": True,
            "original_response": response_text,
        }

    except ValueError as error:
        raise RuntimeError(
            "Qwen returned malformed JSON, and the automatic "
            "repair attempt could not correct it. "
            f"Original response: {response_text[:1500]}"
        ) from error


def _advisory_verification(content, passages):
    """
    Verification is advisory and must never discard
    generated study content.
    """

    try:
        return verify_grounding(
            generated_content=content,
            passages=passages,
        )

    except Exception as error:
        return {
            "supported": None,
            "status": "verification_unavailable",
            "message": str(error),
        }


# ---------------------------------------------------------
# PAGE NORMALIZATION AND CHUNKING
# ---------------------------------------------------------

def _normalise_page_records(pages):
    """
    Normalize page records returned by SQLite.

    Supports both database field names:
    - page_number / page_text
    - page / text
    """

    normalized = []

    for index, page in enumerate(pages or [], start=1):

        if isinstance(page, dict):
            page_number = (
                page.get("page_number")
                or page.get("page")
                or index
            )

            page_text = (
                page.get("page_text")
                or page.get("text")
                or ""
            )

        else:
            try:
                page_number = (
                    page["page_number"]
                    or page["page"]
                )
            except (KeyError, IndexError, TypeError):
                page_number = index

            try:
                page_text = (
                    page["page_text"]
                    or page["text"]
                    or ""
                )
            except (KeyError, IndexError, TypeError):
                page_text = ""

        page_text = str(page_text or "").strip()

        if not page_text:
            continue

        normalized.append({
            "page": int(page_number),
            "text": page_text,
        })

    return normalized


def _make_source_chunks(pages, max_chars=CHUNK_SIZE):
    chunks = []
    current_pages = []
    current_chars = 0

    for page in pages:
        page_number = page["page"]
        page_text = page["text"]

        if len(page_text) > max_chars:
            if current_pages:
                chunks.append(current_pages)
                current_pages = []
                current_chars = 0

            for start in range(0, len(page_text), max_chars):
                part = page_text[start:start + max_chars]

                chunks.append([{
                    "page": page_number,
                    "text": part,
                }])

            continue

        if current_pages and current_chars + len(page_text) > max_chars:
            chunks.append(current_pages)
            current_pages = []
            current_chars = 0

        current_pages.append({
            "page": page_number,
            "text": page_text,
        })

        current_chars += len(page_text)

    if current_pages:
        chunks.append(current_pages)

    return chunks


def _pages_in_range(pages, start_page, end_page):
    return [
        page
        for page in pages
        if start_page <= page["page"] <= end_page
    ]


# ---------------------------------------------------------
# SOURCE CHUNK ANALYSIS
# ---------------------------------------------------------

def _analyse_source_chunk(
    chunk_pages,
    section_title,
    chunk_number,
    total_chunks,
):
    passages = [
        {
            "page": page["page"],
            "text": page["text"],
        }
        for page in chunk_pages
    ]

    start_page = min(page["page"] for page in chunk_pages)
    end_page = max(page["page"] for page in chunk_pages)

    task = f"""
Analyse this part of a document for a personal study application.

Document section: {section_title}
Chunk: {chunk_number} of {total_chunks}
Source pages: {start_page}-{end_page}

Identify:
- Main ideas and concepts
- Definitions and terminology
- Important explanations
- Processes and sequences
- Examples
- Relationships between concepts
- Important facts and distinctions

You may explain relationships between concepts and use
reasonable interpretation. Do not invent quotations,
names, dates, figures or page references.

Return JSON:

{{
  "summary": "Concise but informative explanation",
  "topics": ["Topic 1", "Topic 2"],
  "key_points": ["Important point 1", "Important point 2"],
  "important_terms": [
    {{
      "term": "Term",
      "definition": "Explanation"
    }}
  ],
  "page_references": [
    {{
      "page": 1,
      "relevance": "What this page contains"
    }}
  ]
}}
"""

    generated = _generate_json(
        task=task,
        passages=passages,
        output_instructions=(
            "Return valid JSON only. Use clear study-friendly "
            "language. Preserve the document's meaning. "
            "Do not invent page numbers."
        ),
    )

    data = generated["data"]

    return {
        "summary": str(data.get("summary", "")).strip(),
        "topics": _clean_string_list(
            data.get("topics"),
            MAX_TOPICS,
        ),
        "key_points": _clean_string_list(
            data.get("key_points"),
            MAX_KEY_POINTS,
        ),
        "important_terms": (
            data.get("important_terms", [])
            if isinstance(data.get("important_terms"), list)
            else []
        ),
        "page_references": (
            data.get("page_references", [])
            if isinstance(data.get("page_references"), list)
            else []
        ),
        "start_page": start_page,
        "end_page": end_page,
        "json_repaired": generated["json_repaired"],
    }


# ---------------------------------------------------------
# SECTION SYNTHESIS
# ---------------------------------------------------------

def _synthesise_section(
    section_title,
    section_pages,
    chunk_results,
):
    source_text = json.dumps(
        chunk_results,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    passages = [{
        "page": section_pages[0]["page"],
        "text": source_text,
    }]

    task = f"""
Create a unified study analysis for this document section.

Section title: {section_title}

The supplied material contains analyses of different parts
of the same section. Combine them into one coherent analysis.

Requirements:
- Preserve important information from all chunks.
- Remove repetition.
- Connect related concepts.
- Do not lose important definitions or distinctions.
- Do not invent facts or page references.
- Retain page references wherever available.

Return JSON:

{{
  "summary": "Complete section summary",
  "topics": ["Topic 1"],
  "key_points": ["Key point 1"],
  "important_terms": [
    {{
      "term": "Term",
      "definition": "Definition"
    }}
  ],
  "page_references": [
    {{
      "page": 1,
      "relevance": "Explanation"
    }}
  ]
}}
"""

    generated = _generate_json(
        task=task,
        passages=passages,
        output_instructions=(
            "Return valid JSON only. Preserve all important "
            "section information."
        ),
    )

    data = generated["data"]

    return {
        "summary": str(data.get("summary", "")).strip(),
        "topics": _clean_string_list(
            data.get("topics"),
            MAX_TOPICS,
        ),
        "key_points": _clean_string_list(
            data.get("key_points"),
            MAX_KEY_POINTS,
        ),
        "important_terms": (
            data.get("important_terms", [])
            if isinstance(data.get("important_terms"), list)
            else []
        ),
        "page_references": (
            data.get("page_references", [])
            if isinstance(data.get("page_references"), list)
            else []
        ),
        "json_repaired": generated["json_repaired"],
    }


def _analyse_section(pages, section):
    section_title = section.get("title", "Untitled section")
    start_page = int(section["start_page"])
    end_page = int(section["end_page"])

    section_pages = _pages_in_range(
        pages,
        start_page,
        end_page,
    )

    if not section_pages:
        raise RuntimeError(
            f"No extracted text found for section "
            f"'{section_title}' ({start_page}-{end_page})."
        )

    chunks = _make_source_chunks(section_pages)
    chunk_results = []

    print(
        f"[Document Analysis] Analysing section: "
        f"{section_title} | Pages {start_page}-{end_page} "
        f"| {len(chunks)} chunk(s)"
    )

    for chunk_number, chunk in enumerate(chunks, start=1):
        print(
            f"[Document Analysis]   Chunk "
            f"{chunk_number}/{len(chunks)}"
        )

        result = _analyse_source_chunk(
            chunk_pages=chunk,
            section_title=section_title,
            chunk_number=chunk_number,
            total_chunks=len(chunks),
        )

        chunk_results.append(result)

    print(
        f"[Document Analysis] Synthesising section: "
        f"{section_title}"
    )

    synthesis = _synthesise_section(
        section_title=section_title,
        section_pages=section_pages,
        chunk_results=chunk_results,
    )

    subsections = section.get("subsections", [])

    return {
        "chapter_number": section.get("chapter_number"),
        "chapter_title": section_title,
        "start_page": start_page,
        "end_page": end_page,
        "summary": synthesis["summary"],
        "topics": synthesis["topics"],
        "key_points": synthesis["key_points"],
        "important_terms": synthesis["important_terms"],
        "page_references": synthesis["page_references"],
        "subsections": subsections,
        "subsections_json": subsections,
        "json_repaired": (
            any(item["json_repaired"] for item in chunk_results)
            or synthesis["json_repaired"]
        ),
    }


# ---------------------------------------------------------
# DOCUMENT-WIDE SYNTHESIS
# ---------------------------------------------------------

def _make_synthesis_groups(chapter_analyses):
    groups = []
    current_group = []

    for chapter in chapter_analyses:
        candidate = current_group + [chapter]

        candidate_text = json.dumps(
            candidate,
            ensure_ascii=False,
            indent=2,
        )

        if current_group and len(candidate_text) > MAX_GROUP_CHARS:
            groups.append(current_group)
            current_group = [chapter]
        else:
            current_group = candidate

    if current_group:
        groups.append(current_group)

    return groups


def _summarise_chapter_group(
    group,
    group_number,
    total_groups,
):
    compact_chapters = []

    for chapter in group:
        compact_chapters.append({
            "chapter_title": chapter.get("chapter_title"),
            "start_page": chapter.get("start_page"),
            "end_page": chapter.get("end_page"),
            "summary": str(
                chapter.get("summary", "")
            )[:MAX_GROUP_SUMMARY_CHARS],
            "topics": chapter.get("topics", [])[:10],
            "key_points": chapter.get("key_points", [])[:10],
            "important_terms": chapter.get(
                "important_terms", []
            )[:10],
        })

    source_text = json.dumps(
        compact_chapters,
        ensure_ascii=False,
        indent=2,
    )

    if len(source_text) > MAX_GROUP_CHARS:
        raise RuntimeError(
            f"Synthesis group {group_number} exceeds "
            f"{MAX_GROUP_CHARS} characters. "
            "Reduce MAX_GROUP_SUMMARY_CHARS."
        )

    start_page = min(
        int(chapter["start_page"])
        for chapter in group
    )

    end_page = max(
        int(chapter["end_page"])
        for chapter in group
    )

    passages = [{
        "page": start_page,
        "text": source_text,
    }]

    task = f"""
Create a concise synthesis of a group of document sections.

Group: {group_number} of {total_groups}
Source pages: {start_page}-{end_page}

The supplied JSON contains section summaries, topics,
key points and important terms.

Your job:
- Identify the shared themes.
- Connect related ideas across sections.
- Preserve important distinctions.
- Avoid repeating the same point.
- Retain important terminology.
- Do not invent facts or page references.

Return JSON:

{{
  "summary": "Group synthesis",
  "topics": ["Topic 1"],
  "key_points": ["Key point 1"],
  "important_terms": [
    {{
      "term": "Term",
      "definition": "Definition"
    }}
  ]
}}
"""

    generated = _generate_json(
        task=task,
        passages=passages,
        output_instructions=(
            "Return valid JSON only. "
            "Keep the synthesis concise but informative."
        ),
    )

    data = generated["data"]

    return {
        "summary": str(data.get("summary", "")).strip(),
        "topics": _clean_string_list(data.get("topics"), 10),
        "key_points": _clean_string_list(
            data.get("key_points"),
            15,
        ),
        "important_terms": (
            data.get("important_terms", [])
            if isinstance(data.get("important_terms"), list)
            else []
        ),
        "start_page": start_page,
        "end_page": end_page,
        "json_repaired": generated["json_repaired"],
    }


def _analyse_overall_document(chapter_analyses, all_pages):
    if not chapter_analyses:
        raise RuntimeError(
            "Cannot synthesise a document with no section analyses."
        )

    groups = _make_synthesis_groups(chapter_analyses)
    group_summaries = []

    print(
        f"[Document Analysis] Starting document-wide "
        f"synthesis using {len(groups)} group(s)."
    )

    for group_number, group in enumerate(groups, start=1):
        print(
            f"[Document Analysis] Synthesising group "
            f"{group_number}/{len(groups)} "
            f"({len(group)} section(s))."
        )

        result = _summarise_chapter_group(
            group=group,
            group_number=group_number,
            total_groups=len(groups),
        )

        group_summaries.append(result)

    final_source = json.dumps(
        group_summaries,
        ensure_ascii=False,
        indent=2,
    )

    if len(final_source) > MAX_FINAL_SYNTHESIS_CHARS:
        raise RuntimeError(
            "Final document synthesis input exceeds "
            f"{MAX_FINAL_SYNTHESIS_CHARS} characters. "
            "Reduce MAX_GROUP_SUMMARY_CHARS or use "
            "smaller synthesis groups."
        )

    document_start = min(page["page"] for page in all_pages)
    document_end = max(page["page"] for page in all_pages)

    passages = [{
        "page": document_start,
        "text": final_source,
    }]

    task = f"""
Create the final study overview for the complete document.

Document pages: {document_start}-{document_end}

The supplied JSON contains summaries of groups of sections
from the same document.

Produce a unified overview that:
- Explains the document's central purpose and subject.
- Connects important ideas across the document.
- Identifies the most important topics.
- Preserves essential concepts and distinctions.
- Avoids repetition.
- Does not invent facts, figures, quotations or page numbers.

You may use reasonable interpretation to explain
relationships between concepts.

Return JSON:

{{
  "summary": "Overall document summary",
  "topics": ["Topic 1", "Topic 2"],
  "key_points": ["Important point 1", "Important point 2"],
  "important_terms": [
    {{
      "term": "Term",
      "definition": "Definition"
    }}
  ]
}}
"""

    generated = _generate_json(
        task=task,
        passages=passages,
        output_instructions=(
            "Return valid JSON only. The result must represent "
            "the complete document, not just one section."
        ),
    )

    data = generated["data"]

    final_result = {
        "summary": str(data.get("summary", "")).strip(),
        "topics": _clean_string_list(
            data.get("topics"),
            MAX_TOPICS,
        ),
        "key_points": _clean_string_list(
            data.get("key_points"),
            MAX_KEY_POINTS,
        ),
        "important_terms": (
            data.get("important_terms", [])
            if isinstance(data.get("important_terms"), list)
            else []
        ),
        "group_count": len(groups),
        "group_summaries": group_summaries,
        "json_repaired": (
            generated["json_repaired"]
            or any(
                group["json_repaired"]
                for group in group_summaries
            )
        ),
    }

    verification = _advisory_verification(
        content=json.dumps(
            final_result,
            ensure_ascii=False,
        ),
        passages=all_pages,
    )

    final_result["verification"] = verification

    return final_result


# ---------------------------------------------------------
# MAIN DOCUMENT ANALYSIS PIPELINE
# ---------------------------------------------------------

def analyse_document(document_id: str):
    print(
        f"\n[Document Analysis] Starting analysis "
        f"for document: {document_id}"
    )

    document = get_document(document_id)

    if not document:
        raise ValueError(
            f"Document not found: {document_id}"
        )

    raw_pages = get_document_pages(document_id)
    pages = _normalise_page_records(raw_pages)

    if not pages:
        raise ValueError(
            "No extracted text is available for this document."
        )

    print(
        f"[Document Analysis] Loaded {len(pages)} "
        f"page(s) for analysis."
    )

    # STEP 1: Detect structure

    print(
        "[Document Analysis] Detecting document structure..."
    )

    structure = detect_document_structure(pages)

    if not isinstance(structure, dict):
        raise RuntimeError(
            "Structure detector returned an invalid result."
        )

    if not structure.get("success"):
        raise RuntimeError(
            "Document structure detection failed: "
            f"{structure.get('error', 'Unknown error')}"
        )

    sections = structure.get("sections", [])

    if not isinstance(sections, list):
        sections = []

    # STEP 2: Whole-document fallback

    if not sections:
        print(
            "[Document Analysis] No reliable sections detected. "
            "Analysing the complete document as one unit."
        )

        sections = [{
            "chapter_number": 1,
            "title": "Complete Document",
            "start_page": min(
                page["page"] for page in pages
            ),
            "end_page": max(
                page["page"] for page in pages
            ),
            "level": 1,
            "detection_type": "whole_document_fallback",
            "confidence": "high",
            "boundary_evidence": (
                "No reliable section boundaries were detected. "
                "The complete document is retained as one unit."
            ),
            "subsections": [],
        }]

        structure["analysis_fallback"] = True

    else:
        structure["analysis_fallback"] = False

    # STEP 3: Analyse sections

    chapter_analyses = []
    total_sections = len(sections)

    print(
        f"[Document Analysis] Analysing "
        f"{total_sections} section(s)."
    )

    for index, section in enumerate(sections, start=1):
        section["chapter_number"] = index

        print(
            f"\n[Document Analysis] "
            f"Section {index}/{total_sections}: "
            f"{section.get('title', 'Untitled section')}"
        )

        chapter_result = _analyse_section(
            pages=pages,
            section=section,
        )

        chapter_analyses.append(chapter_result)

        print(
            f"[Document Analysis] Completed "
            f"section {index}/{total_sections}."
        )

    # STEP 4: Overall synthesis

    print(
        "\n[Document Analysis] Starting overall "
        "document synthesis..."
    )

    document_analysis = _analyse_overall_document(
        chapter_analyses=chapter_analyses,
        all_pages=pages,
    )

    # STEP 5: Save only after all analysis succeeds

    print(
        "[Document Analysis] Saving analysis results..."
    )

    save_analysis_results(
        document_id=document_id,
        document_analysis=document_analysis,
        chapter_analyses=chapter_analyses,
        structure=structure,
    )

    print(
        "[Document Analysis] Analysis completed and saved."
    )

    # STEP 6: Return result

    return {
        "success": True,
        "document_id": document_id,
        "filename": _get_field(
            document,
            "original_filename",
            "Unknown document",
        ),
        "sections_analysed": len(chapter_analyses),
        "subsections_detected": sum(
            len(chapter.get("subsections", []))
            for chapter in chapter_analyses
        ),
        "document_analysis": document_analysis,
        "chapter_analyses": chapter_analyses,
        "structure": structure,
    }
