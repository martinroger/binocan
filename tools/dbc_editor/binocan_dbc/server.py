"""Local HTTP server for the browser UI (standard library only).

Binds to 127.0.0.1. Every /api call must carry the random session token that
is put in the URL the tool opens, and the Host header must be local, so other
pages open in the browser cannot reach the API.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import busload, deps, generate, model, validate
from .jsonmodel import database_json

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Session:
    """The open DBC, reloaded whenever the file changes on disk."""

    def __init__(self, dbc_path: Path, c_output_dir: Path):
        self.path = Path(dbc_path).resolve()
        self.c_output_dir = Path(c_output_dir).resolve()
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._mtime: Optional[float] = None
        self.db = None
        self.text = ""

    def current(self):
        with self._lock:
            mtime = self.path.stat().st_mtime
            if self.db is None or mtime != self._mtime:
                self.db, self.text = model.load(self.path)
                self._mtime = mtime
            return self.db, self.text


def _parse_overrides(raw: str) -> Dict[int, int]:
    out = {}
    for item in filter(None, raw.split(",")):
        fid, _, ms = item.partition(":")
        out[int(fid, 0)] = int(ms)
    return out


def make_handler(session: Session):
    class Handler(BaseHTTPRequestHandler):
        server_version = "binocan-dbc"

        def log_message(self, fmt, *args):  # quieter console
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost", "[::1]")

        def _authorised(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Token", ""), session.token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"bad host", "text/plain")
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                if not self._authorised():
                    return self._json({"error": "missing or wrong token"}, 403)
                try:
                    return self._api_get(url.path, parse_qs(url.query))
                except Exception as exc:  # report, keep serving
                    return self._json({"error": str(exc)}, 500)
            return self._static(url.path)

        def do_POST(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"bad host", "text/plain")
            url = urlparse(self.path)
            if not self._authorised():
                return self._json({"error": "missing or wrong token"}, 403)
            try:
                if url.path == "/api/generate":
                    res = generate.generate(session.path, session.c_output_dir)
                    return self._json(res)
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
            return self._json({"error": "not found"}, 404)

        def _api_get(self, path: str, query: Dict[str, list]):
            db, text = session.current()
            if path == "/api/db":
                payload = database_json(db)
                payload["file"] = session.path.name
                return self._json(payload)
            if path == "/api/check":
                return self._json(validate.check(db))
            if path == "/api/busload":
                baud = int(query["baud"][0]) if query.get("baud") else None
                overrides = _parse_overrides(query.get("o", [""])[0])
                return self._json(busload.compute(db, baud, overrides, text))
            if path == "/api/deps":
                return self._json(deps.status(check_latest=True))
            return self._json({"error": "not found"}, 404)

        def _static(self, path: str):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            return self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    return Handler


def make_server(session: Session, port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(session))


def serve(dbc_path: Path, c_output_dir: Path, port: int = 0, open_browser: bool = True) -> None:
    session = Session(dbc_path, c_output_dir)
    session.current()  # fail early on a broken DBC
    httpd = make_server(session, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/#t={session.token}"
    print(f"[edit] {session.path.name} served at {url}")
    print("[edit] Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[edit] Stopped.")
    finally:
        httpd.server_close()
