from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request


class Cancelled(Exception):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def model_urlopen(request, timeout):
    # Model credentials belong to the configured endpoint. Never let urllib
    # forward its Authorization header to a redirected address.
    opener = urllib.request.build_opener(_RejectRedirects())
    return opener.open(request, timeout=timeout)


class ChatCompletionsProvider:
    """OpenAI-compatible Chat Completions transport with bounded retries."""

    def __init__(self, settings: dict):
        self.settings = settings

    def complete(self, messages, tools=None, cancel=None):
        cancel = cancel or threading.Event()
        if cancel.is_set():
            raise Cancelled("任务已取消")
        if self.settings.get("requires_api_key", True) and not self.settings.get("api_key"):
            raise ValueError("所选模型尚未配置 API Key，请在模型设置中配置，或使用离线演示")
        if tools and not self.settings.get("supports_tools", True):
            raise ValueError("所选模型未启用 Function Calling，无法执行 Agent 工具调用")
        base = self.settings["base_url"].rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": self.settings["model"], "messages": messages, "stream": False}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        headers = {"Content-Type": "application/json"}
        if self.settings.get("api_key"):
            headers["Authorization"] = "Bearer " + self.settings["api_key"]
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
        for attempt in range(3):
            if cancel.is_set():
                raise Cancelled("任务已取消")
            try:
                with model_urlopen(request, timeout=self.settings["timeout"]) as response:
                    data = json.loads(response.read(4 * 1024 * 1024))
                if cancel.is_set():
                    raise Cancelled("任务已取消")
                message = data["choices"][0]["message"]
                # Preserve reasoning_content for providers requiring it on subsequent tool turns.
                message = {k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "reasoning_content")}
                return message, data.get("usage", {})
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    raise RuntimeError("模型接口返回重定向，已停止转发凭据；请将 Base URL 配置为最终接口地址") from None
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


# Keep existing imports and transport mocks compatible while removing the
# provider-specific name from the Agent runtime itself.
DeepSeekProvider = ChatCompletionsProvider


class LLMGateway:
    """Route all Agent calls through one immutable per-run model connection."""

    def __init__(self, settings: dict):
        self.settings = copy.deepcopy(settings)
        profile = self.settings.get("model_profile", {})
        if profile.get("protocol", "openai_chat") != "openai_chat":
            raise ValueError("当前模型网关仅支持 Chat Completions 兼容协议")
        for key in ("base_url", "model", "requires_api_key", "supports_tools"):
            if key in profile:
                self.settings[key] = profile[key]
        self.transport = ChatCompletionsProvider(self.settings)

    def complete(self, messages, tools=None, cancel=None):
        return self.transport.complete(messages, tools, cancel)


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
