from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse


class Settings:
    """Persist public configuration; keep each profile's credentials in memory."""

    PROFILE_FIELDS = {"id", "label", "protocol", "base_url", "model", "supports_tools", "requires_api_key", "api_key_env"}

    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"
        self.lock = threading.RLock()
        self.api_keys = {}
        self.values = {
            "max_workers": 3,
            "max_steps": 40,
            "max_tasks": 8,
            "timeout": 45,
            "context_chars": 48000,
            "allowed_commands": [],
            "disabled_skills": [],
            "mcp_servers": [],
        }
        saved = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text("utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("设置文件必须是对象")
            self.values.update({k: v for k, v in saved.items() if k in self.values})
        if "model_profiles" in saved:
            profiles = saved["model_profiles"]
            if not isinstance(profiles, list) or any(not isinstance(p, dict) for p in profiles):
                raise ValueError("model_profiles 必须是配置对象数组")
            # Never adopt credentials from disk, including older files.
            self.values["model_profiles"] = [{k: v for k, v in p.items() if k in self.PROFILE_FIELDS} for p in profiles]
            self.values["default_model_id"] = saved.get("default_model_id", profiles[0].get("id") if profiles else "")
        else:
            self.values["model_profiles"] = [{
                "id": "deepseek", "label": "DeepSeek", "protocol": "openai_chat",
                "base_url": saved.get("base_url", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")),
                "model": saved.get("model", os.getenv("DEEPSEEK_MODEL", "deepseek-chat")),
                "supports_tools": True, "requires_api_key": True,
                "api_key_env": saved.get("api_key_env", "DEEPSEEK_API_KEY"),
            }]
            self.values["default_model_id"] = "deepseek"
        self._validate(self.values)

    @property
    def api_key(self):
        with self.lock:
            return self._profile_key(self._selected_profile(self.values, None))

    def _selected_profile(self, values, identifier):
        if identifier is not None and not isinstance(identifier, str):
            raise ValueError("模型配置 ID 必须为字符串")
        identifier = values["default_model_id"] if identifier is None else identifier
        profile = next((p for p in values["model_profiles"] if p["id"] == identifier), None)
        if profile is None:
            raise ValueError("模型配置不存在")
        return profile

    def _profile_key(self, profile):
        return self.api_keys.get(profile["id"], "") or (os.getenv(profile["api_key_env"], "").strip() if profile["api_key_env"] else "")

    def snapshot(self, secret=False, model_profile_id=None):
        with self.lock:
            selected = self._selected_profile(self.values, model_profile_id)
            result = json.loads(json.dumps(self.values))
            result["model_profiles"] = [{**p, "has_api_key": bool(self._profile_key(p))} for p in result["model_profiles"]]
            result["model_profile"] = next(dict(p) for p in result["model_profiles"] if p["id"] == selected["id"])
            for key in ("base_url", "model", "supports_tools", "requires_api_key", "has_api_key"):
                result[key] = result["model_profile"][key]
            if secret:
                result["api_key"] = self._profile_key(selected)
            return result

    def update(self, updates):
        with self.lock:
            if not isinstance(updates, dict):
                raise ValueError("设置必须为对象")
            if "model_profiles" in updates:
                raise ValueError("请使用模型配置接口 upsert_model 编辑 model_profiles")
            if set(updates) - (set(self.values) | {"api_key", "base_url", "model"}):
                raise ValueError("设置包含不支持的字段")
            if "api_key" in updates and not isinstance(updates["api_key"], str):
                raise ValueError("API Key 必须为字符串")
            values = json.loads(json.dumps(self.values))
            values.update({k: v for k, v in updates.items() if k in self.values})
            profile = self._selected_profile(values, None)
            previous_url = profile["base_url"]
            profile.update({k: updates[k] for k in ("base_url", "model") if k in updates})
            self._validate(values)
            keys = dict(self.api_keys)
            if profile["base_url"] != previous_url:
                keys.pop(profile["id"], None)
                profile["api_key_env"] = ""
            if "api_key" in updates:
                key = updates["api_key"].strip()
                if key:
                    keys[profile["id"]] = key
                else:
                    keys.pop(profile["id"], None)
                    profile["api_key_env"] = ""
            self._commit(values, keys)
            return self.snapshot()

    def upsert_model(self, data):
        with self.lock:
            if not isinstance(data, dict) or set(data) - (self.PROFILE_FIELDS | {"api_key", "clear_api_key"}):
                raise ValueError("模型配置必须为对象，且仅包含支持的字段")
            identifier = data.get("id", "")
            if not isinstance(identifier, str):
                raise ValueError("模型配置 ID 必须为字符串")
            identifier = identifier or "model-" + uuid.uuid4().hex[:12]
            if "api_key" in data and not isinstance(data["api_key"], str):
                raise ValueError("API Key 必须为字符串")
            if "clear_api_key" in data and not isinstance(data["clear_api_key"], bool):
                raise ValueError("clear_api_key 必须为布尔值")
            if data.get("clear_api_key") and data.get("api_key", "").strip():
                raise ValueError("不能同时清除和设置 API Key")
            values = json.loads(json.dumps(self.values))
            profile = next((p for p in values["model_profiles"] if p["id"] == identifier), None)
            previous_url = profile["base_url"] if profile else None
            if profile is None:
                profile = {"id": identifier, "label": identifier, "protocol": "openai_chat", "base_url": "", "model": "", "supports_tools": True, "requires_api_key": True, "api_key_env": ""}
                values["model_profiles"].append(profile)
            profile.update({k: v for k, v in data.items() if k in self.PROFILE_FIELDS and k != "id"})
            self._validate(values)
            keys = dict(self.api_keys)
            if previous_url is not None and profile["base_url"] != previous_url:
                keys.pop(identifier, None)
                profile["api_key_env"] = data.get("api_key_env", "")
            if data.get("clear_api_key"):
                keys.pop(identifier, None)
                profile["api_key_env"] = data.get("api_key_env", "")
            if data.get("api_key", "").strip():
                keys[identifier] = data["api_key"].strip()
            self._commit(values, keys)
            return self.snapshot()

    def delete_model(self, identifier):
        with self.lock:
            if not isinstance(identifier, str):
                raise ValueError("模型配置 ID 必须为字符串")
            self._selected_profile(self.values, identifier)
            if len(self.values["model_profiles"]) == 1:
                raise ValueError("至少保留一个模型配置")
            values = json.loads(json.dumps(self.values))
            values["model_profiles"] = [p for p in values["model_profiles"] if p["id"] != identifier]
            if values["default_model_id"] == identifier:
                values["default_model_id"] = values["model_profiles"][0]["id"]
            keys = dict(self.api_keys)
            keys.pop(identifier, None)
            self._commit(values, keys)
            return self.snapshot()

    def redact(self, text):
        with self.lock:
            keys = set(self.api_keys.values()) | {self._profile_key(p) for p in self.values["model_profiles"]}
            # Also mask configured environment keys when an in-memory key overrides them.
            keys.update(os.getenv(p["api_key_env"], "").strip() for p in self.values["model_profiles"] if p["api_key_env"])
            result = str(text)
            for key in sorted((key for key in keys if key), key=len, reverse=True):
                result = result.replace(key, "[redacted]")
            return result

    def _commit(self, values, keys):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.path)
        self.values = values
        self.api_keys = keys

    def _validate(self, values):
        profiles = values["model_profiles"]
        if not isinstance(profiles, list) or not 1 <= len(profiles) <= 12:
            raise ValueError("模型配置必须为数组，且保留 1–12 项")
        identifiers = set()
        for profile in profiles:
            if not isinstance(profile, dict) or set(profile) != self.PROFILE_FIELDS:
                raise ValueError("模型配置字段不完整或包含不支持的字段")
            identifier = profile["id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) or identifier in identifiers:
                raise ValueError("模型配置 ID 必须是唯一的 1–64 位字母数字、下划线或连字符")
            identifiers.add(identifier)
            for field, maximum in (("label", 80), ("model", 200), ("base_url", 2048)):
                if not isinstance(profile[field], str) or not profile[field].strip() or len(profile[field]) > maximum:
                    raise ValueError(f"模型配置 {field} 必须为 1–{maximum} 个字符")
                profile[field] = profile[field].strip()
                if field == "base_url":
                    profile[field] = profile[field].rstrip("/")
            if profile["protocol"] != "openai_chat":
                raise ValueError("目前仅支持 openai_chat 协议")
            for field in ("supports_tools", "requires_api_key"):
                if not isinstance(profile[field], bool):
                    raise ValueError(f"{field} 必须为布尔值")
            env = profile["api_key_env"]
            if not isinstance(env, str) or len(env) > 128 or (env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env)):
                raise ValueError("api_key_env 必须为空或合法环境变量名")
            parsed = urlparse(profile["base_url"])
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in profile["base_url"]):
                raise ValueError("base_url 必须是无凭据、无查询参数的 http(s) URL")
            try:
                parsed.port
            except ValueError:
                raise ValueError("base_url 端口无效") from None
        if not isinstance(values["default_model_id"], str) or values["default_model_id"] not in identifiers:
            raise ValueError("默认模型配置不存在")
        for key, low, high in (("max_workers", 1, 6), ("max_steps", 2, 40), ("max_tasks", 1, 12), ("timeout", 5, 120), ("context_chars", 8000, 120000)):
            values[key] = int(values[key])
            if not low <= values[key] <= high:
                raise ValueError(f"{key} 必须在 {low}–{high} 之间")
        commands = values["allowed_commands"]
        if not isinstance(commands, list) or any(not isinstance(c, list) or not c or any(not isinstance(a, str) for a in c) for c in commands):
            raise ValueError("allowed_commands 必须是命令参数数组的数组")
        if not isinstance(values["disabled_skills"], list) or any(not isinstance(s, str) for s in values["disabled_skills"]):
            raise ValueError("disabled_skills 必须是字符串数组")
        if not isinstance(values["mcp_servers"], list) or len(values["mcp_servers"]) > 8:
            raise ValueError("mcp_servers 必须是数组，最多 8 个服务")
        names = set()
        for server in values["mcp_servers"]:
            if not isinstance(server, dict) or not isinstance(server.get("name"), str) or not re.fullmatch(r"[a-zA-Z0-9]{1,24}", server["name"]) or server["name"] in names:
                raise ValueError("MCP 服务名必须为唯一的字母数字字符串")
            if set(server) - {"name", "transport", "command", "url", "bearer_env", "allowed_tools", "enabled"}:
                raise ValueError("MCP 配置包含不支持的字段；凭据必须通过 bearer_env 指定环境变量")
            if "bearer_env" in server and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(server["bearer_env"])):
                raise ValueError("bearer_env 必须为环境变量名")
            if "enabled" in server and not isinstance(server["enabled"], bool):
                raise ValueError("MCP enabled 必须为布尔值")
            names.add(server["name"])
            transport = server.get("transport", "stdio")
            if transport == "stdio":
                if not isinstance(server.get("command"), list) or not server["command"] or any(not isinstance(a, str) for a in server["command"]):
                    raise ValueError("MCP stdio command 必须是非空参数数组")
            elif transport == "http":
                url = urlparse(str(server.get("url", "")))
                if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                    raise ValueError("MCP HTTP URL 无效")
            else:
                raise ValueError("仅支持 stdio / http MCP 传输")
            if not isinstance(server.get("allowed_tools", []), list) or any(not isinstance(t, str) for t in server.get("allowed_tools", [])):
                raise ValueError("MCP allowed_tools 必须为工具名数组")
