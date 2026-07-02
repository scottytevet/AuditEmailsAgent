#!/usr/bin/env python3
"""Normalize Peak Sales Recruiting email exports into JSONL.

This script intentionally uses only the Python standard library. Outlook MSG
files are OLE Compound File Binary files; the parser below extracts root-level
MAPI property streams such as subject, sender, timestamps, headers, and body.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email
from email import policy
from email.parser import BytesParser
import hashlib
import html
import json
import os
from pathlib import Path
import re
import struct
import sys
from typing import Any, Iterable


DEFAULT_SOURCE = Path(r"C:\Users\ScottyGomez\Documents\PeakSalesRecruiting")
DEFAULT_REPORT = DEFAULT_SOURCE / (
    "Reports-PeakSaleRecruiting_com-Peaksalesrecruiting_com-"
    "StartDirectExport-PeakSalesRecruiting_MessageDump-2026-06-26_15-36-50"
) / "Items_0_2026-06-26_15-36-50.csv"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "AI-Outputs" / "normalized_emails.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "AI-Outputs" / "normalized_emails_summary.json"

FREE_SECT = 0xFFFFFFFF
END_OF_CHAIN = 0xFFFFFFFE
FAT_SECT = 0xFFFFFFFD
DIFAT_SECT = 0xFFFFFFFC

MSG_PROP_RE = re.compile(r"^__substg1\.0_([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})$")

PROP_NAMES = {
    "001A": "message_class",
    "0037": "subject",
    "0039": "client_submit_time",
    "0042": "sent_representing_name",
    "0064": "sent_representing_email",
    "0070": "conversation_topic",
    "0071": "conversation_index",
    "007D": "transport_headers",
    "0C1A": "sender_name",
    "0C1D": "sender_search_key",
    "0C1E": "sender_address_type",
    "0C1F": "sender_email",
    "0E02": "display_bcc",
    "0E03": "display_cc",
    "0E04": "display_to",
    "0E06": "message_delivery_time",
    "0E07": "message_flags",
    "0E1D": "normalized_subject",
    "0E28": "primary_send_account",
    "0E29": "next_send_acct",
    "0E2B": "trust_sender",
    "1000": "body",
    "1009": "rtf_compressed",
    "1013": "html_body",
    "1035": "internet_message_id",
    "1042": "in_reply_to_id",
    "1046": "return_path",
    "3001": "display_name",
    "3002": "address_type",
    "3003": "email_address",
    "39FE": "smtp_address",
}

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


class CfbError(Exception):
    """Raised when a compound file cannot be parsed."""


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


class DirectoryEntry:
    def __init__(self, index: int, raw: bytes) -> None:
        self.index = index
        name_len = u16(raw, 64)
        if name_len >= 2:
            self.name = raw[: name_len - 2].decode("utf-16le", "replace")
        else:
            self.name = ""
        self.type = raw[66]
        self.left = u32(raw, 68)
        self.right = u32(raw, 72)
        self.child = u32(raw, 76)
        self.start_sector = u32(raw, 116)
        self.size = u64(raw, 120)

    @property
    def is_stream(self) -> bool:
        return self.type == 2

    @property
    def is_storage(self) -> bool:
        return self.type in (1, 5)


class CompoundFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        if self.data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise CfbError("not an OLE compound file")

        self.sector_size = 1 << u16(self.data, 30)
        self.mini_sector_size = 1 << u16(self.data, 32)
        self.num_fat_sectors = u32(self.data, 44)
        self.first_dir_sector = u32(self.data, 48)
        self.mini_cutoff_size = u32(self.data, 56)
        self.first_minifat_sector = u32(self.data, 60)
        self.num_minifat_sectors = u32(self.data, 64)
        self.first_difat_sector = u32(self.data, 68)
        self.num_difat_sectors = u32(self.data, 72)

        self.difat = self._load_difat()
        self.fat = self._load_fat()
        self.directory = self._load_directory()
        self.root = self.directory[0] if self.directory else None
        self.minifat = self._load_minifat()
        self.mini_stream = self._load_mini_stream()

    def _sector_offset(self, sector: int) -> int:
        return (sector + 1) * self.sector_size

    def _read_sector(self, sector: int) -> bytes:
        offset = self._sector_offset(sector)
        return self.data[offset : offset + self.sector_size]

    def _load_difat(self) -> list[int]:
        difat = [
            value
            for value in struct.unpack_from("<109I", self.data, 76)
            if value not in (FREE_SECT, END_OF_CHAIN)
        ]

        sector = self.first_difat_sector
        seen: set[int] = set()
        for _ in range(self.num_difat_sectors):
            if sector in (FREE_SECT, END_OF_CHAIN) or sector in seen:
                break
            seen.add(sector)
            block = self._read_sector(sector)
            entries = struct.unpack_from(
                f"<{(self.sector_size // 4) - 1}I", block, 0
            )
            difat.extend(v for v in entries if v not in (FREE_SECT, END_OF_CHAIN))
            sector = u32(block, self.sector_size - 4)

        return difat[: self.num_fat_sectors]

    def _load_fat(self) -> list[int]:
        fat: list[int] = []
        for sector in self.difat:
            block = self._read_sector(sector)
            fat.extend(struct.unpack_from(f"<{self.sector_size // 4}I", block, 0))
        return fat

    def _chain(self, start_sector: int) -> list[int]:
        if start_sector in (FREE_SECT, END_OF_CHAIN):
            return []
        chain: list[int] = []
        seen: set[int] = set()
        sector = start_sector
        while sector not in (FREE_SECT, END_OF_CHAIN):
            if sector in seen:
                raise CfbError(f"cycle in FAT chain at sector {sector}")
            if sector >= len(self.fat):
                raise CfbError(f"sector {sector} outside FAT")
            seen.add(sector)
            chain.append(sector)
            sector = self.fat[sector]
        return chain

    def _read_regular_stream(self, start_sector: int, size: int | None = None) -> bytes:
        chunks = [self._read_sector(sector) for sector in self._chain(start_sector)]
        data = b"".join(chunks)
        return data if size is None else data[:size]

    def _load_directory(self) -> list[DirectoryEntry]:
        directory_data = self._read_regular_stream(self.first_dir_sector)
        entries: list[DirectoryEntry] = []
        for i in range(0, len(directory_data), 128):
            raw = directory_data[i : i + 128]
            if len(raw) < 128:
                break
            entry = DirectoryEntry(i // 128, raw)
            if entry.type != 0:
                entries.append(entry)
            else:
                entries.append(entry)
        return entries

    def _load_minifat(self) -> list[int]:
        if self.num_minifat_sectors == 0 or self.first_minifat_sector in (
            FREE_SECT,
            END_OF_CHAIN,
        ):
            return []
        data = self._read_regular_stream(
            self.first_minifat_sector, self.num_minifat_sectors * self.sector_size
        )
        if not data:
            return []
        return list(struct.unpack_from(f"<{len(data) // 4}I", data, 0))

    def _load_mini_stream(self) -> bytes:
        if not self.root or self.root.start_sector in (FREE_SECT, END_OF_CHAIN):
            return b""
        return self._read_regular_stream(self.root.start_sector, int(self.root.size))

    def _read_mini_stream(self, start_sector: int, size: int) -> bytes:
        if start_sector in (FREE_SECT, END_OF_CHAIN):
            return b""
        chunks: list[bytes] = []
        seen: set[int] = set()
        sector = start_sector
        while sector not in (FREE_SECT, END_OF_CHAIN):
            if sector in seen:
                raise CfbError(f"cycle in mini FAT chain at sector {sector}")
            if sector >= len(self.minifat):
                raise CfbError(f"mini sector {sector} outside mini FAT")
            seen.add(sector)
            offset = sector * self.mini_sector_size
            chunks.append(self.mini_stream[offset : offset + self.mini_sector_size])
            sector = self.minifat[sector]
        return b"".join(chunks)[:size]

    def read_stream(self, entry: DirectoryEntry) -> bytes:
        if entry.size < self.mini_cutoff_size and self.minifat:
            return self._read_mini_stream(entry.start_sector, int(entry.size))
        return self._read_regular_stream(entry.start_sector, int(entry.size))

    def _child_ids(self, parent: DirectoryEntry) -> list[int]:
        ids: list[int] = []
        seen: set[int] = set()

        def walk(entry_id: int) -> None:
            if entry_id in (FREE_SECT, END_OF_CHAIN) or entry_id >= len(self.directory):
                return
            if entry_id in seen:
                return
            seen.add(entry_id)
            entry = self.directory[entry_id]
            walk(entry.left)
            ids.append(entry_id)
            walk(entry.right)

        walk(parent.child)
        return ids

    def iter_streams(self) -> Iterable[tuple[tuple[str, ...], DirectoryEntry]]:
        if not self.directory:
            return

        def descend(parent: DirectoryEntry, prefix: tuple[str, ...]) -> Iterable[tuple[tuple[str, ...], DirectoryEntry]]:
            for child_id in self._child_ids(parent):
                entry = self.directory[child_id]
                if not entry.name:
                    continue
                path = prefix + (entry.name,)
                if entry.is_stream:
                    yield path, entry
                elif entry.is_storage:
                    yield from descend(entry, path)

        yield from descend(self.directory[0], tuple())


def decode_filetime(data: bytes) -> str | None:
    if len(data) < 8:
        return None
    value = u64(data, 0)
    if value == 0:
        return None
    try:
        base = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
        return (base + dt.timedelta(microseconds=value / 10)).isoformat()
    except (OverflowError, ValueError):
        return None


def strip_nulls(value: str) -> str:
    return value.replace("\x00", "").strip()


def decode_prop(prop_type: str, data: bytes) -> Any:
    if prop_type == "001F":
        return strip_nulls(data.decode("utf-16le", "replace"))
    if prop_type == "001E":
        return strip_nulls(data.decode("cp1252", "replace"))
    if prop_type == "0040":
        return decode_filetime(data)
    if prop_type in ("0003", "000B"):
        if len(data) >= 4:
            value = struct.unpack_from("<i", data, 0)[0]
            return bool(value) if prop_type == "000B" else value
        return None
    if prop_type == "0102":
        return data
    return data.hex()


def html_to_text(raw: bytes) -> str:
    sample = raw[:500].decode("ascii", "ignore").lower()
    charset_match = re.search(r"charset=[\"']?([a-z0-9_\-]+)", sample)
    encodings = []
    if charset_match:
        encodings.append(charset_match.group(1))
    encodings.extend(["utf-8", "utf-16le", "cp1252"])

    text = ""
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    if not text:
        text = raw.decode("utf-8", "replace")

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = TAG_RE.sub(" ", text)
    return normalize_text(html.unescape(text))


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_transport_headers(raw_headers: str) -> dict[str, Any]:
    if not raw_headers:
        return {}
    try:
        msg = email.message_from_string(raw_headers, policy=policy.default)
    except Exception:
        return {}
    return {
        "message_id": msg.get("Message-ID"),
        "in_reply_to": msg.get("In-Reply-To"),
        "references": msg.get_all("References", []),
        "from": msg.get("From"),
        "to": msg.get("To"),
        "cc": msg.get("Cc"),
        "date": msg.get("Date"),
    }


def extract_email_addresses(value: str | None) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    addresses: list[str] = []
    for match in EMAIL_RE.finditer(value):
        address = match.group(0).lower()
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return addresses


def first_email_address(*values: str | None) -> str | None:
    for value in values:
        addresses = extract_email_addresses(value)
        if addresses:
            return addresses[0]
    return None


def parse_msg(path: Path) -> dict[str, Any]:
    cfb = CompoundFile(path)
    props: dict[str, Any] = {}
    prop_types: dict[str, str] = {}

    for stream_path, entry in cfb.iter_streams():
        if len(stream_path) != 1:
            continue
        stream_name = stream_path[0]
        match = MSG_PROP_RE.match(stream_name)
        if not match:
            continue
        prop_id, prop_type = match.groups()
        prop_id = prop_id.upper()
        prop_type = prop_type.upper()
        name = PROP_NAMES.get(prop_id, f"mapi_{prop_id}")
        value = decode_prop(prop_type, cfb.read_stream(entry))
        props[name] = value
        prop_types[name] = prop_type

    body_text = ""
    if isinstance(props.get("body"), str):
        body_text = normalize_text(props["body"])
    elif isinstance(props.get("html_body"), bytes):
        body_text = html_to_text(props["html_body"])

    html_length = len(props["html_body"]) if isinstance(props.get("html_body"), bytes) else 0
    rtf_length = (
        len(props["rtf_compressed"]) if isinstance(props.get("rtf_compressed"), bytes) else 0
    )

    binary_props = {"html_body", "rtf_compressed", "conversation_index"}
    safe_props = {
        key: value
        for key, value in props.items()
        if key not in binary_props and not isinstance(value, (bytes, bytearray))
    }

    headers = parse_transport_headers(
        safe_props.get("transport_headers") if isinstance(safe_props.get("transport_headers"), str) else ""
    )

    internet_message_id = safe_props.get("internet_message_id") or headers.get("message_id")
    in_reply_to = safe_props.get("in_reply_to_id") or headers.get("in_reply_to")

    return {
        "msg": safe_props,
        "body_text": body_text,
        "body_length": len(body_text),
        "body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        if body_text
        else None,
        "html_body_length": html_length,
        "rtf_compressed_length": rtf_length,
        "headers": headers,
        "internet_message_id": internet_message_id,
        "in_reply_to_id": in_reply_to,
    }


def clean_subject(subject: str | None) -> str:
    if not subject:
        return ""
    value = subject.strip()
    while True:
        new_value = re.sub(r"^\s*((re|fw|fwd|ext)\s*[:_\-]\s*)+", "", value, flags=re.I)
        if new_value == value:
            break
        value = new_value
    value = value.replace("_", " ")
    return WS_RE.sub(" ", value).strip().lower()


def read_purview_rows(report_path: Path) -> list[dict[str, str]]:
    with report_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_purview_lookup(report_path: Path) -> tuple[list[dict[str, str]], dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    rows = read_purview_rows(report_path)
    by_message_id: dict[str, list[dict[str, str]]] = {}
    by_subject: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        message_id = (row.get("Internet message ID") or "").strip().lower()
        if message_id:
            by_message_id.setdefault(message_id, []).append(row)
        key = clean_subject(row.get("Subject/Title"))
        if key:
            by_subject.setdefault(key, []).append(row)
    return rows, by_message_id, by_subject


def find_purview_match(
    parsed: dict[str, Any],
    msg_path: Path,
    source_dir: Path,
    by_message_id: dict[str, list[dict[str, str]]],
    by_subject: dict[str, list[dict[str, str]]],
) -> dict[str, str] | None:
    message_id = (parsed.get("internet_message_id") or "").strip().lower()
    if message_id and message_id in by_message_id:
        return by_message_id[message_id][0]

    subject = parsed.get("msg", {}).get("subject") or msg_path.stem
    matches = by_subject.get(clean_subject(subject), [])
    if not matches:
        return None

    rel_parts = {part.lower() for part in msg_path.relative_to(source_dir).parts}
    best = None
    best_score = -1
    sender = (parsed.get("msg", {}).get("sender_email") or "").lower()
    for row in matches:
        score = 0
        if sender and sender in (row.get("Sender") or "").lower():
            score += 4
        target_path = (row.get("Target path") or "").lower()
        original_path = (row.get("Original path") or "").lower()
        for part in rel_parts:
            if part and (part in target_path or part in original_path):
                score += 1
        if score > best_score:
            best = row
            best_score = score
    return best


def purview_record(row: dict[str, str] | None) -> dict[str, Any]:
    if not row:
        return {}
    keys = [
        "Status",
        "Custodian",
        "Data source",
        "Date",
        "Email date sent",
        "Received",
        "Sender",
        "To",
        "CC",
        "BCC",
        "Subject/Title",
        "Internet message ID",
        "Has attachment",
        "File extension",
        "Original path",
        "Target path",
        "Size",
        "Sensitive type",
        "Recipient count",
        "Message kind",
        "Type",
        "Workload",
    ]
    return {key: row.get(key, "") for key in keys if row.get(key, "")}


def build_record(
    path: Path,
    source_dir: Path,
    parsed: dict[str, Any],
    matched_row: dict[str, str] | None,
) -> dict[str, Any]:
    msg = parsed.get("msg", {})
    purview = purview_record(matched_row)
    subject = msg.get("subject") or purview.get("Subject/Title") or path.stem
    headers = parsed.get("headers", {})
    sender = (
        first_email_address(
            msg.get("sender_email"),
            headers.get("from") if isinstance(headers, dict) else None,
            purview.get("Sender"),
            msg.get("sender_name"),
        )
        or purview.get("Sender")
        or msg.get("sender_email")
        or msg.get("sender_name")
    )
    to = msg.get("display_to") or purview.get("To")
    cc = msg.get("display_cc") or purview.get("CC")
    bcc = msg.get("display_bcc") or purview.get("BCC")
    sent_at = (
        msg.get("client_submit_time")
        or msg.get("message_delivery_time")
        or purview.get("Email date sent")
        or purview.get("Date")
        or purview.get("Received")
    )

    participants = []
    for value in [sender, to, cc, bcc, purview.get("Email recipients")]:
        participants.extend(extract_email_addresses(value))
    participants = sorted(set(participants))

    stat = path.stat()
    body_text = parsed.get("body_text") or ""
    normalized_subject = msg.get("normalized_subject") or clean_subject(subject)
    conversation_key_parts = [
        parsed.get("internet_message_id") or "",
        parsed.get("in_reply_to_id") or "",
        normalized_subject or "",
    ]
    conversation_seed = "|".join(str(part) for part in conversation_key_parts if part)
    conversation_key = hashlib.sha256(conversation_seed.encode("utf-8")).hexdigest()[:16] if conversation_seed else None

    return {
        "source": {
            "kind": "msg",
            "path": str(path),
            "relative_path": str(path.relative_to(source_dir)),
            "size_bytes": stat.st_size,
            "modified_at": dt.datetime.fromtimestamp(
                stat.st_mtime, tz=dt.timezone.utc
            ).isoformat(),
        },
        "subject": subject,
        "normalized_subject": normalized_subject,
        "sender": sender,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "sent_at": sent_at,
        "internet_message_id": parsed.get("internet_message_id")
        or purview.get("Internet message ID"),
        "in_reply_to_id": parsed.get("in_reply_to_id"),
        "participants": participants,
        "has_attachment": purview.get("Has attachment"),
        "body_text": body_text,
        "body_length": parsed.get("body_length", 0),
        "body_sha256": parsed.get("body_sha256"),
        "html_body_length": parsed.get("html_body_length", 0),
        "rtf_compressed_length": parsed.get("rtf_compressed_length", 0),
        "conversation_key": conversation_key,
        "purview": purview,
        "headers": headers,
        "parse": {
            "status": "ok",
            "purview_matched": bool(matched_row),
        },
    }


def normalize_msg_exports(source_dir: Path, report_path: Path, output_path: Path) -> dict[str, Any]:
    purview_rows, by_message_id, by_subject = load_purview_lookup(report_path)
    msg_paths = sorted(source_dir.rglob("*.msg"))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "source_dir": str(source_dir),
        "report_path": str(report_path),
        "output_path": str(output_path),
        "purview_rows": len(purview_rows),
        "msg_files_found": len(msg_paths),
        "records_written": 0,
        "parse_errors": 0,
        "purview_matches": 0,
        "body_text_records": 0,
        "total_body_chars": 0,
        "errors": [],
    }

    with output_path.open("w", encoding="utf-8", newline="\n") as out:
        for i, path in enumerate(msg_paths, start=1):
            try:
                parsed = parse_msg(path)
                matched_row = find_purview_match(
                    parsed, path, source_dir, by_message_id, by_subject
                )
                record = build_record(path, source_dir, parsed, matched_row)
                if matched_row:
                    summary["purview_matches"] += 1
                if record["body_length"]:
                    summary["body_text_records"] += 1
                    summary["total_body_chars"] += record["body_length"]
            except Exception as exc:
                summary["parse_errors"] += 1
                record = {
                    "source": {
                        "kind": "msg",
                        "path": str(path),
                        "relative_path": str(path.relative_to(source_dir)),
                    },
                    "parse": {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                }
                if len(summary["errors"]) < 25:
                    summary["errors"].append(record)

            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            summary["records_written"] += 1
            if i % 250 == 0:
                print(f"processed {i}/{len(msg_paths)} msg files", file=sys.stderr)

    return summary


def write_summary(summary: dict[str, Any], summary_path: Path) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()

    if not args.source.exists():
        print(f"source folder not found: {args.source}", file=sys.stderr)
        return 2
    if not args.report.exists():
        print(f"Purview report not found: {args.report}", file=sys.stderr)
        return 2

    summary = normalize_msg_exports(args.source, args.report, args.output)
    write_summary(summary, args.summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["records_written"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
