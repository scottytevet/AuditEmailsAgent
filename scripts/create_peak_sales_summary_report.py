#!/usr/bin/env python3
"""Create local markdown summaries from normalized Peak Sales emails."""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "AI-Outputs" / "normalized_emails.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "AI-Outputs" / "peak_sales_recruiting_summary.md"
DEFAULT_EVIDENCE = PROJECT_ROOT / "AI-Outputs" / "peak_sales_evidence_index.csv"
DEFAULT_EXECUTIVE = PROJECT_ROOT / "AI-Outputs" / "peak_sales_executive_recap.md"

WS_RE = re.compile(r"\s+")
PREFIX_RE = re.compile(r"^\s*((re|fw|fwd)\s*[:_\-]\s*)+", re.I)
EXT_RE = re.compile(r"^\s*(\[[^\]]*ext[^\]]*\]\s*)+", re.I)


TOPICS = [
    {
        "name": "VP Sales search",
        "terms": [
            "vp sales",
            "vp of sales",
            "rob dean",
            "jody kemp",
            "carla caldwell",
            "charn pram",
        ],
    },
    {
        "name": "ISR search",
        "terms": [
            "isr",
            "inside sales",
            "janna shepard",
            "preston mahler",
            "connor briggs",
            "benjamin boyd",
            "bonnie hadley",
            "kathy schuchardy",
            "beth terranova",
        ],
    },
    {
        "name": "Account Manager search",
        "terms": [
            "account manager",
            "am candidate",
            "ryan hofmockel",
            "jerry prucha",
            "evan harris",
            "cindy sorensen",
            "craig erwin",
            "greg young",
        ],
    },
    {
        "name": "Invoices and payment dispute",
        "terms": [
            "invoice",
            "past due",
            "wire payment",
            "1-6631",
            "1-6680",
            "1-6331",
            "1-6374",
        ],
    },
    {
        "name": "Performance dispute / escalation",
        "terms": [
            "peak sales performance",
            "failed isr hiring cycle",
            "nonperformance",
            "preserves all documents",
            "reviewing it with counsel",
            "candidate disclosure",
        ],
    },
]


