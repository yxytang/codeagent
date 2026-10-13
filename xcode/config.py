from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse


class Settings:
    """Persist public settings; credentials live only in process memory / env."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"
        self.lock = threading.RLock()
        self.values = {
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            "max_workers": 3,
            "max_steps": 16,
            "max_tasks": 8,
            "max_depth": 2,
            "timeout": 45,
            "context_chars": 48000,
            "allowed_commands": [],
            "disabled_skills": [],
            "mcp_servers": [],
        }
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if self.path.exists():
            saved = json.loads(self.path.read_text("utf-8"))
            self.values.update({k: v for k, v in saved.items() if k in self.values})

    def snapshot(self, secret=False):
        with self.lock:
            result = json.loads(json.dumps(self.values))
            result["api_key" if secret else "has_api_key"] = self.api_key if secret else bool(self.api_key)
            return result

    def update(self, updates):
        with self.lock:
            if "api_key" in updates and not isinstance(updates["api_key"], str):
                raise ValueError("API Key 必须为字符串")
            values = {**self.values, **{k: v for k, v in updates.items() if k in self.values}}
            parsed = urlparse(str(values["base_url"]))
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base_url 必须是无凭据、无查询参数的 http(s) URL")
            if not isinstance(values["model"], str) or not values["model"].strip():
                raise ValueError("模型名不能为空")
            for key, low, high in (("max_workers", 1, 6), ("max_steps", 2, 40), ("max_tasks", 1, 12), ("max_depth", 1, 3), ("timeout", 5, 120), ("context_chars", 8000, 120000)):
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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), "utf-8")
            temp.replace(self.path)
            self.values = values
            if "api_key" in updates:
                self.api_key = updates["api_key"].strip()
            return self.snapshot()
