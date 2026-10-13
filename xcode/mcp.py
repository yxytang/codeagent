from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import urllib.request


def process_env():
    # Do not forward API credentials to commands or stdio MCP children.
    return {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "LANG", "PYTHONPATH"}}


class MCPClient:
    """Synchronous MCP subset: initialize, tools/list, tools/call.

    stdio JSON lines and Streamable HTTP JSON/SSE POST responses. No legacy HTTP+SSE,
    OAuth, sampling, server-to-client requests, or resumable event streams.
    """
    def __init__(self, config, cwd, timeout=20):
        self.config, self.cwd, self.timeout = config, cwd, timeout
        self.process = None
        self.session = None
        self.protocol = "2025-06-18"
        self.sequence = 0
        self.lock = threading.RLock()
        self.responses = queue.Queue()

    def open(self):
        if self.config.get("transport", "stdio") == "stdio":
            self.process = subprocess.Popen(self.config["command"], cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=process_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def reader():
                try:
                    for line in self.process.stdout:
                        try:
                            self.responses.put(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                finally:
                    self.responses.put({"_closed": True})
            threading.Thread(target=reader, daemon=True).start()
        result = self.request("initialize", {"protocolVersion": self.protocol, "capabilities": {}, "clientInfo": {"name": "xcode", "version": "0.1.0"}})
        self.protocol = result.get("protocolVersion", self.protocol)
        self.request("notifications/initialized", notification=True)
        tools = []
        cursor = None
        for _ in range(10):
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return tools

    def request(self, method, params=None, notification=False):
        with self.lock:
            self.sequence += 1
            identifier = self.sequence
            payload = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            if not notification:
                payload["id"] = identifier
            if self.process:
                self.process.stdin.write(json.dumps(payload) + "\n")
                self.process.stdin.flush()
                if notification:
                    return {}
                import time
                deadline = time.monotonic() + self.timeout
                while True:
                    try:
                        message = self.responses.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        raise TimeoutError("MCP 请求超时") from None
                    if message.get("_closed"):
                        raise RuntimeError("MCP 子进程已退出")
                    if message.get("id") == identifier:
                        return self.unwrap(message)
                    if "id" in message and "method" in message:
                        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Client does not support server requests"}}) + "\n")
                        self.process.stdin.flush()
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if self.session:
                headers["Mcp-Session-Id"] = self.session
            if method != "initialize":
                headers["MCP-Protocol-Version"] = self.protocol
            token_var = self.config.get("bearer_env")
            if token_var:
                headers["Authorization"] = "Bearer " + os.getenv(token_var, "")
            req = urllib.request.Request(self.config["url"], data=json.dumps(payload).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if notification or response.status == 202:
                    return {}
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    # Only the response stream for this POST; notifications are ignored.
                    chunks = []
                    size = 0
                    for line in response:
                        size += len(line)
                        if size > 2 * 1024 * 1024:
                            raise ValueError("MCP 响应超过 2MB")
                        text = line.decode("utf-8").rstrip("\r\n")
                        if text.startswith("data:"):
                            chunks.append(text[5:].lstrip())
                        elif not text and chunks:
                            message = json.loads("\n".join(chunks))
                            chunks.clear()
                            if message.get("id") == identifier:
                                return self.unwrap(message)
                    raise RuntimeError("MCP SSE 流未返回对应结果")
                message = json.loads(response.read(2 * 1024 * 1024))
                if message.get("id") != identifier:
                    raise RuntimeError("MCP 响应 id 不匹配")
                return self.unwrap(message)

    @staticmethod
    def unwrap(message):
        if "error" in message:
            raise RuntimeError("MCP 错误：" + str(message["error"].get("message", "unknown"))[:600])
        return message.get("result", {})

    def call(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
