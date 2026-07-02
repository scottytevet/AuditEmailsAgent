"""Microsoft Entra ID authentication helpers for the evidence QA server."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from urllib import error, parse, request

EMAIL_CLAIMS = ("preferred_username", "email", "emails", "upn", "unique_name")
TENANT_CLAIMS = ("tid", "tenantid", "http://schemas.microsoft.com/identity/claims/tenantid")
OBJECT_ID_CLAIMS = ("oid", "sub", "http://schemas.microsoft.com/identity/claims/objectidentifier")
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class AuthError(Exception):
    """Raised when authentication or authorization fails."""


class AuthConfigError(AuthError):
    """Raised when auth is enabled but required configuration is missing."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def first_claim(claims: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = claims.get(name)
        if isinstance(value, list):
            value = next((item for item in value if item), "")
        if value:
            return str(value).strip()
    return ""


def looks_like_guid(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 5 and all(parts) and all(all(ch in "0123456789abcdefABCDEF" for ch in part) for part in parts)


def safe_return_path(value: str) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    parsed = parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


class AuthManager:
    def __init__(self) -> None:
        self._metadata: dict | None = None
        self._metadata_at = 0.0
        self._jwks: dict | None = None
        self._jwks_at = 0.0

    def enabled(self) -> bool:
        return env_bool("AUTH_ENABLED", False)

    def me_payload(self, handler) -> dict:
        if not self.enabled():
            return {"auth_enabled": False, "authenticated": True, "user": None}
        try:
            user = self.current_user(handler.headers)
        except AuthConfigError as exc:
            return {"auth_enabled": True, "authenticated": False, "user": None, "configuration_error": str(exc)}
        return {"auth_enabled": True, "authenticated": user is not None, "user": user}

    def handle_auth_route(self, handler, parsed) -> bool:
        path = parsed.path.rstrip("/") or "/"
        if not (path == "/auth" or path.startswith("/auth/")):
            return False
        if path in {"/auth", "/auth/login"}:
            self.start_login(handler, parsed)
            return True
        if path == "/auth/callback":
            self.finish_login(handler, parsed)
            return True
        if path == "/auth/logout":
            self.logout(handler)
            return True
        if path == "/auth/access-denied":
            reason = parse.parse_qs(parsed.query).get("reason", ["Access denied."])[0]
            self.write_html(handler, "Access denied", f"<p>{html_escape(reason)}</p>", HTTPStatus.FORBIDDEN)
            return True
        self.write_html(handler, "Not found", "<p>Authentication route not found.</p>", HTTPStatus.NOT_FOUND)
        return True

    def authorize_request(self, handler, path: str, method: str) -> bool:
        if not self.enabled():
            return True
        try:
            user = self.current_user(handler.headers)
        except AuthConfigError as exc:
            self.auth_config_response(handler, path, str(exc))
            return False
        if not user:
            self.unauthenticated_response(handler, path)
            return False
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and not self.same_origin_request(handler):
            self.forbidden_response(handler, path, "Request origin is not allowed.")
            return False
        handler.auth_user = user
        return True

    def current_user(self, headers) -> dict | None:
        cookie_value = self.cookie_value(headers, self.cookie_name())
        if not cookie_value:
            return None
        payload = self.unsign(cookie_value, "session")
        if not payload:
            return None
        return {
            "email": payload.get("email", ""),
            "name": payload.get("name", ""),
            "tenant_id": payload.get("tenant_id", ""),
            "object_id": payload.get("object_id", ""),
        }
    def start_login(self, handler, parsed) -> None:
        try:
            self.require_config()
        except AuthConfigError as exc:
            self.auth_config_response(handler, parsed.path, str(exc))
            return
        query = parse.parse_qs(parsed.query)
        return_to = safe_return_path(query.get("return", ["/"])[0])
        now = int(time.time())
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        state_cookie = self.sign(
            {
                "state": state,
                "nonce": nonce,
                "code_verifier": code_verifier,
                "return_to": return_to,
                "iat": now,
                "exp": now + 600,
            },
            "login_state",
        )
        params = {
            "client_id": self.client_id(),
            "response_type": "code",
            "redirect_uri": self.redirect_uri(handler),
            "response_mode": "query",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        location = f"{self.authority_base()}/oauth2/v2.0/authorize?{parse.urlencode(params)}"
        self.redirect(handler, location, [self.cookie_header(self.state_cookie_name(), state_cookie, 600, handler)])

    def finish_login(self, handler, parsed) -> None:
        try:
            self.require_config()
            query = parse.parse_qs(parsed.query)
            if query.get("error"):
                reason = query.get("error_description", query.get("error", ["Sign-in failed."]))[0]
                self.write_html(handler, "Sign-in failed", f"<p>{html_escape(reason)}</p>", HTTPStatus.UNAUTHORIZED)
                return
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            state_payload = self.unsign(self.cookie_value(handler.headers, self.state_cookie_name()), "login_state")
            if not code or not state or not state_payload or not hmac.compare_digest(state, state_payload.get("state", "")):
                raise AuthError("The sign-in response did not match the login request. Please try again.")
            token_response = self.exchange_code(code, state_payload.get("code_verifier", ""), handler)
            claims = self.verify_id_token(token_response.get("id_token", ""), state_payload.get("nonce", ""))
            user = self.user_from_claims(claims)
            now = int(time.time())
            session = self.sign(
                {
                    "email": user["email"],
                    "name": user.get("name", ""),
                    "tenant_id": user.get("tenant_id", ""),
                    "object_id": user.get("object_id", ""),
                    "iat": now,
                    "exp": now + self.session_seconds(),
                },
                "session",
            )
            cookies = [
                self.cookie_header(self.cookie_name(), session, self.session_seconds(), handler),
                self.clear_cookie_header(self.state_cookie_name(), handler),
            ]
            self.redirect(handler, safe_return_path(state_payload.get("return_to", "/")), cookies)
        except AuthConfigError as exc:
            self.auth_config_response(handler, parsed.path, str(exc))
        except AuthError as exc:
            self.redirect(
                handler,
                f"/auth/access-denied?{parse.urlencode({'reason': str(exc)})}",
                [self.clear_cookie_header(self.state_cookie_name(), handler)],
            )

    def logout(self, handler) -> None:
        cookies = [self.clear_cookie_header(self.cookie_name(), handler), self.clear_cookie_header(self.state_cookie_name(), handler)]
        self.redirect(handler, os.environ.get("AUTH_LOGOUT_REDIRECT", "/"), cookies)

    def exchange_code(self, code: str, code_verifier: str, handler) -> dict:
        form = parse.urlencode(
            {
                "client_id": self.client_id(),
                "client_secret": self.client_secret(),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(handler),
                "code_verifier": code_verifier,
            }
        ).encode("utf-8")
        token_request = request.Request(
            f"{self.authority_base()}/oauth2/v2.0/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(token_request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read(1200).decode("utf-8", errors="replace")
            raise AuthError(f"Microsoft sign-in token exchange failed ({exc.code}): {body}") from exc
        except error.URLError as exc:
            raise AuthError(f"Microsoft sign-in token exchange failed: {exc.reason}") from exc
    def verify_id_token(self, token: str, nonce: str) -> dict:
        if not token:
            raise AuthError("Microsoft sign-in did not return an ID token.")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("Microsoft sign-in returned an invalid ID token.")
        try:
            header = json.loads(b64url_decode(parts[0]).decode("utf-8"))
            claims = json.loads(b64url_decode(parts[1]).decode("utf-8"))
            signature = b64url_decode(parts[2])
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthError("Microsoft sign-in returned an unreadable ID token.") from exc
        if header.get("alg") != "RS256":
            raise AuthError("Microsoft sign-in used an unsupported token algorithm.")
        jwk = self.find_jwk(header.get("kid", ""))
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        if not verify_rs256(signing_input, signature, jwk):
            raise AuthError("Microsoft sign-in token signature could not be verified.")
        now = int(time.time())
        if int(claims.get("exp", 0)) < now - 60:
            raise AuthError("Microsoft sign-in token is expired.")
        if int(claims.get("nbf", 0)) > now + 300:
            raise AuthError("Microsoft sign-in token is not active yet.")
        aud = claims.get("aud")
        audience_ok = self.client_id() in aud if isinstance(aud, list) else aud == self.client_id()
        if not audience_ok:
            raise AuthError("Microsoft sign-in token was issued for a different application.")
        if nonce and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise AuthError("Microsoft sign-in nonce did not match the login request.")
        issuer = str(claims.get("iss", "")).rstrip("/")
        expected_issuer = str(self.openid_metadata().get("issuer", "")).rstrip("/")
        if expected_issuer and issuer != expected_issuer:
            raise AuthError("Microsoft sign-in token issuer does not match the configured tenant.")
        tenant_id = self.tenant_id()
        token_tenant = str(claims.get("tid", ""))
        if looks_like_guid(tenant_id) and not hmac.compare_digest(token_tenant.lower(), tenant_id.lower()):
            raise AuthError("The signed-in user is not from the configured organization tenant.")
        return claims

    def user_from_claims(self, claims: dict) -> dict:
        email = first_claim(claims, EMAIL_CLAIMS).lower()
        tenant_id = first_claim(claims, TENANT_CLAIMS)
        object_id = first_claim(claims, OBJECT_ID_CLAIMS)
        if not email or "@" not in email:
            raise AuthError("The signed-in user does not have a verifiable email or UPN.")
        domain = email.rsplit("@", 1)[-1]
        if domain not in self.allowed_domains():
            raise AuthError("The signed-in user is not in an allowed email domain.")
        return {"email": email, "name": str(claims.get("name") or email), "tenant_id": tenant_id, "object_id": object_id}

    def find_jwk(self, kid: str) -> dict:
        for force in (False, True):
            keys = self.jwks(force=force).get("keys", [])
            for key in keys:
                if key.get("kid") == kid and key.get("kty") == "RSA":
                    return key
        raise AuthError("Microsoft signing key was not found for this token.")

    def openid_metadata(self) -> dict:
        if self._metadata and time.time() - self._metadata_at < 86400:
            return self._metadata
        metadata_url = f"{self.authority_base()}/v2.0/.well-known/openid-configuration"
        self._metadata = fetch_json(metadata_url)
        self._metadata_at = time.time()
        return self._metadata

    def jwks(self, force: bool = False) -> dict:
        if not force and self._jwks and time.time() - self._jwks_at < 86400:
            return self._jwks
        jwks_uri = self.openid_metadata().get("jwks_uri")
        if not jwks_uri:
            raise AuthError("Microsoft OpenID metadata did not include signing keys.")
        self._jwks = fetch_json(jwks_uri)
        self._jwks_at = time.time()
        return self._jwks

    def sign(self, payload: dict, purpose: str) -> str:
        envelope = {**payload, "purpose": purpose}
        body = b64url_encode(json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = hmac.new(self.session_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{b64url_encode(signature)}"

    def unsign(self, value: str | None, purpose: str) -> dict | None:
        if not value or "." not in value:
            return None
        try:
            body, signature = value.rsplit(".", 1)
            expected = hmac.new(self.session_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(b64url_decode(signature), expected):
                return None
            payload = json.loads(b64url_decode(body).decode("utf-8"))
            if payload.get("purpose") != purpose or int(payload.get("exp", 0)) < int(time.time()):
                return None
            return payload
        except (ValueError, json.JSONDecodeError):
            return None
    def same_origin_request(self, handler) -> bool:
        origin = handler.headers.get("Origin")
        if not origin:
            return True
        expected = os.environ.get("AUTH_PUBLIC_ORIGIN", self.base_url(handler)).rstrip("/")
        return origin.rstrip("/").lower() == expected.lower()

    def cookie_value(self, headers, name: str) -> str:
        raw = headers.get("Cookie") if headers else ""
        if not raw:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(name)
        return morsel.value if morsel else ""

    def cookie_header(self, name: str, value: str, max_age: int, handler) -> str:
        same_site = os.environ.get("AUTH_COOKIE_SAMESITE", "Lax")
        parts = [f"{name}={value}", "Path=/", "HttpOnly", f"SameSite={same_site}", f"Max-Age={max_age}"]
        if self.secure_cookie(handler):
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self, name: str, handler) -> str:
        same_site = os.environ.get("AUTH_COOKIE_SAMESITE", "Lax")
        parts = [f"{name}=", "Path=/", "HttpOnly", f"SameSite={same_site}", "Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"]
        if self.secure_cookie(handler):
            parts.append("Secure")
        return "; ".join(parts)

    def redirect(self, handler, location: str, cookies: list[str] | None = None) -> None:
        handler.send_response(HTTPStatus.FOUND)
        handler.send_header("Location", location)
        handler.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            handler.send_header("Set-Cookie", cookie)
        handler.end_headers()

    def unauthenticated_response(self, handler, path: str) -> None:
        login_url = f"/auth/login?{parse.urlencode({'return': handler.path})}"
        if path.startswith("/api/"):
            self.write_json(handler, {"error": "Authentication is required.", "login_url": login_url}, HTTPStatus.UNAUTHORIZED)
            return
        self.redirect(handler, login_url)

    def forbidden_response(self, handler, path: str, reason: str) -> None:
        if path.startswith("/api/"):
            self.write_json(handler, {"error": reason}, HTTPStatus.FORBIDDEN)
            return
        self.redirect(handler, f"/auth/access-denied?{parse.urlencode({'reason': reason})}")

    def auth_config_response(self, handler, path: str, reason: str) -> None:
        if path.startswith("/api/"):
            self.write_json(handler, {"error": reason}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.write_html(handler, "Authentication not configured", f"<p>{html_escape(reason)}</p>", HTTPStatus.SERVICE_UNAVAILABLE)

    def write_json(self, handler, payload: dict, status: HTTPStatus) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def write_html(self, handler, title: str, body_html: str, status: HTTPStatus) -> None:
        body = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html_escape(title)}</title>
  <style>body{{font-family:Segoe UI,Arial,sans-serif;margin:40px;color:#17202a}}main{{max-width:680px}}a{{color:#1c6dd0}}</style>
</head>
<body><main><h1>{html_escape(title)}</h1>{body_html}<p><a href=\"/auth/login\">Sign in</a></p></main></body>
</html>""".encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def require_config(self) -> None:
        missing = []
        if not self.tenant_id():
            missing.append("AUTH_TENANT_ID")
        if not self.client_id():
            missing.append("AUTH_CLIENT_ID")
        if not self.client_secret():
            missing.append("AUTH_CLIENT_SECRET")
        if len(os.environ.get("AUTH_SESSION_SECRET", "")) < 32:
            missing.append("AUTH_SESSION_SECRET (32+ characters)")
        if missing:
            raise AuthConfigError("Authentication is enabled, but configuration is missing: " + ", ".join(missing) + ".")

    def allowed_domains(self) -> set[str]:
        raw = os.environ.get("AUTH_ALLOWED_DOMAINS") or os.environ.get("AUTH_ALLOWED_DOMAIN") or "tevet.com"
        return {item.strip().lower().lstrip("@") for item in raw.replace(";", ",").split(",") if item.strip()}

    def authority_base(self) -> str:
        explicit = os.environ.get("AUTH_AUTHORITY", "").strip().rstrip("/")
        if explicit.lower().endswith("/v2.0"):
            explicit = explicit[:-5].rstrip("/")
        if explicit:
            return explicit
        host = os.environ.get("AUTH_AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/")
        return f"{host}/{self.tenant_id()}"

    def base_url(self, handler) -> str:
        if os.environ.get("AUTH_PUBLIC_ORIGIN"):
            return os.environ["AUTH_PUBLIC_ORIGIN"].rstrip("/")
        proto = handler.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
        if not proto:
            proto = "https" if self.secure_cookie(handler) else "http"
        host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "127.0.0.1"
        return f"{proto}://{host}"

    def redirect_uri(self, handler) -> str:
        return os.environ.get("AUTH_REDIRECT_URI") or f"{self.base_url(handler)}/auth/callback"

    def secure_cookie(self, handler) -> bool:
        value = os.environ.get("AUTH_COOKIE_SECURE")
        if value is not None and value.strip() != "":
            return env_bool("AUTH_COOKIE_SECURE", False)
        forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        return forwarded_proto == "https"

    def session_seconds(self) -> int:
        return int(os.environ.get("AUTH_SESSION_SECONDS", "28800"))

    def cookie_name(self) -> str:
        return os.environ.get("AUTH_COOKIE_NAME", "communications_evidence_session")

    def state_cookie_name(self) -> str:
        return f"{self.cookie_name()}_state"

    def tenant_id(self) -> str:
        return (os.environ.get("AUTH_TENANT_ID") or os.environ.get("AZURE_TENANT_ID") or "").strip()

    def client_id(self) -> str:
        return os.environ.get("AUTH_CLIENT_ID", "").strip()

    def client_secret(self) -> str:
        return os.environ.get("AUTH_CLIENT_SECRET", "")

    def session_secret(self) -> str:
        secret = os.environ.get("AUTH_SESSION_SECRET", "")
        if len(secret) < 32:
            raise AuthConfigError("AUTH_SESSION_SECRET must be set to at least 32 characters when authentication is enabled.")
        return secret

def verify_rs256(signing_input: bytes, signature: bytes, jwk: dict) -> bool:
    try:
        n = int.from_bytes(b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(b64url_decode(jwk["e"]), "big")
    except KeyError as exc:
        raise AuthError("Microsoft signing key is missing RSA parameters.") from exc
    key_length = (n.bit_length() + 7) // 8
    if len(signature) != key_length:
        return False
    decrypted = pow(int.from_bytes(signature, "big"), e, n).to_bytes(key_length, "big")
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    padding_length = key_length - len(digest_info) - 3
    if padding_length < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
    return hmac.compare_digest(decrypted, expected)


def fetch_json(url: str) -> dict:
    try:
        with request.urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.URLError as exc:
        raise AuthError(f"Could not read Microsoft OpenID metadata: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise AuthError("Microsoft OpenID metadata was not valid JSON.") from exc


def html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )