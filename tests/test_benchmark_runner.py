"""Offline integration checks; mocked model responses are not benchmark scores."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.__main__ import main
from benchmarks.evaluator import evaluate, write_files
from benchmarks.runner import (
    BenchmarkConfig, FrozenSettings, VARIANTS, connection_from_env, run_suite,
)
from benchmarks.task_suite import TaskCase
from xcode.provider import DeepSeekProvider


BAD_ADD = "def add(a, b):\n    return a - b\n"
GOOD_ADD = "def add(a, b):\n    return a + b\n"
PUBLIC_TEST = (
    "import unittest\n"
    "from calculator import add\n\n"
    "class AdditionTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
PRIVATE_GRADER = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path.cwd()))\n"
    "from calculator import add\n"
    "assert add(7, 4) == 11, 'private addition acceptance failed'\n"
    "assert add(-2, 5) == 3, 'private signed addition acceptance failed'\n"
    "print('private acceptance passed')\n"
)


def calculator_case():
    return TaskCase(
        id="integration-calculator", category="bug_fix",
        objective="Fix calculator.add(a, b) to add numbers. Run the public tests.",
        files={"calculator.py": BAD_ADD, "test_public.py": PUBLIC_TEST},
        solution={"calculator.py": GOOD_ADD}, grader_source=PRIVATE_GRADER,
    )


def tool_response(name, arguments):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "offline-tool", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


def plan_response():
    return {"role": "assistant", "content": json.dumps({
        "tasks": [{"id": "fix", "label": "Fix calculator",
                   "objective": "Fix calculator.py and run public tests.",
                   "depends_on": []}],
    })}


def done_response():
    return {"role": "assistant", "content": "Mock transport: finished for offline integration checks."}


class BenchmarkRunnerTests(unittest.TestCase):
    """Exercise actual Engine, tools and independent Python acceptance processes."""

    KEY = "synthetic-benchmark-test-key-never-real"
    URL = "https://offline-benchmark.invalid/v1"
    MODEL = "frozen-offline-test-model"
    USAGE = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}

    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_BASE_URL": "https://ambient-settings.invalid",
            "DEEPSEEK_MODEL": "ambient-model-must-not-be-used",
        })
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory(prefix="xcode-runner-test-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()
        self.environment.stop()

    def connection(self):
        return {"base_url": self.URL, "model": self.MODEL, "api_key": self.KEY}

    def config(self, variants=("baseline",)):
        return BenchmarkConfig(
            variants=variants, max_steps=6, max_tasks=1,
            max_model_calls=8, max_tool_calls=12, max_total_tokens=1000,
            max_batch_tokens=10000, task_timeout=30,
        )

    @staticmethod
    def public_command():
        return [sys.executable, "-I", "-B", "-m", "unittest",
                "discover", "-s", ".", "-p", "test*.py"]

    def scripted_run(self, case, responses, name="scripted"):
        requests = []
        responses = iter(responses)

        def transport(provider, messages, tools=None, cancel=None):
            requests.append(deepcopy(messages))
            return next(responses), dict(self.USAGE)

        output = self.root / name
        with patch.object(DeepSeekProvider, "complete", new=transport):
            report = run_suite(output, self.connection(), [case], self.config())
        self.assertEqual(len(report["results"]), 1, report)
        return report["results"][0], requests, output

    def test_failed_public_test_is_observed_then_fixed_and_rerun(self):
        record, requests, output = self.scripted_run(calculator_case(), [
            plan_response(),
            tool_response("run_command", {"argv": self.public_command()}),
            tool_response("write_file", {"path": "calculator.py", "content": GOOD_ADD}),
            tool_response("run_command", {"argv": self.public_command()}),
            done_response(),
        ])
        self.assertTrue(record["success"], record)
        self.assertEqual(record["status"], "ready")
        self.assertTrue(record["evaluator"]["passed"])
        self.assertEqual(record["public_verification"]["status"], "passed")

        # The next model request actually contains the failed command result.
        after_failure = [json.loads(message["content"]) for message in requests[2]
                         if message["role"] == "tool"]
        self.assertEqual(after_failure[0]["exit_code"], 1)
        self.assertIn("AssertionError", after_failure[0]["output"])
        self.assertIn("-1 != 5", after_failure[0]["output"])
        after_repair = [json.loads(message["content"]) for message in requests[4]
                        if message["role"] == "tool"]
        self.assertEqual(after_repair[-1]["exit_code"], 0)
        self.assertIn("OK", after_repair[-1]["output"])

        metrics = record["metrics"]
        self.assertEqual(metrics["model_rounds"], 5)  # Includes planning.
        self.assertEqual(metrics["tool_calls"], 4)  # Three Agent calls + final verification.
        self.assertEqual(metrics["command_failures"], 1)
        self.assertEqual(metrics["tool_errors"], 1)
        self.assertEqual([entry["exit_code"] for entry in metrics["test_command_results"]], [1, 0, 0])
        self.assertEqual([entry["source"] for entry in metrics["test_command_results"]],
                         ["execution", "execution", "verification"])
        self.assertEqual(metrics["reported_total_tokens"], 150)
        source = output / "cells" / "integration-calculator" / "baseline" / "repeat-1" / "workspace"
        self.assertEqual((source / "calculator.py").read_text(encoding="utf-8"), BAD_ADD,
                         "benchmark candidate must not be merged into the seed workspace")

    def test_replacing_public_tests_cannot_bypass_private_acceptance(self):
        passing_but_useless_test = (
            "import unittest\n"
            "class FakeTests(unittest.TestCase):\n"
            "    def test_always_passes(self):\n"
            "        self.assertTrue(True)\n"
        )
        record, requests, output = self.scripted_run(calculator_case(), [
            plan_response(),
            tool_response("write_file", {"path": "test_public.py", "content": passing_but_useless_test}),
            tool_response("run_command", {"argv": self.public_command()}),
            done_response(),
        ], "tampered-tests")
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["public_verification"]["status"], "passed")
        self.assertFalse(record["success"])
        self.assertFalse(record["evaluator"]["passed"])
        self.assertEqual(record["evaluator"]["exit_code"], 1)
        self.assertIn("private addition acceptance failed", record["evaluator"]["output"])
        self.assertNotIn("private addition acceptance", json.dumps(requests),
                         "private feedback must never enter Agent requests")
        cell = output / "cells" / "integration-calculator" / "baseline" / "repeat-1"
        self.assertEqual((cell / "evaluation" / "candidate" / "test_public.py").read_text(encoding="utf-8"),
                         passing_but_useless_test)
        self.assertEqual((cell / "workspace" / "test_public.py").read_text(encoding="utf-8"), PUBLIC_TEST)
        self.assertFalse((cell / "evaluation" / "candidate" / "acceptance.py").exists())
        self.assertTrue((cell / "evaluation" / "acceptance.py").is_file())

    def test_four_variants_share_frozen_model_tools_and_budgets(self):
        connection = self.connection()
        calls, steps = [], {}

        def transport(provider, messages, tools=None, cancel=None):
            calls.append({"settings": deepcopy(provider.settings), "tools": deepcopy(tools)})
            if tools is None:
                # Deliberately change both caller state and ambient defaults mid-batch.
                connection.update(model="changed-model", base_url="https://changed.invalid", api_key="changed-key")
                os.environ["DEEPSEEK_MODEL"] = "changed-environment-model"
                return plan_response(), dict(self.USAGE)
            step = steps.get(provider, 0)
            steps[provider] = step + 1
            if step == 0:
                return tool_response("write_file", {"path": "calculator.py", "content": GOOD_ADD}), dict(self.USAGE)
            return done_response(), dict(self.USAGE)

        output = self.root / "four-variants"
        with patch.object(DeepSeekProvider, "complete", new=transport):
            report = run_suite(output, connection, [calculator_case()], self.config(tuple(VARIANTS)))
        self.assertEqual(len(report["results"]), 4, report)
        self.assertTrue(all(record["success"] for record in report["results"]), report)
        self.assertEqual({record["variant"] for record in report["results"]}, set(VARIANTS))
        common_options = None
        common_tools = None
        for record in report["results"]:
            self.assertEqual({key: record["runtime_options"][key] for key in ("context_management", "prompt_guidance")},
                             VARIANTS[record["variant"]])
            budgets = {key: value for key, value in record["runtime_options"].items()
                       if key not in ("context_management", "prompt_guidance")}
            if common_options is None:
                common_options = budgets
            self.assertEqual(budgets, common_options)
        for call in calls:
            settings = call["settings"]
            self.assertEqual(settings["model"], self.MODEL)
            self.assertEqual(settings["base_url"], self.URL)
            self.assertEqual(settings["api_key"], self.KEY)
            self.assertEqual(settings["max_workers"], 1)
            self.assertEqual(settings["mcp_servers"], [])
            self.assertEqual(settings["allowed_commands"], [self.public_command()])
            if call["tools"] is not None:
                if common_tools is None:
                    common_tools = call["tools"]
                self.assertEqual(call["tools"], common_tools)
        self.assertEqual(report["config"]["model"], {"base_url": self.URL, "model": self.MODEL})
        self.assertNotIn(self.KEY, json.dumps(report))
        for path in output.rglob("*.json"):
            self.assertNotIn(self.KEY, path.read_text(encoding="utf-8"), str(path))

    def test_model_exception_is_redacted_before_reports_and_runtime_logs(self):
        def transport(provider, messages, tools=None, cancel=None):
            raise RuntimeError("mock provider failure containing " + self.KEY)

        output = self.root / "redacted-error"
        with patch.object(DeepSeekProvider, "complete", new=transport):
            report = run_suite(output, self.connection(), [calculator_case()], self.config())
        self.assertFalse(report["results"][0]["success"])
        self.assertIn("[redacted]", report["results"][0]["error"])
        self.assertNotIn(self.KEY, json.dumps(report))
        for path in output.rglob("*.json"):
            self.assertNotIn(self.KEY, path.read_text(encoding="utf-8"), str(path))

    def test_frozen_settings_return_detached_snapshots_and_reject_other_profile(self):
        values = {"api_key": self.KEY, "model_profile": {"id": "fixed", "model": self.MODEL},
                  "nested": {"values": [1]}}
        frozen = FrozenSettings(values)
        values["nested"]["values"].append(2)
        first = frozen.snapshot(secret=True)
        first["nested"]["values"].append(3)
        self.assertEqual(frozen.snapshot(secret=True)["nested"]["values"], [1])
        self.assertNotIn("api_key", frozen.snapshot())
        self.assertEqual(frozen.snapshot(secret=True)["api_key"], self.KEY)
        with self.assertRaisesRegex(ValueError, "frozen"):
            frozen.snapshot(model_profile_id="other")

    def test_live_cli_without_key_sends_no_request_and_creates_no_output(self):
        output = self.root / "no-key-output"
        stderr = io.StringIO()
        with patch.object(DeepSeekProvider, "complete") as complete, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                main(["--live", "--limit", "1", "--output", str(output)])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("Missing DEEPSEEK_API_KEY", stderr.getvalue())
        self.assertFalse(output.exists())
        complete.assert_not_called()

    def test_list_cli_requires_no_key_and_creates_no_output(self):
        output = self.root / "list-output"
        stdout = io.StringIO()
        with patch.object(DeepSeekProvider, "complete") as complete, redirect_stdout(stdout):
            self.assertEqual(main(["--list", "--limit", "1", "--output", str(output)]), 0)
        self.assertIn("understand-01", stdout.getvalue())
        self.assertFalse(output.exists())
        complete.assert_not_called()

    def test_existing_output_is_refused_without_overwriting_or_requests(self):
        output = self.root / "prior-output"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("previous evidence", encoding="utf-8")
        with patch.object(DeepSeekProvider, "complete") as complete:
            with self.assertRaisesRegex(ValueError, "never overwritten"):
                run_suite(output, self.connection(), [calculator_case()], self.config())
        complete.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "previous evidence")
        self.assertEqual({path.name for path in output.iterdir()}, {"keep.txt"})

    def test_private_evaluator_timeout_is_failure(self):
        case = TaskCase("timeout-case", "feature", "unused", {}, {}, "while True:\n    pass\n")
        source = self.root / "timeout-source"
        write_files(source, {})
        result = evaluate(case, source, self.root / "timeout-evaluation", timeout=0.2)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "timeout")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertLess(result["latency_seconds"], 5)

    def test_connection_validation_rejects_secret_urls_and_bad_variable_names(self):
        with patch.dict(os.environ, {"TEST_BENCHMARK_KEY": self.KEY}):
            for url in ["https://user:secret@example.com", "https://example.com?key=secret",
                        "file:///tmp/model", "https://example.com#token"]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    connection_from_env(url, "model", "TEST_BENCHMARK_KEY")
            with self.assertRaises(ValueError):
                connection_from_env(self.URL, "model", "BAD-ENV-NAME")


if __name__ == "__main__":
    unittest.main()
