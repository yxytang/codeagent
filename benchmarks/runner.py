"""Fixed tasks, cold-start cells, bounded runs, and independent acceptance."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

from xcode.config import Settings
from xcode.engine import Engine
from xcode.memory import utcnow

from .evaluator import evaluate, evaluate_public, write_files
from .reporting import write_reports
from .task_suite import get_cases


VARIANTS = {
    "baseline": {"context_management": False, "prompt_guidance": False},
    "context": {"context_management": True, "prompt_guidance": False},
    "prompt": {"context_management": False, "prompt_guidance": True},
    "full": {"context_management": True, "prompt_guidance": True},
}


@dataclass(frozen=True)
class BenchmarkConfig:
    variants: tuple[str, ...] = tuple(VARIANTS)
    repeats: int = 1
    max_steps: int = 16
    max_tasks: int = 3
    max_model_calls: int = 18
    max_tool_calls: int = 48
    max_total_tokens: int = 32000
    max_batch_tokens: int = 500000
    context_chars: int = 12000
    request_timeout: int = 30
    task_timeout: int = 180
    evaluator_timeout: int = 45

    def validate(self):
        if not self.variants or len(set(self.variants)) != len(self.variants) or set(self.variants) - VARIANTS.keys():
            raise ValueError("variants must be unique baseline/context/prompt/full values")
        ranges = {
            "repeats": (1, 5), "max_steps": (2, 40), "max_tasks": (1, 12),
            "max_model_calls": (1, 500), "max_tool_calls": (1, 1000),
            "max_total_tokens": (1, 1000000), "max_batch_tokens": (1, 10000000),
            "context_chars": (8000, 120000), "request_timeout": (5, 120),
            "task_timeout": (1, 1800), "evaluator_timeout": (1, 120),
        }
        for name, (low, high) in ranges.items():
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ValueError(f"{name} must be an integer in {low}..{high}")


class FrozenSettings:
    """An isolated immutable connection; no global settings writes or Key export."""
    def __init__(self, values):
        self._values = deepcopy(values)

    def snapshot(self, secret=False, model_profile_id=None):
        if model_profile_id and model_profile_id != self._values["model_profile"]["id"]:
            raise ValueError("benchmark model connection is frozen")
        result = deepcopy(self._values)
        if not secret:
            result.pop("api_key", None)
        return result

    def redact(self, value):
        value = str(value)
        key = self._values.get("api_key")
        return value.replace(key, "[redacted]") if key else value


def connection_from_env(base_url=None, model=None, api_key_env="DEEPSEEK_API_KEY"):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError("invalid API key environment variable name")
    base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    key = os.getenv(api_key_env, "").strip()
    if not key:
        raise ValueError(f"Missing {api_key_env}; configure it locally before --live. No model requests were sent.")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url must be an HTTP(S) endpoint without credentials or query parameters")
    return {"base_url": base_url.rstrip("/"), "model": model, "api_key": key}


def select_cases(ids=None, limit=None):
    cases = get_cases()
    if ids:
        unknown = set(ids) - {case.id for case in cases}
        if unknown:
            raise ValueError("Unknown task ids: " + ", ".join(sorted(unknown)))
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task ids")
        cases = [case for case in cases if case.id in ids]
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be positive")
        cases = cases[:limit]
    return cases


def _fingerprint(cases):
    payload = json.dumps([asdict(case) for case in cases], ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _new_report(mode, output, config, cases, plan, connection=None):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be new or empty; previous runs are never overwritten.")
    output.mkdir(parents=True, exist_ok=True)
    public_model = {k: connection[k] for k in ("base_url", "model")} if connection else None
    return {
        "schema_version": 1, "mode": mode, "created": utcnow(),
        "config": {**asdict(config), "variants_definition": VARIANTS, "model": public_model,
                   "suite_sha256": _fingerprint(cases), "python": sys.version,
                   "latency_includes_evaluator": False,
                   "max_workers": 1, "mcp": "disabled", "skills": "empty",
                   "memory": "fresh database per task/variant/repeat",
                   "sampling": "provider defaults, identical across variants"},
        "plan": plan, "results": [],
        "notes": [
            "Model rounds are logical calls including planning, not HTTP attempts/retries.",
            "Reported tokens are provider-returned usage, not billing; unavailable usage is unknown.",
            "Token/time limits stop subsequent work; blocking requests and child processes are not hard-real-time sandboxes.",
            "Public test commands feed execution feedback; evaluator-owned acceptance runs only after the Agent stops.",
            "Context/Prompt form a 2x2 ablation; Prompt is existing fixed execution-progress guidance, not a learned optimizer.",
            "No compression-trigger evidence or no positive comparison means no claimed compression/performance gain.",
            "These 20 small local tasks are not SWE-bench or a broad production benchmark.",
        ],
    }


def _settings(output, connection, config):
    values = Settings(output / "settings-template").snapshot(secret=True)
    profile = {"id": "benchmark", "label": "Benchmark frozen connection", "protocol": "openai_chat",
               "base_url": connection["base_url"], "model": connection["model"],
               "supports_tools": True, "requires_api_key": True, "api_key_env": ""}
    values.update(
        model_profiles=[profile], model_profile=profile, default_model_id="benchmark",
        base_url=connection["base_url"], model=connection["model"], api_key=connection["api_key"],
        supports_tools=True, requires_api_key=True, max_workers=1,
        max_steps=config.max_steps, max_tasks=config.max_tasks, timeout=config.request_timeout,
        context_chars=config.context_chars, disabled_skills=[], mcp_servers=[],
        allowed_commands=[[sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test*.py"]],
    )
    return FrozenSettings(values)


def self_check(output: Path, cases=None, config=None, progress=None):
    """Calibrate acceptance: reference passes and unmodified seeds fail."""
    output = output.resolve()
    cases = cases if cases is not None else get_cases()
    config = config or BenchmarkConfig()
    config.validate()
    plan = [{"task_id": case.id, "category": case.category, "variant": variant, "repeat": 1}
            for case in cases for variant in ("seed", "reference")]
    report = _new_report("selfcheck", output, config, cases, plan)
    write_reports(report, output)
    for case in cases:
        for variant in ("seed", "reference"):
            cell = output / "cells" / case.id / variant
            files = dict(case.files)
            if variant == "reference":
                files.update(case.solution)
            write_files(cell / "workspace", files)
            result = evaluate(case, cell / "workspace", cell / "evaluation", config.evaluator_timeout)
            public = evaluate_public(case, cell / "workspace", cell / "public-validation", config.evaluator_timeout) if variant == "reference" else None
            record = {"task_id": case.id, "category": case.category, "variant": variant, "repeat": 1,
                      "success": result["passed"] and (public is None or public["passed"]), "status": "calibrated", "metrics": {},
                      "latency_seconds": None, "evaluator": result,
                      "public_verification": public,
                      "selfcheck_expected_pass": variant == "reference"}
            report["results"].append(record)
            write_reports(report, output)
            if progress:
                progress(record)
            if not result.get("cleanup_confirmed", True) or (public is not None and not public.get("cleanup_confirmed", True)):
                report["stop_reason"] = "evaluator process cleanup unconfirmed; selfcheck stopped"
                write_reports(report, output)
                return report
    report["finished"] = utcnow()
    write_reports(report, output)
    return report


def run_suite(output: Path, connection: dict, cases=None, config=None, progress=None):
    output = output.resolve()
    cases = cases if cases is not None else get_cases()
    config = config or BenchmarkConfig()
    config.validate()
    # Freeze before any cell and never load user project memories or MCP.
    frozen = _settings(output, connection, config)
    plan = []
    lookup = {case.id: case for case in cases}
    for repeat in range(1, config.repeats + 1):
        for index, case in enumerate(cases):
            shift = (index + repeat - 1) % len(config.variants)
            order = config.variants[shift:] + config.variants[:shift]
            plan.extend({"task_id": case.id, "category": case.category, "variant": name, "repeat": repeat} for name in order)
    report = _new_report("live", output, config, cases, plan, connection)
    write_reports(report, output)
    reported_batch_tokens = 0
    for item in plan:
        if config.max_batch_tokens - reported_batch_tokens < config.max_total_tokens:
            report["stop_reason"] = "batch cannot admit a full equal-budget cell; remaining cells not run"
            break
        case = lookup[item["task_id"]]
        cell = output / "cells" / case.id / item["variant"] / f"repeat-{item['repeat']}"
        workspace = cell / "workspace"
        write_files(workspace, case.files)
        options = {**VARIANTS[item["variant"]], "max_model_calls": config.max_model_calls,
                   "max_tool_calls": config.max_tool_calls,
                   "max_total_tokens": config.max_total_tokens}
        engine = Engine(cell / "runtime", workspace, frozen, runtime_options=options)
        argv = frozen.snapshot()["allowed_commands"][0]
        objective = case.objective + "\n可运行的精确公开测试命令（参数数组）为：" + json.dumps(argv, ensure_ascii=False)
        started = time.perf_counter()
        run = None
        timed_out = False
        try:
            run = engine.create(objective, str(workspace), mode="live", allow_commands=True)
            thread = engine.threads[run["id"]]
            deadline = time.monotonic() + config.task_timeout
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(0.1)
            if thread.is_alive():
                timed_out = True
                engine.cancel(run["id"])
                # Do not start another cell while a request from this one remains in flight.
                thread.join(config.request_timeout * 3 + 70)
            run = engine.get_run(run["id"])
            if thread.is_alive():
                report["stop_reason"] = "cancelled cell has in-flight work; batch stopped"
                run.setdefault("metrics", {})["usage_complete"] = False
                run["observation_complete"] = False
            (cell / "engine-run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
            latency = round(time.perf_counter() - started, 4)
            candidate = cell / "runtime" / "runs" / run["id"] / "candidate"
            grader = evaluate(case, candidate, cell / "evaluation", config.evaluator_timeout) if candidate.is_dir() and not thread.is_alive() else {
                "passed": False, "exit_code": None, "reason": "no completed candidate", "output": "", "latency_seconds": 0,
            }
            record = {**item, "success": run["status"] == "ready" and grader["passed"] and not timed_out,
                      "status": "timeout" if timed_out else run["status"], "run_id": run["id"],
                      "latency_seconds": latency, "metrics": run.get("metrics", {}), "evaluator": grader,
                      "public_verification": run.get("verification"), "error": run.get("error"),
                      "runtime_options": options, "plan_tasks": len(run.get("plan", {}).get("tasks", [])) if isinstance(run.get("plan"), dict) else len(run.get("plan", []))}
            if not grader.get("cleanup_confirmed", True):
                report["stop_reason"] = "evaluator process cleanup unconfirmed; batch stopped"
        except (KeyboardInterrupt, SystemExit):
            if run:
                engine.cancel(run["id"])
            report["stop_reason"] = "user interrupted; completed results preserved"
            write_reports(report, output)
            raise
        except Exception as exc:
            if run:
                engine.cancel(run["id"])
                engine.threads[run["id"]].join(config.request_timeout * 3 + 70)
                run = engine.get_run(run["id"])
                if engine.threads[run["id"]].is_alive():
                    run.setdefault("metrics", {})["usage_complete"] = False
            record = {**item, "success": False, "status": "runner_error", "error": frozen.redact(exc),
                      "latency_seconds": round(time.perf_counter() - started, 4),
                      "metrics": run.get("metrics", {}) if run else {},
                      "evaluator": {"passed": False, "reason": "runner_error"}}
            report["stop_reason"] = "runner error; batch stopped to preserve fairness"
        # Never serialize the private connection; error/results can only contain redacted engine values.
        reported_batch_tokens += int(record["metrics"].get("reported_total_tokens", 0))
        if record["metrics"].get("model_rounds", 0) and not record["metrics"].get("usage_complete", False):
            report.setdefault("stop_reason", "model usage incomplete; remaining spending cannot be bounded reliably")
        report["results"].append(record)
        write_reports(report, output)
        if progress:
            progress(record)
        if report.get("stop_reason"):
            break
    report["finished"] = utcnow()
    write_reports(report, output)
    return report
