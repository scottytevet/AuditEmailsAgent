# Communications Evidence QA MVP

This project now includes a small React + Python MVP for searching and asking questions across Peak Sales communication evidence: exported emails, Teams transcripts, meeting reconciliation rows, placeholder transcript artifacts, and recovery backlog metadata.

## Shape

- `app/email_index.py` builds a SQLite database from `AI-Outputs/normalized_emails.jsonl`, `AI-Outputs/email_threads.jsonl`, and optional `AI-Outputs/graph_source_records.jsonl`.
- `app/server.py` serves the React evidence workbench and JSON API from one Python process.
- `web/` contains the generalized React source workbench.
- The local SQLite database defaults to `%LOCALAPPDATA%\PeakSalesEmailMVP\email_mvp.sqlite`.

The app uses SQLite FTS5 for keyword search and optional Azure AI Foundry embeddings for semantic retrieval. If the model provider is not configured, search still works and the ask flow returns source citations without an AI-written answer.

## Setup

1. Copy `.env.example` to `.env`.
2. Add Azure AI Foundry settings if you want embeddings and generated answers.
3. Run the existing normalization pipeline:

```powershell
python scripts\normalize_peak_sales_emails.py
python scripts\build_peak_sales_thread_manifest.py
```

4. Build the local index:

```powershell
python -m app.email_index rebuild
```

5. Optional: build embeddings:

```powershell
python -m app.email_index embeddings
```

6. Start the MVP:

```powershell
python -m app.server
```

Open `http://127.0.0.1:8765`.


## Authentication

The server includes a backend-owned Microsoft Entra OIDC layer. It uses only identity scopes (`openid profile email`), verifies the Microsoft ID token signature, checks the configured tenant, requires an allowed email domain, and then issues a signed HttpOnly app session cookie. It does not save Microsoft access or refresh tokens and does not need Microsoft Graph permissions.

For production, create a Microsoft Entra app registration in the TEVET tenant, add a Web redirect URI such as `https://your-host/auth/callback`, create a client secret, and set:

```powershell
AUTH_ENABLED=true
AUTH_TENANT_ID=<tevet-tenant-guid>
AUTH_CLIENT_ID=<app-client-id>
AUTH_CLIENT_SECRET=<client-secret-value>
AUTH_REDIRECT_URI=https://your-host/auth/callback
AUTH_ALLOWED_DOMAIN=tevet.com
AUTH_SESSION_SECRET=<long-random-secret>
AUTH_COOKIE_SECURE=true
AUTH_PUBLIC_ORIGIN=https://your-host
```

For local testing, use `http://127.0.0.1:8765/auth/callback` as an additional redirect URI and keep `AUTH_COOKIE_SECURE=false`. When auth is enabled, unauthenticated browser requests redirect to Microsoft sign-in and API requests return `401` with a login URL.
## Azure AI Foundry Provider

The default model provider is Azure AI Foundry. Configure `.env` like this:

```powershell
LLM_PROVIDER=azure_foundry
AZURE_FOUNDRY_ENDPOINT=https://YOUR-RESOURCE-NAME.services.ai.azure.com/openai/v1
AZURE_FOUNDRY_API_KEY=your-key
AZURE_FOUNDRY_ANSWER_MODEL=your-chat-or-responses-deployment
AZURE_FOUNDRY_EMBEDDING_MODEL=text-embedding-3-small
```

If Azure gives you a full endpoint such as `https://<resource>.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview`, you can paste that into `AZURE_FOUNDRY_ENDPOINT`; the app normalizes it before calling `responses` or `embeddings`.

For the classic Foundry model inference route, use a `/models` endpoint and set:

```powershell
AZURE_FOUNDRY_ENDPOINT=https://YOUR-RESOURCE-NAME.services.ai.azure.com/models
AZURE_FOUNDRY_API_STYLE=model_inference
AZURE_FOUNDRY_API_VERSION=2024-05-01-preview
```

The app still supports public OpenAI by setting `LLM_PROVIDER=openai` and `OPENAI_API_KEY`, but Azure Foundry is the intended provider for this project.
## Import Graph Communications Data

