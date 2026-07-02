#!/usr/bin/env python3
"""Import Graph Communications Audit outputs as source records for the QA MVP.

The importer is intentionally file-based. It reads the adjacent
GraphCommunicationsAudit output folder and emits email-like JSONL records that
can be indexed by app.email_index alongside normalized MSG emails.
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
DEFAULT_GRAPH_ROOT = PROJECT_ROOT.parent / "GraphCommunicationsAudit"
DEFAULT_OUTPUT = PROJECT_ROOT / "AI-Outputs" / "graph_source_records.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "AI-Outputs" / "graph_import_summary.json"

DATE_RE = re.compile(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)")
SPACE_RE = re.compile(r"\s+")

CANDIDATE_TERMS = [
    "Rob Dean",
    "Ryan Hofmockel",
    "Evan Harris",
    "Jody Kemp",
    "Charn Pram",
    "Connor Fletcher",
    "Peak Sales",
    "Carla Caldwell",
    "Greg Young",
    "Jerry Prucha",
    "Benjamin Boyd",
    "Ben Boyd",
    "Preston Mahler",
    "Janna Shepard",
    "Bonnie Hadley",
    "Beth Terranova",
    "Kathy Schuchardy",
]


def compact(value: str | None) -> str:
    return SPACE_RE.sub(" ", value or "").strip()


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_file(root: Path, pattern: str) -> Path | None:
    matches = sorted(root.glob(pattern))
    return matches[-1] if matches else None


def rows_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return [row for row in value["rows"] if isinstance(row, dict)]
    return []


def date_from_text(*values: str | None) -> str | None:
    for value in values:
        match = DATE_RE.search(value or "")
        if not match:
            continue
        year, month, day = match.groups()
        try:
            return dt.date(int(year), int(month), int(day)).isoformat() + "T00:00:00+00:00"
        except ValueError:
            continue
    return None


def relative_to_graph(path: Path, graph_root: Path) -> str:
    try:
        return str(path.relative_to(graph_root))
    except ValueError:
        return str(path)


def record_hash(*parts: str) -> str:
    seed = "|".join(parts)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def detected_terms(*values: str | None) -> list[str]:
    text = " ".join(value or "" for value in values).lower()
    found = []
    for term in CANDIDATE_TERMS:
        if term.lower() in text:
            found.append(term)
    return found


def source_record(
    *,
    kind: str,
    subject: str,
    sender: str,
    sent_at: str | None,
    source_path: Path | None,
    graph_root: Path,
    body_text: str,
    participants: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body_text = body_text.strip()
    source_path_text = str(source_path) if source_path else ""
    digest = record_hash(kind, subject, source_path_text, body_text)
    rel_path = relative_to_graph(source_path, graph_root) if source_path else f"generated/{kind}/{digest[:12]}"
    if kind == "graph_meeting_reconciliation":
        rel_path = f"{rel_path}#{digest[:12]}"
    return {
        "source": {
            "kind": kind,
            "path": source_path_text,
            "relative_path": f"GraphCommunicationsAudit\\{rel_path}",
        },
        "subject": subject,
        "normalized_subject": compact(subject).lower(),
        "sender": sender,
        "to": "",
        "cc": "",
        "bcc": "",
        "sent_at": sent_at,
        "internet_message_id": f"graph:{kind}:{digest[:32]}",
        "in_reply_to_id": None,
        "participants": sorted(set(participants or [])),
        "has_attachment": "false",
        "body_text": body_text,
        "body_length": len(body_text),
        "body_sha256": digest,
        "purview": {},
        "headers": {},
        "parse": {
            "status": "ok",
            "source_project": "GraphCommunicationsAudit",
            "source_kind": kind,
            **(metadata or {}),
        },
        "conversation_key": f"graph-{digest[:16]}",
    }


def import_transcriptions(graph_root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    summary_path = graph_root / "output" / "transcriptions" / "all-recording-transcription-summary.json"
    rows = rows_from_json(load_json(summary_path, []))
    records: list[dict[str, Any]] = []
    counts = {"high": 0, "low": 0, "missing_text_file": 0}
    for row in rows:
        text_path_raw = row.get("transcriptTextPath") or ""
        text_path = Path(text_path_raw) if text_path_raw else None
        if not text_path or not text_path.exists():
            counts["missing_text_file"] += 1
            continue
        transcript_text = text_path.read_text(encoding="utf-8", errors="replace")
        word_count = int(row.get("wordCount") or len(transcript_text.split()))
        status = row.get("transcriptStatus") or "unknown"
        quality = "high" if status == "transcribed" and word_count >= 500 else "low"
        counts[quality] += 1
        recording_name = row.get("recordingName") or text_path.stem
        participants = detected_terms(recording_name, transcript_text[:2000])
        body = "\n".join(
            [
                f"Graph transcript source: {recording_name}",
                f"Transcript quality: {quality}",
                f"Transcript status: {status}",
                f"Word count: {word_count}",
                f"Duration seconds: {row.get('durationSeconds') or ''}",
                f"Recording file: {row.get('recordingPath') or ''}",
                f"Transcript VTT: {row.get('transcriptVttPath') or ''}",
                "",
                transcript_text,
            ]
        )
        records.append(
            source_record(
                kind="graph_transcript",
                subject=f"[Transcript][{quality}] {recording_name}",
                sender="Graph Communications Audit",
                sent_at=date_from_text(recording_name, str(text_path)),
                source_path=text_path,
                graph_root=graph_root,
                body_text=body,
                participants=participants,
                metadata={
                    "quality": quality,
                    "transcript_status": status,
                    "word_count": word_count,
                    "duration_seconds": row.get("durationSeconds"),
                    "recording_path": row.get("recordingPath"),
                    "transcript_vtt_path": row.get("transcriptVttPath"),
                    "transcript_json_path": row.get("transcriptJsonPath"),
                },
            )
        )
    return records, counts


def import_placeholder_artifacts(graph_root: Path) -> tuple[list[dict[str, Any]], int]:
    path = graph_root / "output" / "transcript-only-artifacts" / "transcript-only-artifacts.json"
    rows = rows_from_json(load_json(path, {}))
    records: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("name") or "Unnamed transcript artifact"
        participants = detected_terms(name, row.get("matchedTerms"))
        source_path = Path(row["downloadedPath"]) if row.get("downloadedPath") else path
        body = "\n".join(
            [
                f"Graph artifact source: {name}",
                "Transcript quality: metadata_only",
                "Artifact type: Teams Meeting Transcript placeholder video",
                "Usable transcript text: no",
                f"Owner: {row.get('owner') or ''}",
                f"Created: {row.get('createdDateTime') or ''}",
                f"Matched terms: {row.get('matchedTerms') or ''}",
                f"Duration seconds: {row.get('durationSeconds') or ''}",
                f"Streams: {row.get('streams') or ''}",
                f"Web URL: {row.get('webUrl') or ''}",
                "",
                "This row is evidence that a Teams transcript artifact was found, but it is not usable transcript content for answer generation unless a text transcript is recovered later.",
            ]
        )
        records.append(
            source_record(
                kind="graph_transcript_placeholder",
                subject=f"[Transcript missing][metadata] {name}",
                sender=row.get("owner") or "Graph Communications Audit",
                sent_at=row.get("createdDateTime") or date_from_text(name),
                source_path=source_path,
                graph_root=graph_root,
                body_text=body,
                participants=participants,
                metadata={
                    "quality": "metadata_only",
                    "artifact_type": "placeholder_transcript_video",
                    "web_url": row.get("webUrl"),
                    "matched_terms": row.get("matchedTerms"),
                },
            )
        )
    return records, len(records)


def import_meeting_reconciliation(graph_root: Path) -> tuple[list[dict[str, Any]], dict[str, int], str | None]:
    path = latest_file(graph_root / "output", "meeting-reconciliation-*.json")
    if not path:
        return [], {}, None
    data = load_json(path, {})
    rows = rows_from_json(data)
    records: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status") or "unknown"
        recovery_track = row.get("recoveryTrack") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        subject = row.get("subject") or "Unlabeled meeting"
        participants = detected_terms(
            subject,
            row.get("matchingEmails"),
            row.get("purviewSearchTerms"),
            row.get("artifactNames"),
        )
        body = "\n".join(
            [
                f"Meeting reconciliation source: {subject}",
                f"Start: {row.get('startDateTime') or ''}",
                f"End: {row.get('endDateTime') or ''}",
                f"Status: {status}",
                f"Recovery track: {recovery_track}",
                f"Scan users: {row.get('scanUsers') or ''}",
                f"Organizers: {row.get('organizers') or ''}",
                f"Matching emails: {row.get('matchingEmails') or ''}",
                f"Graph transcript metadata rows: {row.get('graphTranscriptMetadataRows') or 0}",
                f"Artifact categories: {row.get('artifactCategories') or ''}",
                f"Artifact names: {row.get('artifactNames') or ''}",
                f"Purview date start: {row.get('purviewDateStart') or ''}",
                f"Purview date end: {row.get('purviewDateEnd') or ''}",
                f"Purview query: {row.get('purviewQuery') or ''}",
                "",
                "Use this row for audit availability, missing-content, and recovery-backlog questions. It is not a transcript unless a linked transcript source exists.",
            ]
        )
        records.append(
            source_record(
                kind="graph_meeting_reconciliation",
                subject=f"[Meeting status][{recovery_track}] {subject}",
                sender=row.get("organizers") or "Graph Communications Audit",
                sent_at=row.get("startDateTime"),
                source_path=path,
                graph_root=graph_root,
                body_text=body,
                participants=participants,
                metadata={
                    "quality": "metadata_only",
                    "status": status,
                    "recovery_track": recovery_track,
                    "artifact_categories": row.get("artifactCategories"),
                    "graph_transcript_metadata_rows": row.get("graphTranscriptMetadataRows"),
                },
            )
        )
    return records, status_counts, str(path)


def import_graph_communications(graph_root: Path, output_path: Path, summary_path: Path) -> dict[str, Any]:
    all_records: list[dict[str, Any]] = []

    transcript_records, transcript_counts = import_transcriptions(graph_root)
    placeholder_records, placeholder_count = import_placeholder_artifacts(graph_root)
    meeting_records, meeting_status_counts, reconciliation_path = import_meeting_reconciliation(graph_root)

    all_records.extend(transcript_records)
    all_records.extend(placeholder_records)
    all_records.extend(meeting_records)
    all_records.sort(key=lambda row: (row.get("sent_at") or "", row.get("subject") or ""))

    write_jsonl(output_path, all_records)
    summary = {
        "graph_root": str(graph_root),
        "output_path": str(output_path),
        "records_written": len(all_records),
        "transcript_records": len(transcript_records),
        "transcript_quality_counts": transcript_counts,
        "placeholder_records": placeholder_count,
        "meeting_reconciliation_records": len(meeting_records),
        "meeting_status_counts": meeting_status_counts,
        "meeting_reconciliation_source": reconciliation_path,
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-root", type=Path, default=DEFAULT_GRAPH_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()

    if not args.graph_root.exists():
        raise SystemExit(f"GraphCommunicationsAudit folder not found: {args.graph_root}")

    summary = import_graph_communications(args.graph_root, args.output, args.summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

