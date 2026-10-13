from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request


class Cancelled(Exception):
    pass


class DeepSeekProvider:
    """Chat Completions tool loop transport, with bounded retry and timeout."""

    def __init__(self, settings: dict):
        self.settings = settings

    def complete(self, messages, tools=None, cancel=None):
        cancel = cancel or threading.Event()
        if cancel.is_set():
            raise Cancelled("任务已取消")
        if not self.settings.get("api_key"):
            raise ValueError("请在设置页填写 DeepSeek API Key，或使用离线演示")
        base = self.settings["base_url"].rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": self.settings["model"], "messages": messages, "stream": False}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={
            "Authorization": "Bearer " + self.settings["api_key"],
            "Content-Type": "application/json",
        })
        for attempt in range(3):
            if cancel.is_set():
                raise Cancelled("任务已取消")
            try:
                with urllib.request.urlopen(request, timeout=self.settings["timeout"]) as response:
                    data = json.loads(response.read(4 * 1024 * 1024))
                if cancel.is_set():
                    raise Cancelled("任务已取消")
                message = data["choices"][0]["message"]
                # Preserve reasoning_content for providers requiring it on subsequent tool turns.
                message = {k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "reasoning_content")}
                return message, data.get("usage", {})
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    if cancel.wait(2 ** attempt):
                        raise Cancelled("任务已取消")
                    continue
                raise RuntimeError(f"模型接口返回 HTTP {exc.code}，请检查模型名、base_url、Key 或额度") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 2:
                    if cancel.wait(2 ** attempt):
                        raise Cancelled("任务已取消")
                    continue
                raise RuntimeError("模型连接超时或网络不可达，请检查 base_url 和网络") from None
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                raise RuntimeError("模型返回了无法解析的 Chat Completions 响应") from None


def parse_json(content: str):
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("规划代理未返回有效 JSON") from None
        return json.loads(text[start:end + 1])
