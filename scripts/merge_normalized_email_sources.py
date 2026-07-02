#!/usr/bin/env python3
"""Merge normalized MSG and PST JSONL records into the main email input.

The merge prefers the record with the longest body text when duplicates share the
same Internet Message-ID, then falls back to body hash + sender + timestamp.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MSG_INPUT = PROJECT_ROOT / "AI-Outputs" / "normalized_emails.jsonl"
DEFAULT_PST_INPUT = PROJECT_ROOT / "AI-Outputs" / "normalized_pst_emails.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "AI-Outputs" / "normalized_emails_merged.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "AI-Outputs" / "normalized_emails_merged_summary.json"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_source_kind(record: dict[str, Any]) -> str:
    return (record.get("parse") or {}).get("source_kind") or (record.get("source") or {}).get("kind") or "email"


def dedupe_key(record: dict[str, Any]) -> str:
    message_id = (record.get("internet_message_id") or "").strip().lower()
    if message_id:
        return f"message-id:{message_id}"
    body_hash = record.get("body_sha256")
    if body_hash:
        sender = (record.get("sender") or "").lower()
        sent_at = (record.get("sent_at") or "")[:19]
        return f"body:{body_hash}:{sender}:{sent_at}"
    source = record.get("source") or {}
    return f"source:{source.get('relative_path') or source.get('path') or ''}"


def better_record(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    existing_len = existing.get("body_length") or len(existing.get("body_text") or "")
    candidate_len = candidate.get("body_length") or len(candidate.get("body_text") or "")
    if candidate_len > existing_len:
        return candidate
    existing_kind = record_source_kind(existing)
    candidate_kind = record_source_kind(candidate)
    if existing_kind == "email_pst" and candidate_kind != "email_pst" and candidate_len >= existing_len:
        return candidate
    return existing


def merge_sources(inputs: list[Path], output: Path) -> dict[str, Any]:
    by_key: dict[str, dict[str, Any]] = {}
    input_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    duplicate_count = 0

    for path in inputs:
        records = read_jsonl(path)
        input_counts[str(path)] = len(records)
        for record in records:
            if (record.get("parse") or {}).get("status") == "error":
                continue
            key = dedupe_key(record)
            kind = record_source_kind(record)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            if key in by_key:
                duplicate_count += 1
                by_key[key] = better_record(by_key[key], record)
            else:
                by_key[key] = record

    merged = list(by_key.values())
    merged.sort(key=lambda record: ((record.get("sent_at") or ""), (record.get("subject") or ""), (record.get("source") or {}).get("relative_path") or ""))
    write_jsonl(merged, output)
    return {
        "inputs": input_counts,
        "output": str(output),
        "records_written": len(merged),
        "duplicate_records_removed": duplicate_count,
        "input_kind_counts": kind_counts,
        "output_kind_counts": output_kind_counts(merged),
    }


def output_kind_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        kind = record_source_kind(record)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--msg-input", type=Path, default=DEFAULT_MSG_INPUT)
    parser.add_argument("--pst-input", type=Path, default=DEFAULT_PST_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--replace-main", action="store_true", help="Also overwrite AI-Outputs/normalized_emails.jsonl with merged output.")
    parser.add_argument("--main-output", type=Path, default=DEFAULT_MSG_INPUT, help="Main normalized email file to replace when --replace-main is set.")
    args = parser.parse_args()

    summary = merge_sources([args.msg_input, args.pst_input], args.output)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.replace_main:
        args.main_output.write_text(args.output.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        summary["replaced_main"] = str(args.main_output)
        args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["records_written"] else 1


if __name__ == "__main__":
    raise SystemExit(main())