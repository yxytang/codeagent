from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xcode.config import Settings
from xcode.engine import DEMO_CODE, Engine, combine_changes, validate_plan
from xcode.memory import MemoryStore
from xcode.mcp import MCPClient, process_env
from xcode.provider import Cancelled, DeepSeekProvider, parse_json
from xcode.skills import SkillRegistry, frontmatter
from xcode.tools import Toolbox
from xcode.workspace import Workspace, apply_patches, digest, safe_path


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            for cancel in engine.cancels.values():
                cancel.set()
            for thread in engine.threads.values():
                thread.join(timeout=5)
        self.temp.cleanup()


class WorkspaceTests(TemporaryCase):
    def test_path_traversal_and_credentials_are_rejected(self):
        for name in ("../escape.py", "C:/escape.py", ".git/config", ".GIT/config", ".env", ".ENV", ".env.production", "folder/id_rsa", "a\\b.py", "/absolute.py", "CON.py", "file.", "file "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_path(self.root, name)
        self.assertEqual(safe_path(self.root, "src/main.py"), self.root / "src" / "main.py")

    def test_snapshot_excludes_secrets_and_merge_is_explicit(self):
        source = self.root / "source"
        source.mkdir()
        (source / "a.py").write_text("old\n")
        (source / ".env").write_text("secret")
        ws = Workspace(source, self.root / "base")
        self.assertEqual(list(ws.original), ["a.py"])
        ws.clone(self.root / "leaf", {"a.py": "new\n"})
        self.assertEqual((source / "a.py").read_text(), "old\n")
        patches = ws.diffs({"a.py": "new\n", "b.py": "new file\n"})
        apply_patches(source, patches)
        self.assertEqual((source / "a.py").read_text(), "new\n")
        self.assertEqual((source / "b.py").read_text(), "new file\n")

    def test_stale_hash_blocks_all_writes(self):
        (self.root / "a.py").write_text("edited\n")
        patches = [{"path": "b.py", "content": "new", "base_hash": None}, {"path": "a.py", "content": "overwrite", "base_hash": digest(b"old\n")}]
        with self.assertRaises(ValueError):
            apply_patches(self.root, patches)
        self.assertFalse((self.root / "b.py").exists())
        self.assertEqual((self.root / "a.py").read_text(), "edited\n")

    def test_merge_rolls_back_after_second_write_error(self):
        (self.root / "a.py").write_text("old")
        patches = [{"path": "a.py", "content": "new", "base_hash": digest(b"old")}, {"path": "b.py", "content": "other", "base_hash": None}]
        original = Path.replace
        def fail_second(path, target):
            if target.name == "b.py":
                raise OSError("injected write failure")
            return original(path, target)
        with patch.object(Path, "replace", fail_second), self.assertRaises(OSError):
            apply_patches(self.root, patches)
        self.assertEqual((self.root / "a.py").read_text(), "old")
        self.assertFalse((self.root / "b.py").exists())


class PlanTests(unittest.TestCase):
    def test_cycle_and_missing_ids_are_rejected(self):
        plans = [
            [{"id":"a","objective":"a","depends_on":["b"]}, {"id":"b","objective":"b","depends_on":["a"]}],
            [{"id":"a","objective":"a","depends_on":["missing"]}],
        ]
        for tasks in plans:
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                validate_plan({"tasks":tasks})

    def test_dependency_chain_can_use_full_task_budget(self):
        tasks = [{"id": f"task{i}", "objective": "task", "depends_on": [f"task{i-1}"] if i else []} for i in range(12)]
        validated = validate_plan({"tasks": tasks}, max_tasks=12)
        self.assertEqual([task["depth"] for task in validated], list(range(1, 13)))
        with self.assertRaises(ValueError):
            validate_plan({"tasks": tasks}, max_tasks=8)

    def test_independent_writes_conflict_but_dependencies_supersede(self):
        tasks = validate_plan({"tasks":[{"id":"a","objective":"a"}, {"id":"b","objective":"b"}, {"id":"c","objective":"c","depends_on":["a"]}]})
        merged, conflicts = combine_changes(tasks, {"a":{"file":"first"}, "b":{"file":"other"}})
        self.assertEqual(merged, {})
        self.assertEqual(conflicts[0]["agents"], ["a", "b"])
        merged, conflicts = combine_changes(tasks, {"a":{"file":"first"}, "c":{"file":"next"}})
        self.assertEqual(merged, {"file":"next"})
        self.assertFalse(conflicts)

    def test_identical_concurrent_writes_are_compatible(self):
        tasks = validate_plan({"tasks":[{"id":"a","objective":"a"}, {"id":"b","objective":"b"}]})
        self.assertEqual(combine_changes(tasks, {"a":{"file":"same"}, "b":{"file":"same"}}), ({"file":"same"}, []))

    def test_reserved_agent_ids_rejected(self):
        for identifier in ("planner", "base", "candidate", "CON", "LPT1"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                validate_plan({"tasks":[{"id":identifier, "objective":"task"}]})


class MemoryTests(TemporaryCase):
    def test_chinese_recall_short_words_and_scope_isolation(self):
        memory = MemoryStore(self.root / "memory.db")
        memory.add("p1", "修复用户登录", "登录认证接口的校验逻辑", "raw")
        memory.add("p2", "修复用户登录", "另一个项目")
        self.assertEqual(len(memory.search("登录认证", "p1")), 1)
        self.assertEqual(len(memory.search("登录", "p1")), 1)
        self.assertEqual(memory.search("登录认证", "p2"), [])
        self.assertNotIn("raw", memory.search("登录", "p1")[0])
        self.assertEqual(memory.search("登录", "p1", raw=True)[0]["raw"], "raw")

    def test_fts_query_input_is_literal(self):
        memory = MemoryStore(self.root / "memory.db")
        memory.add("p", "abc", "sqlite coding memory")
        self.assertIsInstance(memory.search('sqlite \" OR *'), list)


class SkillTests(TemporaryCase):
    def test_frontmatter_and_lazy_loading(self):
        directory = self.root / "skills" / "quality"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text('---\nname: quality\ndescription: >\n  useful Python\n  checks\ntools: [read_file, write_file]\nenabled: true\n---\nFull instructions.', "utf-8")
        registry = SkillRegistry([directory.parent])
        self.assertEqual(registry.entries[0]["description"], "useful Python checks")
        self.assertNotIn("Full instructions", registry.prompt_catalog())
        self.assertEqual(registry.load("quality"), "Full instructions.")
        with self.assertRaises(ValueError):
            SkillRegistry([directory.parent], ["quality"]).load("quality")


class SettingsTests(TemporaryCase):
    def test_key_not_persisted_or_returned(self):
        settings = Settings(self.root)
        settings.update({"api_key":"secret-test", "max_workers":2})
        self.assertNotIn("secret-test", settings.path.read_text())
        self.assertNotIn("api_key", settings.snapshot())
        self.assertTrue(settings.snapshot()["has_api_key"])

    def test_invalid_configuration_rejected(self):
        settings = Settings(self.root)
        for values in ({"max_workers":0}, {"base_url":"file:///etc/passwd"}, {"allowed_commands":["python"]}, {"mcp_servers":[{"name":"a", "command":"python"}]}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                settings.update(values)


class ProviderTests(unittest.TestCase):
    def test_tool_call_schema_and_reasoning_preserved(self):
        settings = {"base_url":"https://api.deepseek.com/v1", "model":"deepseek-chat", "api_key":"secret", "timeout":5}
        class Response(io.BytesIO):
            pass
        message = {"role":"assistant", "content":None, "reasoning_content":"thinking", "tool_calls":[{"id":"call1", "type":"function", "function":{"name":"list_files", "arguments":"{}"}}]}
        payload = {"choices":[{"message":message}], "usage":{"total_tokens":23}}
        with patch("urllib.request.urlopen", return_value=Response(json.dumps(payload).encode())) as request:
            result, usage = DeepSeekProvider(settings).complete([{"role":"user","content":"task"}], [{"type":"function"}])
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, "https://api.deepseek.com/v1/chat/completions")
            self.assertEqual(json.loads(sent.data)["tool_choice"], "auto")
            self.assertEqual(result, message)
            self.assertEqual(usage["total_tokens"], 23)

    def test_cancel_prevents_network_call(self):
        cancel=threading.Event();cancel.set()
        with patch("urllib.request.urlopen") as request, self.assertRaises(Cancelled):
            DeepSeekProvider({"api_key":"secret", "base_url":"https://api.deepseek.com"}).complete([], cancel=cancel)
        request.assert_not_called()

    def test_json_plan_parsing(self):
        self.assertEqual(parse_json('```json\n{"tasks": []}\n```'), {"tasks":[]})


class ToolTests(TemporaryCase):
    def test_commands_cannot_bypass_exact_allowlist(self):
        memory = MemoryStore(self.root / ".xcode" / "memory.db")
        toolbox = Toolbox(self.root, memory, "p", SkillRegistry([]), {"allowed_commands":[["python","--version"]]}, threading.Event(), True)
        with self.assertRaises(ValueError):
            toolbox.call("run_command", {"argv":["python","-c","print('unapproved')"]})
        toolbox.allow_commands = False
        with self.assertRaises(ValueError):
            toolbox.call("run_command", {"argv":["python","--version"]})

    def test_process_env_excludes_credentials(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"secret", "AWS_SECRET_ACCESS_KEY":"other"}):
            self.assertNotIn("DEEPSEEK_API_KEY", process_env())
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", process_env())


class EngineTests(TemporaryCase):
    def engine(self):
        project=self.root / "project";project.mkdir()
        data=self.root / "data"
        settings=Settings(data)
        engine = Engine(data, project, settings)
        self.engines.append(engine)
        return engine

    def wait_done(self, engine, identifier, timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            run=engine.get_run(identifier)
            if run["status"] in ("ready","failed","cancelled","conflict"):
                return run
            time.sleep(.05)
        self.fail("engine did not reach terminal state")

    def test_demo_dependency_execution_real_tests_and_apply(self):
        engine=self.engine()
        run=engine.create("demo",mode="demo")
        run=self.wait_done(engine,run["id"])
        self.assertEqual(run["status"],"ready",run.get("error"))
        self.assertEqual(run["verification"]["status"],"passed")
        core = next(a for a in run["agents"] if a["id"] == "core")
        docs = next(a for a in run["agents"] if a["id"] == "docs")
        self.assertLess(core["started"], docs["finished"])
        self.assertLess(docs["started"], core["finished"])
        self.assertIn("Ran 3 tests",run["verification"]["results"][0]["output"])
        root=Path(run["workspace"])
        self.assertFalse((root / "calculator.py").exists())
        leaf=engine.data_dir / "runs" / run["id"] / "tests" / "calculator.py"
        self.assertEqual(leaf.read_text(),DEMO_CODE)
        engine.apply(run["id"])
        self.assertTrue((root / "calculator.py").exists())
        self.assertEqual(engine.get_run(run["id"])["status"],"applied")

    def test_cancel_does_not_write_to_project(self):
        engine=self.engine()
        run=engine.create("demo",mode="demo")
        engine.cancel(run["id"])
        result=self.wait_done(engine,run["id"])
        self.assertEqual(result["status"],"cancelled")
        self.assertFalse((Path(result["workspace"]) / "calculator.py").exists())

    def test_live_provider_tool_loop_with_mock_transport(self):
        engine=self.engine();engine.settings.update({"api_key":"test", "max_workers":1})
        responses=[
            ({"role":"assistant","content":json.dumps({"tasks":[{"id":"writer","objective":"write a module"}]})}, {"total_tokens":2}),
            ({"role":"assistant","content":None,"tool_calls":[{"id":"c1","type":"function","function":{"name":"write_file","arguments":json.dumps({"path":"hello.py","content":"VALUE = 42\n"})}}]}, {"total_tokens":3}),
            ({"role":"assistant","content":"完成 hello.py；未运行验证"}, {"total_tokens":4}),
        ]
        with patch.object(DeepSeekProvider,"complete",side_effect=responses) as mocked:
            run=engine.create("write a module",str(engine.project_dir))
            run=self.wait_done(engine,run["id"])
        self.assertEqual(run["status"],"ready",run.get("error"))
        self.assertEqual(run["tokens"],9)
        self.assertEqual(mocked.call_count,3)
        self.assertEqual(run["patches"][0]["path"],"hello.py")
        self.assertFalse((engine.project_dir / "hello.py").exists())

    def test_read_progress_survives_context_compaction(self):
        engine = self.engine()
        engine.settings.update({"api_key": "test", "max_workers": 1})
        (engine.project_dir / "hello.py").write_text("VALUE = 1\n")
        responses = [
            ({"role": "assistant", "content": json.dumps({"tasks": [{"id": "writer", "objective": "update hello"}]})}, {}),
            ({"role": "assistant", "tool_calls": [{"id": "read", "function": {"name": "read_file", "arguments": json.dumps({"path": "hello.py"})}}]}, {}),
            ({"role": "assistant", "content": "已检查，无需修改"}, {}),
        ]
        with patch.object(DeepSeekProvider, "complete", side_effect=responses) as mocked, patch.object(engine, "compact", side_effect=lambda identifier, agent_id, messages, scope, limit: messages[:2]):
            run = engine.create("inspect hello", str(engine.project_dir))
            run = self.wait_done(engine, run["id"])
        self.assertEqual(run["status"], "ready", run.get("error"))
        progress = mocked.call_args_list[2].args[0][-1]["content"]
        self.assertIn("hello.py", progress)
        self.assertIn('"remaining_rounds": 39', progress)

    def test_compaction_keeps_tool_call_pairing_and_saves_raw(self):
        engine=self.engine();run=engine.create("demo",mode="demo");engine.cancel(run["id"]);self.wait_done(engine,run["id"])
        messages=[{"role":"system","content":"rules"},{"role":"user","content":"objective"}]
        for i in range(6):
            messages.extend([{"role":"assistant","tool_calls":[{"id":str(i),"function":{"name":"read_file","arguments":"{}"}}]}, {"role":"tool","tool_call_id":str(i),"content":"a"*2500}])
        compacted=engine.compact(run["id"],"leaf",messages,"p",8000)
        self.assertLess(len(json.dumps(compacted)),len(json.dumps(messages)))
        self.assertEqual(compacted[3]["role"],"assistant")
        self.assertTrue(engine.memory.search("read_file","p",raw=True)[0]["raw"])

    def test_failed_leaf_blocks_dependents(self):
        engine=self.engine();engine.settings.update({"api_key":"test"})
        plan={"tasks":[{"id":"core","objective":"core"},{"id":"tests","objective":"tests","depends_on":["core"]}]}
        with patch.object(DeepSeekProvider,"complete",side_effect=[({"role":"assistant","content":json.dumps(plan)},{}), RuntimeError("provider failure")]):
            run=engine.create("task",str(engine.project_dir))
            run=self.wait_done(engine,run["id"])
        self.assertEqual(run["status"],"failed")
        self.assertEqual(next(a for a in run["agents"] if a["id"]=="tests")["status"],"blocked")


class MCPTests(TemporaryCase):
    def test_stdio_initialize_discover_call_and_close(self):
        script=self.root / "mcp_server.py"
        script.write_text('import sys,json\nfor line in sys.stdin:\n p=json.loads(line)\n if "id" not in p: continue\n m=p["method"]\n r={"protocolVersion":"2025-06-18","capabilities":{"tools":{}}} if m=="initialize" else {"tools":[{"name":"echo","inputSchema":{"type":"object","properties":{}}}]} if m=="tools/list" else {"content":[{"type":"text","text":p["params"]["arguments"]["text"]}]}\n print(json.dumps({"jsonrpc":"2.0","id":p["id"],"result":r}),flush=True)\n',"utf-8")
        client=MCPClient({"name":"local","transport":"stdio","command":[sys.executable,str(script)]},self.root)
        try:
            self.assertEqual(client.open()[0]["name"],"echo")
            self.assertEqual(client.call("echo",{"text":"hello"})["content"][0]["text"],"hello")
        finally:
            client.close()
        self.assertIsNotNone(client.process.poll())


if __name__ == "__main__":
    unittest.main()
