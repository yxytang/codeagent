"""Exercise opt-in benchmark controls through the real runtime without network."""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from xcode.config import Settings
from xcode.engine import Engine, RuntimeBudgetExceeded
from xcode.provider import DeepSeekProvider


class BenchmarkRuntimeTests(unittest.TestCase):
    KEY = "synthetic-benchmark-runtime-key"

    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
        self.environment.start()
        # Use an ordinary workspace directory: on Windows the AppContainer
        # temp directory can inherit an ACL that prevents creating children.
        self.temp_root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.root = self.temp_root / ("benchmark-runtime-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.engines = []
        self.next_engine = 0

    def tearDown(self):
        for engine in self.engines:
            for cancel in engine.cancels.values():
                cancel.set()
            for thread in engine.threads.values():
                thread.join(timeout=10)
        if not self.root.resolve().is_relative_to(self.temp_root):
            raise RuntimeError("test cleanup escaped its workspace directory")
        shutil.rmtree(self.root)
        self.environment.stop()

    def engine(self, options=None, workers=1):
        folder = self.root / str(self.next_engine)
        self.next_engine += 1
        project = folder / "project"
        project.mkdir(parents=True)
        (project / "sample.py").write_text("VALUE = 1\n", "utf-8")
        settings = Settings(folder / "data")
        settings.update({"api_key": self.KEY, "max_workers": workers})
        engine = Engine(folder / "data", project, settings, runtime_options=options)
        self.engines.append(engine)
        return engine

    def finish(self, engine, run):
        thread = engine.threads[run["id"]]
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "benchmark runtime did not finish")
        return engine.get_run(run["id"])

    @staticmethod
    def plan(*ids):
        return {"role": "assistant", "content": json.dumps({"tasks": [
            {"id": identifier, "objective": "inspect sample.py"}
            for identifier in (ids or ("worker",))]})}

    @staticmethod
    def calls(*entries):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{i}", "function": {"name": name,
             "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}
            for i, (name, arguments) in enumerate(entries)]}

    @staticmethod
    def done():
        return {"role": "assistant", "content": "inspected; no changes required"}

    @staticmethod
    def usage():
        return {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}

    def test_default_controls_preserve_loop_and_collect_planning_usage(self):
        engine = self.engine()
        responses = [(self.plan(), self.usage()),
                     (self.calls(("read_file", {"path": "sample.py"})), self.usage()),
                     (self.done(), self.usage())]
        with patch.object(DeepSeekProvider, "complete", side_effect=responses) as transport:
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
        self.assertEqual(run["status"], "ready", run["error"])
        metrics = run["metrics"]
        self.assertEqual((metrics["model_rounds"], metrics["model_successes"]), (3, 3))
        self.assertEqual((metrics["tool_calls"], metrics["tool_errors"]), (1, 0))
        self.assertEqual((metrics["prompt_tokens"], metrics["completion_tokens"],
                          metrics["reported_total_tokens"], run["tokens"]), (6, 3, 9, 9))
        self.assertTrue(metrics["usage_complete"])
        progress = transport.call_args_list[-1].args[0][-1]["content"]
        self.assertIn("执行进度：", progress)
        self.assertIn("sample.py", progress)
        self.assertNotIn(self.KEY, repr(run))

    def test_context_and_progress_switches_are_independent_and_immutable(self):
        for context, guidance in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(context=context, guidance=guidance):
                options = {"context_management": context, "prompt_guidance": guidance}
                engine = self.engine(options)
                options["context_management"] = not context
                responses = [(self.plan(), self.usage()), (self.done(), self.usage())]
                with patch.object(DeepSeekProvider, "complete", side_effect=responses) as transport, \
                     patch.object(engine, "compact", side_effect=lambda i, a, m, s, l: m) as compact:
                    run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
                self.assertEqual(run["status"], "ready", run["error"])
                self.assertEqual(compact.call_count, int(context))
                request = transport.call_args_list[1].args[0]
                self.assertEqual(any(m.get("content", "").startswith("执行进度：")
                                     for m in request if isinstance(m.get("content"), str)), guidance)
                self.assertEqual(run["runtime_options"]["context_management"], context)

    def test_missing_usage_and_provider_failure_remain_observable(self):
        for usage in ({}, self.usage()):
            with self.subTest(first_usage=usage):
                engine = self.engine()
                responses = [(self.plan(), usage), RuntimeError("synthetic provider failure")]
                with patch.object(DeepSeekProvider, "complete", side_effect=responses):
                    run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
                self.assertEqual(run["status"], "failed")
                self.assertEqual((run["metrics"]["model_rounds"], run["metrics"]["model_successes"]), (2, 1))
                self.assertFalse(run["metrics"]["usage_complete"])
                self.assertEqual(run["metrics"]["reported_total_tokens"], usage.get("total_tokens", 0))

    def test_invalid_json_and_non_object_arguments_count_once_and_feed_back(self):
        engine = self.engine()
        responses = [(self.plan(), self.usage()),
                     (self.calls(("read_file", "{"), ("read_file", "[]"),
                                 ("read_file", {"path": "missing.py"})), self.usage()),
                     (self.done(), self.usage())]
        with patch.object(DeepSeekProvider, "complete", side_effect=responses) as transport:
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
        self.assertEqual(run["status"], "ready", run["error"])
        metrics = run["metrics"]
        self.assertEqual((metrics["tool_calls"], metrics["tool_errors"], metrics["invalid_arguments"]), (3, 3, 2))
        feedback = [json.loads(m["content"]) for m in transport.call_args_list[-1].args[0] if m["role"] == "tool"]
        self.assertEqual(len(feedback), 3)
        self.assertTrue(all("error" in result for result in feedback))

    def test_command_failures_are_feedback_and_recorded_without_secrets(self):
        engine = self.engine()
        command = ["synthetic-python", "-m", "unittest"]
        engine.settings.update({"allowed_commands": [command]})
        output = "FAILED " + self.KEY + "!" * 3000
        command_result = {"argv": command, "exit_code": 1, "output": output, "reason": None}
        requests = []
        responses = iter([(self.plan(), self.usage()),
                          (self.calls(("run_command", {"argv": command})), self.usage()),
                          (self.done(), self.usage())])

        def transport(provider, messages, tools=None, cancel=None):
            requests.append(copy.deepcopy(messages))
            return next(responses)

        # Coordinator verification also uses execute_command; return success
        # there, so the earlier command failure remains ordinary Agent feedback.
        with patch.object(DeepSeekProvider, "complete", new=transport), \
             patch("xcode.tools.execute_command", return_value=command_result), \
             patch("xcode.engine.execute_command", return_value={"argv": command, "exit_code": 0, "output": "OK", "reason": None}):
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir), allow_commands=True))
        self.assertEqual(run["status"], "ready", run["error"])
        self.assertEqual((run["metrics"]["tool_calls"], run["metrics"]["tool_errors"],
                          run["metrics"]["command_failures"]), (2, 1, 1))
        results = run["metrics"]["test_command_results"]
        self.assertEqual([r["exit_code"] for r in results], [1, 0])
        self.assertTrue(results[0]["output_truncated"])
        self.assertNotIn(self.KEY, repr(run))
        feedback = next(json.loads(m["content"]) for m in requests[-1] if m["role"] == "tool")
        self.assertEqual(feedback["exit_code"], 1)
        self.assertEqual(feedback["output"], output, "metrics must not change tool return semantics")

    def test_command_timeout_is_failure_even_if_exit_code_is_zero(self):
        engine = self.engine()
        command = ["synthetic-python", "-m", "unittest"]
        engine.settings.update({"allowed_commands": [command]})
        responses = [(self.plan(), self.usage()), (self.done(), self.usage())]
        result = {"argv": command, "exit_code": 0, "output": "timed out", "reason": "timeout"}
        with patch.object(DeepSeekProvider, "complete", side_effect=responses), \
             patch("xcode.engine.execute_command", return_value=result):
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir), allow_commands=True))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["verification"]["status"], "failed")
        self.assertEqual((run["metrics"]["tool_errors"], run["metrics"]["command_failures"]), (1, 1))
        self.assertEqual(run["metrics"]["test_command_results"][0]["reason"], "timeout")

    def test_model_budget_includes_planner_and_stops_before_next_request(self):
        engine = self.engine({"max_model_calls": 1})
        with patch.object(DeepSeekProvider, "complete", return_value=(self.plan(), self.usage())) as transport:
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(run["metrics"]["model_rounds"], 1)
        self.assertIn("max_model_calls=1", run["error"])

    def test_reported_token_budget_allows_one_inflight_response_then_stops(self):
        engine = self.engine({"max_total_tokens": 2})
        # The first response reports three tokens. It is accounted, then the
        # next logical request is denied; no claim of a hard token cap is made.
        with patch.object(DeepSeekProvider, "complete", return_value=(self.plan(), self.usage())) as transport:
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(run["metrics"]["reported_total_tokens"], 3)
        self.assertIn("max_total_tokens=2", run["error"])

    def test_tool_budget_stops_on_second_tool_in_same_round(self):
        engine = self.engine({"max_tool_calls": 1})
        responses = [(self.plan(), self.usage()),
                     (self.calls(("read_file", {"path": "sample.py"}),
                                 ("write_file", {"path": "forbidden.py", "content": "no"})), self.usage())]
        with patch.object(DeepSeekProvider, "complete", side_effect=responses) as transport:
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(run["metrics"]["tool_calls"], 1)
        self.assertEqual(run["metrics"]["tool_errors"], 0, "a budget refusal is not an executed tool failure")
        self.assertIn("max_tool_calls=1", run["error"])
        self.assertFalse((engine.data_dir / "runs" / run["id"] / "worker" / "forbidden.py").exists())

    def test_parallel_agents_share_atomic_model_budget(self):
        engine = self.engine({"max_model_calls": 3}, workers=2)
        barrier = threading.Barrier(2)
        calls = []

        def transport(provider, messages, tools=None, cancel=None):
            calls.append(bool(tools))
            if tools is None:
                return self.plan("alpha", "beta"), self.usage()
            barrier.wait(timeout=5)
            return self.calls(("read_file", {"path": "sample.py"})), self.usage()

        with patch.object(DeepSeekProvider, "complete", new=transport):
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
        self.assertEqual(run["status"], "failed", run["error"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(run["metrics"]["model_rounds"], 3)
        self.assertEqual(run["metrics"]["model_successes"], 3)

    def test_real_compaction_counts_only_completed_compressions(self):
        engine = self.engine()
        with patch.object(DeepSeekProvider, "complete", side_effect=[(self.plan(), self.usage()), (self.done(), self.usage())]):
            run = self.finish(engine, engine.create("inspect", str(engine.project_dir)))
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "objective"}]
        for index in range(6):
            messages.extend([{"role": "assistant", "tool_calls": [{"id": str(index), "function": {"name": "read_file", "arguments": "{}"}}]},
                             {"role": "tool", "tool_call_id": str(index), "content": "x" * 2500}])
        self.assertIs(engine.compact(run["id"], "worker", messages, run["workspace"], 100000), messages)
        compacted = engine.compact(run["id"], "worker", messages, run["workspace"], 8000)
        self.assertLess(len(json.dumps(compacted)), len(json.dumps(messages)))
        self.assertEqual(engine.get_run(run["id"])["metrics"]["context_compressions"], 1)

    def test_invalid_options_and_explicit_budget_exception_type(self):
        for options in ({"unknown": True}, {"prompt_guidance": 1},
                        {"max_tool_calls": True}, {"max_model_calls": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.engine(options)
        self.assertTrue(issubclass(RuntimeBudgetExceeded, RuntimeError))


if __name__ == "__main__":
    unittest.main()
