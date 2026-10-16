from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xcode.config import Settings
from xcode.engine import Engine
from xcode.provider import DeepSeekProvider


class KnowledgeUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.data = self.root / "data"
        self.settings = Settings(self.data)
        self.settings.update({"api_key": "fake-for-local-tests", "max_workers": 1})
        self.engine = Engine(self.data, self.project, self.settings)
        self.engines = [self.engine]
        self.skill_body = "SKILL_BODY_ONLY_AFTER_LOAD: write clear code and verify the result."
        skill = self.project / "skills" / "python-quality" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: python-quality\ndescription: Python implementation checklist\n"
            "tools: [read_file, write_file]\nenabled: true\n---\n" + self.skill_body,
            encoding="utf-8",
        )

    def tearDown(self):
        for engine in self.engines:
            for cancel in engine.cancels.values():
                cancel.set()
            for thread in engine.threads.values():
                thread.join(timeout=5)
        self.temp.cleanup()

    def wait_done(self, identifier, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.engine.get_run(identifier)
            if run["status"] in {"ready", "failed", "cancelled", "conflict"}:
                self.engine.threads[identifier].join(timeout=5)
                return self.engine.get_run(identifier)
            time.sleep(0.025)
        self.fail("engine did not finish the knowledge usage scenario")

    @staticmethod
    def plan():
        return {
            "role": "assistant",
            "content": json.dumps({"tasks": [{"id": "writer", "objective": "implement greeting"}]}),
        }

    @staticmethod
    def calls(*specifications):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
                for index, (name, arguments) in enumerate(specifications)
            ],
        }

    def test_live_loop_loads_skill_on_demand_and_records_scoped_memory_use(self):
        scope = str(self.project.resolve())
        note = "PROJECT_NOTE_RECALLED: public functions need a short docstring."
        private = "OTHER_PROJECT_NOTE_MUST_STAY_PRIVATE"
        raw = "RAW_HISTORY_NOT_AUTOMATICALLY_INJECTED"
        note_id = self.engine.memory.add(scope, "Project conventions", note, kind="note")
        self.engine.memory.add(str((self.root / "other-project").resolve()), "Project conventions", private, kind="note")
        history_id = self.engine.memory.add(scope, "greeting implementation", "greeting returns a friendly string", raw)
        captured = []
        responses = [
            self.plan(),
            self.calls(
                ("load_skill", {"name": "python-quality"}),
                ("recall_memory", {"query": "greeting"}),
                ("write_file", {"path": "greeting.py", "content": 'def greet():\n    """Return a greeting."""\n    return "hello"\n'}),
            ),
            {"role": "assistant", "content": "已参考技能与项目记忆，完成 greeting.py；未运行命令。"},
        ]

        def fake_provider(messages, tools=None, cancel=None):
            captured.append(copy.deepcopy(messages))
            return responses[len(captured) - 1], {"total_tokens": 1}

        with patch.object(DeepSeekProvider, "complete", side_effect=fake_provider):
            run = self.engine.create("implement greeting", scope)
            run = self.wait_done(run["id"])

        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(len(captured), 3)
        for messages in captured[:2]:
            text = json.dumps(messages, ensure_ascii=False)
            self.assertIn(note, text)
            self.assertNotIn(private, text)
            self.assertNotIn(raw, text)
            self.assertNotIn(self.skill_body, text)
        self.assertIn("Python implementation checklist", captured[1][0]["content"])
        self.assertIn(self.skill_body, json.dumps(captured[2], ensure_ascii=False))
        self.assertEqual(run["patches"][0]["path"], "greeting.py")

        knowledge = run["knowledge"]
        self.assertEqual(knowledge["available_skills"], [{"name": "python-quality", "description": "Python implementation checklist"}])
        loads = knowledge["skill_loads"]
        self.assertEqual(len(loads), 1)
        self.assertEqual((loads[0]["agent"], loads[0]["name"], loads[0]["source"]), ("writer", "python-quality", "tool"))
        self.assertEqual(loads[0]["description"], "Python implementation checklist")
        self.assertTrue(loads[0]["time"])
        recalls = knowledge["memory_recalls"]
        self.assertEqual({record["source"] for record in recalls}, {"planning", "execution", "tool"})
        for record in recalls:
            if record["source"] in {"planning", "execution"}:
                self.assertIn(note_id, {item["id"] for item in record["items"]})
            self.assertNotIn(private, json.dumps(record, ensure_ascii=False))
            self.assertTrue(record["time"])
            for item in record["items"]:
                self.assertTrue({"id", "title", "summary", "kind", "created"} <= item.keys())
                self.assertNotIn("raw", item)
        tool_recall = next(record for record in recalls if record["source"] == "tool")
        self.assertEqual((tool_recall["agent"], tool_recall["query"]), ("writer", "greeting"))
        self.assertIn(history_id, {item["id"] for item in tool_recall["items"]})
        self.assertTrue(knowledge["memory_saves"])
        stored = {item["id"]: item for item in self.engine.memory.search(scope=scope, limit=50, raw=True)}
        for saved in knowledge["memory_saves"]:
            record = stored[saved["id"]]
            self.assertEqual(record["run_id"], run["id"])
            self.assertEqual((record["kind"], record["agent"]), (saved["kind"], saved["agent"]))
            self.assertTrue(saved["time"])

    def test_disabled_skill_cannot_be_reported_as_successfully_loaded(self):
        self.settings.update({"disabled_skills": ["python-quality"]})
        captured = []
        responses = [
            self.plan(),
            self.calls(("load_skill", {"name": "python-quality"})),
            {"role": "assistant", "content": "技能已禁用，未加载；本次无文件修改。"},
        ]

        def fake_provider(messages, tools=None, cancel=None):
            captured.append(copy.deepcopy(messages))
            return responses[len(captured) - 1], {}

        with patch.object(DeepSeekProvider, "complete", side_effect=fake_provider):
            run = self.engine.create("inspect greeting", str(self.project))
            run = self.wait_done(run["id"])

        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(run["knowledge"]["available_skills"], [])
        self.assertEqual(run["knowledge"]["skill_loads"], [])
        results = [message for message in captured[-1] if message["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertIn("error", json.loads(results[0]["content"]))
        self.assertNotIn(self.skill_body, json.dumps(captured, ensure_ascii=False))

    def test_large_skill_and_memory_results_reach_next_model_turn_without_truncation(self):
        self.settings.update({"context_chars": 120000})
        skill_tail = "UNIQUE_SKILL_END_MUST_REACH_MODEL"
        large_skill = "Follow this coding checklist.\n" * 900 + skill_tail
        self.assertGreater(len(large_skill), 20000)
        self.assertLess(len(large_skill.encode("utf-8")), 64000)
        skill = self.project / "skills" / "python-quality" / "SKILL.md"
        skill.write_text(
            "---\nname: python-quality\ndescription: Python implementation checklist\n---\n" + large_skill,
            encoding="utf-8",
        )
        scope = str(self.project.resolve())
        memory_ids = set()
        memory_tails = []
        for index in range(5):
            tail = f"UNIQUE_MEMORY_END_{index}_MUST_REACH_MODEL"
            memory_tails.append(tail)
            summary = "LongRecall knowledge " + "x" * 5000 + tail
            memory_ids.add(self.engine.memory.add(scope, f"LongRecall history {index}", summary))
        captured = []
        responses = [
            self.plan(),
            self.calls(
                ("load_skill", {"name": "python-quality"}),
                ("recall_memory", {"query": "LongRecall"}),
            ),
            {"role": "assistant", "content": "已读取技能与历史经验，本次无需修改。"},
        ]

        def fake_provider(messages, tools=None, cancel=None):
            captured.append(copy.deepcopy(messages))
            return responses[len(captured) - 1], {}

        with patch.object(DeepSeekProvider, "complete", side_effect=fake_provider):
            run = self.engine.create("inspect greeting", scope)
            run = self.wait_done(run["id"])

        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertEqual(len(captured), 3)
        self.assertNotIn(skill_tail, json.dumps(captured[1]))
        tool_messages = {message["tool_call_id"]: message["content"] for message in captured[2] if message["role"] == "tool"}
        self.assertEqual(json.loads(tool_messages["call-0"]), large_skill)
        recalled = json.loads(tool_messages["call-1"])
        self.assertGreater(len(tool_messages["call-1"]), 20000)
        self.assertEqual({item["id"] for item in recalled}, memory_ids)
        for tail in memory_tails:
            self.assertIn(tail, tool_messages["call-1"])
        loads = run["knowledge"]["skill_loads"]
        self.assertEqual(len(loads), 1)
        self.assertEqual((loads[0]["name"], loads[0]["source"]), ("python-quality", "tool"))
        recall = next(item for item in run["knowledge"]["memory_recalls"] if item["source"] == "tool")
        self.assertEqual({item["id"] for item in recall["items"]}, memory_ids)

    def test_offline_demo_reuses_previous_run_memory_and_records_real_skill_load(self):
        with patch.object(DeepSeekProvider, "complete", side_effect=AssertionError("offline demo must not call a model")):
            first = self.engine.create("calculator demo", mode="demo")
            first = self.wait_done(first["id"])
            self.assertEqual(first["status"], "ready", first.get("error"))
            saved_ids = {item["id"] for item in first["knowledge"]["memory_saves"]}
            self.assertTrue(saved_ids)
            second = self.engine.create("calculator demo", mode="demo")
            second = self.wait_done(second["id"])

        self.assertEqual(second["status"], "ready", second.get("error"))
        self.assertEqual(first["workspace"], second["workspace"])
        self.assertEqual(Path(second["workspace"]), (self.data / "demo-project").resolve())
        for run in (first, second):
            self.assertEqual(run["verification"]["status"], "passed")
            self.assertTrue(any(load["name"] == "python-quality" and load["source"] == "demo" for load in run["knowledge"]["skill_loads"]))
        recalled_ids = {item["id"] for record in second["knowledge"]["memory_recalls"] for item in record["items"]}
        self.assertTrue(saved_ids & recalled_ids, "second demo should recall memories saved by the first demo")

    def test_run_detail_persists_knowledge_but_history_excludes_memory_bodies(self):
        scope = str(self.project.resolve())
        body = "DETAILED_PROJECT_NOTE_BODY " * 80
        self.engine.memory.add(scope, "Permanent project convention", body, kind="note")
        responses = [
            (self.plan(), {}),
            ({"role": "assistant", "content": "已阅读项目约定，本次无需修改。"}, {}),
        ]
        with patch.object(DeepSeekProvider, "complete", side_effect=responses):
            run = self.engine.create("inspect greeting", scope)
            run = self.wait_done(run["id"])
        self.assertEqual(run["status"], "ready", run.get("error"))
        self.assertIn("DETAILED_PROJECT_NOTE_BODY", json.dumps(run["knowledge"]))
        self.assertNotIn("DETAILED_PROJECT_NOTE_BODY", json.dumps(self.engine.list_runs()))

        restarted = Engine(self.data, self.project, self.settings)
        self.engines.append(restarted)
        self.assertEqual(restarted.get_run(run["id"])["knowledge"], run["knowledge"])


if __name__ == "__main__":
    unittest.main()