CANDIDATES = [
    {
        "name": "Rob Dean",
        "role": "VP Sales",
        "status": "Hired/placed. Later tied to invoice 1-6631 for a $28,000 placement fee.",
        "terms": ["rob dean", "invoice 1-6631", "vp of sales"],
    },
    {
        "name": "Charn Pram",
        "role": "VP Sales",
        "status": "Submitted and scheduled as a VP Sales candidate; final outcome is not clear in the MSG export.",
        "terms": ["charn pram"],
    },
    {
        "name": "Carla Caldwell",
        "role": "VP Sales / later Director of Sales possibility",
        "status": "Moved through VP Sales discussions. March email says Peak closed out with her, and she remained interested in a Director of Sales possibility.",
        "terms": ["carla caldwell"],
    },
    {
        "name": "Jody Kemp",
        "role": "VP Sales",
        "status": "Targeted and actively followed up; later emails show she was still checking status. Final outcome is not clear.",
        "terms": ["jody kemp", "jodi kemp"],
    },
    {
        "name": "Ryan Hofmockel",
        "role": "Account Manager",
        "status": "Interviewed and appears to have reached offer/acceptance. Internal email describes him as the last no-charge placement.",
        "terms": ["ryan hofmockel"],
    },
    {
        "name": "Greg Young",
        "role": "Account Manager",
        "status": "Interviewed, then TEVET asked Peak to close him out/release him from consideration.",
        "terms": ["greg young"],
    },
    {
        "name": "Evan Harris",
        "role": "DOE Account Manager",
        "status": "TEVET referral/contact; interview scheduling appears in February. Final outcome is not clear.",
        "terms": ["evan harris"],
    },
    {
        "name": "Jerry Prucha",
        "role": "DOE Account Manager",
        "status": "Submitted by Peak for DOE Account Manager; TEVET raised location/relocation fit questions.",
        "terms": ["jerry prucha"],
    },
    {
        "name": "Craig Erwin",
        "role": "DOE Account Manager",
        "status": "Submitted by Peak for DOE Account Manager; final outcome is not clear.",
        "terms": ["craig erwin"],
    },
    {
        "name": "Cindy Sorensen",
        "role": "Account Manager",
        "status": "Interview scheduling appears; final outcome is not clear.",
        "terms": ["cindy sorensen"],
    },
    {
        "name": "Janna Shepard",
        "role": "ISR",
        "status": "Placed/hired, then later described by Peak as already replaced.",
        "terms": ["janna shepard"],
    },
    {
        "name": "Preston Mahler",
        "role": "ISR",
        "status": "Listed by Peak as still employed at TEVET as of the March replacement discussion.",
        "terms": ["preston mahler"],
    },
    {
        "name": "Connor Briggs",
        "role": "ISR",
        "status": "Listed by Peak as still employed at TEVET as of the March replacement discussion.",
        "terms": ["connor briggs"],
    },
    {
        "name": "Benjamin Boyd",
        "role": "ISR",
        "status": "Listed by Peak as an active replacement-search obligation; next ISR placement would not create a new invoice.",
        "terms": ["benjamin boyd", "ben boyd"],
    },
    {
        "name": "Bonnie Hadley",
        "role": "ISR",
        "status": "Advanced in interviews but not selected; email cites salary expectations as the gap.",
        "terms": ["bonnie hadley"],
    },
    {
        "name": "Beth Terranova",
        "role": "ISR",
        "status": "Interviewed in the ISR process; final outcome is not clear.",
        "terms": ["beth terranova"],
    },
    {
        "name": "Kathy Schuchardy",
        "role": "ISR",
        "status": "Interviewed in the ISR process; final outcome is not clear.",
        "terms": ["kathy schuchardy", "kathy schuchardt"],
    },
    {
        "name": "Amanda Mullenax",
        "role": "VP Sales",
        "status": "Submitted/scheduled as a VP Sales candidate; final outcome is not clear.",
        "terms": ["amanda mullenax"],
    },
    {
        "name": "Harley",
        "role": "VP Sales",
        "status": "Referenced in VP Sales candidate follow-up; final outcome is not clear.",
        "terms": ["harley"],
    },
]


FINDINGS = [
    {
        "finding": "Peak was initially favored for ISR recruiting because of perceived cost/value and behavioral analysis depth.",
        "terms": ["prefer peak sales"],
    },
    {
        "finding": "A VP Sales engagement agreement was routed/signed around October 2025.",
        "terms": ["signed this agreement", "peak sales agreement vp sales"],
    },
    {
        "finding": "The early ISR pipeline moved through multi-round interviews for Janna Shepard, Beth Terranova, Bonnie Hadley, and Kathy Schuchardy.",
        "terms": ["isr interview changes", "janna shepard", "beth terranova", "bonnie hadley", "kathy schuchardy"],
    },
    {
        "finding": "Bonnie Hadley appears to have been declined largely because salary expectations were too far apart.",
        "terms": ["salary expectations ultimately created too wide"],
    },
    {
        "finding": "Peak later acknowledged ISR replacement obligations: Janna was already replaced, Preston Mahler and Connor Briggs were still employed, and Benjamin Boyd still required a replacement.",
        "terms": ["next isr we place will not have a new invoice"],
    },
    {
        "finding": "Rob Dean was treated as a completed VP Sales placement, with invoice 1-6631 for $28,000 becoming the main payment dispute.",
        "terms": ["rob dean as your vp of sales", "invoice 1-6631"],
    },
    {
        "finding": "Ryan Hofmockel appears to have been the last/no-charge Account Manager placement under the dispute strategy.",
        "terms": ["ryan hofmockel is the last placement"],
    },
    {
        "finding": "TEVET asked Peak to release Greg Young from Account Manager consideration.",
        "terms": ["release greg young"],
    },
    {
        "finding": "TEVET's May performance letter centered on the ISR portion of the engagement and requested credit/refund treatment, cost recognition, and a hold on late charges for invoice 1-6631.",
        "terms": ["failed isr hiring cycle", "not willing to extend"],
    },
    {
        "finding": "Peak responded that it was reviewing the matter with counsel and disagreed with TEVET's characterization.",
        "terms": ["reviewing it with counsel"],
    },
    {
        "finding": "TEVET requested preservation of documents and candidate/account handling records related to the engagement.",
        "terms": ["preserves all documents", "candidate disclosure", "handling of this engagement"],
    },
]


