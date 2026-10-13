from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from xcode.config import Settings
from xcode.engine import Engine
from xcode.mcp import MCPClient
from xcode.server import make_handler


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / ".xcode")
        self.engine = Engine(self.root / ".xcode", self.root, self.settings)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine, self.settings))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        for cancel in self.engine.cancels.values():
            cancel.set()
        for thread in self.engine.threads.values():
            thread.join(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, route, data=None, headers=None):
        common = {"Content-Type":"application/json", "X-XCode-Client":"1"}
        if headers:
            common.update(headers)
        req = urllib.request.Request(self.base + route, data=json.dumps(data).encode() if data is not None else None, headers=common)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    def test_static_page_csp_and_public_configuration(self):
        with urllib.request.urlopen(self.base) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("编程工作台", response.read().decode())
        self.request("/api/settings", {"api_key":"integration-secret"})
        state = self.request("/api/state")
        self.assertTrue(state["settings"]["has_api_key"])
        self.assertNotIn("integration-secret", json.dumps(state))
        self.assertNotIn("integration-secret", self.settings.path.read_text())

    def test_mutation_requires_header_origin_and_trusted_host(self):
        cases = [{"X-XCode-Client":""}, {"Origin":"https://attacker.example"}, {"Host":"attacker.example"}]
        for headers in cases:
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/settings", {"max_workers":2}, headers)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.settings.snapshot()["max_workers"], 3)

    def test_bad_json_and_unknown_route_are_errors(self):
        for route, data, code in [("/api/settings", {"max_workers":999}, 400), ("/api/missing", {}, 404)]:
            with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route, data)
            self.assertEqual(error.exception.code, code)

    def test_demo_http_run_diff_review_apply_and_memory(self):
        run = self.request("/api/runs", {"objective":"HTTP demo", "mode":"demo"})
        deadline = time.monotonic() + 10
        while run["status"] not in ("ready", "failed") and time.monotonic() < deadline:
            time.sleep(.05)
            run = self.request("/api/runs/" + run["id"])
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(len(run["patches"]), 3)
        self.assertTrue(all(p["diff"] for p in run["patches"]))
        self.assertEqual(run["verification"]["status"], "passed")
        applied = self.request("/api/runs/" + run["id"] + "/apply", {})
        self.assertEqual(applied["status"], "applied")
        self.assertTrue((Path(run["workspace"]) / "calculator.py").exists())
        memories = self.request("/api/memory?q=HTTP&raw=1")
        self.assertTrue(memories)
        self.assertIn("raw", memories[0])


class MCPHTTPTests(unittest.TestCase):
    def test_http_negotiated_headers_session_and_sse_tool_response(self):
        class Response(io.BytesIO):
            def __init__(self, data, headers, status=200):
                super().__init__(data)
                self.headers, self.status = headers, status
        calls = []
        def transport(req, **kwargs):
            payload = json.loads(req.data)
            calls.append((payload, {k.lower():v for k,v in req.header_items()}))
            method = payload["method"]
            if method == "notifications/initialized":
                return Response(b"", {}, 202)
            if method == "initialize":
                result = {"protocolVersion":"2025-03-26", "capabilities":{"tools":{}}}
                headers = {"Mcp-Session-Id":"session-123", "Content-Type":"application/json"}
            elif method == "tools/list":
                result = {"tools":[{"name":"echo", "inputSchema":{"type":"object"}}]}
                headers = {"Content-Type":"application/json"}
            else:
                result = {"content":[{"type":"text", "text":"ok"}]}
                message = json.dumps({"jsonrpc":"2.0", "id":payload["id"], "result":result})
                return Response(("event: message\ndata: " + message + "\n\n").encode(), {"Content-Type":"text/event-stream"})
            return Response(json.dumps({"jsonrpc":"2.0", "id":payload["id"], "result":result}).encode(), headers)
        client = MCPClient({"name":"remote", "transport":"http", "url":"https://mcp.example/mcp"}, Path.cwd())
        with patch("urllib.request.urlopen", side_effect=transport):
            self.assertEqual(client.open()[0]["name"], "echo")
            self.assertEqual(client.call("echo", {})["content"][0]["text"], "ok")
        self.assertEqual(calls[1][1]["mcp-session-id"], "session-123")
        self.assertEqual(calls[1][1]["mcp-protocol-version"], "2025-03-26")
        client.close()


if __name__ == "__main__":
    unittest.main()
