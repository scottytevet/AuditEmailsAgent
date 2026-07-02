#!/usr/bin/env python3
"""Build a de-duplicated conversation manifest from normalized email JSONL."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "AI-Outputs" / "normalized_emails.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "AI-Outputs" / "email_threads.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "AI-Outputs" / "email_threads_summary.json"

RE_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|ext)\s*[:_\-]\s*)+", re.I)
WS_RE = re.compile(r"\s+")


def normalize_subject(value: str | None) -> str:
    if not value:
        return ""
    subject = value.replace("_", " ").strip()
    while True:
        cleaned = RE_PREFIX_RE.sub("", subject)
        if cleaned == subject:
            break
        subject = cleaned
    return WS_RE.sub(" ", subject).strip().lower()


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def dedupe_key(record: dict[str, Any]) -> str:
    message_id = (record.get("internet_message_id") or "").strip().lower()
    if message_id:
        return f"message-id:{message_id}"
    body_hash = record.get("body_sha256")
    if body_hash:
        sender = (record.get("sender") or "").lower()
        sent_at = (record.get("sent_at") or "")[:19]
        return f"body:{body_hash}:{sender}:{sent_at}"
    source_path = record.get("source", {}).get("path") or ""
    return f"source:{source_path}"


def thread_key(record: dict[str, Any]) -> str:
    subject = normalize_subject(record.get("normalized_subject") or record.get("subject"))
    in_reply_to = (record.get("in_reply_to_id") or "").strip().lower()
    message_id = (record.get("internet_message_id") or "").strip().lower()
    if in_reply_to:
        seed = f"reply:{subject}:{in_reply_to}"
    elif subject:
        seed = f"subject:{subject}"
    elif message_id:
        seed = f"message:{message_id}"
    else:
        seed = f"source:{record.get('source', {}).get('path', '')}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def body_excerpt(value: str, limit: int = 900) -> str:
    text = WS_RE.sub(" ", value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def build_manifest(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unique_records: dict[str, dict[str, Any]] = {}
    duplicate_counts: dict[str, int] = {}

    for record in records:
        key = dedupe_key(record)
        duplicate_counts[key] = duplicate_counts.get(key, 0) + 1
        existing = unique_records.get(key)
        if not existing:
            unique_records[key] = record
            continue
        existing_len = existing.get("body_length") or 0
        new_len = record.get("body_length") or 0
        if new_len > existing_len:
            unique_records[key] = record

    groups: dict[str, list[dict[str, Any]]] = {}
    for record in unique_records.values():
        groups.setdefault(thread_key(record), []).append(record)

    threads: list[dict[str, Any]] = []
    for key, group in groups.items():
        group.sort(key=lambda r: parse_time(r.get("sent_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        times = [parse_time(r.get("sent_at")) for r in group]
        times = [t for t in times if t]
        subjects = [
            r.get("subject")
            for r in group
            if r.get("subject")
        ]
        normalized_subject = normalize_subject(subjects[0] if subjects else "")
        senders = sorted({r.get("sender") for r in group if r.get("sender")})
        participants = sorted(
            {
                participant
                for r in group
                for participant in (r.get("participants") or [])
                if participant
            }
        )
        body_chars = sum((r.get("body_length") or 0) for r in group)
        sources = [
            {
                "path": r.get("source", {}).get("path"),
                "relative_path": r.get("source", {}).get("relative_path"),
                "internet_message_id": r.get("internet_message_id"),
                "sent_at": r.get("sent_at"),
                "sender": r.get("sender"),
                "subject": r.get("subject"),
                "body_sha256": r.get("body_sha256"),
            }
            for r in group
        ]
        sample_bodies = [
            body_excerpt(r.get("body_text") or "")
            for r in group
            if r.get("body_text")
        ][:3]

        threads.append(
            {
                "thread_id": key,
                "normalized_subject": normalized_subject,
                "display_subject": subjects[0] if subjects else "",
                "message_count": len(group),
                "duplicate_source_count": sum(
                    duplicate_counts.get(dedupe_key(r), 1) for r in group
                ),
                "start_at": iso(min(times)) if times else None,
                "end_at": iso(max(times)) if times else None,
                "senders": senders,
                "participants": participants,
                "body_chars": body_chars,
                "has_body_text": body_chars > 0,
                "sources": sources,
                "sample_body_excerpts": sample_bodies,
            }
        )

    threads.sort(key=lambda t: (t.get("start_at") or "", t.get("display_subject") or ""))

    summary = {
        "input_records": len(records),
        "unique_messages": len(unique_records),
        "duplicate_records": len(records) - len(unique_records),
        "thread_count": len(threads),
        "threads_with_body_text": sum(1 for t in threads if t["has_body_text"]),
        "largest_threads": sorted(
            [
                {
                    "thread_id": t["thread_id"],
                    "display_subject": t["display_subject"],
                    "message_count": t["message_count"],
                    "duplicate_source_count": t["duplicate_source_count"],
                    "start_at": t["start_at"],
                    "end_at": t["end_at"],
                }
                for t in threads
            ],
            key=lambda t: (t["message_count"], t["duplicate_source_count"]),
            reverse=True,
        )[:25],
    }
    return threads, summary


def write_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()

    records = read_jsonl(args.input)
    threads, summary = build_manifest(records)
    write_jsonl(threads, args.output)
    args.summary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
