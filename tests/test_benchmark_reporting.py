from __future__ import annotations

import copy
import csv
import json
import shutil
import unittest
import uuid
from pathlib import Path

from benchmarks.reporting import summarize, write_reports


class BenchmarkReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.root = self.temp_root / ("benchmark-reporting-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)

    def tearDown(self):
        if not self.root.resolve().is_relative_to(self.temp_root):
            raise RuntimeError("test cleanup escaped its workspace directory")
        shutil.rmtree(self.root)

    @staticmethod
    def report(variants=("baseline",), tasks=3, mode="live"):
        return {"schema_version": 1, "mode": mode, "config": {"model": "mock-only", "max_workers": 1},
                "plan": [{"task_id": f"task-{i}", "category": "修复" if i % 2 else "实现",
                          "variant": variant, "repeat": 1}
                         for variant in variants for i in range(tasks)],
                "results": [], "notes": ["只含合成测试数据，不调用模型。"]}

    @staticmethod
    def result(planned, success=True, mode="live", metrics=None, latency=1.0):
        return {**planned, "success": success, "status": "ready" if mode == "live" else "checked",
                "latency_seconds": latency, "metrics": metrics or {},
                "evaluator": {"passed": success, "exit_code": 0 if success else 1}}

    def test_incomplete_batch_keeps_plan_denominator_and_pending_csv_rows(self):
        report = self.report(tasks=20)
        report["results"] = [self.result(p) for p in report["plan"][:3]]
        summary = summarize(report)
        group = summary["variants"]["baseline"]
        self.assertEqual((group["attempted"], group["planned"], group["pending"]), (3, 20, 17))
        self.assertIsNone(group["success_rate"])
        self.assertEqual(group["completed_success_rate"], 1)
        paths = write_reports(report, self.root)
        excerpt = Path(paths["resume_excerpt_md"]).read_text("utf-8")
        self.assertIn("3/20", excerpt)
        self.assertIn("暂不写总体成功率", excerpt)
        self.assertNotIn("100.0%", excerpt)
        with Path(paths["results_csv"]).open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(row["attempted"] == "True" for row in rows), 3)
        self.assertEqual(sum(row["status"] == "pending" for row in rows), 17)

    def test_missing_tokens_are_partial_and_failed_attempts_enter_means(self):
        report = self.report()
        metrics = [
            {"model_rounds": 2, "tool_calls": 1, "tool_errors": 0,
             "prompt_tokens": 9, "completion_tokens": 1, "reported_total_tokens": 10, "usage_complete": True},
            {"model_rounds": 8, "tool_calls": 5, "tool_errors": 3, "usage_complete": False},
            {"prompt_tokens": 4, "completion_tokens": 1, "reported_total_tokens": 5, "usage_complete": True},
        ]
        report["results"] = [self.result(p, success=i != 1, metrics=metrics[i], latency=(1, None, 5)[i])
                             for i, p in enumerate(report["plan"])]
        summary = summarize(report)
        group = summary["variants"]["baseline"]
        self.assertEqual(group["success_rate"], 2 / 3)
        self.assertEqual(group["means"]["model_rounds"], 5)
        self.assertEqual(group["metric_counts"]["model_rounds"], 2)
        self.assertEqual(group["means"]["reported_total_tokens"], 7.5)
        self.assertEqual(group["sums"]["reported_total_tokens"], 15)
        self.assertEqual(group["means"]["latency_seconds"], 3)
        self.assertEqual(group["metric_counts"]["latency_seconds"], 2)
        self.assertFalse(group["usage_complete"])
        self.assertEqual(group["token_accounting"], "partial_nonbilling")
        paths = write_reports(report, self.root)
        text = Path(paths["report_md"]).read_text("utf-8")
        self.assertIn("partial_nonbilling", text)
        self.assertIn("不等于底层 HTTP", text)
        self.assertIn("并非精确计费", text)

    def test_explicit_pending_result_is_not_an_attempt(self):
        report = self.report(tasks=2)
        pending = self.result(report["plan"][1], success=False)
        pending["status"] = "not_run"
        report["results"] = [self.result(report["plan"][0]), pending]
        summary = summarize(report)
        self.assertEqual((summary["attempted"], summary["pending"]), (1, 1))
        self.assertIsNone(summary["success_rate"])

    def test_live_success_requires_ready_and_private_evaluator_pass(self):
        report = self.report(tasks=2)
        first, second = [self.result(p) for p in report["plan"]]
        first["status"] = "failed"
        second["evaluator"]["passed"] = False
        report["results"] = [first, second]
        self.assertEqual(summarize(report)["success_rate"], 0)

    def test_selfcheck_cannot_become_model_success_rate_or_resume_claim(self):
        report = self.report(("reference", "seed"), tasks=20, mode="selfcheck")
        report["results"] = [self.result(p, success=p["variant"] == "reference", mode="selfcheck")
                             for p in report["plan"]]
        summary = summarize(report)
        self.assertFalse(summary["is_agent_evaluation"])
        self.assertIsNone(summary["success_rate"])
        self.assertIsNone(summary["completed_success_rate"])
        self.assertTrue(summary["selfcheck"]["passed"])
        self.assertEqual(summary["selfcheck"]["seed_rejected"], 20)
        self.assertEqual(set(summary["variants"]), {"reference", "seed"})
        paths = write_reports(report, self.root)
        excerpt = Path(paths["resume_excerpt_md"]).read_text("utf-8")
        self.assertIn("已搭建", excerpt)
        self.assertIn("自检通过，真实 Agent 结果未测", excerpt)
        self.assertNotIn("100.0%", excerpt)
        self.assertNotIn("20/20", excerpt)
        markdown = Path(paths["report_md"]).read_text("utf-8")
        self.assertIn("不测模型成功率", markdown)

    def test_selfcheck_timeout_does_not_count_as_correct_negative_rejection(self):
        report = self.report(("reference", "seed"), tasks=1, mode="selfcheck")
        reference = self.result(report["plan"][0], mode="selfcheck")
        seed = self.result(report["plan"][1], success=False, mode="selfcheck")
        seed["evaluator"].update(exit_code=-1, reason="timeout")
        report["results"] = [reference, seed]
        summary = summarize(report)
        self.assertFalse(summary["selfcheck"]["passed"])
        self.assertEqual(summary["selfcheck"]["seed_rejected"], 0)
        excerpt = Path(write_reports(report, self.root)["resume_excerpt_md"]).read_text("utf-8")
        self.assertIn("自检尚未全部通过", excerpt)
        self.assertIn("真实 Agent 结果未测", excerpt)

    def test_full_gain_is_only_written_for_complete_equal_task_groups(self):
        report = self.report(("baseline", "context", "prompt", "full"), tasks=2)
        report["results"] = [self.result(p, success=not (p["variant"] == "baseline" and p["task_id"] == "task-0"))
                             for p in report["plan"]]
        paths = write_reports(report, self.root)
        excerpt = Path(paths["resume_excerpt_md"]).read_text("utf-8")
        self.assertIn("Full Harness 独立验收通过 2/2（100.0%）", excerpt)
        self.assertIn("Baseline 为 1/2（50.0%）", excerpt)
        self.assertIn("仅描述本轮自建任务对照结果", excerpt)
        # Even complete Baseline/Full groups cannot mask an unfinished Context run.
        report["results"] = [r for r in report["results"] if not (r["variant"] == "context" and r["task_id"] == "task-0")]
        excerpt = Path(write_reports(report, self.root)["resume_excerpt_md"]).read_text("utf-8")
        self.assertNotIn("100.0%", excerpt)
        self.assertIn("7/8", excerpt)

    def test_full_below_baseline_does_not_invent_an_improvement(self):
        report = self.report(("baseline", "full"), tasks=2)
        report["results"] = [self.result(p, success=p["variant"] != "full") for p in report["plan"]]
        excerpt = Path(write_reports(report, self.root)["resume_excerpt_md"]).read_text("utf-8")
        self.assertIn("Full Harness 独立验收通过 0/2", excerpt)
        self.assertIn("未观察到 Full Harness 成功率高于 Baseline", excerpt)
        self.assertNotIn("验证了 Harness 的有效性", excerpt)

    def test_categories_compression_and_command_sequence_are_observations(self):
        report = self.report(tasks=2)
        metrics = {"context_compressions": 2, "test_command_results": [
            {"agent": "a", "exit_code": 1, "reason": None},
            {"agent": "orchestrator", "exit_code": 0, "reason": None}]}
        report["results"] = [self.result(report["plan"][0], metrics=metrics), self.result(report["plan"][1])]
        summary = summarize(report)
        self.assertEqual(set(summary["variants"]["baseline"]["categories"]), {"实现", "修复"})
        self.assertEqual(summary["compression_cases"], 1)
        self.assertEqual(summary["sums"]["context_compressions"], 2)
        self.assertEqual(summary["command_failure_then_success_cases"], 1)
        text = Path(write_reports(report, self.root)["report_md"]).read_text("utf-8")
        self.assertIn("不保证是同一条命令", text)
        self.assertIn("不证明某一组件", text)

    def test_duplicate_and_unplanned_results_are_rejected(self):
        report = self.report(tasks=1)
        result = self.result(report["plan"][0])
        report["results"] = [result, copy.deepcopy(result)]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            summarize(report)
        report["results"] = [{**result, "task_id": "outside-plan"}]
        with self.assertRaisesRegex(ValueError, "outside"):
            summarize(report)
        wrong_mode = self.report(("full",), mode="selfcheck")
        with self.assertRaisesRegex(ValueError, "variant"):
            summarize(wrong_mode)

    def test_outputs_are_utf8_absolute_paths_and_input_is_not_mutated(self):
        report = self.report(tasks=1)
        report["results"] = [self.result(report["plan"][0])]
        before = copy.deepcopy(report)
        paths = write_reports(report, self.root)
        self.assertEqual(report, before)
        self.assertEqual(set(paths), {"report_json", "report_md", "results_csv", "resume_excerpt_md"})
        for path in paths.values():
            self.assertTrue(Path(path).is_absolute())
            self.assertTrue(Path(path).read_bytes().decode("utf-8"))
        saved = json.loads(Path(paths["report_json"]).read_text("utf-8"))
        self.assertIn("summary", saved)
        self.assertEqual(saved["summary"]["planned"], 1)


if __name__ == "__main__":
    unittest.main()
