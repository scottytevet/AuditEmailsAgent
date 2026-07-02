#!/usr/bin/env python3
"""Import Peak New Docs files as source records for the QA MVP.

The importer reads PDFs, Word documents, and Excel workbooks from the Peak Sales
Recruiting document folder and emits email-shaped JSONL records that can be
indexed by app.email_index alongside mail and Graph communications records.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(r"C:\Users\ScottyGomez\Documents\PeakSalesRecruiting\Peak New Docs")
DEFAULT_OUTPUT = PROJECT_ROOT / "AI-Outputs" / "peak_new_docs_source_records.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "AI-Outputs" / "peak_new_docs_import_summary.json"

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx"}
SPACE_RE = re.compile(r"[ \t\f\v]+")
BLANK_LINE_RE = re.compile(r"\n{3,}")
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)

CANDIDATE_TERMS = [
    "Peak Sales",
    "TEVET",
    "Carly Van Gogh",
    "Bojan",
    "Sarah Zamudio",
    "Carla Caldwell",
    "Donna Millard",
    "Jerri Gore",
    "John Vandewater",
    "Tracy Solomon",
    "Vivian Martin",
    "Emilee Askin",
    "Rob Dean",
    "Ryan Hofmockel",
    "Evan Harris",
    "Jody Kemp",
    "Sales Talent",
    "Test Equity",
]


def normalize_text(value: str | None) -> str:
    value = value or ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = SPACE_RE.sub(" ", value)
    value = BLANK_LINE_RE.sub("\n\n", value)
    return value.strip()


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_subject(value: str | None) -> str:
    if not value:
        return ""
    subject = value.replace("_", " ").strip()
    subject = re.sub(r"^\s*((re|fw|fwd|ext)\s*[:_\-]\s*)+", "", subject, flags=re.I)
    return compact(subject).lower()


def extract_email_addresses(*values: str | None) -> list[str]:
    seen: set[str] = set()
    addresses: list[str] = []
    for value in values:
        for match in EMAIL_RE.finditer(value or ""):
            address = match.group(0).lower()
            if address not in seen:
                seen.add(address)
                addresses.append(address)
    return addresses


def detected_terms(*values: str | None) -> list[str]:
    text = " ".join(value or "" for value in values).lower()
    found = []
    for term in CANDIDATE_TERMS:
        if term.lower() in text:
            found.append(term)
    return found


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def iso_mtime(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat()


def rel_path(path: Path, source_dir: Path) -> str:
    try:
        return str(path.relative_to(source_dir))
    except ValueError:
        return path.name


def require_package(import_name: str, install_hint: str) -> Any:
    try:
        return __import__(import_name)
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(f"Missing {install_hint}. Run: .venv\\Scripts\\python.exe -m pip install -r requirements-docs.txt") from exc


def extract_pdf(path: Path) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = {
        "extractor": "pdfplumber",
        "pages": 0,
        "page_text_chars": [],
    }
    try:
        pdfplumber = require_package("pdfplumber", "pdfplumber")
        parts: list[str] = []
        with pdfplumber.open(path) as pdf:
            metadata["pages"] = len(pdf.pages)
            for page_index, page in enumerate(pdf.pages, start=1):
                page_text = normalize_text(page.extract_text() or "")
                metadata["page_text_chars"].append(len(page_text))
                if page_text:
                    parts.append(f"Page {page_index}\n{page_text}")
        return normalize_text("\n\n".join(parts)), metadata
    except Exception as exc:
        metadata["extractor_error"] = f"{type(exc).__name__}: {exc}"

    pypdf = require_package("pypdf", "pypdf")
    parts = []
    reader = pypdf.PdfReader(str(path))
    metadata["extractor"] = "pypdf"
    metadata["pages"] = len(reader.pages)
    metadata["page_text_chars"] = []
    for page_index, page in enumerate(reader.pages, start=1):
        page_text = normalize_text(page.extract_text() or "")
        metadata["page_text_chars"].append(len(page_text))
        if page_text:
            parts.append(f"Page {page_index}\n{page_text}")
    return normalize_text("\n\n".join(parts)), metadata


def table_rows_to_text(rows: list[list[str]]) -> str:
    lines = []
    for row in rows:
        cleaned = [compact(cell) for cell in row]
        if any(cleaned):
            lines.append(" | ".join(cleaned))
    return "\n".join(lines)


def extract_docx(path: Path) -> tuple[str, dict[str, Any]]:
    docx = require_package("docx", "python-docx")
    document = docx.Document(str(path))
    parts: list[str] = []
    paragraphs = [normalize_text(paragraph.text) for paragraph in document.paragraphs]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if paragraphs:
        parts.append("\n\n".join(paragraphs))

    table_count = 0
    table_row_count = 0
    for table_index, table in enumerate(document.tables, start=1):
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        table_text = table_rows_to_text(rows)
        if table_text:
            parts.append(f"Table {table_index}\n{table_text}")
            table_count += 1
            table_row_count += len(rows)

    core = document.core_properties
    metadata = {
        "extractor": "python-docx",
        "paragraphs": len(paragraphs),
        "tables": table_count,
        "table_rows": table_row_count,
        "author": core.author,
        "created": core.created.isoformat() if core.created else None,
        "modified": core.modified.isoformat() if core.modified else None,
    }
    return normalize_text("\n\n".join(parts)), metadata


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def extract_xlsx(path: Path) -> tuple[str, dict[str, Any]]:
    openpyxl = require_package("openpyxl", "openpyxl")
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    parts: list[str] = []
    sheet_summaries = []
    total_rows = 0
    total_cells = 0
    try:
        for sheet in workbook.worksheets:
            lines = []
            row_count = 0
            cell_count = 0
            for row in sheet.iter_rows(values_only=True):
                values = [value_text(cell) for cell in row]
                trimmed = [compact(value) for value in values]
                while trimmed and not trimmed[-1]:
                    trimmed.pop()
                if not any(trimmed):
                    continue
                row_count += 1
                cell_count += sum(1 for value in trimmed if value)
                lines.append(" | ".join(trimmed))
            total_rows += row_count
            total_cells += cell_count
            sheet_summaries.append({"name": sheet.title, "rows": row_count, "non_empty_cells": cell_count})
            if lines:
                parts.append(f"Sheet: {sheet.title}\n" + "\n".join(lines))
    finally:
        workbook.close()
    metadata = {
        "extractor": "openpyxl",
        "sheets": sheet_summaries,
        "sheet_count": len(sheet_summaries),
        "rows": total_rows,
        "non_empty_cells": total_cells,
    }
    return normalize_text("\n\n".join(parts)), metadata


def extract_file_text(path: Path) -> tuple[str, dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".xlsx":
        return extract_xlsx(path)
    raise ValueError(f"Unsupported file type: {suffix}")


def quality_for_text(extracted_text: str, extraction_status: str) -> str:
    if extraction_status != "ok":
        return "metadata-only"
    if len(extracted_text) >= 500:
        return "high"
    if extracted_text:
        return "low"
    return "metadata-only"


def source_record(path: Path, source_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    stat = path.stat()
    relative = rel_path(path, source_dir)
    extracted_text = ""
    extraction_meta: dict[str, Any] = {}
    extraction_status = "ok"
    extraction_error = None
    try:
        extracted_text, extraction_meta = extract_file_text(path)
    except Exception as exc:
        extraction_status = "error"
        extraction_error = f"{type(exc).__name__}: {exc}"
        extraction_meta = {"extractor": "unavailable", "error": extraction_error}

    modified_at = iso_mtime(path)
    quality = quality_for_text(extracted_text, extraction_status)
    header = "\n".join(
        [
            f"Peak New Docs source: {path.name}",
            f"File type: {path.suffix.lower().lstrip('.')}",
            f"Modified: {modified_at}",
            f"Source path: {path}",
            f"Extraction status: {extraction_status}",
            f"Extraction quality: {quality}",
        ]
    )
    body_text = normalize_text(f"{header}\n\n{extracted_text}") if extracted_text else header
    digest = stable_hash(str(path), body_text, modified_at)
    participants = sorted(set(extract_email_addresses(body_text) + detected_terms(path.name, body_text[:4000])))

    record = {
        "source": {
            "kind": "peak_new_docs",
            "path": str(path),
            "relative_path": f"Peak New Docs\\{relative}",
            "size_bytes": stat.st_size,
            "modified_at": modified_at,
            "file_extension": path.suffix.lower(),
        },
        "subject": path.stem,
        "normalized_subject": clean_subject(path.stem),
        "sender": "Peak New Docs",
        "to": "",
        "cc": "",
        "bcc": "",
        "sent_at": modified_at,
        "internet_message_id": f"peak-new-docs:{digest[:32]}",
        "in_reply_to_id": None,
        "participants": participants,
        "has_attachment": "false",
        "body_text": body_text,
        "body_length": len(body_text),
        "body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        "purview": {},
        "headers": {},
        "parse": {
            "status": "ok",
            "source_kind": "peak_new_docs",
            "source_system": "Peak New Docs",
            "source_project": "Peak Sales Recruiting",
            "source_label": "Peak New Docs",
            "quality": quality,
            "file_extension": path.suffix.lower(),
            "extraction_status": extraction_status,
            "extraction_error": extraction_error,
            **extraction_meta,
        },
        "conversation_key": f"peak-new-docs-{digest[:16]}",
    }
    stats = {
        "quality": quality,
        "extraction_status": extraction_status,
        "body_length": len(body_text),
        "extracted_length": len(extracted_text),
        "file_extension": path.suffix.lower(),
        "error": extraction_error,
    }
    return record, stats


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def import_peak_new_docs(source_dir: Path, output_path: Path, summary_path: Path) -> dict[str, Any]:
    files = sorted(path for path in source_dir.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)
    records: list[dict[str, Any]] = []
    extension_counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    extraction_status_counts: dict[str, int] = {}
    total_body_chars = 0
    total_extracted_chars = 0
    errors = []

    for path in files:
        record, stats = source_record(path, source_dir)
        records.append(record)
        extension = stats["file_extension"]
        quality = stats["quality"]
        status = stats["extraction_status"]
        extension_counts[extension] = extension_counts.get(extension, 0) + 1
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
        extraction_status_counts[status] = extraction_status_counts.get(status, 0) + 1
        total_body_chars += int(stats["body_length"])
        total_extracted_chars += int(stats["extracted_length"])
        if stats.get("error"):
            errors.append({"path": str(path), "error": stats["error"]})

    write_jsonl(output_path, records)
    summary = {
        "source_dir": str(source_dir),
        "output_path": str(output_path),
        "files_found": len(files),
        "records_written": len(records),
        "extension_counts": dict(sorted(extension_counts.items())),
        "quality_counts": dict(sorted(quality_counts.items())),
        "extraction_status_counts": dict(sorted(extraction_status_counts.items())),
        "total_body_chars": total_body_chars,
        "total_extracted_chars": total_extracted_chars,
        "errors": errors[:25],
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Peak New Docs folder not found: {args.source}")

    summary = import_peak_new_docs(args.source, args.output, args.summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["records_written"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
