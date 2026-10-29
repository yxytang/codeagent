from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xcode.config import Settings


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def profile(self, identifier="other", **changes):
        return {"id": identifier, "label": "Other model", "base_url": "https://models.example/v1", "model": "coding-model", **changes}

    def test_legacy_migration_ignores_disk_key_and_preserves_budget(self):
        legacy = {"base_url": "https://legacy.example/v1/", "model": "old-model", "max_workers": 2, "max_tasks": 11, "api_key": "never-adopt-this-disk-key"}
        (self.root / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "legacy-env-key"}):
            settings = Settings(self.root)
            state = settings.snapshot(secret=True)
            self.assertEqual(state["model_profile"]["id"], "deepseek")
            self.assertEqual((state["base_url"], state["model"], state["max_workers"], state["max_tasks"]), ("https://legacy.example/v1", "old-model", 2, 11))
            self.assertEqual(state["api_key"], "legacy-env-key")
            settings.update({"timeout": 60})
            persisted = settings.path.read_text(encoding="utf-8")
            self.assertNotIn("never-adopt-this-disk-key", persisted)
            self.assertNotIn("legacy-env-key", persisted)
            self.assertEqual(json.loads(persisted)["default_model_id"], "deepseek")
            self.assertNotIn("api_key", json.loads(persisted)["model_profiles"][0])

    def test_new_profiles_have_independent_credentials_and_switch_defaults(self):
        settings = Settings(self.root)
        settings.update({"api_key": "first-profile-key"})
        settings.upsert_model(self.profile())
        self.assertFalse(settings.snapshot(model_profile_id="other")["has_api_key"])
        settings.upsert_model({"id": "other", "api_key": "second-profile-key"})
        first = settings.snapshot(secret=True)
        second = settings.snapshot(secret=True, model_profile_id="other")
        self.assertEqual(first["api_key"], "first-profile-key")
        self.assertEqual(second["api_key"], "second-profile-key")
        self.assertNotIn("first-profile-key", json.dumps(second))
        self.assertNotIn("second-profile-key", json.dumps(first))
        self.assertEqual(second["model_profile"]["id"], "other")
        self.assertEqual(second["default_model_id"], "deepseek")
        self.assertEqual(second["base_url"], "https://models.example/v1")
        self.assertTrue(second["supports_tools"])
        settings.update({"default_model_id": "other"})
        self.assertEqual(settings.api_key, "second-profile-key")
        self.assertEqual(settings.snapshot()["model"], "coding-model")
        self.assertNotIn("api_key", settings.snapshot())
        self.assertNotIn("first-profile-key", settings.path.read_text(encoding="utf-8"))
        self.assertNotIn("second-profile-key", settings.path.read_text(encoding="utf-8"))

    def test_legacy_environment_does_not_spill_into_added_profile(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "legacy-only-env"}):
            settings = Settings(self.root)
            settings.upsert_model(self.profile())
            self.assertEqual(settings.snapshot(secret=True)["api_key"], "legacy-only-env")
            self.assertEqual(settings.snapshot(secret=True, model_profile_id="other")["api_key"], "")
            settings.update({"default_model_id": "other", "api_key": "other-memory-key"})
            self.assertEqual(settings.snapshot(secret=True)["api_key"], "other-memory-key")
            settings.update({"api_key": ""})
            self.assertFalse(settings.snapshot()["has_api_key"])

    def test_url_change_detaches_old_memory_key_and_environment(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "old-env-key"}):
            settings = Settings(self.root)
            settings.update({"api_key": "old-memory-key"})
            settings.upsert_model({"id": "deepseek", "base_url": "https://different.example/v1", "api_key": "  "})
            state = settings.snapshot(secret=True)
            self.assertEqual(state["api_key"], "")
            self.assertEqual(state["model_profile"]["api_key_env"], "")
            self.assertFalse(state["has_api_key"])
            settings.upsert_model({"id": "deepseek", "base_url": "https://third.example/v1", "api_key": "new-target-key"})
            self.assertEqual(settings.snapshot(secret=True)["api_key"], "new-target-key")
            settings.upsert_model({"id": "deepseek", "base_url": "https://fourth.example/v1", "api_key_env": "DEEPSEEK_API_KEY"})
            self.assertEqual(settings.snapshot(secret=True)["api_key"], "old-env-key")

    def test_path_change_is_new_target_but_trailing_slash_and_model_change_keep_key(self):
        settings = Settings(self.root)
        settings.upsert_model(self.profile(api_key="same-target-key"))
        settings.upsert_model({"id": "other", "model": "another-model", "base_url": "https://models.example/v1/"})
        self.assertEqual(settings.snapshot(secret=True, model_profile_id="other")["api_key"], "same-target-key")
        settings.upsert_model({"id": "other", "base_url": "https://models.example/v2"})
        self.assertEqual(settings.snapshot(secret=True, model_profile_id="other")["api_key"], "")

    def test_legacy_url_update_does_not_forward_old_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "old-env-key"}):
            settings = Settings(self.root)
            settings.update({"api_key": "old-memory-key"})
            settings.update({"base_url": "https://new.example/v1"})
            self.assertFalse(settings.snapshot()["has_api_key"])
            self.assertEqual(settings.snapshot()["model_profile"]["api_key_env"], "")
            settings.update({"base_url": "https://newer.example/v1", "api_key": "explicit-replacement"})
            self.assertEqual(settings.snapshot(secret=True)["api_key"], "explicit-replacement")

    def test_blank_upsert_preserves_key_and_explicit_clear_also_detaches_env(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-key"}):
            settings = Settings(self.root)
            settings.update({"api_key": "memory-key"})
            settings.upsert_model({"id": "deepseek", "label": "Renamed", "api_key": "  "})
            self.assertEqual(settings.snapshot(secret=True)["api_key"], "memory-key")
            settings.upsert_model({"id": "deepseek", "clear_api_key": True})
            self.assertFalse(settings.snapshot()["has_api_key"])
            self.assertEqual(settings.snapshot()["model_profile"]["api_key_env"], "")

    def test_snapshot_is_independent_and_contains_only_selected_secret(self):
        settings = Settings(self.root)
        settings.update({"api_key": "frozen-key"})
        frozen = settings.snapshot(secret=True)
        settings.upsert_model(self.profile(api_key="other-key"))
        settings.update({"default_model_id": "other", "max_workers": 1})
        settings.delete_model("deepseek")
        self.assertEqual((frozen["model"], frozen["api_key"], frozen["max_workers"]), ("deepseek-chat", "frozen-key", 3))
        frozen["model_profiles"][0]["model"] = "tampered"
        self.assertEqual(settings.snapshot()["model"], "coding-model")

    def test_public_profiles_reload_without_in_memory_credentials(self):
        settings = Settings(self.root)
        settings.upsert_model(self.profile(api_key="ephemeral-key", requires_api_key=False))
        settings.update({"default_model_id": "other"})
        restarted = Settings(self.root)
        self.assertEqual(restarted.snapshot()["default_model_id"], "other")
        self.assertFalse(restarted.snapshot()["has_api_key"])
        self.assertFalse(restarted.snapshot()["requires_api_key"])
        # Even credentials maliciously placed inside a saved profile are ignored.
        persisted = json.loads(settings.path.read_text(encoding="utf-8"))
        persisted["api_key"] = "root-disk-key"
        persisted["model_profiles"][1]["api_key"] = "nested-disk-key"
        settings.path.write_text(json.dumps(persisted), encoding="utf-8")
        restarted = Settings(self.root)
        self.assertEqual(restarted.snapshot(secret=True)["api_key"], "")
        restarted.update({"max_tasks": 9})
        self.assertNotIn("disk-key", restarted.path.read_text(encoding="utf-8"))

    def test_redaction_covers_all_profiles_and_overridden_environment_credentials(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "legacy-env-secret", "CUSTOM_MODEL_KEY": "custom-env-secret"}):
            settings = Settings(self.root)
            settings.update({"api_key": "legacy-memory-secret"})
            settings.upsert_model(self.profile(api_key_env="CUSTOM_MODEL_KEY", api_key="custom-memory-secret"))
            text = " ".join(["legacy-env-secret", "custom-env-secret", "legacy-memory-secret", "custom-memory-secret"])
            self.assertEqual(settings.redact(text), " ".join(["[redacted]"] * 4))

    def test_invalid_ids_and_profile_payloads_fail_without_changing_configuration(self):
        settings = Settings(self.root)
        settings.update({"api_key": "preserved-key"})
        before = settings.snapshot(secret=True)
        invalid = [
            {"id": []}, {"id": "bad id"}, {"id": "x" * 65}, {"id": "deepseek", "label": ""},
            {"id": "deepseek", "label": "x" * 81}, {"id": "deepseek", "model": "x" * 201},
            {"id": "deepseek", "base_url": "file:///tmp/model"}, {"id": "deepseek", "base_url": "https://user:pass@example.test"},
            {"id": "deepseek", "base_url": "https://example.test/?secret=value"}, {"id": "deepseek", "base_url": "https://example.test:bad"},
            {"id": "deepseek", "supports_tools": "true"}, {"id": "deepseek", "requires_api_key": 1},
            {"id": "deepseek", "api_key_env": "NOT-A-NAME"}, {"id": "deepseek", "protocol": "unknown"},
            {"id": "deepseek", "api_key": 7}, {"id": "deepseek", "clear_api_key": "yes"},
            {"id": "deepseek", "has_api_key": True}, {"id": "deepseek", "extra": "field"},
        ]
        for data in invalid:
            with self.subTest(fields=list(data)), self.assertRaises(ValueError):
                settings.upsert_model(data)
            self.assertEqual(settings.snapshot(secret=True), before)
        for identifier in ("unknown", [], False):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                settings.snapshot(model_profile_id=identifier)
        for data in ({"default_model_id": "unknown"}, {"default_model_id": []}, {"model_profiles": []}, {"api_key": None}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                settings.update(data)

    def test_profile_limit_generated_id_and_delete_default(self):
        settings = Settings(self.root)
        generated = settings.upsert_model({"base_url": "http://localhost:8080/v1", "model": "local-model", "requires_api_key": False})
        identifier = generated["model_profiles"][1]["id"]
        self.assertTrue(identifier.startswith("model-"))
        for i in range(10):
            settings.upsert_model(self.profile(identifier=f"extra-{i}"))
        with self.assertRaises(ValueError):
            settings.upsert_model(self.profile(identifier="thirteenth"))
        settings.update({"default_model_id": identifier})
        settings.delete_model(identifier)
        self.assertEqual(settings.snapshot()["default_model_id"], "deepseek")
        for profile in settings.snapshot()["model_profiles"][1:]:
            settings.delete_model(profile["id"])
        with self.assertRaises(ValueError):
            settings.delete_model("deepseek")
        with self.assertRaises(ValueError):
            settings.delete_model("unknown")

    def test_duplicate_persisted_ids_are_rejected(self):
        settings = Settings(self.root)
        settings.update({"timeout": 50})
        data = json.loads(settings.path.read_text(encoding="utf-8"))
        data["model_profiles"].append(dict(data["model_profiles"][0]))
        settings.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            Settings(self.root)

    def test_failed_persistence_does_not_change_configuration_or_secret(self):
        settings = Settings(self.root)
        settings.update({"api_key": "original-key"})
        before = settings.snapshot(secret=True)
        saved = settings.path.read_text(encoding="utf-8")
        with patch.object(Path, "replace", side_effect=OSError("injected persistence failure")), self.assertRaises(OSError):
            settings.upsert_model({"id": "deepseek", "model": "changed", "api_key": "replacement-key"})
        self.assertEqual(settings.snapshot(secret=True), before)
        self.assertEqual(settings.path.read_text(encoding="utf-8"), saved)
        self.assertNotIn("replacement-key", settings.path.with_suffix(".tmp").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
