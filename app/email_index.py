#!/usr/bin/env python3
"""SQLite indexing, search, embeddings, and answer helpers for the communications evidence MVP."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import tempfile
from typing import Any
from urllib import error, request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EMAILS = PROJECT_ROOT / "AI-Outputs" / "normalized_emails.jsonl"
DEFAULT_THREADS = PROJECT_ROOT / "AI-Outputs" / "email_threads.jsonl"
DEFAULT_GRAPH_RECORDS = PROJECT_ROOT / "AI-Outputs" / "graph_source_records.jsonl"
DEFAULT_PEAK_NEW_DOCS = PROJECT_ROOT / "AI-Outputs" / "peak_new_docs_source_records.jsonl"
DEFAULT_SOURCE_CATALOG = PROJECT_ROOT / "AI-Outputs" / "sources.json"


def default_db_path() -> Path:
    base = os.environ.get("PEAK_EMAIL_MVP_HOME") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "PeakSalesEmailMVP" / "email_mvp.sqlite"
    return Path(tempfile.gettempdir()) / "PeakSalesEmailMVP" / "email_mvp.sqlite"


DEFAULT_DB = default_db_path()

DEFAULT_MODEL_PROVIDER = "azure_foundry"
DEFAULT_AZURE_FOUNDRY_API_VERSION = "2024-05-01-preview"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_ANSWER_MODEL = "gpt-4.1-mini"

WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@/#:\-]{1,80}")
SPACE_RE = re.compile(r"\s+")


class AppError(Exception):
    """Expected application-level error for API responses."""


def load_env(path: Path | None = None) -> None:
    """Load simple KEY=VALUE lines from .env without overriding the shell."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY,
            source_path TEXT,
            relative_path TEXT,
            subject TEXT,
            normalized_subject TEXT,
            sender TEXT,
            recipients TEXT,
            cc TEXT,
            bcc TEXT,
            sent_at TEXT,
            internet_message_id TEXT,
            in_reply_to_id TEXT,
            participants_json TEXT,
            has_attachment TEXT,
            body_text TEXT,
            body_length INTEGER DEFAULT 0,
            body_sha256 TEXT,
            purview_json TEXT,
            headers_json TEXT,
            parse_json TEXT,
            thread_id TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_source
            ON emails(COALESCE(relative_path, source_path));
        CREATE INDEX IF NOT EXISTS idx_emails_sender ON emails(sender);
        CREATE INDEX IF NOT EXISTS idx_emails_sent_at ON emails(sent_at);
        CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);
        CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(internet_message_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS email_fts USING fts5(
            email_id UNINDEXED,
            subject,
            sender,
            participants,
            body_text,
            source_path,
            tokenize = 'unicode61'
        );

        CREATE TABLE IF NOT EXISTS threads (
            thread_id TEXT PRIMARY KEY,
            display_subject TEXT,
            normalized_subject TEXT,
            message_count INTEGER DEFAULT 0,
            duplicate_source_count INTEGER DEFAULT 0,
            start_at TEXT,
            end_at TEXT,
            senders_json TEXT,
            participants_json TEXT,
            body_chars INTEGER DEFAULT 0,
            sources_json TEXT,
            sample_body_excerpts_json TEXT
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
            thread_id TEXT,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            sent_at TEXT,
            source_path TEXT,
            embedding_json TEXT,
            embedding_model TEXT,
            embedded_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_email ON chunks(email_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_thread ON chunks(thread_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_embedding_model ON chunks(embedding_model);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
            chunk_id UNINDEXED,
            email_id UNINDEXED,
            subject,
            sender,
            chunk_text,
            source_path,
            tokenize = 'unicode61'
        );
        """
    )


def reset_index(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM chunk_fts;
        DELETE FROM chunks;
        DELETE FROM email_fts;
        DELETE FROM threads;
        DELETE FROM emails;
        """
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise AppError(f"Input file not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def compact_text(value: str | None) -> str:
    return SPACE_RE.sub(" ", value or "").strip()


def json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def source_key(record: dict[str, Any]) -> str:
    source = record.get("source") or {}
    return source.get("relative_path") or source.get("path") or ""


def build_thread_lookup(threads: list[dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for thread in threads:
        thread_id = thread.get("thread_id")
        if not thread_id:
            continue
        for source in thread.get("sources") or []:
            key = source.get("relative_path") or source.get("path")
            if key:
                lookup[key] = thread_id
    return lookup


def insert_threads(conn: sqlite3.Connection, threads: list[dict[str, Any]]) -> None:
    rows = []
    for thread in threads:
        thread_id = thread.get("thread_id")
        if not thread_id:
            continue
        rows.append(
            (
                thread_id,
                thread.get("display_subject") or "",
                thread.get("normalized_subject") or "",
                thread.get("message_count") or 0,
                thread.get("duplicate_source_count") or 0,
                thread.get("start_at"),
                thread.get("end_at"),
                json_dumps(thread.get("senders") or []),
                json_dumps(thread.get("participants") or []),
                thread.get("body_chars") or 0,
                json_dumps(thread.get("sources") or []),
                json_dumps(thread.get("sample_body_excerpts") or []),
            )
        )
    conn.executemany(
        """
        INSERT OR REPLACE INTO threads (
            thread_id, display_subject, normalized_subject, message_count,
            duplicate_source_count, start_at, end_at, senders_json,
            participants_json, body_chars, sources_json, sample_body_excerpts_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_emails(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    thread_lookup: dict[str, str],
) -> int:
    inserted = 0
    for record in records:
        if (record.get("parse") or {}).get("status") == "error":
            continue
        source = record.get("source") or {}
        participants = record.get("participants") or []
        participants_text = " ".join(participants)
        key = source_key(record)
        thread_id = thread_lookup.get(key) or record.get("conversation_key")
        cursor = conn.execute(
            """
            INSERT INTO emails (
                source_path, relative_path, subject, normalized_subject, sender,
                recipients, cc, bcc, sent_at, internet_message_id, in_reply_to_id,
                participants_json, has_attachment, body_text, body_length,
                body_sha256, purview_json, headers_json, parse_json, thread_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.get("path"),
                source.get("relative_path"),
                record.get("subject") or "",
                record.get("normalized_subject") or "",
                record.get("sender") or "",
                record.get("to") or "",
                record.get("cc") or "",
                record.get("bcc") or "",
                record.get("sent_at"),
                record.get("internet_message_id"),
                record.get("in_reply_to_id"),
                json_dumps(participants),
                str(record.get("has_attachment") or ""),
                record.get("body_text") or "",
                record.get("body_length") or len(record.get("body_text") or ""),
                record.get("body_sha256"),
                json_dumps(record.get("purview") or {}),
                json_dumps(record.get("headers") or {}),
                json_dumps(record.get("parse") or {}),
                thread_id,
            ),
        )
        email_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO email_fts (
                email_id, subject, sender, participants, body_text, source_path
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                record.get("subject") or "",
                record.get("sender") or "",
                participants_text,
                record.get("body_text") or "",
                source.get("relative_path") or source.get("path") or "",
            ),
        )
        insert_chunks(conn, email_id, record, thread_id)
        inserted += 1
    return inserted


def chunk_body(
    subject: str,
    sender: str,
    sent_at: str | None,
    source_path: str,
    body_text: str,
    max_chars: int = 1800,
    overlap: int = 180,
) -> list[str]:
    header = "\n".join(
        [
            f"Subject: {subject}",
            f"From: {sender}",
            f"Sent: {sent_at or ''}",
            f"Source: {source_path}",
        ]
    ).strip()
    body = compact_text(body_text)
    if not body:
        return [header]

    chunks: list[str] = []
    start = 0
    while start < len(body):
        end = min(start + max_chars, len(body))
        if end < len(body):
            split_at = body.rfind(". ", start, end)
            if split_at > start + int(max_chars * 0.55):
                end = split_at + 1
        segment = body[start:end].strip()
        if segment:
            chunks.append(f"{header}\n\n{segment}")
        if end >= len(body):
            break
        start = max(end - overlap, start + 1)
    return chunks


def insert_chunks(
    conn: sqlite3.Connection,
    email_id: int,
    record: dict[str, Any],
    thread_id: str | None,
) -> None:
    source = record.get("source") or {}
    source_path = source.get("relative_path") or source.get("path") or ""
    subject = record.get("subject") or ""
    sender = record.get("sender") or ""
    sent_at = record.get("sent_at")
    chunks = chunk_body(subject, sender, sent_at, source_path, record.get("body_text") or "")
    for index, chunk in enumerate(chunks):
        cursor = conn.execute(
            """
            INSERT INTO chunks (
                email_id, thread_id, chunk_index, chunk_text, subject,
                sender, sent_at, source_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (email_id, thread_id, index, chunk, subject, sender, sent_at, source_path),
        )
        chunk_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO chunk_fts (
                chunk_id, email_id, subject, sender, chunk_text, source_path
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chunk_id, email_id, subject, sender, chunk, source_path),
        )


def rebuild_database(
    db_path: Path = DEFAULT_DB,
    emails_path: Path = DEFAULT_EMAILS,
    threads_path: Path = DEFAULT_THREADS,
    graph_records_path: Path = DEFAULT_GRAPH_RECORDS,
    peak_new_docs_path: Path = DEFAULT_PEAK_NEW_DOCS,
    with_embeddings: bool = False,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        create_schema(conn)
        reset_index(conn)
        emails = read_jsonl(emails_path)
        graph_records = read_jsonl(graph_records_path) if graph_records_path.exists() else []
        peak_new_docs = read_jsonl(peak_new_docs_path) if peak_new_docs_path.exists() else []
        all_records = emails + graph_records + peak_new_docs
        threads = read_jsonl(threads_path) if threads_path.exists() else []
        insert_threads(conn, threads)
        inserted = insert_emails(conn, all_records, build_thread_lookup(threads))
        conn.commit()
        catalog_path = write_source_catalog(conn)
        result = database_status(conn, db_path)
        result.update(
            {
                "emails_input": str(emails_path),
                "threads_input": str(threads_path),
                "graph_records_input": str(graph_records_path),
                "peak_new_docs_input": str(peak_new_docs_path),
                "emails_read": len(emails),
                "graph_records_read": len(graph_records),
                "peak_new_docs_read": len(peak_new_docs),
                "threads_read": len(threads),
                "source_records_indexed": inserted,
                "source_catalog_written": str(catalog_path),
            }
        )
        if with_embeddings:
            result["embeddings"] = build_missing_embeddings(
                conn, model=embedding_model or current_embedding_model()
            )
            conn.commit()
        return result
    finally:
        conn.close()


def parse_json_column(row: sqlite3.Row, key: str, fallback: Any) -> Any:
    value = row[key]
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def email_summary(row: sqlite3.Row) -> dict[str, Any]:
    body = row["body_text"] or ""
    snippet = compact_text(body)[:360]
    source_meta = parse_json_column(row, "parse_json", {})
    return {
        "id": row["id"],
        "subject": row["subject"],
        "sender": row["sender"],
        "to": row["recipients"],
        "cc": row["cc"],
        "sent_at": row["sent_at"],
        "thread_id": row["thread_id"],
        "source_path": row["source_path"],
        "relative_path": row["relative_path"],
        "internet_message_id": row["internet_message_id"],
        "participants": parse_json_column(row, "participants_json", []),
        "has_attachment": row["has_attachment"],
        "body_length": row["body_length"],
        "source_kind": source_meta.get("source_kind") or "email",
        "source_origin": source_origin(source_meta, row["relative_path"] or row["source_path"]),
        "quality": source_meta.get("quality"),
        "snippet": snippet,
    }


def email_detail(row: sqlite3.Row) -> dict[str, Any]:
    detail = email_summary(row)
    detail.update(
        {
            "bcc": row["bcc"],
            "normalized_subject": row["normalized_subject"],
            "in_reply_to_id": row["in_reply_to_id"],
            "body_text": row["body_text"],
            "body_sha256": row["body_sha256"],
            "purview": parse_json_column(row, "purview_json", {}),
            "headers": parse_json_column(row, "headers_json", {}),
            "parse": parse_json_column(row, "parse_json", {}),
        }
    )
    return detail


def make_fts_query(query: str, limit: int = 16) -> str:
    terms = []
    for term in WORD_RE.findall(query):
        cleaned = term.strip(".:_-").lower()
        if len(cleaned) >= 2 and cleaned not in terms:
            terms.append(cleaned)
        if len(terms) >= limit:
            break
    if not terms:
        return ""
    return " OR ".join(f'"{term}"' for term in terms)


def filter_sql(filters: dict[str, Any], alias: str = "e") -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    sender = compact_text(filters.get("sender"))
    if sender:
        clauses.append(f"LOWER({alias}.sender) LIKE ?")
        params.append(f"%{sender.lower()}%")

    participant = compact_text(filters.get("participant"))
    if participant:
        clauses.append(f"LOWER({alias}.participants_json) LIKE ?")
        params.append(f"%{participant.lower()}%")

    date_from = compact_text(filters.get("date_from"))
    if date_from:
        clauses.append(f"{alias}.sent_at >= ?")
        params.append(date_from)

    date_to = compact_text(filters.get("date_to"))
    if date_to:
        clauses.append(f"{alias}.sent_at <= ?")
        params.append(date_to)

    has_attachment = filters.get("has_attachment")
    if has_attachment not in (None, "", "any"):
        clauses.append(f"LOWER({alias}.has_attachment) = ?")
        params.append(str(has_attachment).lower())

    source_type = compact_text(filters.get("source_type"))
    if source_type and source_type != "any":
        if source_type == "email":
            clauses.append(
                f"({alias}.parse_json IS NULL OR {alias}.parse_json = '' "
                f"OR {alias}.parse_json NOT LIKE ? OR {alias}.parse_json LIKE ?)"
            )
            params.extend(["%\"source_kind\"%", "%\"source_kind\": \"email\"%"])
        else:
            clauses.append(f"{alias}.parse_json LIKE ?")
            params.append(f"%\"source_kind\": \"{source_type}\"%")

    quality = compact_text(filters.get("quality"))
    if quality and quality != "any":
        clauses.append(f"{alias}.parse_json LIKE ?")
        params.append(f"%\"quality\": \"{quality}\"%")

    if not clauses:
        return "", params
    return " AND " + " AND ".join(clauses), params


def search_sources(
    conn: sqlite3.Connection,
    query: str = "",
    filters: dict[str, Any] | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    filters = filters or {}
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    where_filter, filter_params = filter_sql(filters)
    fts_query = make_fts_query(query)

    if fts_query:
        rows = conn.execute(
            f"""
            SELECT e.*, bm25(email_fts) AS rank
            FROM email_fts
            JOIN emails e ON e.id = email_fts.email_id
            WHERE email_fts MATCH ? {where_filter}
            ORDER BY
                CASE
                    WHEN e.subject LIKE '[Transcript][high]%' THEN 0
                    WHEN e.subject LIKE '[Transcript][low]%' THEN 1
                    WHEN e.subject LIKE '[Transcript missing]%' THEN 3
                    WHEN e.subject LIKE '[Meeting status]%' THEN 4
                    ELSE 2
                END,
                rank,
                e.sent_at DESC
            LIMIT ? OFFSET ?
            """,
            [fts_query, *filter_params, limit, offset],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT e.*, 0 AS rank
            FROM emails e
            WHERE 1=1 {where_filter}
            ORDER BY e.sent_at DESC
            LIMIT ? OFFSET ?
            """,
            [*filter_params, limit, offset],
        ).fetchall()

    if fts_query and not rows:
        like = f"%{query.lower()}%"
        rows = conn.execute(
            f"""
            SELECT e.*, 0 AS rank
            FROM emails e
            WHERE (
                LOWER(e.subject) LIKE ?
                OR LOWER(e.body_text) LIKE ?
                OR LOWER(e.sender) LIKE ?
                OR LOWER(e.participants_json) LIKE ?
            ) {where_filter}
            ORDER BY e.sent_at DESC
            LIMIT ? OFFSET ?
            """,
            [like, like, like, like, *filter_params, limit, offset],
        ).fetchall()

    return {
        "query": query,
        "limit": limit,
        "offset": offset,
        "results": [email_summary(row) for row in rows],
    }


def get_source(conn: sqlite3.Connection, email_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if not row:
        raise AppError(f"Email not found: {email_id}")
    return email_detail(row)


search_emails = search_sources
get_email = get_source


def get_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, Any]:
    thread = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    emails = conn.execute(
        """
        SELECT *
        FROM emails
        WHERE thread_id = ?
        ORDER BY sent_at, id
        """,
        (thread_id,),
    ).fetchall()
    if not thread and not emails:
        raise AppError(f"Thread not found: {thread_id}")
    return {
        "thread": {
            "thread_id": thread_id,
            "display_subject": thread["display_subject"] if thread else "",
            "normalized_subject": thread["normalized_subject"] if thread else "",
            "message_count": thread["message_count"] if thread else len(emails),
            "duplicate_source_count": thread["duplicate_source_count"] if thread else len(emails),
            "start_at": thread["start_at"] if thread else None,
            "end_at": thread["end_at"] if thread else None,
            "senders": parse_json_column(thread, "senders_json", []) if thread else [],
            "participants": parse_json_column(thread, "participants_json", []) if thread else [],
            "body_chars": thread["body_chars"] if thread else 0,
            "sources": parse_json_column(thread, "sources_json", []) if thread else [],
        },
        "emails": [email_summary(row) for row in emails],
    }


def human_label(value: str) -> str:
    return re.sub(r"[_\-]+", " ", value or "source").strip().title()


def app_metadata() -> dict[str, str]:
    return {
        "title": os.environ.get("APP_TITLE", "Source Evidence QA"),
        "subtitle": os.environ.get(
            "APP_SUBTITLE",
            "Ask questions across indexed source records.",
        ),
        "environment": os.environ.get("SOURCE_ENV_LABEL", "Local index"),
    }


def source_catalog_path() -> Path:
    configured = compact_text(os.environ.get("SOURCE_CATALOG_PATH"))
    return Path(configured) if configured else DEFAULT_SOURCE_CATALOG


def read_source_catalog_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    catalog_path = path or source_catalog_path()
    if not catalog_path.exists():
        return {}
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        raw_sources = data.get("sources") or []
        if isinstance(raw_sources, dict):
            raw_sources = [dict(value, kind=key) for key, value in raw_sources.items()]
    elif isinstance(data, list):
        raw_sources = data
    else:
        raw_sources = []
    overrides: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            continue
        kind = compact_text(item.get("kind") or item.get("source_kind"))
        if not kind:
            continue
        entry = dict(item)
        entry.setdefault("order", index)
        overrides[kind] = entry
    return overrides


def source_origin(meta: dict[str, Any], source_path: str | None = None) -> str:
    for key in ("source_name", "source_label", "source_system", "source_project", "origin"):
        value = compact_text(meta.get(key))
        if value:
            return value
    source = compact_text(source_path)
    if source:
        return source.split("/", 1)[0].split("\\", 1)[0]
    return "Indexed source"


def build_source_catalog(
    conn: sqlite3.Connection,
    catalog_path: Path | None = None,
) -> list[dict[str, Any]]:
    overrides = read_source_catalog_overrides(catalog_path)
    catalog: dict[str, dict[str, Any]] = {}
    rows = conn.execute("SELECT parse_json, source_path, relative_path FROM emails").fetchall()
    for row in rows:
        meta: dict[str, Any] = {}
        if row["parse_json"]:
            try:
                meta = json.loads(row["parse_json"])
            except json.JSONDecodeError:
                meta = {}
        kind = compact_text(meta.get("source_kind") or "email")
        override = overrides.get(kind, {})
        entry = catalog.setdefault(
            kind,
            {
                "kind": kind,
                "label": override.get("label") or meta.get("source_label") or human_label(kind),
                "description": override.get("description") or "",
                "count": 0,
                "qualities": {},
                "origins": {},
                "systems": {},
                "projects": {},
                "order": override.get("order", 1000),
            },
        )
        entry["count"] += 1
        quality = compact_text(meta.get("quality"))
        if quality:
            entry["qualities"][quality] = entry["qualities"].get(quality, 0) + 1
        origin = source_origin(meta, row["relative_path"] or row["source_path"])
        if origin:
            entry["origins"][origin] = entry["origins"].get(origin, 0) + 1
        system = compact_text(meta.get("source_system"))
        if system:
            entry["systems"][system] = entry["systems"].get(system, 0) + 1
        project = compact_text(meta.get("source_project"))
        if project:
            entry["projects"][project] = entry["projects"].get(project, 0) + 1

    for kind, override in overrides.items():
        if kind not in catalog:
            catalog[kind] = {
                "kind": kind,
                "label": override.get("label") or human_label(kind),
                "description": override.get("description") or "",
                "count": 0,
                "qualities": {},
                "origins": {},
                "systems": {},
                "projects": {},
                "order": override.get("order", 1000),
            }

    entries = list(catalog.values())
    for entry in entries:
        entry["qualities"] = dict(sorted(entry["qualities"].items()))
        entry["origins"] = dict(sorted(entry["origins"].items(), key=lambda item: (-item[1], item[0])))
        entry["systems"] = dict(sorted(entry["systems"].items()))
        entry["projects"] = dict(sorted(entry["projects"].items()))
    entries.sort(key=lambda item: (item.get("order", 1000), -item.get("count", 0), item.get("label", "")))
    return entries


def write_source_catalog(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    catalog_path = path or source_catalog_path()
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "app": app_metadata(),
        "sources": build_source_catalog(conn, catalog_path),
    }
    catalog_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return catalog_path


def available_source_kinds(conn: sqlite3.Connection) -> list[str]:
    return [item["kind"] for item in build_source_catalog(conn) if item.get("count", 0) > 0]

def database_status(conn: sqlite3.Connection, db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    create_schema(conn)
    counts = {}
    for table in ["emails", "threads", "chunks"]:
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    source_catalog = build_source_catalog(conn)
    source_counts = {entry["kind"]: entry["count"] for entry in source_catalog}
    quality_counts: dict[str, int] = {}
    for entry in source_catalog:
        for quality, count in entry.get("qualities", {}).items():
            quality_counts[quality] = quality_counts.get(quality, 0) + count
    embedded = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE embedding_json IS NOT NULL"
    ).fetchone()[0]
    return {
        "db_path": str(Path(db_path)),
        "exists": Path(db_path).exists(),
        "counts": counts,
        "source_counts": source_counts,
        "source_catalog": source_catalog,
        "source_catalog_path": str(source_catalog_path()),
        "quality_counts": quality_counts,
        "app": app_metadata(),
        "embedded_chunks": embedded,
        "model_provider": current_model_provider(),
        "model_provider_label": provider_display_name(),
        "model_api_style": current_model_api_style(),
        "llm_configured": model_configured(),
        "embedding_model": current_embedding_model(),
        "answer_model": current_answer_model(),
        "openai_configured": model_configured(),
        "inputs": {
            "normalized_emails": str(DEFAULT_EMAILS),
            "normalized_emails_exists": DEFAULT_EMAILS.exists(),
            "email_threads": str(DEFAULT_THREADS),
            "email_threads_exists": DEFAULT_THREADS.exists(),
            "graph_source_records": str(DEFAULT_GRAPH_RECORDS),
            "graph_source_records_exists": DEFAULT_GRAPH_RECORDS.exists(),
            "peak_new_docs_source_records": str(DEFAULT_PEAK_NEW_DOCS),
            "peak_new_docs_source_records_exists": DEFAULT_PEAK_NEW_DOCS.exists(),
            "source_catalog": str(source_catalog_path()),
            "source_catalog_exists": source_catalog_path().exists(),
        },
    }

def current_model_provider() -> str:
    provider = compact_text(os.environ.get("LLM_PROVIDER") or os.environ.get("MODEL_PROVIDER")).lower()
    if provider:
        return provider
    if os.environ.get("AZURE_FOUNDRY_ENDPOINT") or os.environ.get("AZURE_OPENAI_ENDPOINT"):
        return "azure_foundry"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return DEFAULT_MODEL_PROVIDER


def provider_display_name(provider: str | None = None) -> str:
    provider = provider or current_model_provider()
    return {
        "azure_foundry": "Azure AI Foundry",
        "azure": "Azure AI Foundry",
        "openai": "OpenAI",
    }.get(provider, provider.replace("_", " ").title())


def current_embedding_model() -> str:
    if current_model_provider() in ("azure", "azure_foundry"):
        return (
            os.environ.get("AZURE_FOUNDRY_EMBEDDING_MODEL")
            or os.environ.get("AZURE_OPENAI_EMBEDDING_MODEL")
            or os.environ.get("OPENAI_EMBEDDING_MODEL")
            or DEFAULT_EMBEDDING_MODEL
        )
    return os.environ.get("OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)


def current_answer_model() -> str:
    if current_model_provider() in ("azure", "azure_foundry"):
        return (
            os.environ.get("AZURE_FOUNDRY_ANSWER_MODEL")
            or os.environ.get("AZURE_FOUNDRY_CHAT_MODEL")
            or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            or os.environ.get("OPENAI_ANSWER_MODEL")
            or DEFAULT_ANSWER_MODEL
        )
    return os.environ.get("OPENAI_ANSWER_MODEL", DEFAULT_ANSWER_MODEL)


def current_azure_foundry_endpoint() -> str:
    return compact_text(os.environ.get("AZURE_FOUNDRY_ENDPOINT") or os.environ.get("AZURE_OPENAI_ENDPOINT"))


def current_azure_foundry_key() -> str:
    return compact_text(os.environ.get("AZURE_FOUNDRY_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY"))


def endpoint_query_value(name: str) -> str:
    endpoint = current_azure_foundry_endpoint()
    if "?" not in endpoint:
        return ""
    query = endpoint.split("?", 1)[1]
    for part in query.split("&"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.lower() == name.lower():
            return value
    return ""


def current_azure_foundry_api_version() -> str:
    return (
        os.environ.get("AZURE_FOUNDRY_API_VERSION")
        or endpoint_query_value("api-version")
        or DEFAULT_AZURE_FOUNDRY_API_VERSION
    )


def normalize_model_api_style(style: str) -> str:
    aliases = {
        "azure_openai": "azure_openai_api_version",
        "azure_openai_preview": "azure_openai_api_version",
        "api_version": "azure_openai_api_version",
        "responses": "azure_openai_api_version",
        "openai_v1": "openai_v1",
        "v1": "openai_v1",
        "model_inference": "model_inference",
        "models": "model_inference",
    }
    return aliases.get(style, style)


def current_model_api_style() -> str:
    if current_model_provider() not in ("azure", "azure_foundry"):
        return "openai"
    style = normalize_model_api_style(compact_text(os.environ.get("AZURE_FOUNDRY_API_STYLE")).lower())
    if style:
        return style
    endpoint = current_azure_foundry_endpoint().rstrip("/").lower()
    if endpoint.endswith("/models") or "/models/" in endpoint:
        return "model_inference"
    if "api-version=" in endpoint or endpoint.endswith("/openai") or (
        "/openai/" in endpoint and "/openai/v1" not in endpoint
    ):
        return "azure_openai_api_version"
    return "openai_v1"


def strip_endpoint_query(endpoint: str) -> str:
    return endpoint.split("?", 1)[0].rstrip("/")


def strip_known_model_route(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/embeddings"):
        if base.lower().endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


def azure_endpoint_base(style: str) -> str:
    base = strip_known_model_route(strip_endpoint_query(current_azure_foundry_endpoint()))
    lower = base.lower()
    if style == "model_inference":
        if lower.endswith("/models"):
            return base
        return f"{base}/models"
    if style == "azure_openai_api_version":
        marker = "/openai/"
        if marker in lower:
            return base[: lower.index(marker) + len("/openai")]
        if lower.endswith("/openai"):
            return base
        return f"{base}/openai"
    if style == "openai_v1":
        marker = "/openai/v1/"
        if marker in lower:
            return base[: lower.index(marker) + len("/openai/v1")]
        if lower.endswith("/openai/v1"):
            return base
        if lower.endswith("/openai"):
            return f"{base}/v1"
        return f"{base}/openai/v1"
    return base


def azure_url_for(path: str, style: str) -> str:
    base = azure_endpoint_base(style)
    if style in ("model_inference", "azure_openai_api_version"):
        return f"{join_url(base, path)}?api-version={current_azure_foundry_api_version()}"
    return join_url(base, path)

def model_configured() -> bool:
    if current_model_provider() in ("azure", "azure_foundry"):
        return bool(current_azure_foundry_endpoint() and current_azure_foundry_key())
    return bool(os.environ.get("OPENAI_API_KEY"))


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def model_json(path: str, payload: dict[str, Any], timeout: int = 90) -> dict[str, Any]:
    provider = current_model_provider()
    headers = {"Content-Type": "application/json"}
    if provider in ("azure", "azure_foundry"):
        endpoint = current_azure_foundry_endpoint()
        api_key = current_azure_foundry_key()
        if not endpoint or not api_key:
            raise AppError(
                "Azure AI Foundry is not configured. Set AZURE_FOUNDRY_ENDPOINT and AZURE_FOUNDRY_API_KEY."
            )
        style = current_model_api_style()
        url = azure_url_for(path, style)
        headers["api-key"] = api_key
        provider_label = "Azure AI Foundry"
    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise AppError("OPENAI_API_KEY is not configured.")
        url = f"https://api.openai.com/v1/{path.lstrip('/')}"
        headers["Authorization"] = f"Bearer {api_key}"
        provider_label = "OpenAI"
    else:
        raise AppError(f"Unsupported LLM_PROVIDER: {provider}")

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method="POST", headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise AppError(f"{provider_label} request failed ({exc.code}): {detail}") from exc
    except error.URLError as exc:
        raise AppError(f"{provider_label} request failed: {exc.reason}") from exc


openai_json = model_json

def create_embeddings(texts: list[str], model: str | None = None) -> list[list[float]]:
    if not texts:
        return []
    data = model_json(
        "embeddings",
        {
            "model": model or current_embedding_model(),
            "input": texts,
        },
    )
    rows = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
    return [row["embedding"] for row in rows]


def build_missing_embeddings(
    conn: sqlite3.Connection,
    model: str | None = None,
    batch_size: int = 64,
    limit: int | None = None,
) -> dict[str, Any]:
    model = model or current_embedding_model()
    rows = conn.execute(
        """
        SELECT id, chunk_text
        FROM chunks
        WHERE embedding_json IS NULL OR embedding_model IS NULL OR embedding_model != ?
        ORDER BY id
        LIMIT ?
        """,
        (model, limit or 1_000_000_000),
    ).fetchall()
    started = time.time()
    embedded = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        vectors = create_embeddings([row["chunk_text"] for row in batch], model=model)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        conn.executemany(
            """
            UPDATE chunks
            SET embedding_json = ?, embedding_model = ?, embedded_at = ?
            WHERE id = ?
            """,
            [
                (json.dumps(vector, separators=(",", ":")), model, now, row["id"])
                for row, vector in zip(batch, vectors, strict=True)
            ],
        )
        conn.commit()
        embedded += len(batch)
    return {
        "model": model,
        "chunks_scanned": len(rows),
        "chunks_embedded": embedded,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = 0.0
    left_mag = 0.0
    right_mag = 0.0
    for a, b in zip(left, right, strict=False):
        dot += a * b
        left_mag += a * a
        right_mag += b * b
    if not left_mag or not right_mag:
        return 0.0
    return dot / (math.sqrt(left_mag) * math.sqrt(right_mag))


def semantic_chunks(
    conn: sqlite3.Connection,
    question: str,
    filters: dict[str, Any] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    filters = filters or {}
    embedded = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE embedding_json IS NOT NULL"
    ).fetchone()[0]
    if not embedded:
        return []
    query_vector = create_embeddings([question], model=current_embedding_model())[0]
    where_filter, filter_params = filter_sql(filters)
    rows = conn.execute(
        f"""
        SELECT c.*, e.participants_json, e.recipients, e.parse_json
        FROM chunks c
        JOIN emails e ON e.id = c.email_id
        WHERE c.embedding_json IS NOT NULL {where_filter}
        """,
        filter_params,
    ).fetchall()
    ranked = []
    for row in rows:
        try:
            vector = json.loads(row["embedding_json"])
        except json.JSONDecodeError:
            continue
        ranked.append((cosine_similarity(query_vector, vector), row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [chunk_result(row, score=score, source="semantic") for score, row in ranked[:limit]]


def keyword_chunks(
    conn: sqlite3.Connection,
    question: str,
    filters: dict[str, Any] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    filters = filters or {}
    fts_query = make_fts_query(question)
    if not fts_query:
        return []
    where_filter, filter_params = filter_sql(filters)
    rows = conn.execute(
        f"""
        SELECT c.*, e.participants_json, e.recipients, e.parse_json, bm25(chunk_fts) AS rank
        FROM chunk_fts
        JOIN chunks c ON c.id = chunk_fts.chunk_id
        JOIN emails e ON e.id = c.email_id
        WHERE chunk_fts MATCH ? {where_filter}
        ORDER BY rank
        LIMIT ?
        """,
        [fts_query, *filter_params, limit],
    ).fetchall()
    return [chunk_result(row, score=float(row["rank"] or 0), source="keyword") for row in rows]


def chunk_result(row: sqlite3.Row, score: float, source: str) -> dict[str, Any]:
    text = row["chunk_text"] or ""
    source_meta = parse_json_column(row, "parse_json", {}) if "parse_json" in row.keys() else {}
    return {
        "chunk_id": row["id"],
        "email_id": row["email_id"],
        "thread_id": row["thread_id"],
        "subject": row["subject"],
        "sender": row["sender"],
        "sent_at": row["sent_at"],
        "source_path": row["source_path"],
        "text": text,
        "snippet": compact_text(text)[:520],
        "score": score,
        "retrieval": source,
        "source_kind": source_meta.get("source_kind") or "email",
        "source_origin": source_origin(source_meta, row["source_path"]),
        "quality": source_meta.get("quality"),
    }


def retrieve_context(
    conn: sqlite3.Connection,
    question: str,
    filters: dict[str, Any] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    filters = filters or {}
    results: dict[int, dict[str, Any]] = {}
    for item in keyword_chunks(conn, question, filters, limit):
        results[item["chunk_id"]] = item
    try:
        for item in semantic_chunks(conn, question, filters, limit):
            existing = results.get(item["chunk_id"])
            if existing:
                existing["retrieval"] = "hybrid"
                existing["semantic_score"] = item["score"]
            else:
                results[item["chunk_id"]] = item
    except AppError:
        # Keyword retrieval still gives a useful evidence set if the model provider is not configured.
        pass
    ordered = list(results.values())
    ordered.sort(
        key=lambda item: (
            0 if item["retrieval"] in ("hybrid", "semantic") else 1,
            -float(item.get("semantic_score", item.get("score", 0))),
        )
    )
    return ordered[:limit]


FIDELITY_STOPWORDS = {
    "about", "after", "against", "also", "and", "another", "are", "because", "been",
    "before", "being", "between", "both", "but", "can", "could", "did", "does",
    "doing", "each", "email", "emails", "from", "had", "has", "have", "into", "its",
    "meeting", "meetings", "more", "not", "only", "other", "over", "record", "records",
    "said", "same", "should", "source", "sources", "than", "that", "the", "their",
    "them", "then", "there", "these", "this", "those", "through", "transcript",
    "transcripts", "was", "were", "what", "when", "where", "which", "while", "with",
    "would", "your",
}
SOURCE_SCOPE_FILTERS = {"source_type", "quality", "has_attachment"}
ANSWER_SOURCE_TYPES = (
    "email",
    "graph_transcript",
    "graph_transcript_placeholder",
    "graph_meeting_reconciliation",
)


def answer_scope_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    scoped = dict(filters or {})
    for key in SOURCE_SCOPE_FILTERS:
        scoped.pop(key, None)
    return scoped


def significant_terms(text: str, limit: int = 40) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in WORD_RE.findall(text.lower()):
        cleaned = term.strip(".:_-/\\#")
        if len(cleaned) < 3 or cleaned in FIDELITY_STOPWORDS:
            continue
        if cleaned.isdigit() and len(cleaned) < 5:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            terms.append(cleaned)
        if len(terms) >= limit:
            break
    return terms


def build_expansion_query(question: str, draft_answer: str) -> str:
    return " ".join(significant_terms(f"{question} {draft_answer}", limit=18))


def merge_contexts(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    limit: int = 14,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in [*primary, *secondary]:
        chunk_id = int(item["chunk_id"])
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged



def retrieve_answer_context(
    conn: sqlite3.Connection,
    question: str,
    filters: dict[str, Any] | None,
    limit: int,
    use_all_sources: bool,
) -> list[dict[str, Any]]:
    base_filters = answer_scope_filters(filters) if use_all_sources else (filters or {})
    primary = retrieve_context(conn, question, base_filters, limit)
    if not use_all_sources:
        return primary

    diverse_context: list[dict[str, Any]] = []
    per_type_limit = max(2, min(4, limit // 2))
    for source_type in available_source_kinds(conn):
        source_filters = dict(base_filters)
        source_filters["source_type"] = source_type
        diverse_context.extend(retrieve_context(conn, question, source_filters, per_type_limit))
    return merge_contexts(primary, diverse_context, limit=max(limit, 14))
def build_citations(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "number": index + 1,
            "email_id": item["email_id"],
            "thread_id": item["thread_id"],
            "subject": item["subject"],
            "sender": item["sender"],
            "sent_at": item["sent_at"],
            "source_path": item["source_path"],
            "snippet": item["snippet"],
            "retrieval": item["retrieval"],
            "source_kind": item.get("source_kind"),
            "source_origin": item.get("source_origin"),
            "quality": item.get("quality"),
        }
        for index, item in enumerate(context)
    ]


def source_mix_from_context(context: list[dict[str, Any]]) -> dict[str, int]:
    mix: dict[str, int] = {}
    for item in context:
        source_kind = item.get("source_kind") or "email"
        mix[source_kind] = mix.get(source_kind, 0) + 1
    return mix


def format_context_sources(context: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[{index + 1}] {item['subject']} | {item['sender']} | {item['sent_at']}\n"
        f"Kind: {item.get('source_kind') or 'email'} | Quality: {item.get('quality') or 'unknown'}\n"
        f"Source: {item['source_path']}\n{item['text'][:2400]}"
        for index, item in enumerate(context)
    )


def generate_evidence_answer(
    question: str,
    context: list[dict[str, Any]],
    stage: str = "final",
) -> str:
    source_text = format_context_sources(context)
    if stage == "draft":
        task = "Create a concise draft answer that names the strongest supported facts and gaps."
    else:
        task = "Create the final concise answer. Keep it helpful, direct, and limited to supported facts."
    prompt = (
        f"{task} Use only the provided communications sources, including emails, transcripts, "
        "meeting-status rows, and metadata rows. Cite sources with bracket numbers like [1] or [2]. "
        "If the evidence is thin, missing, or only metadata, say that plainly. Do not invent dates, "
        "people, commitments, or outcomes that are not in the sources.\n\n"
        f"Question: {question}\n\nSources:\n{source_text}"
    )
    data = model_json(
        "responses",
        {
            "model": current_answer_model(),
            "input": [
                {
                    "role": "system",
                    "content": "You are an evidence-focused communications archive analyst.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        },
    )
    return data.get("output_text") or extract_response_text(data)



def generate_model_text(system_prompt: str, user_prompt: str, temperature: float = 0.1) -> str:
    if current_model_provider() in ("azure", "azure_foundry") and current_model_api_style() == "model_inference":
        data = model_json(
            "chat/completions",
            {
                "model": current_answer_model(),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
            },
        )
        return extract_chat_response_text(data)

    data = model_json(
        "responses",
        {
            "model": current_answer_model(),
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        },
    )
    return data.get("output_text") or extract_response_text(data)

def answer_claims(answer: str) -> list[str]:
    cleaned = re.sub(r"\[[0-9,\s]+\]", "", answer or "")
    pieces = re.split(r"(?:\n+|(?<=[.!?])\s+)", cleaned)
    claims: list[str] = []
    for piece in pieces:
        claim = piece.strip(" -*\t")
        if len(significant_terms(claim, limit=12)) < 4:
            continue
        claims.append(claim[:280])
        if len(claims) >= 8:
            break
    return claims


def fuzzy_support_score(claim: str, evidence_texts: list[str]) -> float:
    claim_terms = set(significant_terms(claim, limit=30))
    if not claim_terms:
        return 1.0
    best = 0.0
    for evidence in evidence_texts:
        evidence_terms = set(significant_terms(evidence, limit=260))
        if not evidence_terms:
            continue
        overlap = len(claim_terms & evidence_terms) / len(claim_terms)
        best = max(best, overlap)
    return best


def review_answer_fidelity(answer: str, context: list[dict[str, Any]]) -> dict[str, Any]:
    claims = answer_claims(answer)
    evidence_texts = [item.get("text") or "" for item in context]
    weak_claims = []
    total = 0.0
    for claim in claims:
        score = fuzzy_support_score(claim, evidence_texts)
        total += score
        if score < 0.34:
            weak_claims.append({"claim": claim, "score": round(score, 2)})
    average = total / len(claims) if claims else None
    notes: list[str] = []
    source_mix = source_mix_from_context(context)
    if len(source_mix) <= 1:
        notes.append("Evidence came from one source type after retrieval.")
    if any((item.get("quality") == "metadata_only") for item in context):
        notes.append("Some reviewed evidence is metadata-only.")
    return {
        "status": "pass" if claims and not weak_claims and (average or 0) >= 0.45 else "caution",
        "score": round(average, 2) if average is not None else None,
        "claim_count": len(claims),
        "supported_claims": max(0, len(claims) - len(weak_claims)),
        "weak_claims": weak_claims[:3],
        "source_mix": source_mix,
        "notes": notes,
    }


def review_retrieval_context(context: list[dict[str, Any]], note: str) -> dict[str, Any]:
    return {
        "status": "retrieval-only",
        "score": None,
        "claim_count": 0,
        "supported_claims": 0,
        "weak_claims": [],
        "source_mix": source_mix_from_context(context),
        "notes": [note],
    }


def answer_question(
    conn: sqlite3.Connection,
    question: str,
    filters: dict[str, Any] | None = None,
    limit: int = 10,
    use_all_sources: bool = True,
    review: bool = True,
) -> dict[str, Any]:
    question = compact_text(question)
    if not question:
        raise AppError("Question is required.")

    limit = max(4, min(limit, 16))
    retrieval_filters = filters or {}
    initial_context = retrieve_answer_context(conn, question, retrieval_filters, limit, use_all_sources)
    if not initial_context:
        return {
            "question": question,
            "answer": "I could not find matching source records for that question.",
            "citations": [],
            "mode": "no-context",
            "used_all_sources": use_all_sources,
            "review": review_retrieval_context([], "No matching evidence was retrieved."),
        }

    if not model_configured():
        return {
            "question": question,
            "answer": f"I found relevant evidence, but {provider_display_name()} is not configured, so I did not generate a narrative answer.",
            "citations": build_citations(initial_context),
            "mode": "retrieval-only",
            "used_all_sources": use_all_sources,
            "review": review_retrieval_context(
                initial_context,
                f"Generated answer and fidelity review were skipped because {provider_display_name()} is not configured.",
            ),
        }

    draft_answer = generate_evidence_answer(question, initial_context, stage="draft")
    expansion_query = build_expansion_query(question, draft_answer)
    expanded_context = (
        retrieve_answer_context(conn, expansion_query, retrieval_filters, limit, use_all_sources)
        if expansion_query
        else []
    )
    context = merge_contexts(initial_context, expanded_context, limit=max(limit, 14))
    answer = generate_evidence_answer(question, context, stage="final")
    fidelity = review_answer_fidelity(answer, context) if review else None
    return {
        "question": question,
        "answer": answer or "The model returned no answer text.",
        "citations": build_citations(context),
        "mode": "llm-reviewed",
        "model": current_answer_model(),
        "used_all_sources": use_all_sources,
        "review": fidelity,
    }

def extract_chat_response_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content:
            chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    chunks.append(str(item["text"]))
    return "\n".join(chunks).strip()

def extract_response_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for output in data.get("output") or []:
        for content in output.get("content") or []:
            text = content.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["rebuild", "status", "embeddings"])
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("EMAIL_MVP_DB", DEFAULT_DB)))
    parser.add_argument("--emails", type=Path, default=DEFAULT_EMAILS)
    parser.add_argument("--threads", type=Path, default=DEFAULT_THREADS)
    parser.add_argument("--graph-records", type=Path, default=DEFAULT_GRAPH_RECORDS)
    parser.add_argument("--peak-new-docs", type=Path, default=DEFAULT_PEAK_NEW_DOCS)
    parser.add_argument("--with-embeddings", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if args.command == "rebuild":
        result = rebuild_database(
            db_path=args.db,
            emails_path=args.emails,
            threads_path=args.threads,
            graph_records_path=args.graph_records,
            peak_new_docs_path=args.peak_new_docs,
            with_embeddings=args.with_embeddings,
        )
    else:
        conn = connect(args.db)
        try:
            create_schema(conn)
            if args.command == "embeddings":
                result = build_missing_embeddings(conn, limit=args.limit)
                conn.commit()
            else:
                result = database_status(conn, args.db)
        finally:
            conn.close()

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





