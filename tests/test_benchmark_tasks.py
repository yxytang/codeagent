"""Acceptance evidence for the benchmark itself; no model calls are made."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from benchmarks.task_suite import get_cases


class BenchmarkTaskTests(unittest.TestCase):
    def run_case(self, case, *, reference=False, overrides=None, public=False):
        with tempfile.TemporaryDirectory(prefix="xcode-task-test-") as directory:
            root = Path(directory)
            candidate = root / "candidate"
            candidate.mkdir()
            files = dict(case.files)
            if reference:
                files.update(case.solution)
            files.update(overrides or {})
            for name, content in files.items():
                path = candidate / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            # Evaluator-only checks are a sibling, never candidate input.
            grader = root / "grader.py"
            grader.write_text(case.grader_source, encoding="utf-8")
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            command = ([sys.executable, "-I", "-B", "-m", "unittest", "discover",
                        "-s", ".", "-p", "test*.py"] if public else
                       [sys.executable, "-I", "-B", str(grader)])
            return subprocess.run(
                command, cwd=candidate,
                env=environment, capture_output=True, text=True, timeout=45,
            )

    def run_grader(self, case, *, reference=False, overrides=None):
        return self.run_case(case, reference=reference, overrides=overrides)

    def test_suite_shape_and_agent_visible_files(self):
        cases = get_cases()
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({case.id for case in cases}), 20)
        self.assertEqual(Counter(case.category for case in cases), {
            "code_understanding": 4, "bug_fix": 4, "feature": 4,
            "refactor": 4, "test_writing": 4,
        })
        for case in cases:
            with self.subTest(task=case.id):
                self.assertTrue(case.objective)
                self.assertTrue(case.solution)
                self.assertLessEqual(len(case.files), 3)
                for name in case.files:
                    self.assertFalse(Path(name).is_absolute())
                    self.assertNotIn("..", Path(name).parts)
                    self.assertNotIn("grader", name)
                    self.assertNotIn("solution", name)
                for name, source in {**case.files, **case.solution}.items():
                    if name.endswith(".py"):
                        compile(source, case.id + "/" + name, "exec")
                compile(case.grader_source, case.id + "-grader.py", "exec")

    def test_every_reference_solution_passes_private_acceptance(self):
        for case in get_cases():
            with self.subTest(task=case.id):
                result = self.run_grader(case, reference=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_unmodified_seed_fails_private_acceptance(self):
        for case in get_cases():
            with self.subTest(task=case.id):
                result = self.run_grader(case)
                self.assertNotEqual(result.returncode, 0, case.id + " seed unexpectedly passed")

    def test_every_reference_solution_runs_and_passes_public_tests(self):
        for case in get_cases():
            with self.subTest(task=case.id):
                result = self.run_case(case, reference=True, public=True)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                discovered = re.search(r"Ran (\d+) tests?", output)
                self.assertIsNotNone(discovered, output)
                self.assertGreater(int(discovered.group(1)), 0, output)

    def test_understanding_public_contract_does_not_expose_semantic_answers(self):
        wrong_answers = {
            "understand-01": {"results": [0] * 6, "compute_calls": 0, "keys": []},
            "understand-02": {"balances": {}, "accepted": []},
            "understand-03": {"ready": [], "waiting": [], "blocked": []},
            "understand-04": [{"result": "incorrect", "trace": []} for _ in range(4)],
        }
        for case in get_cases()[:4]:
            with self.subTest(task=case.id):
                override = {"answers.json": json.dumps(wrong_answers[case.id])}
                public = self.run_case(case, overrides=override, public=True)
                self.assertEqual(public.returncode, 0, public.stdout + public.stderr)
                private = self.run_grader(case, overrides=override)
                self.assertNotEqual(private.returncode, 0, "incorrect answer passed private checks")

    def test_understanding_public_contract_rejects_wrong_value_types(self):
        malformed = {
            "understand-01": {"results": [0] * 6, "compute_calls": True, "keys": []},
            "understand-02": {"balances": {"cash": False}, "accepted": []},
            "understand-03": {"ready": [1], "waiting": [], "blocked": []},
            "understand-04": [{"result": 3, "trace": []} for _ in range(4)],
        }
        for case in get_cases()[:4]:
            with self.subTest(task=case.id):
                result = self.run_case(
                    case, overrides={"answers.json": json.dumps(malformed[case.id])}, public=True,
                )
                self.assertNotEqual(result.returncode, 0, "invalid answer structure passed")

    def test_understanding_public_contract_rejects_source_and_input_changes(self):
        for case in get_cases()[:4]:
            for name, source in case.files.items():
                if name == "test_public.py":
                    continue
                with self.subTest(task=case.id, path=name):
                    result = self.run_case(
                        case, reference=True, overrides={name: source + "\n"}, public=True,
                    )
                    self.assertNotEqual(result.returncode, 0, "modified input passed public checks")

    def test_understanding_private_contract_protects_the_public_test_file(self):
        for case in get_cases()[:4]:
            with self.subTest(task=case.id):
                result = self.run_grader(
                    case, reference=True,
                    overrides={"test_public.py": "import unittest\n# checks removed\n"},
                )
                self.assertNotEqual(result.returncode, 0, "modified public test passed private checks")

    def test_repository_wrapper_accepts_legacy_implicit_none_return(self):
        case = next(case for case in get_cases() if case.id == "refactor-02")
        source = case.solution["storage.py"]
        wrapper = "return TodoRepository(path).save(items)"
        self.assertIn(wrapper, source)
        source = source.replace(wrapper, "TodoRepository(path).save(items)")
        result = self.run_grader(case, reference=True, overrides={"storage.py": source})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repository_wrapper_must_delegate_even_if_direct_io_is_correct(self):
        case = next(case for case in get_cases() if case.id == "refactor-02")
        source = case.solution["storage.py"].replace(
            "return TodoRepository(path).save(items)",
            "Path(path).write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')",
        )
        result = self.run_grader(case, reference=True, overrides={"storage.py": source})
        self.assertNotEqual(result.returncode, 0, "non-delegating legacy wrapper passed")
        self.assertIn("Expected 'save' to be called once", result.stdout + result.stderr)

    def test_get_cases_returns_independent_mappings(self):
        first, second = get_cases(), get_cases()
        first[0].files["extra.txt"] = "mutation"
        self.assertNotIn("extra.txt", second[0].files)


if __name__ == "__main__":
    unittest.main()