The adjacent `C:\Users\ScottyGomez\Projects\GraphCommunicationsAudit` project contains Teams meeting reconciliation output, downloaded/transcribed recordings, placeholder transcript artifacts, and missing-content backlog files. Import those into this app with:

```powershell
python scripts\import_graph_communications.py
python -m app.email_index rebuild
```

The importer writes:

- `AI-Outputs\graph_source_records.jsonl`
- `AI-Outputs\graph_import_summary.json`

Current Graph outputs include high-quality transcript records for Rob Dean, Ryan Hofmockel, and Evan Harris, plus metadata-only meeting status and missing transcript records. When more transcript data is recovered later, rerun the importer and rebuild the index.

## Import PST Mailbox Exports

PST ingestion is Windows/Outlook based. Install the extra parser dependencies into the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-pst.txt
```

Then normalize PST records, merge them with the MSG export records, rebuild the thread manifest, and refresh the local index:

```powershell
.\.venv\Scripts\python.exe scripts\normalize_peak_sales_psts.py
.\.venv\Scripts\python.exe scripts\normalize_peak_sales_emails.py --output AI-Outputs\normalized_emails_msg_only.jsonl --summary AI-Outputs\normalized_emails_msg_only_summary.json
.\.venv\Scripts\python.exe scripts\merge_normalized_email_sources.py --msg-input AI-Outputs\normalized_emails_msg_only.jsonl --pst-input AI-Outputs\normalized_pst_emails.jsonl --replace-main
.\.venv\Scripts\python.exe scripts\build_peak_sales_thread_manifest.py
.\.venv\Scripts\python.exe -m app.email_index rebuild
```

The PST normalizer scans `C:\Users\ScottyGomez\Documents\PeakSalesRecruiting` by default and labels records dynamically as `email_pst` / `PST mailbox export`. Embeddings can be rebuilt afterward with `python -m app.email_index embeddings`, but that sends PST-inclusive source text to the configured model provider.

## Source Catalog

Source labels and filter options are dynamic. The backend builds `source_catalog` from indexed records and writes a snapshot to `AI-Outputs\sources.json` during rebuilds. Production importers should set `parse.source_kind` and, when available, `parse.source_system`, `parse.source_project`, or `parse.source_label` on each record.

You can override labels/order without editing React by updating `AI-Outputs\sources.json` or setting `SOURCE_CATALOG_PATH` to another catalog file. The UI title/subtitle come from `APP_TITLE` and `APP_SUBTITLE`.
## Answer Flow

The ask endpoint defaults to `use_all_sources: true`. In that mode it ignores source-type, quality, and attachment filters so answers can pull from emails, transcript records, transcript-missing metadata, and meeting reconciliation rows. Person/date filters can still scope the question.

When Azure AI Foundry is configured, the answer flow does three passes:

1. Retrieve initial evidence with keyword search and embeddings when available.
2. Generate a concise draft, then search again using significant terms from that draft to catch related source records.
3. Generate the final concise answer and run a lightweight fidelity review that checks answer claims against retrieved source text with fuzzy term overlap.

The API response includes `review.status`, `review.score`, `review.source_mix`, and weak claim snippets when the review finds unsupported-looking text. Without a configured model provider, the endpoint stays retrieval-only and still returns citations plus the source mix.
## API

- `GET /api/me`
- `GET /api/status`
- `GET /api/search?q=invoice+1-6631`
- `GET /api/sources/{id}`
- `GET /api/emails/{id}` legacy alias
- `GET /api/threads/{thread_id}`
- `POST /api/rebuild`
- `POST /api/embeddings/build`
- `POST /api/ask`

## Microsoft Graph Note

The current MVP reads from the exported `.msg` / Purview pipeline and imports already-gathered Teams/Graph audit outputs from the adjacent `GraphCommunicationsAudit` project. It does not pull live mail from Graph. The permissions shown in the screenshot do not include Microsoft Graph mail permissions such as `Mail.Read`, so live mailbox ingestion would need an additional application permission and admin consent before this app could pull mail directly from Graph.




