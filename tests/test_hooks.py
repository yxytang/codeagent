from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

from xcode.hooks import EVENTS, HookBlocked, HookContext, HookDecision, HookRegistry
from xcode.provider import Cancelled


class HookRegistryTests(unittest.TestCase):
    def context(self, event="before_tool", **data):
        return HookContext(event, "run-1", "worker-1", "/project", "live", data)

    def test_priority_and_registration_order_are_stable(self):
        registry = HookRegistry()
        calls = []
        registry.register("before_tool", lambda context: calls.append("late"), name="late", priority=5)
        registry.register("before_tool", lambda context: calls.append("first"), name="first", priority=-1)
        registry.register("before_tool", lambda context: calls.append("second"), name="second", priority=-1)
        reports = []
        self.assertEqual(registry.emit(self.context(), reports.append), [])
        self.assertEqual(calls, ["first", "second", "late"])
        self.assertEqual([record["name"] for record in reports], calls)
        for record in reports:
            self.assertEqual(set(record), {"event", "name", "status", "reason", "elapsed_ms"})
            self.assertEqual(record["status"], "ok")
            self.assertGreaterEqual(record["elapsed_ms"], 0)

    def test_each_callback_gets_independent_nested_context(self):
        registry = HookRegistry()
        original = self.context(arguments={"paths": ["first.py"]})
        observed = []

        def mutate(context):
            context.data["arguments"]["paths"].append("injected.py")
            context.data["extra"] = True

        def observe(context):
            observed.append(context.data)
            with self.assertRaises(FrozenInstanceError):
                context.run_id = "another-run"

        registry.register("before_tool", mutate)
        registry.register("before_tool", observe)
        registry.emit(original)
        self.assertEqual(original.data, {"arguments": {"paths": ["first.py"]}})
        self.assertEqual(observed, [original.data])
        self.assertIsNot(observed[0], original.data)

    def test_snapshot_members_survive_removal_and_reject_changes(self):
        registry = HookRegistry()
        called = []
        registry.register("before_tool", lambda context: called.append("original"), name="original")
        frozen = registry.snapshot()
        self.assertTrue(registry.unregister("before_tool", "original"))
        self.assertFalse(registry.unregister("before_tool", "missing"))
        registry.register("before_tool", lambda context: called.append("new"), name="new")
        frozen.emit(self.context())
        registry.emit(self.context())
        self.assertEqual(called, ["original", "new"])
        with self.assertRaises(RuntimeError):
            frozen.register("before_tool", lambda context: None)
        with self.assertRaises(RuntimeError):
            frozen.unregister("before_tool", "original")
        catalog = frozen.catalog()
        catalog[0]["name"] = "external mutation"
        self.assertEqual(frozen.catalog(), [{"event": "before_tool", "name": "original", "priority": 0}])

    def test_duplicate_names_are_rejected_per_event(self):
        registry = HookRegistry()

        def observe(context):
            pass

        self.assertEqual(registry.register("before_tool", observe), "observe")
        with self.assertRaises(ValueError):
            registry.register("before_tool", observe)
        registry.register("after_tool", observe)
        self.assertEqual(len(registry.catalog()), 2)

    def test_registration_rejects_invalid_events_handlers_names_and_priorities(self):
        registry = HookRegistry()
        for event in ("unknown", None, ["before_tool"]):
            with self.subTest(event=event), self.assertRaises(ValueError):
                registry.register(event, lambda context: None)
        with self.assertRaises(TypeError):
            registry.register("before_tool", None)
        for name in ("", "  ", 12):
            with self.subTest(name=name), self.assertRaises(ValueError):
                registry.register("before_tool", lambda context: None, name=name)
        for priority in (True, 1.2, "first"):
            with self.subTest(priority=priority), self.assertRaises(TypeError):
                registry.register("before_tool", lambda context: None, priority=priority)

    def test_explicit_pre_denial_stops_later_callbacks(self):
        for event in ("on_run_start", "before_model", "before_tool"):
            with self.subTest(event=event):
                registry = HookRegistry()
                calls = []
                registry.register(event, lambda context: HookDecision(deny="outside workspace"), name="policy")
                registry.register(event, lambda context: calls.append("should not run"), name="later")
                reports = []
                with self.assertRaisesRegex(HookBlocked, "outside workspace"):
                    registry.emit(self.context(event), reports.append)
                self.assertEqual(calls, [])
                self.assertEqual([(item["name"], item["status"]) for item in reports], [("policy", "denied")])

    def test_before_model_injections_preserve_order_and_enforce_total_limit(self):
        registry = HookRegistry()
        registry.register("before_model", lambda context: HookDecision(system_message="a" * 8000), name="a")
        registry.register("before_model", lambda context: HookDecision(system_message="b" * 8000), name="b")
        messages = registry.emit(self.context("before_model"))
        self.assertEqual(messages, ["a" * 8000, "b" * 8000])
        registry.register("before_model", lambda context: HookDecision(system_message="c"), name="overflow")
        reports = []
        with self.assertRaisesRegex(HookBlocked, "16000"):
            registry.emit(self.context("before_model"), reports.append)
        self.assertEqual([item["status"] for item in reports], ["ok", "ok", "error"])

    def test_invalid_pre_results_fail_closed_without_retry(self):
        invalid = [
            False,
            {},
            "injected directly",
            HookDecision(deny=""),
            HookDecision(deny=123),
            HookDecision(system_message=""),
            HookDecision(system_message="x" * 8001),
            HookDecision(system_message=123),
        ]
        for decision in invalid:
            with self.subTest(decision=repr(decision)[:100]):
                registry = HookRegistry()
                calls = []

                def invalid_handler(context):
                    calls.append(context.event)
                    return decision

                registry.register("before_model", invalid_handler)
                reports = []
                with self.assertRaises(HookBlocked):
                    registry.emit(self.context("before_model"), reports.append)
                self.assertEqual(calls, ["before_model"])
                self.assertEqual(reports[0]["status"], "error")

    def test_only_before_model_may_inject(self):
        for event in EVENTS:
            if event == "before_model":
                continue
            with self.subTest(event=event):
                registry = HookRegistry()
                registry.register(event, lambda context: HookDecision(system_message="extra context"))
                reports = []
                if event in ("on_run_start", "before_tool"):
                    with self.assertRaises(HookBlocked):
                        registry.emit(self.context(event), reports.append)
                else:
                    self.assertEqual(registry.emit(self.context(event), reports.append), [])
                self.assertEqual(reports[0]["status"], "error")

    def test_pre_exceptions_block_and_cancellation_retains_identity(self):
        for exception in (RuntimeError("broken policy"), HookBlocked("raised directly"), Cancelled("cancelled")):
            with self.subTest(exception=exception):
                registry = HookRegistry()
                calls = []

                def fail(context):
                    calls.append(context.event)
                    raise exception

                registry.register("before_tool", fail)
                reports = []
                expected = Cancelled if isinstance(exception, Cancelled) else HookBlocked
                with self.assertRaises(expected) as caught:
                    registry.emit(self.context(), reports.append)
                if isinstance(exception, Cancelled):
                    self.assertIs(caught.exception, exception)
                self.assertEqual(calls, ["before_tool"])
                self.assertEqual(reports[0]["status"], "error")

    def test_post_exceptions_and_illegal_decisions_preserve_completed_result(self):
        for event in ("after_model", "after_tool", "on_tool_error", "on_run_end"):
            for failure in (RuntimeError("audit failed"), Cancelled("late cancellation"), HookBlocked("late block"), "invalid", HookDecision(deny="too late")):
                with self.subTest(event=event, failure=failure):
                    registry = HookRegistry()
                    calls = []

                    def fail(context):
                        calls.append("failure")
                        if isinstance(failure, Exception):
                            raise failure
                        return failure

                    registry.register(event, fail)
                    registry.register(event, lambda context: calls.append("next"), name="next")
                    reports = []
                    self.assertEqual(registry.emit(self.context(event), reports.append), [])
                    self.assertEqual(calls, ["failure", "next"])
                    self.assertEqual([item["status"] for item in reports], ["error", "ok"])

    def test_report_failures_do_not_override_result_or_policy(self):
        def broken_report(record):
            raise RuntimeError("storage unavailable")

        registry = HookRegistry()
        registry.register("before_model", lambda context: HookDecision(system_message="context"))
        self.assertEqual(registry.emit(self.context("before_model"), broken_report), ["context"])
        registry.register("before_tool", lambda context: HookDecision(deny="policy denied"))
        with self.assertRaisesRegex(HookBlocked, "policy denied"):
            registry.emit(self.context(), broken_report)
        registry.register("after_tool", lambda context: HookDecision(deny="late"))
        self.assertEqual(registry.emit(self.context("after_tool"), broken_report), [])

    def test_callback_executes_without_registry_lock_and_uses_fixed_emission_members(self):
        registry = HookRegistry()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def blocking(context):
            calls.append("original")
            started.set()
            if not release.wait(3):
                raise RuntimeError("registration was blocked by callback")

        registry.register("before_tool", blocking, name="original")
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(registry.emit, self.context())
            self.assertTrue(started.wait(3))
            registration = pool.submit(registry.register, "before_tool", lambda context: calls.append("new"), name="new")
            try:
                self.assertEqual(registration.result(timeout=2), "new")
                self.assertTrue(registry.unregister("before_tool", "original"))
            finally:
                release.set()
            self.assertEqual(running.result(timeout=3), [])
        self.assertEqual(calls, ["original"])
        registry.emit(self.context())
        self.assertEqual(calls, ["original", "new"])

    def test_concurrent_emissions_keep_contexts_independent(self):
        registry = HookRegistry()
        barrier = threading.Barrier(4)
        outputs = []
        lock = threading.Lock()

        def observe(context):
            context.data["values"].append(context.run_id)
            barrier.wait(timeout=3)
            with lock:
                outputs.append(context.data["values"][:])

        registry.register("after_tool", observe)
        contexts = [HookContext("after_tool", str(index), "agent", "/project", "live", {"values": []}) for index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(registry.emit, contexts))
        self.assertEqual(results, [[], [], [], []])
        self.assertCountEqual(outputs, [["0"], ["1"], ["2"], ["3"]])
        self.assertTrue(all(context.data == {"values": []} for context in contexts))


if __name__ == "__main__":
    unittest.main()
