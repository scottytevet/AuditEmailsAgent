#!/usr/bin/env python3
"""Normalize PST mailboxes into JSONL records for the evidence QA index.

This script uses Outlook/MAPI through pywin32. It is intentionally separate from
normalize_peak_sales_emails.py because PST parsing requires Windows + Outlook.
Run it with the project virtual environment:

    .venv\\Scripts\\python.exe scripts\\normalize_peak_sales_psts.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

try:
    import pywintypes
    import win32com.client
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "pywin32 is required for PST extraction. Run: .venv\\Scripts\\python.exe -m pip install -r requirements-pst.txt"
    ) from exc


DEFAULT_SOURCE = Path(r"C:\Users\ScottyGomez\Documents\PeakSalesRecruiting")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "AI-Outputs" / "normalized_pst_emails.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "AI-Outputs" / "normalized_pst_emails_summary.json"

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
WS_RE = re.compile(r"\s+")
RE_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|ext)\s*[:_\-]\s*)+", re.I)

OL_STORE_UNICODE = 3
OL_MAIL_ITEM = 43
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_IN_REPLY_TO_ID = "http://schemas.microsoft.com/mapi/proptag/0x1042001F"


def normalize_text(value: str | None) -> str:
    value = value or ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def compact(value: str | None) -> str:
    return WS_RE.sub(" ", value or "").strip()


def clean_subject(subject: str | None) -> str:
    if not subject:
        return ""
    value = subject.replace("_", " ").strip()
    while True:
        cleaned = RE_PREFIX_RE.sub("", value)
        if cleaned == value:
            break
        value = cleaned
    return WS_RE.sub(" ", value).strip().lower()


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


def as_iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromtimestamp(float(value))
        except Exception:
            text = str(value).strip()
            return text or None
    if parsed.year < 1900 or parsed.year > 2100:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(dt.timezone.utc).isoformat()


def safe_get(obj: Any, attr: str, default: Any = "") -> Any:
    try:
        value = getattr(obj, attr)
        return default if value is None else value
    except Exception:
        return default


def safe_property(item: Any, uri: str) -> str:
    try:
        value = item.PropertyAccessor.GetProperty(uri)
        return str(value).strip() if value else ""
    except Exception:
        return ""


def sender_smtp(item: Any) -> str:
    sender_email = str(safe_get(item, "SenderEmailAddress", "") or "").strip()
    sender_name = str(safe_get(item, "SenderName", "") or "").strip()
    if "@" in sender_email:
        return sender_email.lower()
    try:
        sender = item.Sender
        exchange_user = sender.GetExchangeUser() if sender else None
        primary = exchange_user.PrimarySmtpAddress if exchange_user else ""
        if primary:
            return str(primary).lower()
    except Exception:
        pass
    emails = extract_email_addresses(sender_email) or extract_email_addresses(sender_name)
    return emails[0] if emails else sender_email or sender_name


def recipient_smtp(recipient: Any) -> str:
    address = str(safe_get(recipient, "Address", "") or "").strip()
    name = str(safe_get(recipient, "Name", "") or "").strip()
    if "@" in address:
        return address.lower()
    try:
        exchange_user = recipient.AddressEntry.GetExchangeUser()
        primary = exchange_user.PrimarySmtpAddress if exchange_user else ""
        if primary:
            return str(primary).lower()
    except Exception:
        pass
    emails = extract_email_addresses(address) or extract_email_addresses(name)
    return emails[0] if emails else address or name


def collect_recipients(item: Any) -> tuple[str, str, str, list[str]]:
    to_values: list[str] = []
    cc_values: list[str] = []
    bcc_values: list[str] = []
    participants: list[str] = []
    try:
        recipients = item.Recipients
        count = int(recipients.Count)
        for index in range(1, count + 1):
            recipient = recipients.Item(index)
            value = recipient_smtp(recipient)
            if not value:
                continue
            recipient_type = int(safe_get(recipient, "Type", 0) or 0)
            if recipient_type == 1:
                to_values.append(value)
            elif recipient_type == 2:
                cc_values.append(value)
            elif recipient_type == 3:
                bcc_values.append(value)
            participants.extend(extract_email_addresses(value))
    except Exception:
        pass
    return "; ".join(to_values), "; ".join(cc_values), "; ".join(bcc_values), participants


def folder_path(folder: Any) -> str:
    parts: list[str] = []
    current = folder
    while current:
        name = str(safe_get(current, "Name", "") or "")
        if name:
            parts.append(name)
        try:
            current = current.Parent
            if safe_get(current, "Class", None) is None:
                break
        except Exception:
            break
    return "/".join(reversed(parts))


def iter_folders(folder: Any) -> Iterable[Any]:
    yield folder
    try:
        folders = folder.Folders
        count = int(folders.Count)
        for index in range(1, count + 1):
            yield from iter_folders(folders.Item(index))
    except Exception:
        return


def message_record(item: Any, pst_path: Path, source_dir: Path, folder_label: str, item_index: int) -> dict[str, Any]:
    subject = str(safe_get(item, "Subject", "") or "") or "(no subject)"
    sender = sender_smtp(item)
    to, cc, bcc, recipient_participants = collect_recipients(item)
    body_text = normalize_text(str(safe_get(item, "Body", "") or ""))
    sent_at = as_iso(safe_get(item, "SentOn", None)) or as_iso(safe_get(item, "ReceivedTime", None))
    internet_message_id = safe_property(item, PR_INTERNET_MESSAGE_ID)
    in_reply_to_id = safe_property(item, PR_IN_REPLY_TO_ID)
    entry_id = str(safe_get(item, "EntryID", "") or "")
    has_attachment = "true" if int(safe_get(safe_get(item, "Attachments", None), "Count", 0) or 0) > 0 else "false"
    participants = sorted(set(extract_email_addresses(sender) + recipient_participants + extract_email_addresses(to) + extract_email_addresses(cc) + extract_email_addresses(bcc)))
    body_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest() if body_text else None
    normalized_subject = clean_subject(subject)
    conversation_seed = "|".join(part for part in [internet_message_id, in_reply_to_id, normalized_subject] if part)
    conversation_key = hashlib.sha256(conversation_seed.encode("utf-8")).hexdigest()[:16] if conversation_seed else None
    pst_rel = str(pst_path.relative_to(source_dir)) if pst_path.is_relative_to(source_dir) else pst_path.name
    stable_id = internet_message_id or entry_id or f"{folder_label}:{item_index}:{subject}:{sent_at}"
    relative_path = f"{pst_rel}::{folder_label}::{hashlib.sha256(stable_id.encode('utf-8')).hexdigest()[:20]}"
    stat = pst_path.stat()

    return {
        "source": {
            "kind": "pst",
            "path": str(pst_path),
            "relative_path": relative_path,
            "pst_path": str(pst_path),
            "pst_relative_path": pst_rel,
            "folder": folder_label,
            "entry_id": entry_id,
            "size_bytes": stat.st_size,
            "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat(),
        },
        "subject": subject,
        "normalized_subject": normalized_subject,
        "sender": sender,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "sent_at": sent_at,
        "internet_message_id": internet_message_id or None,
        "in_reply_to_id": in_reply_to_id or None,
        "participants": participants,
        "has_attachment": has_attachment,
        "body_text": body_text,
        "body_length": len(body_text),
        "body_sha256": body_sha,
        "html_body_length": len(str(safe_get(item, "HTMLBody", "") or "")),
        "rtf_compressed_length": 0,
        "conversation_key": conversation_key,
        "purview": {},
        "headers": {},
        "parse": {
            "status": "ok",
            "source_kind": "email_pst",
            "source_system": "Microsoft Outlook PST",
            "source_project": "Peak Sales Recruiting PST Export",
            "source_label": "PST mailbox export",
            "quality": "high" if body_text else "metadata-only",
            "pst_folder": folder_label,
        },
    }


def open_pst(namespace: Any, pst_path: Path) -> Any:
    before = {str(safe_get(namespace.Stores.Item(i), "FilePath", "")).lower(): namespace.Stores.Item(i) for i in range(1, int(namespace.Stores.Count) + 1)}
    namespace.AddStoreEx(str(pst_path), OL_STORE_UNICODE)
    target = str(pst_path).lower()
    for index in range(1, int(namespace.Stores.Count) + 1):
        store = namespace.Stores.Item(index)
        file_path = str(safe_get(store, "FilePath", "") or "").lower()
        if file_path == target:
            return store
    for index in range(1, int(namespace.Stores.Count) + 1):
        store = namespace.Stores.Item(index)
        file_path = str(safe_get(store, "FilePath", "") or "").lower()
        if file_path not in before:
            return store
    raise RuntimeError(f"Outlook did not expose PST store after AddStoreEx: {pst_path}")


def close_pst(namespace: Any, store: Any) -> None:
    try:
        namespace.RemoveStore(store.GetRootFolder())
    except Exception:
        pass


def normalize_psts(source_dir: Path, output_path: Path, limit: int | None = None) -> dict[str, Any]:
    pst_paths = sorted(source_dir.rglob("*.pst"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    outlook = win32com.client.Dispatch("Outlook.Application")
    namespace = outlook.GetNamespace("MAPI")

    summary: dict[str, Any] = {
        "source_dir": str(source_dir),
        "output_path": str(output_path),
        "pst_files_found": len(pst_paths),
        "pst_files_processed": 0,
        "records_written": 0,
        "folders_scanned": 0,
        "items_seen": 0,
        "non_mail_items_skipped": 0,
        "parse_errors": 0,
        "body_text_records": 0,
        "total_body_chars": 0,
        "errors": [],
    }

    with output_path.open("w", encoding="utf-8", newline="\n") as out:
        for pst_index, pst_path in enumerate(pst_paths, start=1):
            store = None
            try:
                store = open_pst(namespace, pst_path)
                root = store.GetRootFolder()
                summary["pst_files_processed"] += 1
                for folder in iter_folders(root):
                    folder_label = folder_path(folder)
                    summary["folders_scanned"] += 1
                    try:
                        items = folder.Items
                        count = int(items.Count)
                    except Exception:
                        continue
                    for item_index in range(1, count + 1):
                        if limit and summary["records_written"] >= limit:
                            return summary
                        summary["items_seen"] += 1
                        try:
                            item = items.Item(item_index)
                            if int(safe_get(item, "Class", 0) or 0) != OL_MAIL_ITEM:
                                summary["non_mail_items_skipped"] += 1
                                continue
                            record = message_record(item, pst_path, source_dir, folder_label, item_index)
                            if record["body_length"]:
                                summary["body_text_records"] += 1
                                summary["total_body_chars"] += record["body_length"]
                            out.write(json.dumps(record, ensure_ascii=False) + "\n")
                            summary["records_written"] += 1
                        except Exception as exc:
                            summary["parse_errors"] += 1
                            if len(summary["errors"]) < 50:
                                summary["errors"].append({
                                    "pst": str(pst_path),
                                    "folder": folder_label,
                                    "item_index": item_index,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                })
                print(f"processed {pst_index}/{len(pst_paths)} PST files: {pst_path.name}", file=sys.stderr)
            except Exception as exc:
                summary["parse_errors"] += 1
                if len(summary["errors"]) < 50:
                    summary["errors"].append({
                        "pst": str(pst_path),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
            finally:
                if store is not None:
                    close_pst(namespace, store)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--limit", type=int, default=None, help="Optional max mail records for a smoke run.")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"source folder not found: {args.source}", file=sys.stderr)
        return 2

    summary = normalize_psts(args.source, args.output, args.limit)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["records_written"] else 1


if __name__ == "__main__":
    raise SystemExit(main())