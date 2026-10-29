from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from xcode.config import Settings
from xcode.engine import Engine
from xcode.hooks import HookDecision, HookRegistry
from xcode.provider import Cancelled, DeepSeekProvider
from xcode.tools import Toolbox


class EngineHookTests(unittest.TestCase):
    """Exercise hooks through real coordinators and isolated workspaces, without network."""

    KEY = "synthetic-frozen-hook-test-key"
    EVENTS = ("on_run_start", "before_model", "after_model", "before_tool", "after_tool", "on_tool_error", "on_run_end")

    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            for cancel in engine.cancels.values():
                cancel.set()
            for thread in engine.threads.values():
                thread.join(timeout=10)
        self.temp.cleanup()
        self.environment.stop()

    def make_engine(self, hooks=None, workers=1):
        folder = self.root / str(len(self.engines))
        project = folder / "project"
        project.mkdir(parents=True)
        settings = Settings(folder / "data")
        settings.update({"api_key": self.KEY, "max_workers": workers})
        engine = Engine(folder / "data", project, settings, hooks=hooks or HookRegistry())
        self.engines.append(engine)
        return engine, project, settings

    def finish(self, engine, run):
        thread = engine.threads[run["id"]]
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "hook integration task did not finish")
        return engine.get_run(run["id"])

    @staticmethod
    def plan(*identifiers):
        return {"role": "assistant", "content": json.dumps({"tasks": [{"id": i, "objective": f"implement {i}"} for i in (identifiers or ("writer",))]})}

    @staticmethod
    def tools(*entries):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for index, (name, arguments) in enumerate(entries)
        ]}

    @staticmethod
    def done():
        return {"role": "assistant", "content": "finished"}

    def test_live_prechecks_and_post_failures_preserve_actual_tool_results(self):
        registry = HookRegistry()
        observed = []
        for event in self.EVENTS:
            registry.register(event, lambda ctx: observed.append((ctx.event, ctx.agent_id, copy.deepcopy(ctx.data))), name="observe-" + event)

        def inject(ctx):
            return HookDecision(system_message="HOOK_INSTRUCTION_" + ctx.data["source"])

        def deny_one(ctx):
            if ctx.data["arguments"].get("path") == "blocked.py":
                return HookDecision(deny="blocked by hook policy")

        def broken_observer(ctx):
            raise RuntimeError("observer failed after successful tool")

        registry.register("before_model", inject, name="instructions")
        registry.register("before_tool", deny_one, name="deny-blocked-file")
        registry.register("after_tool", broken_observer, name="broken-observer")
        engine, project, _ = self.make_engine(registry)
        requests, dispatched = [], []
        real_call = Toolbox.call

        def dispatch(box, name, arguments):
            dispatched.append((name, copy.deepcopy(arguments)))
            return real_call(box, name, arguments)

        responses = iter([self.plan(), self.tools(
            ("write_file", {"path": "blocked.py", "content": "blocked\n"}),
            ("write_file", {"path": "allowed.py", "content": "allowed\n"}),
        ), self.done()])

        def transport(provider, messages, tools=None, cancel=None):
            requests.append(copy.deepcopy(messages))
            return next(responses), {}

        with patch.object(DeepSeekProvider, "complete", new=transport), patch.object(Toolbox, "call", new=dispatch):
            run = self.finish(engine, engine.create("implement two files", str(project)))
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(dispatched, [("write_file", {"path": "allowed.py", "content": "allowed\n"})])
        self.assertEqual([p["path"] for p in run["patches"]], ["allowed.py"])
        self.assertFalse((project / "allowed.py").exists(), "review is required before merging")
        self.assertTrue(any(m["role"] == "system" and "HOOK_INSTRUCTION_planning" in m["content"] for m in requests[0]))
        self.assertTrue(any(m["role"] == "system" and "HOOK_INSTRUCTION_execution" in m["content"] for m in requests[1]))
        tool_results = [json.loads(m["content"]) for m in requests[2] if m["role"] == "tool"]
        self.assertIn("blocked by hook policy", tool_results[0]["error"])
        self.assertEqual(tool_results[1]["written"], "allowed.py")
        self.assertNotIn("error", tool_results[1], "post-hook failure must not disguise a completed write as a failed tool")
        errors = [data for event, _, data in observed if event == "on_tool_error"]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0]["blocked"])
        self.assertEqual(errors[0]["arguments"]["path"], "blocked.py")
        self.assertEqual(sum(event == "on_run_end" for event, _, _ in observed), 1)
        self.assertIn("deny-blocked-file", json.dumps(run["hooks"]))
        self.assertTrue(any(e["kind"] == "hook" and "broken-observer" in e["message"] for e in run["events"]))

    def test_cancellation_during_before_tool_prevents_dispatch_and_finalizes_once(self):
        for cancellation in ("engine-signal", "hook-exception"):
            with self.subTest(cancellation=cancellation):
                registry = HookRegistry()
                endings = []
                engine, project, _ = self.make_engine(registry)

                def cancel_before_tool(ctx):
                    if cancellation == "hook-exception":
                        raise Cancelled("hook requested cancellation")
                    engine.cancel(ctx.run_id)

                registry.register("before_tool", cancel_before_tool, name="cancel-at-tool")
                registry.register("on_run_end", lambda ctx: endings.append(copy.deepcopy(ctx.data)), name="observe-end")
                responses = [(self.plan(), {}), (self.tools(("write_file", {"path": "cancelled.py", "content": "must not be written"})), {})]
                with patch.object(DeepSeekProvider, "complete", side_effect=responses), patch.object(Toolbox, "call") as dispatch:
                    run = self.finish(engine, engine.create("cancel before writing", str(project)))
                dispatch.assert_not_called()
                self.assertEqual(run["status"], "cancelled", run.get("error"))
                self.assertEqual(len(endings), 1)
                self.assertEqual(endings[0]["status"], "cancelled")
                self.assertFalse(run["patches"])

    def test_each_run_uses_the_registry_snapshot_taken_at_creation(self):
        registry = HookRegistry()
        engine, project, _ = self.make_engine(registry)
        entered, release = threading.Event(), threading.Event()
        late = []

        def transport(provider, messages, tools=None, cancel=None):
            if tools is None:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test planner was not released")
                return self.plan(), {}
            return self.done(), {}

        with patch.object(DeepSeekProvider, "complete", new=transport):
            first = engine.create("first snapshot", str(project))
            try:
                self.assertTrue(entered.wait(timeout=5))
                registry.register("before_model", lambda ctx: late.append(ctx.run_id), name="registered-later")
            finally:
                release.set()
            first = self.finish(engine, first)
            second = self.finish(engine, engine.create("second snapshot", str(project)))
        self.assertEqual(first["status"], "ready", first.get("error"))
        self.assertEqual(second["status"], "ready", second.get("error"))
        self.assertNotIn(first["id"], late)
        self.assertEqual(late, [second["id"], second["id"]])
        self.assertNotIn("registered-later", json.dumps(first["hooks"]))
        self.assertIn("registered-later", json.dumps(second["hooks"]))

    def test_demo_tools_and_coordinator_verification_use_the_same_hook_boundary(self):
        registry = HookRegistry()
        observed, endings = [], []
        registry.register("before_tool", lambda ctx: observed.append(copy.deepcopy(ctx.data)), name="observe-demo-tools")
        registry.register("on_run_end", lambda ctx: endings.append(copy.deepcopy(ctx.data)), name="observe-end")
        engine, project, _ = self.make_engine(registry, workers=2)
        skill = project / "skills" / "python-quality" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: python-quality\ndescription: Write and test Python\n---\nUse unittest.\n", "utf-8")
        with patch.object(DeepSeekProvider, "complete") as model:
            run = self.finish(engine, engine.create("offline demo hook coverage", mode="demo"))
        model.assert_not_called()
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(run["verification"]["status"], "passed")
        self.assertTrue({"load_skill", "read_file", "write_file"}.issubset({d["name"] for d in observed if d["source"] == "demo"}))
        checks = [d for d in observed if d["source"] == "verification"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["name"], "run_command")
        self.assertEqual(len(endings), 1)
        self.assertEqual(endings[0]["status"], "ready")
        before_apply = len(observed)
        engine.apply(run["id"])
        self.assertEqual(len(observed), before_apply, "manual merge is not a tool execution hook")
        self.assertEqual(len(endings), 1, "merging a completed run must not end it again")

    def test_parallel_agents_and_handlers_receive_independent_context_copies(self):
        registry = HookRegistry()
        barrier = threading.Barrier(2)
        first_contexts, second_contexts = [], []

        def mutate_own_copy(ctx):
            if ctx.event == "before_tool":
                first_contexts.append((ctx.run_id, ctx.agent_id, copy.deepcopy(ctx.data)))
                ctx.data["arguments"]["path"] = "mutated-by-observer.py"
                barrier.wait(timeout=5)

        def observe_copy(ctx):
            second_contexts.append((ctx.run_id, ctx.agent_id, copy.deepcopy(ctx.data)))

        registry.register("before_tool", mutate_own_copy, name="mutate-observer-copy", priority=-10)
        registry.register("before_tool", observe_copy, name="observe-independent-copy", priority=10)
        engine, project, _ = self.make_engine(registry, workers=2)

        def transport(provider, messages, tools=None, cancel=None):
            if tools is None:
                return self.plan("alpha", "beta"), {}
            task = json.loads(next(m["content"] for m in messages if m["role"] == "user"))["task"]
            if any(m["role"] == "tool" for m in messages):
                return self.done(), {}
            return self.tools(("write_file", {"path": task["id"] + ".py", "content": task["id"] + "\n"})), {}

        with patch.object(DeepSeekProvider, "complete", new=transport):
            run = self.finish(engine, engine.create("parallel isolated hooks", str(project)))
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual({p["path"] for p in run["patches"]}, {"alpha.py", "beta.py"})
        self.assertEqual(len(first_contexts), 2)
        self.assertEqual(len(second_contexts), 2)
        for run_id, agent_id, data in second_contexts:
            self.assertEqual(run_id, run["id"])
            self.assertEqual(data["arguments"]["path"], agent_id + ".py")

    def test_hook_context_errors_and_persistence_redact_a_deleted_profiles_frozen_key(self):
        registry = HookRegistry()
        contexts, endings = [], []
        registry.register("before_tool", lambda ctx: contexts.append(repr(ctx)), name="observe-no-secret")

        def failing_hook(ctx):
            raise RuntimeError("hook credential failure " + self.KEY)

        registry.register("before_tool", failing_hook, name="broken-precheck")
        registry.register("on_run_end", lambda ctx: endings.append(copy.deepcopy(ctx.data)), name="observe-end")
        engine, project, settings = self.make_engine(registry)
        settings.upsert_model({"id": "frozen", "label": "Frozen", "base_url": "http://127.0.0.1:9", "model": "local", "api_key": self.KEY, "api_key_env": ""})
        entered, release = threading.Event(), threading.Event()
        requests = []

        def transport(provider, messages, tools=None, cancel=None):
            requests.append(copy.deepcopy(messages))
            if tools is None:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test planner was not released")
                return self.plan(), {}
            if any(m["role"] == "tool" for m in messages):
                return self.done(), {}
            return self.tools(("write_file", {"path": "no-secret.py", "content": "safe\n"})), {}

        with patch.object(DeepSeekProvider, "complete", new=transport), patch.object(Toolbox, "call") as dispatch:
            run = engine.create("frozen key redaction", str(project), model_profile_id="frozen")
            try:
                self.assertTrue(entered.wait(timeout=5))
                settings.update({"api_key": "different-synthetic-current-key"})
                settings.delete_model("frozen")
            finally:
                release.set()
            run = self.finish(engine, run)
        dispatch.assert_not_called()
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(len(endings), 1)
        for value in (run, engine.list_runs(), engine.memory.load_runs(), contexts, endings, requests, settings.path.read_text("utf-8")):
            self.assertNotIn(self.KEY, repr(value))
        self.assertTrue(any("hook credential failure" in e["message"] for e in run["events"]))

    def test_end_hook_runs_once_for_denials_conflict_and_failing_cleanup(self):
        for outcome in ("start-denied", "model-denied", "hook-cancel-key", "conflict", "cleanup-error"):
            with self.subTest(outcome=outcome):
                registry = HookRegistry()
                endings = []
                registry.register("on_run_end", lambda ctx: endings.append(copy.deepcopy(ctx.data)), name="observe-end")
                if outcome in {"start-denied", "model-denied"}:
                    registry.register("on_run_start" if outcome == "start-denied" else "before_model", lambda ctx: HookDecision(deny="policy denied task"), name="deny-task")
                if outcome == "hook-cancel-key":
                    def cancel_with_sensitive_reason(ctx):
                        raise Cancelled("hook requested cancellation " + self.KEY)

                    registry.register("before_model", cancel_with_sensitive_reason, name="cancel-with-sensitive-reason")
                engine, project, settings = self.make_engine(registry, workers=2)
                if outcome == "cleanup-error":
                    settings.update({"mcp_servers": [{"name": "mock", "transport": "stdio", "command": ["unused"], "allowed_tools": ["inspect"]}]})

                class BrokenCleanupClient:
                    def __init__(self, *args):
                        pass

                    def open(self):
                        return [{"name": "inspect", "inputSchema": {"type": "object", "properties": {}}}]

                    def close(self):
                        raise RuntimeError("synthetic MCP cleanup failure")

                transport_calls = []

                def transport(provider, messages, tools=None, cancel=None):
                    transport_calls.append(copy.deepcopy(messages))
                    if tools is None:
                        return (self.plan("alpha", "beta") if outcome == "conflict" else self.plan()), {}
                    task = json.loads(next(m["content"] for m in messages if m["role"] == "user"))["task"]
                    if outcome != "conflict" or any(m["role"] == "tool" for m in messages):
                        return self.done(), {}
                    return self.tools(("write_file", {"path": "collision.py", "content": task["id"] + "\n"})), {}

                with patch.object(DeepSeekProvider, "complete", new=transport), patch("xcode.engine.MCPClient", BrokenCleanupClient):
                    run = self.finish(engine, engine.create("check ending " + outcome, str(project)))
                self.assertEqual(len(endings), 1, outcome)
                self.assertEqual(endings[0]["status"], run["status"])
                self.assertIn("changed_files", endings[0])
                self.assertIn("verification", endings[0])
                if outcome in {"start-denied", "model-denied"}:
                    self.assertEqual(run["status"], "failed")
                    self.assertIn("policy denied task", run["error"])
                    self.assertEqual(transport_calls, [], "denied tasks must not invoke the model")
                elif outcome == "hook-cancel-key":
                    self.assertEqual(run["status"], "cancelled")
                    self.assertEqual(transport_calls, [])
                    self.assertNotIn(self.KEY, repr(run))
                    self.assertNotIn(self.KEY, repr(engine.memory.load_runs()))
                elif outcome == "conflict":
                    self.assertEqual(run["status"], "conflict", run.get("error"))
                else:
                    self.assertEqual(run["status"], "ready", run.get("error"))
                    self.assertTrue(any("synthetic MCP cleanup failure" in e["message"] for e in run["events"]))


if __name__ == "__main__":
    unittest.main()
