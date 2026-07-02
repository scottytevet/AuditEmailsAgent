# Peak Sales Recruiting Email Archive Audit

Audit target: `C:\Users\ScottyGomez\Documents\PeakSalesRecruiting`

Audit date: 2026-06-29

## Folder Shape

- Total size: 1.877 GB
- Total files: 1,645
- Total folders: 78
- File types:
  - `.pst`: 31 files, 1,024.24 MB
  - `.msg`: 1,609 files, 890.14 MB
  - `.csv`: 4 files, 6.14 MB
  - `.xlsx`: 1 file, 1.08 MB

## Top-Level Sources

- `00-PeakSalesRecruiting (Purview Dump)`: 31 PST files, 1,024.24 MB
- `PeakSalesRecruiting (Donna Millard)`: 479 MSG files, 251.00 MB
- `PeakSalesRecruiting (Tracy Solomon)`: 431 MSG files, 244.41 MB
- `PeakSalesRecruiting (Jerri Gore)`: 449 MSG files, 129.06 MB
- `PeakSalesRecruiting (Sarah Zamudio)`: 75 MSG files, 134.97 MB
- `PeakSalesRecruiting (John Vandewater)`: 113 MSG files, 96.44 MB
- `PeakSalesRecruiting (Vivian Martin)`: 33 MSG files, 26.19 MB
- `PeakSalesRecruiting (Emilee Askin)`: 29 MSG files, 8.07 MB
- `Reports-PeakSaleRecruiting_com-Peaksalesrecruiting_com-StartDirectExport-PeakSalesRecruiting_MessageDump-2026-06-26_15-36-50`: 5 report files, 7.22 MB

## Purview Report

The Purview `Items_0_2026-06-26_15-36-50.csv` report contains 3,642 item rows.

Useful populated fields include:

- `Sender`
- `To`
- `CC`
- `BCC`
- `Date`
- `Email date sent`
- `Received`
- `Subject/Title`
- `Internet message ID`
- `Immutable ID`
- `Has attachment`
- `File extension`
- `Original path`
- `Target path`
- `Size`
- `Sensitive type`
- `Recipient count`

Detected item date range:

- Earliest email date: 2025-07-28 14:16:04
- Latest email date: 2026-06-01 15:08:46
- Latest modified time in report: 2026-06-25 15:36:34

## Largest PST Files

- `donna.millard@Tevet.com.001.pst`: 358.45 MB
- `jerri.gore@tevet.com.001.pst`: 108.52 MB
- `candace.deuster@Tevet.com.001.pst`: 104.64 MB
- `sarah.zamudio@Tevet.com.001.pst`: 98.83 MB
- `tracy.solomon@tevet.com.001.pst`: 95.68 MB
- `john.vandewater@tevet.com.001.pst`: 80.67 MB
- `security.outbound@Tevet.com.001.pst`: 48.21 MB

## Recommended Next Step

Use the Purview report as the control table, then parse the exported `.msg` files first. The `.msg` folders already cover the most user-readable export set and are smaller/easier to process than PST containers. PST parsing should come second, mainly to fill gaps and validate completeness against the 3,642-row Purview item report.

Suggested pipeline:

1. Build `normalized_emails.jsonl` from `.msg` files plus Purview metadata.
2. De-duplicate by `Internet message ID`, normalized subject, sender, date, and body hash.
3. Group messages into conversations using `Internet message ID`, reply headers when available, and normalized subject fallback.
4. Summarize each conversation with source references.
5. Roll up summaries by person, month, company/contact, topic, open issue, and decision.
6. Produce an executive recap plus a searchable local index.
