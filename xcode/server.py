from __future__ import annotations

import argparse
import importlib
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .engine import Engine
from .hooks import EVENTS


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
                    return self.json_response({"runs": engine.list_runs(), "settings": settings.snapshot(), "skills": engine.skills().catalog(), "hooks": engine.hooks.catalog(), "hook_events": list(EVENTS), "memory_fts": engine.memory.fts, "memory_stats": engine.memory.stats(), "default_workspace": str(engine.project_dir)})
                if route == "/api/skills":
                    query = parse_qs(parsed.query)
                    return self.json_response(engine.skills().preview(query.get("name", [""])[0]))
                if route == "/api/memory":
                    query = parse_qs(parsed.query)
                    return self.json_response(engine.memory.search(query.get("q", [""])[0], query.get("scope", [None])[0], raw=query.get("raw", ["0"])[0] == "1"))
                if route.startswith("/api/memory/"):
                    parts = route.split("/")
                    if len(parts) != 4 or not parts[3].isascii() or not parts[3].isdigit():
                        raise ValueError("记忆 ID 无效")
                    identifier = int(parts[3])
                    if not 0 < identifier < 2 ** 63:
                        raise ValueError("记忆 ID 无效")
                    query = parse_qs(parsed.query)
                    return self.json_response(engine.memory.get(identifier, raw=query.get("raw", ["0"])[0] == "1"))
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
                if route == "/api/models":
                    return self.json_response(settings.upsert_model(data))
                if route.startswith("/api/models/") and route.endswith("/delete"):
                    parts = route.split("/")
                    if len(parts) != 5 or not parts[3]:
                        raise ValueError("模型连接 ID 无效")
                    return self.json_response(settings.delete_model(parts[3]))
                if route == "/api/memory":
                    scope, title, summary = (data.get(key) for key in ("scope", "title", "summary"))
                    if not isinstance(scope, str) or not scope.strip():
                        raise ValueError("项目路径必须是现有绝对目录")
                    project = Path(scope)
                    if not project.is_absolute() or not project.is_dir():
                        raise ValueError("项目路径必须是现有绝对目录")
                    if not isinstance(title, str) or not title.strip() or not 1 <= len(title) <= 300:
                        raise ValueError("标题须为 1–300 个字符")
                    if not isinstance(summary, str) or not summary.strip() or not 1 <= len(summary) <= 8000:
                        raise ValueError("笔记内容须为 1–8000 个字符")
                    identifier = engine.memory.add(str(project.resolve()), title.strip(), summary.strip(), kind="note", agent="user")
                    return self.json_response({"memory": engine.memory.get(identifier)}, 201)
                if route.startswith("/api/memory/") and route.endswith("/delete"):
                    parts = route.split("/")
                    if len(parts) != 5 or not parts[3].isascii() or not parts[3].isdigit():
                        raise ValueError("记忆 ID 无效")
                    identifier = int(parts[3])
                    if not 0 < identifier < 2 ** 63:
                        raise ValueError("记忆 ID 无效")
                    return self.json_response({"deleted": engine.memory.delete_note(identifier)})
                if route == "/api/runs":
                    return self.json_response(engine.create(data.get("objective", ""), data.get("workspace", ""), data.get("mode", "live"), data.get("allow_commands", False), data.get("model_profile_id")), 201)
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
                error = settings.redact(str(exc))
                self.json_response({"error": error[:1200]}, 400)

    return Handler


def load_hook_modules(registry, module_names):
    """Explicit service-side imports only; there is no HTTP code-loading API."""
    for module_name in module_names:
        if not isinstance(module_name, str) or not module_name or any(not part.isidentifier() for part in module_name.split(".")):
            raise ValueError("Hook 模块必须是 Python 模块名，例如 examples.hooks_example")
        module = importlib.import_module(module_name)
        register = getattr(module, "register_hooks", None)
        if not callable(register):
            raise ValueError(f"Hook 模块 {module_name} 需要定义 register_hooks(registry)")
        register(registry)


def main():
    parser = argparse.ArgumentParser(description="XCode local multi-agent console")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--hooks-module", action="append", default=[], metavar="MODULE", help="加载可信 Python 模块的 register_hooks(registry)，可重复指定")
    args = parser.parse_args()
    project = args.project.resolve()
    if not project.is_dir():
        parser.error("--project 必须是现有目录")
    data_dir = (args.data_dir or project / ".xcode").resolve()
    settings = Settings(data_dir)
    engine = Engine(data_dir, project, settings)
    try:
        load_hook_modules(engine.hooks, args.hooks_module)
    except Exception as exc:
        parser.error("Hook 加载失败：" + settings.redact(str(exc)))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine, settings))
    server.daemon_threads = True
    print(f"XCode console: http://127.0.0.1:{server.server_port}", flush=True)
    print("Multi-model Chat Completions gateway | local-only | Ctrl+C to stop", flush=True)
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
