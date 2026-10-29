"""In-process Agent lifecycle hooks with an immutable registry per run.

Hooks are trusted Python callbacks. Each receives an isolated copy of the public
runtime context; they cannot change a pending call by mutating that context.
Only explicit HookDecision values can block a pre-operation or add model context.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .provider import Cancelled


EVENTS = (
    "on_run_start",
    "before_model",
    "after_model",
    "before_tool",
    "after_tool",
    "on_tool_error",
    "on_run_end",
)
_PRE_EVENTS = frozenset(("on_run_start", "before_model", "before_tool"))
_MAX_MESSAGE = 8000
_MAX_TOTAL_MESSAGES = 16000


@dataclass(frozen=True)
class HookContext:
    event: str
    run_id: str
    agent_id: str
    workspace: str
    mode: str
    data: dict


@dataclass(frozen=True)
class HookDecision:
    deny: str | None = None
    system_message: str | None = None


class HookBlocked(RuntimeError):
    """A pre-operation hook refused the action or could not validate it."""


@dataclass(frozen=True)
class _Registration:
    event: str
    name: str
    priority: int
    order: int
    handler: Callable[[HookContext], HookDecision | None]


class HookRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries: tuple[_Registration, ...] = ()
        self._next_order = 0
        self._frozen = False

    @staticmethod
    def _check_event(event):
        if not isinstance(event, str) or event not in EVENTS:
            raise ValueError(f"未知 Hook 事件：{event!r}")

    def register(self, event, handler, *, name=None, priority=0):
        """Register a callback and return its public name (unique per event)."""
        self._check_event(event)
        if not callable(handler):
            raise TypeError("Hook handler 必须可调用")
        if name is None:
            name = getattr(handler, "__name__", handler.__class__.__name__)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Hook name 必须是非空字符串")
        if type(priority) is not int:
            raise TypeError("Hook priority 必须是整数")
        with self._lock:
            if self._frozen:
                raise RuntimeError("任务 Hook 快照不可修改")
            if any(item.event == event and item.name == name for item in self._entries):
                raise ValueError(f"Hook 已注册：{event}/{name}")
            entry = _Registration(event, name, priority, self._next_order, handler)
            self._entries += (entry,)
            self._next_order += 1
        return name

    def unregister(self, event, name):
        self._check_event(event)
        with self._lock:
            if self._frozen:
                raise RuntimeError("任务 Hook 快照不可修改")
            remaining = tuple(item for item in self._entries if (item.event, item.name) != (event, name))
            changed = len(remaining) != len(self._entries)
            self._entries = remaining
            return changed

    def catalog(self):
        """Return public metadata only, without exposing callback objects."""
        with self._lock:
            entries = self._entries
        return [
            {"event": item.event, "name": item.name, "priority": item.priority}
            for item in sorted(entries, key=lambda item: (EVENTS.index(item.event), item.priority, item.order))
        ]

    def snapshot(self):
        """Freeze the current members; callbacks themselves remain trusted code."""
        with self._lock:
            result = HookRegistry()
            result._entries = self._entries
            result._next_order = self._next_order
            result._frozen = True
            return result

    @staticmethod
    def _validate_decision(event, decision):
        if decision is None:
            return
        if not isinstance(decision, HookDecision):
            raise TypeError("Hook 必须返回 None 或 HookDecision")
        if decision.deny is not None:
            if not isinstance(decision.deny, str) or not decision.deny.strip():
                raise ValueError("Hook deny 必须是非空字符串")
            if event not in _PRE_EVENTS:
                raise ValueError(f"{event} 不允许拒绝已完成的操作")
        if decision.system_message is not None:
            if event != "before_model":
                raise ValueError("只有 before_model 可以注入 system_message")
            if not isinstance(decision.system_message, str) or not 1 <= len(decision.system_message) <= _MAX_MESSAGE:
                raise ValueError("Hook system_message 长度必须在 1..8000 字符之间")

    @staticmethod
    def _report(report, entry, status, reason, started):
        if report is None:
            return
        record = {
            "event": entry.event,
            "name": entry.name,
            "status": status,
            "reason": reason,
            "elapsed_ms": round(max(0.0, time.perf_counter() - started) * 1000, 3),
        }
        try:
            report(record)
        except Exception:
            # Audit storage must not turn a successful tool into a failed tool,
            # or suppress a policy denial while attempting to log that denial.
            pass

    def emit(self, context: HookContext, report=None) -> list[str]:
        """Run callbacks once in priority order, returning model system additions.

        Pre-operation failures block the action; cancellation remains cancellation.
        Post-operation failures are reported while preserving the completed result.
        No registry lock is held during callback or report execution.
        """
        if not isinstance(context, HookContext):
            raise TypeError("Hook context 必须是 HookContext")
        self._check_event(context.event)
        with self._lock:
            entries = sorted(
                (item for item in self._entries if item.event == context.event),
                key=lambda item: (item.priority, item.order),
            )
        messages = []
        message_chars = 0
        for entry in entries:
            started = time.perf_counter()
            try:
                decision = entry.handler(copy.deepcopy(context))
                self._validate_decision(context.event, decision)
                if decision is not None and decision.system_message is not None:
                    if message_chars + len(decision.system_message) > _MAX_TOTAL_MESSAGES:
                        raise ValueError("Hook system_message 总长度不能超过 16000 字符")
            except Exception as exc:
                self._report(report, entry, "error", str(exc), started)
                if context.event in _PRE_EVENTS:
                    if isinstance(exc, Cancelled):
                        raise
                    raise HookBlocked(f"Hook {entry.name} 执行失败：{exc}") from exc
                continue
            if decision is not None and decision.deny is not None:
                self._report(report, entry, "denied", decision.deny, started)
                raise HookBlocked(f"Hook {entry.name} 拒绝执行：{decision.deny}")
            if decision is not None and decision.system_message is not None:
                messages.append(decision.system_message)
                message_chars += len(decision.system_message)
            self._report(report, entry, "ok", "", started)
        return messages
