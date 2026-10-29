from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from examples import hooks_example
from xcode.hooks import HookBlocked, HookContext, HookDecision, HookRegistry
from xcode.server import load_hook_modules


class HookModuleTests(unittest.TestCase):
    def context(self, event, **data):
        return HookContext(event, "module-test-run", "worker", "/project", "live", data)

    def test_explicit_example_module_registers_named_hooks_and_executes_policies(self):
        registry = HookRegistry()
        with patch("xcode.server.importlib.import_module", return_value=hooks_example) as importer:
            load_hook_modules(registry, ["examples.hooks_example"])
        importer.assert_called_once_with("examples.hooks_example")
        self.assertEqual(registry.catalog(), [
            {"event": "before_model", "name": "编码与验证指引", "priority": 0},
            {"event": "before_tool", "name": "阻止删除工具", "priority": -10},
            {"event": "on_run_end", "name": "任务状态记录", "priority": 0},
        ])
        self.assertEqual(registry.emit(self.context("before_model")), ["避免无关改动，完成后给出验证证据。"])
        with self.assertRaisesRegex(HookBlocked, "delete_file"):
            registry.emit(self.context("before_tool", name="delete_file", arguments={"path": "file.py"}))
        # The example blocks this tool entry point, not arbitrary file edits.
        self.assertEqual(registry.emit(self.context("before_tool", name="write_file", arguments={"path": "file.py"})), [])
        with patch("builtins.print") as printer:
            self.assertEqual(registry.emit(self.context("on_run_end", status="ready", objective="private objective", output="private code")), [])
        printer.assert_called_once_with("[hook] run=module-test-run status=ready", flush=True)

    def test_invalid_module_names_are_rejected_before_import(self):
        for module_name in ("", None, [], "../untrusted", ".relative", "trailing.", "two..dots", "with/slash", "hyphen-name", " leading", "trailing ", "module;command", "module.py()", "123module"):
            with self.subTest(module_name=module_name):
                registry = HookRegistry()
                with patch("xcode.server.importlib.import_module") as importer:
                    with self.assertRaisesRegex(ValueError, "Python 模块名"):
                        load_hook_modules(registry, [module_name])
                importer.assert_not_called()
                self.assertEqual(registry.catalog(), [])

    def test_module_must_export_callable_registration_entry(self):
        for module in (SimpleNamespace(), SimpleNamespace(register_hooks=None), SimpleNamespace(register_hooks=True), SimpleNamespace(register_hooks="execute me")):
            with self.subTest(module=module):
                registry = HookRegistry()
                with patch("xcode.server.importlib.import_module", return_value=module):
                    with self.assertRaisesRegex(ValueError, r"register_hooks\(registry\)"):
                        load_hook_modules(registry, ["trusted.module"])
                self.assertEqual(registry.catalog(), [])

    def test_registration_exception_is_not_suppressed(self):
        failure = RuntimeError("invalid trusted hook configuration")

        def register(registry):
            raise failure

        with patch("xcode.server.importlib.import_module", return_value=SimpleNamespace(register_hooks=register)):
            with self.assertRaises(RuntimeError) as caught:
                load_hook_modules(HookRegistry(), ["trusted.broken"])
        self.assertIs(caught.exception, failure)

    def test_import_error_is_not_suppressed(self):
        failure = ModuleNotFoundError("trusted module dependency missing")
        with patch("xcode.server.importlib.import_module", side_effect=failure):
            with self.assertRaises(ModuleNotFoundError) as caught:
                load_hook_modules(HookRegistry(), ["trusted.missing"])
        self.assertIs(caught.exception, failure)

    def test_multiple_modules_register_in_requested_order(self):
        registry = HookRegistry()
        calls = []

        def first(registry):
            calls.append("register-first")
            registry.register("before_model", lambda context: HookDecision(system_message="first"), name="first")

        def second(registry):
            calls.append("register-second")
            registry.register("before_model", lambda context: HookDecision(system_message="second"), name="second")

        modules = [SimpleNamespace(register_hooks=first), SimpleNamespace(register_hooks=second)]
        with patch("xcode.server.importlib.import_module", side_effect=modules) as importer:
            load_hook_modules(registry, ["trusted.first", "trusted.second"])
        self.assertEqual(importer.call_args_list, [call("trusted.first"), call("trusted.second")])
        self.assertEqual(calls, ["register-first", "register-second"])
        self.assertEqual(registry.emit(self.context("before_model")), ["first", "second"])

    def test_duplicate_hook_names_fail_startup_without_overwriting(self):
        registry = HookRegistry()
        with patch("xcode.server.importlib.import_module", return_value=hooks_example):
            with self.assertRaisesRegex(ValueError, "Hook 已注册"):
                load_hook_modules(registry, ["examples.hooks_example", "examples.hooks_example"])
        self.assertEqual(len(registry.catalog()), 3)
        self.assertEqual(registry.emit(self.context("before_model")), ["避免无关改动，完成后给出验证证据。"])

    def test_empty_module_list_does_not_import_or_register(self):
        registry = HookRegistry()
        with patch("xcode.server.importlib.import_module") as importer:
            load_hook_modules(registry, [])
        importer.assert_not_called()
        self.assertEqual(registry.catalog(), [])


if __name__ == "__main__":
    unittest.main()
