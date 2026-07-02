#!/usr/bin/env python3
"""Small stdlib HTTP server for the communications evidence QA MVP."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import email_index
from .auth import AuthManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"
email_index.load_env()
DB_PATH = Path(os.environ.get("EMAIL_MVP_DB", email_index.DEFAULT_DB))
AUTH = AuthManager()


class Handler(BaseHTTPRequestHandler):
    server_version = "CommunicationsEvidenceMVP/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if AUTH.handle_auth_route(self, parsed):
            return
        if parsed.path == "/api/me":
            self.api(lambda: AUTH.me_payload(self))
            return
        if not AUTH.authorize_request(self, parsed.path, "GET"):
            return
        if parsed.path == "/api/status":
            self.api(self.status_payload)
            return
        if parsed.path == "/api/search":
            self.api(lambda: self.search_payload(parse_qs(parsed.query)))
            return
        if parsed.path.startswith("/api/sources/") or parsed.path.startswith("/api/emails/"):
            source_id = int(parsed.path.rsplit("/", 1)[-1])
            self.api(lambda: self.with_conn(lambda conn: email_index.get_source(conn, source_id)))
            return
        if parsed.path.startswith("/api/threads/"):
            thread_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.api(lambda: self.with_conn(lambda conn: email_index.get_thread(conn, thread_id)))
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not AUTH.authorize_request(self, parsed.path, "POST"):
            return
        if parsed.path == "/api/rebuild":
            self.api(self.rebuild_payload)
            return
        if parsed.path == "/api/embeddings/build":
            self.api(self.embeddings_payload)
            return
        if parsed.path == "/api/ask":
            body = self.read_json()
            self.api(lambda: self.ask_payload(body))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def api(self, callback) -> None:
        try:
            payload = callback()
            self.write_json(payload)
        except email_index.AppError as exc:
            self.write_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self.write_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def with_conn(self, callback):
        conn = email_index.connect(DB_PATH)
        try:
            email_index.create_schema(conn)
            return callback(conn)
        finally:
            conn.close()

    def status_payload(self) -> dict:
        return self.with_conn(lambda conn: email_index.database_status(conn, DB_PATH))

    def search_payload(self, params: dict[str, list[str]]) -> dict:
        query = first(params, "q")
        filters = {
            "sender": first(params, "sender"),
            "participant": first(params, "participant"),
            "date_from": first(params, "date_from"),
            "date_to": first(params, "date_to"),
            "has_attachment": first(params, "has_attachment"),
            "source_type": first(params, "source_type"),
            "quality": first(params, "quality"),
        }
        limit = int(first(params, "limit") or "25")
        offset = int(first(params, "offset") or "0")
        return self.with_conn(
            lambda conn: email_index.search_emails(conn, query, filters, limit, offset)
        )

    def rebuild_payload(self) -> dict:
        body = self.read_json()
        return email_index.rebuild_database(
            db_path=DB_PATH,
            emails_path=Path(body.get("emails") or email_index.DEFAULT_EMAILS),
            threads_path=Path(body.get("threads") or email_index.DEFAULT_THREADS),
            graph_records_path=Path(body.get("graph_records") or email_index.DEFAULT_GRAPH_RECORDS),
            peak_new_docs_path=Path(body.get("peak_new_docs") or email_index.DEFAULT_PEAK_NEW_DOCS),
            with_embeddings=bool(body.get("with_embeddings")),
        )

    def embeddings_payload(self) -> dict:
        body = self.read_json()
        return self.with_conn(
            lambda conn: email_index.build_missing_embeddings(
                conn,
                model=body.get("model") or email_index.current_embedding_model(),
                limit=body.get("limit"),
            )
        )

    def ask_payload(self, body: dict) -> dict:
        question = body.get("question") or ""
        filters = body.get("filters") or {}
        limit = int(body.get("limit") or 10)
        use_all_sources = body.get("use_all_sources", True)
        review = body.get("review", True)
        if isinstance(use_all_sources, str):
            use_all_sources = use_all_sources.lower() not in ("0", "false", "no", "off")
        if isinstance(review, str):
            review = review.lower() not in ("0", "false", "no", "off")
        return self.with_conn(
            lambda conn: email_index.answer_question(
                conn,
                question,
                filters,
                limit,
                use_all_sources=bool(use_all_sources),
                review=bool(review),
            )
        )

    def write_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, raw_path: str) -> None:
        rel = "index.html" if raw_path in ("", "/") else raw_path.lstrip("/")
        target = (WEB_ROOT / rel).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or not target.is_file():
            target = WEB_ROOT / "index.html"
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def first(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or [""]
    return values[0]


def main() -> int:
    email_index.load_env()
    host = os.environ.get("EMAIL_MVP_HOST", "127.0.0.1")
    port = int(os.environ.get("EMAIL_MVP_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Communications evidence QA running at http://{host}:{port}")
    print(f"Database: {DB_PATH}")
    print(f"Authentication: {'enabled' if AUTH.enabled() else 'disabled'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

