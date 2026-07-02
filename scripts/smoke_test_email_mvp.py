#!/usr/bin/env python3
"""Smoke test the email QA MVP with a tiny synthetic archive."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import email_index  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="peak-email-mvp-"))
    emails_path = tmp / "normalized_emails.jsonl"
    threads_path = tmp / "email_threads.jsonl"
    db_path = tmp / "email_mvp.sqlite"

    emails = [
        {
            "source": {
                "kind": "msg",
                "path": r"C:\archive\invoice.msg",
                "relative_path": "Donna/invoice.msg",
            },
            "subject": "Invoice 1-6631 for Rob Dean",
            "normalized_subject": "invoice 1-6631 for rob dean",
            "sender": "peak@example.com",
            "to": "tevet@example.com",
            "cc": "",
            "bcc": "",
            "sent_at": "2026-05-10T12:00:00+00:00",
            "internet_message_id": "<invoice@example.com>",
            "participants": ["peak@example.com", "tevet@example.com"],
            "has_attachment": "false",
            "body_text": "Peak requested payment of invoice 1-6631 for the Rob Dean VP Sales placement.",
            "body_length": 80,
            "body_sha256": "sample1",
            "purview": {},
            "headers": {},
            "parse": {"status": "ok"},
            "conversation_key": "thread-invoice",
        },
        {
            "source": {
                "kind": "msg",
                "path": r"C:\archive\response.msg",
                "relative_path": "Donna/response.msg",
            },
            "subject": "Re: Invoice 1-6631 for Rob Dean",
            "normalized_subject": "invoice 1-6631 for rob dean",
            "sender": "tevet@example.com",
            "to": "peak@example.com",
            "cc": "",
            "bcc": "",
            "sent_at": "2026-05-11T12:00:00+00:00",
            "internet_message_id": "<response@example.com>",
            "in_reply_to_id": "<invoice@example.com>",
            "participants": ["peak@example.com", "tevet@example.com"],
            "has_attachment": "false",
            "body_text": "TEVET disputed late charges while discussing ISR replacement obligations.",
            "body_length": 72,
            "body_sha256": "sample2",
            "purview": {},
            "headers": {},
            "parse": {"status": "ok"},
            "conversation_key": "thread-invoice",
        },
    ]
    threads = [
        {
            "thread_id": "thread-invoice",
            "display_subject": "Invoice 1-6631 for Rob Dean",
            "normalized_subject": "invoice 1-6631 for rob dean",
            "message_count": 2,
            "duplicate_source_count": 2,
            "start_at": "2026-05-10T12:00:00+00:00",
            "end_at": "2026-05-11T12:00:00+00:00",
            "senders": ["peak@example.com", "tevet@example.com"],
            "participants": ["peak@example.com", "tevet@example.com"],
            "body_chars": 152,
            "sources": [
                {"relative_path": "Donna/invoice.msg"},
                {"relative_path": "Donna/response.msg"},
            ],
            "sample_body_excerpts": [],
        }
    ]

    write_jsonl(emails_path, emails)
    write_jsonl(threads_path, threads)
    result = email_index.rebuild_database(db_path, emails_path, threads_path)
    if result["emails_indexed"] != 2:
        raise AssertionError(result)

    conn = email_index.connect(db_path)
    try:
        search = email_index.search_emails(conn, "invoice 1-6631")
        if len(search["results"]) != 2:
            raise AssertionError(search)
        answer = email_index.answer_question(conn, "What happened around invoice 1-6631?")
        if not answer["citations"]:
            raise AssertionError(answer)
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "db": str(db_path),
                "emails_indexed": result["emails_indexed"],
                "search_results": len(search["results"]),
                "ask_mode": answer["mode"],
                "citations": len(answer["citations"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

