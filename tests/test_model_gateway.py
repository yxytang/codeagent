from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from xcode import provider
from xcode.config import Settings
from xcode.engine import Engine
from xcode.server import make_handler


class ModelGatewayTests(unittest.TestCase):
    """Use synthetic credentials and loopback HTTP or mocked model transports only."""

    ALPHA_KEY = "synthetic-alpha-key-for-local-tests"
    BETA_KEY = "synthetic-beta-key-for-local-tests"

    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.settings = Settings(self.root / "data")
        self.settings.update({"max_workers": 1})
        self.engine = Engine(self.root / "data", self.project, self.settings)
        self.servers = []
        self.requests = []

    def tearDown(self):
        for cancel in self.engine.cancels.values():
            cancel.set()
        for thread in self.engine.threads.values():
            thread.join(timeout=5)
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.temp.cleanup()
        self.environment.stop()

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    def start_chat_server(self):
        recorded = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                recorded.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": payload,
                })
                result = {
                    "choices": [{"message": {"role": "assistant", "content": "local response", "reasoning_content": "preserved reasoning"}}],
                    "usage": {"total_tokens": 7},
                }
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return self.start_server(Handler)

    @staticmethod
    def profile(identifier, **overrides):
        result = {
            "id": identifier,
            "label": f"Local test {identifier}",
            "protocol": "openai_chat",
            "base_url": f"http://127.0.0.1:9/{identifier}",
            "model": f"{identifier}-model",
            "api_key": "",
            "api_key_env": "",
            "requires_api_key": True,
            "supports_tools": True,
        }
        result.update(overrides)
        return result

    def register(self, identifier, **overrides):
        return self.settings.upsert_model(self.profile(identifier, **overrides))

    def snapshot(self, identifier):
        return self.settings.snapshot(secret=True, model_profile_id=identifier)

    def wait_done(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = self.engine.get_run(identifier)
            if run["status"] in {"ready", "failed", "cancelled", "conflict"}:
                self.engine.threads[identifier].join(timeout=5)
                return self.engine.get_run(identifier)
            time.sleep(0.025)
        self.fail("model gateway task did not finish")

    @staticmethod
    def plan_message():
        return {"role": "assistant", "content": json.dumps({"tasks": [{"id": "writer", "objective": "write a greeting module"}]})}

    def api_request(self, base, route, data=None):
        request = urllib.request.Request(
            base + route,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", "X-XCode-Client": "1"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def assert_no_secrets(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        self.assertNotIn(self.ALPHA_KEY, text)
        self.assertNotIn(self.BETA_KEY, text)

    def test_profiles_route_model_auth_and_tools_to_their_selected_endpoints(self):
        base = self.start_chat_server()
        self.register("alpha", base_url=base + "/alpha", api_key=self.ALPHA_KEY)
        self.register("beta", base_url=base + "/beta/chat/completions", api_key=self.BETA_KEY)
        messages = [{"role": "user", "content": "only a loopback test"}]
        tools = [{"type": "function", "function": {"name": "list_files", "parameters": {"type": "object", "properties": {}}}}]
        for identifier in ("alpha", "beta"):
            with self.subTest(identifier=identifier):
                message, usage = provider.LLMGateway(self.snapshot(identifier)).complete(messages, tools)
                self.assertEqual(message["content"], "local response")
                self.assertEqual(message["reasoning_content"], "preserved reasoning")
                self.assertEqual(usage["total_tokens"], 7)
        self.assertIs(provider.DeepSeekProvider, provider.ChatCompletionsProvider)
        self.assertEqual([record["path"] for record in self.requests], ["/alpha/chat/completions", "/beta/chat/completions"])
        self.assertEqual([record["payload"]["model"] for record in self.requests], ["alpha-model", "beta-model"])
        self.assertEqual([record["authorization"] for record in self.requests], [f"Bearer {self.ALPHA_KEY}", f"Bearer {self.BETA_KEY}"])
        for record in self.requests:
            self.assertEqual(record["payload"]["messages"], messages)
            self.assertEqual(record["payload"]["tools"], tools)
            self.assertFalse(record["payload"]["stream"])

    def test_local_models_need_no_auth_and_incompatible_profiles_fail_before_network(self):
        base = self.start_chat_server()
        self.register("local", base_url=base + "/local", requires_api_key=False)
        provider.LLMGateway(self.snapshot("local")).complete([{"role": "user", "content": "local"}])
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.requests[0]["authorization"])
        self.register("textonly", base_url=base + "/textonly", requires_api_key=False, supports_tools=False)
        self.register("locked", base_url=base + "/locked", api_key="")
        tools = [{"type": "function", "function": {"name": "list_files", "parameters": {"type": "object"}}}]
        with self.assertRaises(ValueError):
            provider.LLMGateway(self.snapshot("textonly")).complete([], tools)
        with self.assertRaises(ValueError):
            self.engine.create("write code", str(self.project), model_profile_id="textonly")
        with self.assertRaises(ValueError):
            provider.LLMGateway(self.snapshot("locked")).complete([])
        with self.assertRaises(ValueError):
            self.engine.create("write code", str(self.project), model_profile_id="locked")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(provider.Cancelled):
            provider.LLMGateway(self.snapshot("local")).complete([], cancel=cancel)
        with patch.object(provider.ChatCompletionsProvider, "complete", side_effect=[
            (self.plan_message(), {}),
            ({"role": "assistant", "content": "本地模型已完成检查"}, {}),
        ]):
            run = self.engine.create("inspect code", str(self.project), model_profile_id="local")
            run = self.wait_done(run["id"])
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(run["model_profile"]["id"], "local")
        self.assertEqual(len(self.requests), 1, "invalid or cancelled requests must not reach a model service")

    def test_gateway_rejects_redirect_without_forwarding_model_credentials(self):
        origin_requests = []
        target_requests = []

        class TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def record_request(self):
                target_requests.append({"method": self.command, "path": self.path, "authorization": self.headers.get("Authorization")})
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "redirect unexpectedly followed"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.record_request()

            def do_POST(self):
                self.record_request()

        target = self.start_server(TargetHandler)

        class OriginHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                origin_requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "payload": payload})
                self.send_response(302)
                self.send_header("Location", target + "/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()

        origin = self.start_server(OriginHandler)
        self.register("redirecting", base_url=origin + "/origin", api_key=self.ALPHA_KEY)
        with self.assertRaisesRegex(RuntimeError, "重定向"):
            provider.LLMGateway(self.snapshot("redirecting")).complete([{"role": "user", "content": "loopback redirect regression"}])
        self.assertEqual(len(origin_requests), 1)
        self.assertEqual(origin_requests[0]["path"], "/origin/chat/completions")
        self.assertEqual(origin_requests[0]["authorization"], f"Bearer {self.ALPHA_KEY}")
        self.assertEqual(target_requests, [], "neither GET nor POST may reach the redirect target")

    def test_in_flight_run_keeps_its_profile_after_default_edit_and_delete(self):
        self.register("alpha", api_key=self.ALPHA_KEY)
        self.register("beta", api_key=self.BETA_KEY)
        self.settings.update({"default_model_id": "alpha"})
        original = self.snapshot("alpha")
        entered = threading.Event()
        release = threading.Event()
        captured = []

        def transport(transport_self, messages, tools=None, cancel=None):
            captured.append(copy.deepcopy(transport_self.settings))
            if len(captured) == 1:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test did not release the planner")
                return self.plan_message(), {}
            if len(captured) == 2:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "write", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "greeting.py", "content": 'MESSAGE = "hello"\n'})}}],
                }, {}
            return {"role": "assistant", "content": "完成 greeting.py"}, {}

        with patch.object(provider.ChatCompletionsProvider, "complete", new=transport):
            run = self.engine.create("write code", str(self.project), model_profile_id="alpha")
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.settings.update({"default_model_id": "beta"})
                self.register("alpha", base_url="http://127.0.0.1:9/edited", model="edited-model", api_key="replacement-synthetic-key")
                self.settings.delete_model("alpha")
            finally:
                release.set()
            run = self.wait_done(run["id"])

        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(len(captured), 3)
        for frozen in captured:
            self.assertEqual((frozen["base_url"], frozen["model"], frozen["api_key"]), (original["base_url"], original["model"], original["api_key"]))
        self.assertEqual(run["model_profile"]["id"], "alpha")
        self.assertEqual(run["model_profile"]["model"], "alpha-model")
        self.assertEqual(self.settings.snapshot()["default_model_id"], "beta")
        self.assertNotIn("api_key", run["model_profile"])
        self.assertNotIn("api_key_env", run["model_profile"])

    def test_http_register_default_explicit_run_and_delete_profiles(self):
        base = self.start_server(make_handler(self.engine, self.settings))
        for identifier, key in (("alpha", self.ALPHA_KEY), ("beta", self.BETA_KEY)):
            result = self.api_request(base, "/api/models", self.profile(identifier, api_key=key))
            self.assertTrue(any(profile["id"] == identifier for profile in result["model_profiles"]))
            self.assert_no_secrets(result)
        selected = self.api_request(base, "/api/settings", {"default_model_id": "beta"})
        self.assertEqual(selected["default_model_id"], "beta")
        self.assertEqual(selected["model"], "beta-model")
        captured = []

        def transport(transport_self, messages, tools=None, cancel=None):
            captured.append(copy.deepcopy(transport_self.settings))
            return (self.plan_message() if tools is None else {"role": "assistant", "content": "无需修改"}), {}

        with patch.object(provider.ChatCompletionsProvider, "complete", new=transport):
            run = self.api_request(base, "/api/runs", {"objective": "inspect code", "workspace": str(self.project), "model_profile_id": "alpha"})
            run = self.wait_done(run["id"])
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(run["model_profile"]["id"], "alpha")
        self.assertTrue(captured)
        self.assertTrue(all(item["model"] == "alpha-model" for item in captured))
        state = self.api_request(base, "/api/state")
        history = next(item for item in state["runs"] if item["id"] == run["id"])
        self.assertEqual(history["model_profile"]["id"], "alpha")
        self.assert_no_secrets(state)
        deleted = self.api_request(base, "/api/models/alpha/delete", {})
        self.assertFalse(any(profile["id"] == "alpha" for profile in deleted["model_profiles"]))
        self.assertEqual(self.api_request(base, "/api/runs/" + run["id"])["model_profile"]["model"], "alpha-model")

    def test_all_profile_keys_are_redacted_from_errors_history_and_persistence(self):
        self.register("alpha", api_key=self.ALPHA_KEY)
        self.register("beta", api_key=self.BETA_KEY)
        self.settings.update({"default_model_id": "beta"})
        failure = f"synthetic gateway failure {self.ALPHA_KEY}; {self.BETA_KEY}"
        with patch.object(provider.ChatCompletionsProvider, "complete", side_effect=RuntimeError(failure)):
            run = self.engine.create("inspect code", str(self.project), model_profile_id="alpha")
            run = self.wait_done(run["id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("synthetic gateway failure", run["error"])
        for value in (run, self.engine.list_runs(), self.engine.memory.load_runs(), self.settings.snapshot(), self.settings.path.read_text("utf-8")):
            self.assert_no_secrets(value)
        for profile in self.settings.snapshot()["model_profiles"]:
            self.assertNotIn("api_key", profile)
        self.assert_no_secrets(self.engine.memory.search(scope=str(self.project.resolve()), raw=True))

        base = self.start_server(make_handler(self.engine, self.settings))
        with patch.object(self.settings, "upsert_model", side_effect=ValueError(failure)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.api_request(base, "/api/models", self.profile("invalid"))
        self.assertEqual(error.exception.code, 400)
        self.assert_no_secrets(error.exception.read().decode("utf-8"))
        self.assert_no_secrets(self.api_request(base, "/api/state"))


if __name__ == "__main__":
    unittest.main()
