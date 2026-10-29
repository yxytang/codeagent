"""Optional runtime hooks loaded explicitly by the local server.

Run from the repository root:
    python -m xcode.server --hooks-module examples.hooks_example

Callbacks can run concurrently for different agents. These stateless examples
keep no shared mutable state and deliberately avoid logging request contents.
"""

from xcode.hooks import HookContext, HookDecision, HookRegistry


def coding_guidance(context: HookContext) -> HookDecision:
    """Add a short instruction to each planning/execution model request."""
    return HookDecision(system_message="避免无关改动，完成后给出验证证据。")


def deny_delete_tool(context: HookContext) -> HookDecision | None:
    """Block this tool entry point; this is not a file-content policy.

    write_file and authorized commands can still remove existing content.
    Review the resulting diff before applying changes to the original project.
    """
    if context.data.get("name") == "delete_file":
        return HookDecision(deny="示例 Hook 禁止调用 delete_file；请保留文件并说明所需删除操作。")
    return None


def report_run_end(context: HookContext) -> None:
    """Print only run identity and status, never objectives, code or keys."""
    print(f"[hook] run={context.run_id} status={context.data.get('status', 'unknown')}", flush=True)


def register_hooks(registry: HookRegistry) -> None:
    """The explicit module entry point called during server startup."""
    registry.register("before_model", coding_guidance, name="编码与验证指引")
    registry.register("before_tool", deny_delete_tool, name="阻止删除工具", priority=-10)
    registry.register("on_run_end", report_run_end, name="任务状态记录")
