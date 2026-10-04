"""Local HTTP server for the browser UI (standard library only).

Binds to 127.0.0.1. Every /api call must carry the random session token that
is put in the URL the tool opens, and the Host header must be local, so other
pages open in the browser cannot reach the API.
"""

from __future__ import annotations

import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import busload, deps, generate, model, ops, validate
from .jsonmodel import database_json
from .session import Session

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 1_000_000
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


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

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def do_POST(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"bad host", "text/plain")
            url = urlparse(self.path)
            if not self._authorised():
                return self._json({"error": "missing or wrong token"}, 403)
            try:
                body = self._body()
                return self._api_post(url.path, body)
            except ops.OpError as exc:
                return self._json({"error": str(exc)}, 400)
            except model.SaveError as exc:
                return self._json({"error": str(exc)}, 409)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json({"error": f"bad request: {exc}"}, 400)
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        def _merge_text(self, body: Dict[str, Any]) -> str:
            if isinstance(body.get("file"), str):
                return session.read_sibling(body["file"])
            return body.get("text")

        def _api_post(self, path: str, body: Dict[str, Any]):
            if path == "/api/merge/preview":
                return self._json(session.merge_preview(self._merge_text(body)))
            if path == "/api/op":
                op = body.get("op")
                if isinstance(op, dict) and op.get("op") == "merge.apply":
                    op = {**op, "text": self._merge_text(op)}
                res = session.apply(op)
                res["status"] = session.status()
                return self._json(res)
            if path in ("/api/undo", "/api/redo"):
                ok = session.undo() if path == "/api/undo" else session.redo()
                return self._json({"ok": ok, "status": session.status()})
            if path == "/api/save":
                res = session.save(sync_docs=bool(body.get("sync_docs", True)))
                res["status"] = session.status()
                return self._json(res)
            if path == "/api/reload":
                session.reload()
                return self._json({"status": session.status()})
            if path == "/api/generate":
                if session.dirty:
                    return self._json({"error": "Save the DBC first: C is generated from the "
                                                "saved file."}, 409)
                return self._json(generate.generate(session.path, session.c_output_dir))
            return self._json({"error": "not found"}, 404)

        def _api_get(self, path: str, query: Dict[str, list]):
            db, text = session.current()
            if path == "/api/db":
                payload = database_json(db)
                payload["file"] = session.path.name
                payload["status"] = session.status()
                return self._json(payload)
            if path == "/api/merge/files":
                return self._json(session.sibling_dbcs())
            if path == "/api/status":
                return self._json(session.status())
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
