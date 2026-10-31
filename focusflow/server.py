"""Local-only HTTP server (127.0.0.1): serves the dashboard and the API the browser extension talks to.

Security: bound to loopback, Host header must match (anti DNS-rebinding), every API call needs the
per-install token, and no CORS headers are ever sent - so ordinary web pages cannot read or call it.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import export, winapi

PORTS = range(47321, 47331)
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
EVENT_KINDS = {"interaction_detected", "target_identified", "dwell_complete", "click_assisted",
               "near_miss", "click_prevented", "manual_click"}   # the only kinds the browser extension may log
DETAIL_NUM = {"path_px", "reversals", "duration_ms", "nx", "ny", "tremor", "dwell_ms", "distance_px"}
DETAIL_STR = {"mode", "method", "reason", "kind", "host", "ctype"}


def clean_detail(d):
    out = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if k in DETAIL_NUM and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        elif k in DETAIL_STR and isinstance(v, str):
            out[k] = v[:60]
        elif k in ("corrected", "assisted") and isinstance(v, bool):
            out[k] = v
    return out


class _Httpd(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a *second* socket bind a port that is already in use, so connections can land on
    # a listener nobody is serving. Bind exclusively; if the port is taken we simply try the next one.
    allow_reuse_address = False
    daemon_threads = True


class Server:
    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.port = None

    def start(self):
        app = self.app
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            # -- helpers --
            def _send(self, code, body, ctype="application/json", filename=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                if filename:
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _host_ok(self):
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{outer.port}", f"localhost:{outer.port}")

            def _origin_ok(self):
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                return origin.startswith("chrome-extension://") or origin in (
                    f"http://127.0.0.1:{outer.port}", f"http://localhost:{outer.port}")

            def _authed(self):
                return self.headers.get("X-FocusFlow-Token") == app.cfg["token"] and self._origin_ok()

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 65536:
                    return {}
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return {}

            # -- routes --
            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, {"error": "bad host"})
                url = urlparse(self.path)
                q = parse_qs(url.query)
                if url.path in ("/", "/index.html"):
                    html = DASHBOARD.read_text(encoding="utf-8").replace("__FF_TOKEN__", app.cfg["token"])
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                if url.path == "/api/ping":
                    return self._send(200, {"app": "focusflow", "port": outer.port})
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                if url.path == "/api/stats":
                    demo = q.get("demo", ["0"])[0] == "1"
                    return self._send(200, {
                        "demo": demo, "has_demo": app.store.has_demo(), "stats": app.store.stats(demo),
                        "miss_points": app.store.near_miss_points(demo), "status": app.status()})
                if url.path == "/api/sessions":
                    demo = q.get("demo", ["0"])[0] == "1"
                    store = app.store
                    return self._send(200, {"sessions": store.sessions(100, demo), "modes": store.mode_summary(demo),
                                            "pointer": store.pointer_totals(0.0, demo),
                                            "series": store.pointer_series(60, demo)})
                if url.path == "/api/export":
                    demo = q.get("demo", ["0"])[0] == "1"
                    fmt = q.get("format", ["json"])[0]
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    if fmt == "csv":
                        return self._send(200, export.sessions_csv(app.store.sessions(1000, demo)).encode(),
                                          "text/csv; charset=utf-8", f"focusflow-{stamp}.csv")
                    if fmt == "pdf":
                        return self._send(200, export.report_pdf(export.bundle(app.store, demo)),
                                          "application/pdf", f"focusflow-{stamp}.pdf")
                    if fmt == "json":
                        return self._send(200, export.bundle_json(app.store, demo).encode(),
                                          "application/json", f"focusflow-{stamp}.json")
                    return self._send(400, {"error": "format must be csv, json or pdf"})
                if url.path == "/api/events":
                    demo = q.get("demo", ["0"])[0] == "1"
                    limit = min(int(q.get("limit", ["150"])[0]), 500)
                    return self._send(200, {"events": app.store.recent(limit, demo)})
                if url.path == "/api/ext/config":
                    return self._send(200, app.extension_config())
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._host_ok():
                    return self._send(403, {"error": "bad host"})
                path = urlparse(self.path).path
                if path == "/api/pair":
                    # POST (not GET) so the browser always attaches an Origin header we can check
                    origin = self.headers.get("Origin") or ""
                    if app.cfg["autopair_extension"] and origin.startswith("chrome-extension://"):
                        return self._send(200, {"token": app.cfg["token"]})
                    return self._send(403, {"error": "pairing not allowed; paste the code from FocusFlow settings"})
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                body = self._body()
                if path == "/api/undo":
                    ok, msg = app.undo(int(body.get("id", 0)))
                    return self._send(200, {"ok": ok, "message": msg})
                if path == "/api/toggle":
                    app.set_enabled(bool(body.get("enabled")), log=True)
                    return self._send(200, {"enabled": app.cfg["enabled"]})
                if path == "/api/demo":
                    if body.get("action") == "load":
                        app.store.seed_demo()
                    elif body.get("action") == "clear":
                        app.store.clear(demo=True)
                    return self._send(200, {"ok": True})
                if path == "/api/clear":
                    app.store.clear(demo=False)
                    return self._send(200, {"ok": True})
                if path == "/api/ext/pos":
                    return self._ext_pos(body)
                if path == "/api/ext/leave":
                    app.engine.ext.leave()
                    return self._send(200, {"ok": True})
                if path == "/api/ext/event":
                    kind = body.get("kind")
                    if kind not in EVENT_KINDS:
                        return self._send(400, {"error": "unknown kind"})
                    name = body.get("name")
                    app.store.add_event(kind, "browser", str(name)[:40] if name else None,
                                        clean_detail(body.get("detail")))
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})

            def _ext_pos(self, b):
                """The extension reports where the pointer is inside its viewport; combined with the real
                screen position that fixes the viewport rectangle in screen pixels (zoom/DPI independent)."""
                try:
                    cx, cy, iw, ih, dpr = (float(b[k]) for k in ("cx", "cy", "iw", "ih", "dpr"))
                except (KeyError, TypeError, ValueError):
                    return self._send(400, {"error": "bad pos"})
                if not (0 < dpr < 10 and 0 < iw < 20000 and 0 < ih < 20000):
                    return self._send(400, {"error": "bad pos"})
                px, py = winapi.get_cursor_pos()
                left, top = px - cx * dpr, py - cy * dpr
                app.engine.ext.update((int(left), int(top), int(left + iw * dpr), int(top + ih * dpr)))
                return self._send(200, {"ok": True})

        for port in PORTS:
            try:
                self.httpd = _Httpd(("127.0.0.1", port), Handler)
                self.port = port
                break
            except OSError:
                continue
        if self.httpd is None:
            raise RuntimeError("no free port for the FocusFlow dashboard")
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="ff-http").start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
