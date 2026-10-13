from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .engine import Engine


STATIC = Path(__file__).parent / "static"


def make_handler(engine, settings):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Request bodies and credentials are never logged.
            pass

        def json_response(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return self.headers.get("Host", "") in expected

        def do_GET(self):
            if not self.valid_host():
                return self.json_response({"error": "Host 不受信任"}, 403)
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                if route == "/api/state":
                    return self.json_response({"runs": engine.list_runs(), "settings": settings.snapshot(), "skills": engine.skills().catalog(), "memory_fts": engine.memory.fts, "default_workspace": str(engine.project_dir)})
                if route == "/api/memory":
                    query = parse_qs(parsed.query)
                    return self.json_response(engine.memory.search(query.get("q", [""])[0], query.get("scope", [None])[0], raw=query.get("raw", ["0"])[0] == "1"))
                if route.startswith("/api/runs/"):
                    return self.json_response(engine.get_run(route.rsplit("/", 1)[-1]))
                if route.startswith("/api/"):
                    return self.json_response({"error": "接口不存在"}, 404)
                names = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
                if route not in names:
                    return self.json_response({"error": "文件不存在"}, 404)
                path = STATIC / names[route]
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, OSError) as exc:
                self.json_response({"error": str(exc)}, 400)

        def do_POST(self):
            # Loopback only + exact Host + origin check + custom header prevents drive-by requests.
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if not self.valid_host() or (origin and origin not in allowed) or self.headers.get("X-XCode-Client") != "1":
                return self.json_response({"error": "请求来源未通过校验"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024 or not self.headers.get("Content-Type", "").startswith("application/json"):
                    raise ValueError("请求必须为 JSON 且不超过 1MB")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求体必须是对象")
                route = urlparse(self.path).path
                if route == "/api/settings":
                    return self.json_response(settings.update(data))
                if route == "/api/runs":
                    return self.json_response(engine.create(data.get("objective", ""), data.get("workspace", ""), data.get("mode", "live"), data.get("allow_commands", False)), 201)
                if route.startswith("/api/runs/") and route.endswith(("/cancel", "/apply")):
                    identifier, action = route.split("/")[-2:]
                    return self.json_response(engine.cancel(identifier) if action == "cancel" else engine.apply(identifier))
                if route == "/api/mcp/probe":
                    name = data.get("name")
                    config = next((c for c in settings.snapshot()["mcp_servers"] if c["name"] == name), None)
                    if not config:
                        raise ValueError("请先保存 MCP 配置")
                    return self.json_response({"tools": engine.mcp_probe(config)})
                self.json_response({"error": "接口不存在"}, 404)
            except (ValueError, OSError, KeyError, TypeError, RuntimeError, TimeoutError) as exc:
                key = settings.snapshot(secret=True)["api_key"]
                error = str(exc).replace(key, "[redacted]") if key else str(exc)
                self.json_response({"error": error[:1200]}, 400)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="XCode local multi-agent console")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    if not project.is_dir():
        parser.error("--project 必须是现有目录")
    data_dir = (args.data_dir or project / ".xcode").resolve()
    settings = Settings(data_dir)
    engine = Engine(data_dir, project, settings)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine, settings))
    server.daemon_threads = True
    print(f"XCode console: http://127.0.0.1:{server.server_port}", flush=True)
    print("DeepSeek-compatible API | local-only | Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for event in engine.cancels.values():
            event.set()
        server.server_close()


if __name__ == "__main__":
    main()
