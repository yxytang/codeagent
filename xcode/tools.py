from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from .mcp import process_env
from .provider import Cancelled
from .workspace import MAX_FILE_BYTES, inventory, safe_path


def tool(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


STRING = {"type": "string"}
BUILTINS = [
    tool("list_files", "列出隔离工作区中的文件", {"prefix": STRING}),
    tool("read_file", "读取 UTF-8 文件，支持起始行与行数", {"path": STRING, "start_line": {"type": "integer"}, "lines": {"type": "integer"}}, ["path"]),
    tool("search_files", "在代码文件中搜索字面量", {"query": STRING}, ["query"]),
    tool("write_file", "在自己的隔离工作区创建或完整覆写 UTF-8 文件", {"path": STRING, "content": STRING}, ["path", "content"]),
    tool("delete_file", "在隔离工作区删除文件", {"path": STRING}, ["path"]),
    tool("recall_memory", "检索同一项目的历史任务记忆", {"query": STRING}, ["query"]),
    tool("load_skill", "按需读取已启用技能全文", {"name": STRING}, ["name"]),
    tool("run_command", "运行用户配置的精确允许的参数数组，无 shell", {"argv": {"type": "array", "items": STRING}}, ["argv"]),
]


def execute_command(argv, cwd, cancel=None, timeout=60):
    cancel = cancel or threading.Event()
    # File redirection avoids pipe deadlock; output is bounded by a monitored file size.
    log = Path(cwd) / ".xcode" / "command.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("wb") as output:
        process = subprocess.Popen(argv, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, env=process_env(), shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        reason = None
        while process.poll() is None:
            if cancel.wait(0.1):
                reason = "cancelled"
                break
            if time.monotonic() - started > timeout:
                reason = "timeout"
                break
            if log.stat().st_size > 2 * 1024 * 1024:
                reason = "output_limit"
                break
        if reason:
            process.kill()
        process.wait()
    with log.open("rb") as stream:
        text = stream.read(12000).decode("utf-8", errors="replace")
    if reason == "cancelled":
        raise Cancelled("任务已取消")
    return {"argv": argv, "exit_code": process.returncode, "output": text, "reason": reason}


class Toolbox:
    def __init__(self, root, memory, scope, skills, settings, cancel, allow_commands, external=None):
        self.root, self.memory, self.scope, self.skills = root, memory, scope, skills
        self.settings, self.cancel, self.allow_commands = settings, cancel, allow_commands
        self.external = external or {}

    def schemas(self):
        builtins = [t for t in BUILTINS if self.allow_commands or t["function"]["name"] != "run_command"]
        return builtins + [value[0] for value in self.external.values()]

    def call(self, name, args):
        if self.cancel.is_set():
            raise Cancelled("任务已取消")
        if not isinstance(args, dict):
            raise ValueError("工具参数必须为对象")
        if name in self.external:
            _, client, original = self.external[name]
            return client.call(original, args)
        if name == "list_files":
            prefix = str(args.get("prefix", ""))
            return [n for n in inventory(self.root) if n.startswith(prefix)]
        if name == "read_file":
            path = safe_path(self.root, args["path"])
            text = path.read_text("utf-8")
            start = max(1, int(args.get("start_line", 1)))
            count = min(max(1, int(args.get("lines", 200))), 300)
            rows = text.splitlines()
            return {"path": args["path"], "total_lines": len(rows), "text": "\n".join(f"{i + start}: {line}" for i, line in enumerate(rows[start - 1:start - 1 + count]))[:18000]}
        if name == "search_files":
            query = str(args["query"])
            if not query:
                raise ValueError("搜索词不能为空")
            hits = []
            for path, data in inventory(self.root).items():
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for line, textline in enumerate(text.splitlines(), 1):
                    if query.casefold() in textline.casefold():
                        hits.append({"path": path, "line": line, "text": textline[:400]})
                        if len(hits) >= 40:
                            return hits
            return hits
        if name == "write_file":
            content = args["content"]
            if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
                raise ValueError("文件内容必须是文本，最大 1MB")
            path = safe_path(self.root, args["path"])
            if path.exists():
                path.read_bytes().decode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, "utf-8", newline="")
            return {"written": args["path"], "bytes": len(content.encode())}
        if name == "delete_file":
            path = safe_path(self.root, args["path"])
            if path.exists():
                path.read_bytes().decode("utf-8")
            path.unlink(missing_ok=True)
            return {"deleted": args["path"]}
        if name == "recall_memory":
            return self.memory.search(str(args["query"]), self.scope, limit=5)
        if name == "load_skill":
            return self.skills.load(args["name"])
        if name == "run_command":
            argv = args["argv"]
            if not self.allow_commands or argv not in self.settings["allowed_commands"]:
                raise ValueError("命令未被精确授权：请在设置中添加参数数组，并在创建任务时启用校验")
            return execute_command(argv, self.root, self.cancel)
        raise ValueError("未知工具：" + name)


def changed_files(root, baseline):
    current = inventory(root)
    changes = {}
    for name in current.keys() | baseline.keys():
        if current.get(name) != baseline.get(name):
            if baseline.get(name) is not None:
                baseline[name].decode("utf-8")
            changes[name] = current[name].decode("utf-8") if name in current else None
    return changes
