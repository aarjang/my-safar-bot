"""HTTP Basic Auth for the whole dashboard, plus basic brute-force mitigation.

This app is meant to be exposed on the public internet (behind nginx/TLS), so
every route — including the static page, the JSON API, and FastAPI's own
``/docs``/``/redoc``/``/openapi.json`` — requires a login. There is no
"public" surface by design; the operator is a single person/agency, not a
multi-user product, so one shared username/password is the right amount of
mechanism (a real user table would be over-engineering here).

Credential resolution, in priority order:

1. ``DASHBOARD_USERNAME`` / ``DASHBOARD_PASSWORD_HASH`` env vars — how the
   Docker/production deployment supplies them (see docker-compose.yml / .env).
2. ``data/.dashboard_credentials.json`` — a locally generated fallback so
   ``python -m msbot.web`` still works out of the box for local dev without
   forcing you to configure anything. Generated once, chmod 600, gitignored;
   the plaintext password is printed to the console exactly once at
   generation time and never stored in plaintext afterward.

Passwords are hashed with bcrypt (adaptive cost, salt built in) — never
compared or stored in plaintext after the first console printout.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import bcrypt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("msbot.web.auth")

CREDENTIALS_FILE = Path("data/.dashboard_credentials.json")
DEFAULT_USERNAME = "admin"

#: brute-force mitigation: after this many failed attempts from one IP within
#: WINDOW_SECONDS, that IP is tarpitted (each further attempt sleeps longer)
#: rather than answered instantly — slows down a script, costs a real human
#: almost nothing.
MAX_ATTEMPTS_BEFORE_TARPIT = 5
WINDOW_SECONDS = 300.0
TARPIT_SECONDS = 4.0


def generate_strong_password(length: int = 24) -> str:
    """A genuinely strong password, not a word-based one — this gates a
    dashboard with pricing data, on the open internet, on purpose."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        # keep it simple and guaranteed to satisfy "has every character class"
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%^&*()-_=+" for c in pwd)
        ):
            return pwd


def _load_or_create_local_credentials() -> Tuple[str, str]:
    if CREDENTIALS_FILE.exists():
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        return data["username"], data["password_hash"]

    username = DEFAULT_USERNAME
    password = generate_strong_password()
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(
        json.dumps({"username": username, "password_hash": password_hash}, indent=2),
        encoding="utf-8",
    )
    os.chmod(CREDENTIALS_FILE, 0o600)

    banner = "=" * 72
    log.warning(
        "\n%s\n GENERATED A NEW DASHBOARD LOGIN — SAVE THIS, IT IS SHOWN ONLY ONCE\n"
        "   username: %s\n   password: %s\n"
        " (hash saved to %s; set DASHBOARD_USERNAME/DASHBOARD_PASSWORD_HASH\n"
        "  env vars instead for production so this file is never the source of truth)\n%s",
        banner, username, password, CREDENTIALS_FILE, banner,
    )
    return username, password_hash


def resolve_credentials() -> Tuple[str, str]:
    env_user = os.environ.get("DASHBOARD_USERNAME")
    env_hash = os.environ.get("DASHBOARD_PASSWORD_HASH")
    if env_user and env_hash:
        return env_user, env_hash
    return _load_or_create_local_credentials()


class _AttemptTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: Dict[str, list] = {}

    def record_failure(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._failures.get(key, []) if now - t < WINDOW_SECONDS]
            hits.append(now)
            self._failures[key] = hits
            return len(hits)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def recent_failures(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            return len([t for t in self._failures.get(key, []) if now - t < WINDOW_SECONDS])


_tracker = _AttemptTracker()


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gate every single request behind HTTP Basic Auth.

    Deliberately whole-app, not per-route: this dashboard has no page that's
    safe to leave public (the rate/markup data is exactly what an agency
    doesn't want a competitor casually reading).
    """

    def __init__(self, app, get_credentials: Callable[[], Tuple[str, str]], realm: str = "MySafar Dashboard"):
        super().__init__(app)
        self._get_credentials = get_credentials
        self.realm = realm

    async def dispatch(self, request: Request, call_next) -> Response:
        client_ip = request.client.host if request.client else "unknown"

        recent_fails = _tracker.recent_failures(client_ip)
        if recent_fails >= MAX_ATTEMPTS_BEFORE_TARPIT:
            time.sleep(min(TARPIT_SECONDS * (recent_fails - MAX_ATTEMPTS_BEFORE_TARPIT + 1), 20.0))

        auth_header = request.headers.get("Authorization")
        username, password_hash = self._get_credentials()

        if auth_header and auth_header.startswith("Basic "):
            import base64

            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                given_user, _, given_pass = decoded.partition(":")
            except Exception:
                given_user, given_pass = "", ""

            user_ok = secrets.compare_digest(given_user, username)
            pass_ok = bool(given_pass) and bcrypt.checkpw(given_pass.encode("utf-8"), password_hash.encode("utf-8"))
            if user_ok and pass_ok:
                _tracker.clear(client_ip)
                return await call_next(request)

            _tracker.record_failure(client_ip)
            log.warning("failed login attempt from %s (user=%r)", client_ip, given_user)

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="{}"'.format(self.realm)},
            content="Authentication required.",
        )