def clean_text(value: str | None) -> str:
    return WS_RE.sub(" ", value or "").strip()


def clean_subject(value: str | None) -> str:
    value = clean_text(value)
    while True:
        new_value = PREFIX_RE.sub("", value)
        new_value = EXT_RE.sub("", new_value).strip()
        if new_value == value:
            break
        value = new_value
    return value


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


def date_only(value: str | None) -> str:
    parsed = parse_time(value)
    return parsed.date().isoformat() if parsed else ""


def month_key(value: str | None) -> str:
    parsed = parse_time(value)
    return parsed.strftime("%Y-%m") if parsed else "unknown"


def load_records(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        key = (record.get("internet_message_id") or "").lower()
        if not key:
            key = "|".join(
                [
                    record.get("body_sha256") or "",
                    (record.get("subject") or "").lower(),
                    record.get("sent_at") or "",
                    record.get("sender") or "",
                ]
            )
        existing = by_key.get(key)
        if not existing or (record.get("body_length") or 0) > (existing.get("body_length") or 0):
            by_key[key] = record
    return list(by_key.values())


def record_text(record: dict[str, Any]) -> str:
    return " ".join(
        [
            record.get("subject") or "",
            record.get("normalized_subject") or "",
            record.get("body_text") or "",
        ]
    ).lower()


def matching_records(records: list[dict[str, Any]], terms: list[str]) -> list[dict[str, Any]]:
    lowered_terms = [term.lower() for term in terms]
    return [
        record
        for record in records
        if any(term in record_text(record) for term in lowered_terms)
    ]


def source_ref(record: dict[str, Any]) -> str:
    src = record.get("source") or {}
    rel = src.get("relative_path") or src.get("path") or ""
    subject = clean_subject(record.get("subject")) or "(no subject)"
    date = date_only(record.get("sent_at"))
    sender = record.get("sender") or "unknown sender"
    return f"{date} | {sender} | {subject} | {rel}"


def best_evidence(records: list[dict[str, Any]], terms: list[str], limit: int = 3) -> list[dict[str, Any]]:
    hits = matching_records(records, terms)
    hits.sort(key=lambda r: (r.get("sent_at") or "", r.get("source", {}).get("relative_path") or ""))
    seen = set()
    selected = []
    for record in hits:
        key = (
            record.get("internet_message_id")
            or record.get("body_sha256")
            or record.get("source", {}).get("relative_path")
        )
        if key in seen:
            continue
        seen.add(key)
        selected.append(record)
        if len(selected) >= limit:
            break
    return selected


def hit_count(records: list[dict[str, Any]], terms: list[str]) -> int:
    return len(matching_records(records, terms))


def date_range(records: list[dict[str, Any]]) -> str:
    dates = [parse_time(record.get("sent_at")) for record in records]
    dates = [value for value in dates if value]
    if not dates:
        return ""
    return f"{min(dates).date().isoformat()} to {max(dates).date().isoformat()}"


def top_subjects(records: list[dict[str, Any]], limit: int = 12) -> list[tuple[str, int]]:
    counts = collections.Counter(
        clean_subject(record.get("subject") or record.get("normalized_subject"))
        for record in records
    )
    return [(subject, count) for subject, count in counts.most_common(limit) if subject]


def top_senders(records: list[dict[str, Any]], limit: int = 12) -> list[tuple[str, int]]:
    counts = collections.Counter(record.get("sender") or "unknown" for record in records)
    return counts.most_common(limit)


def top_domains(records: list[dict[str, Any]], limit: int = 12) -> list[tuple[str, int]]:
    counts: collections.Counter[str] = collections.Counter()
    for record in records:
        for participant in record.get("participants") or []:
            if "@" in participant:
                counts[participant.split("@")[-1].lower()] += 1
    return counts.most_common(limit)


def monthly_counts(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts = collections.Counter(month_key(record.get("sent_at")) for record in records)
    return sorted(counts.items())


def make_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe_row = [cell.replace("|", "/") for cell in row]
        lines.append("| " + " | ".join(safe_row) + " |")
    return lines


def build_candidate_rows(records: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for candidate in CANDIDATES:
        hits = matching_records(records, candidate["terms"])
        if not hits:
            continue
        refs = best_evidence(records, candidate["terms"], 2)
        ref_text = "; ".join(source_ref(ref) for ref in refs)
        rows.append(
            [
                candidate["name"],
                candidate["role"],
                str(len(hits)),
                date_range(hits),
                candidate["status"],
                ref_text,
            ]
        )
    return rows


def build_evidence_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for finding in FINDINGS:
        for record in best_evidence(records, finding["terms"], 5):
            rows.append(
                {
                    "finding": finding["finding"],
                    "date": date_only(record.get("sent_at")),
                    "sender": record.get("sender") or "",
                    "subject": clean_subject(record.get("subject")),
                    "relative_path": record.get("source", {}).get("relative_path") or "",
                    "internet_message_id": record.get("internet_message_id") or "",
                }
            )
    return rows


def write_evidence_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "finding",
        "date",
        "sender",
        "subject",
        "relative_path",
        "internet_message_id",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_report(records: list[dict[str, Any]], unique_records: list[dict[str, Any]], evidence_path: Path) -> str:
    body_records = [record for record in records if record.get("body_length")]
    total_body_chars = sum(record.get("body_length") or 0 for record in records)
    first_last = date_range(records)

    topic_rows = []
    for topic in TOPICS:
        hits = matching_records(unique_records, topic["terms"])
        topic_rows.append(
            [
                topic["name"],
                str(len(hits)),
                date_range(hits),
                "; ".join(f"{subject} ({count})" for subject, count in top_subjects(hits, 5)),
            ]
        )

    finding_rows = []
    for finding in FINDINGS:
        refs = best_evidence(unique_records, finding["terms"], 2)
        finding_rows.append(
            [
                finding["finding"],
                "; ".join(source_ref(ref) for ref in refs),
            ]
        )

    lines: list[str] = []
    lines.append("# Peak Sales Recruiting Email Summary")
    lines.append("")
    lines.append(f"Generated from `{DEFAULT_INPUT}`.")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(
        "The normalized MSG export shows a recruiting engagement that began with interest in Peak Sales for ISR recruiting, expanded into VP Sales and Account Manager searches, produced at least one completed VP Sales placement, and later escalated into a performance and payment dispute."
    )
    lines.append("")
    lines.append(
        "The clearest business arc is: initial vendor preference and agreement routing; ISR sourcing and interviews in September/October 2025; VP Sales and Account Manager candidate activity through late 2025 and early 2026; internal concern about replacement obligations and cost per hire in March/April 2026; then a May/June 2026 dispute over Peak's performance, ISR replacement obligations, invoice 1-6631, and preservation of related records."
    )
    lines.append("")
    lines.append("## Dataset Coverage")
    lines.append("")
    lines.extend(
        make_table(
            ["Metric", "Value"],
            [
                ["Parsed MSG records", str(len(records))],
                ["Unique messages after de-duplication", str(len(unique_records))],
                ["Records with body text", str(len(body_records))],
                ["Extracted body text characters", f"{total_body_chars:,}"],
                ["Detected email date range", first_last],
                ["Evidence index", str(evidence_path)],
            ],
        )
    )
    lines.append("")
    lines.append("## Main Themes")
    lines.append("")
    lines.extend(make_table(["Theme", "Unique message hits", "Date range", "Common subject clusters"], topic_rows))
    lines.append("")
    lines.append("## Timeline")
    lines.append("")
    lines.append("- August/September 2025: TEVET compared ISR recruiting vendors and expressed a preference for Peak Sales if moving forward.")
    lines.append("- September/October 2025: ISR candidate sourcing and interview scheduling accelerated, including Janna Shepard, Beth Terranova, Bonnie Hadley, and Kathy Schuchardy.")
    lines.append("- October 2025: VP Sales agreement routing appears in the archive.")
    lines.append("- November/December 2025: VP Sales and Account Manager candidate submissions became active, including target-company lists and candidates such as Carla Caldwell, Jody Kemp, Amanda Mullenax, and Ben/Benjamin Boyd.")
    lines.append("- January/February 2026: VP Sales and AM work continued. Rob Dean and Charn Pram were submitted for VP Sales; Ryan Hofmockel, Greg Young, Evan Harris, Jerry Prucha, and Craig Erwin appear in Account Manager/DOE AM activity.")
    lines.append("- March/April 2026: Internal emails discuss replacement obligations, cost-per-hire concerns, Ryan Hofmockel as a no-charge placement, and Rob Dean's placement/invoice.")
    lines.append("- May/June 2026: The matter escalated into a written performance/payment dispute. TEVET's correspondence focused on the ISR engagement, replacement/credit requests, invoice 1-6631, late charges, and document preservation. Peak replied that it was reviewing with counsel and disagreed with TEVET's characterization.")
    lines.append("")
    lines.append("## Candidate And Placement Summary")
    lines.append("")
    lines.extend(
        make_table(
            ["Name", "Search", "Hits", "Date range", "Observed status", "Example source refs"],
            build_candidate_rows(unique_records),
        )
    )
    lines.append("")
    lines.append("## Key Findings With Source References")
    lines.append("")
    lines.extend(make_table(["Finding", "Example source refs"], finding_rows))
    lines.append("")
    lines.append("## Monthly Volume")
    lines.append("")
    lines.extend(make_table(["Month", "Unique messages"], [[month, str(count)] for month, count in monthly_counts(unique_records)]))
    lines.append("")
    lines.append("## Top Senders")
    lines.append("")
    lines.extend(make_table(["Sender", "Unique messages"], [[sender, str(count)] for sender, count in top_senders(unique_records)]))
    lines.append("")
    lines.append("## Top Participant Domains")
    lines.append("")
    lines.extend(make_table(["Domain", "Participant mentions"], [[domain, str(count)] for domain, count in top_domains(unique_records)]))
    lines.append("")
    lines.append("## Notes And Caveats")
    lines.append("")
    lines.append("- This report is based on the exported MSG files and Purview metadata already normalized into JSONL. PST-only items may add context later.")
    lines.append("- Several candidate outcomes are not explicit in the MSG set. Those are marked as unclear rather than inferred.")
    lines.append("- Calendar invitations and forwarded/duplicated messages are included in source records but de-duplicated for the summary counts.")
    lines.append("- The evidence index CSV gives a cleaner list of source paths for the key findings.")
    lines.append("")
    return "\n".join(lines)


def build_executive_recap(records: list[dict[str, Any]], unique_records: list[dict[str, Any]], full_report: Path) -> str:
    lines = [
        "# Peak Sales Recruiting Executive Recap",
        "",
        "## Short Version",
        "",
        "The email archive shows TEVET's Peak Sales Recruiting engagement moving from vendor selection and active recruiting into a later performance/payment dispute. Peak appears to have been initially preferred for ISR recruiting, then became involved in VP Sales and Account Manager searches. The engagement produced at least one clear VP Sales placement, Rob Dean, and likely an Account Manager placement path for Ryan Hofmockel. By spring 2026, internal TEVET emails were focused on replacement obligations, cost per hire, and whether to continue with Peak. By May/June 2026, the relationship had escalated into a written dispute over ISR results, invoice 1-6631, late charges, and preservation of engagement records.",
        "",
        "## Most Important Findings",
        "",
        "1. Peak was initially favored for ISR recruiting, with internal notes pointing to cost and behavioral-analysis depth as reasons to consider them.",
        "2. The ISR search became the central problem area. Peak later acknowledged replacement obligations: Janna Shepard had been replaced, Preston Mahler and Connor Briggs were still employed, and Benjamin Boyd still required a replacement.",
        "3. Rob Dean appears to have been a completed VP Sales placement. Peak's collection emails tie him to invoice 1-6631 for $28,000.",
        "4. Ryan Hofmockel appears to have reached the offer/acceptance stage for Account Manager and was discussed internally as a last no-charge placement.",
        "5. Some candidates were explicitly closed out: Greg Young was released from Account Manager consideration, and Bonnie Hadley was not selected after salary expectations created too large a gap.",
        "6. TEVET's May performance letter focused on the failed ISR hiring cycle, requested credit/refund treatment, referenced estimated training/onboarding/management costs, and asked that late charges on invoice 1-6631 be held while the parties worked through the dispute.",
        "7. Peak responded that it was reviewing the matter with counsel and disagreed with TEVET's characterization.",
        "",
        "## Candidate Snapshot",
        "",
        "- VP Sales: Rob Dean was placed; Charn Pram, Carla Caldwell, Jody Kemp, Amanda Mullenax, and Harley appear in candidate activity, with several outcomes unclear.",
        "- Account Manager / DOE AM: Ryan Hofmockel appears to have advanced to offer/acceptance; Greg Young was released; Evan Harris, Jerry Prucha, Craig Erwin, and Cindy Sorensen appear in interview/submission activity with unclear final outcomes.",
        "- ISR: Janna Shepard, Preston Mahler, Connor Briggs, and Benjamin/Ben Boyd are the core placement/replacement names in the later dispute. Bonnie Hadley, Beth Terranova, and Kathy Schuchardy appear in interview rounds.",
        "",
        "## Data Coverage",
        "",
        f"- Parsed MSG records: {len(records):,}",
        f"- Unique messages summarized: {len(unique_records):,}",
        f"- Date range: {date_range(records)}",
        f"- Full evidence-backed report: `{full_report}`",
        "",
        "## Practical Next Moves",
        "",
        "1. Use the evidence CSV to pull source emails for the key findings.",
        "2. If this is for a dispute or negotiation, review PST-only data next to confirm there are no missing emails outside the exported MSG set.",
        "3. Build a final chronology packet around the ISR replacement obligation, Rob Dean invoice 1-6631, Ryan Hofmockel placement, and the May/June performance correspondence.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--executive", type=Path, default=DEFAULT_EXECUTIVE)
    args = parser.parse_args()

    records = load_records(args.input)
    unique_records = dedupe_records(records)
    evidence_rows = build_evidence_rows(unique_records)
    write_evidence_csv(evidence_rows, args.evidence)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        build_report(records, unique_records, args.evidence), encoding="utf-8"
    )
    args.executive.write_text(
        build_executive_recap(records, unique_records, args.report), encoding="utf-8"
    )
    print(json.dumps({
        "input_records": len(records),
        "unique_records": len(unique_records),
        "report": str(args.report),
        "executive_recap": str(args.executive),
        "evidence_index": str(args.evidence),
        "evidence_rows": len(evidence_rows),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
