"""Honest, dependency-free reporting for the small XCode benchmark.

Model rounds are logical runtime calls, not HTTP attempts. Token values are
provider-reported observations; missing usage never means free execution.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import math
from collections.abc import Mapping
from pathlib import Path
from statistics import mean


LIVE_VARIANTS = ("baseline", "context", "prompt", "full")
SELFCHECK_VARIANTS = ("reference", "seed")
PENDING_STATUSES = frozenset(("pending", "not_run", "not_started", "queued", "skipped"))
METRICS = ("model_rounds", "tool_calls", "tool_errors", "prompt_tokens", "completion_tokens",
           "reported_total_tokens", "context_compressions", "command_failures")
LABELS = {"baseline": "Baseline", "context": "Context Manager", "prompt": "Prompt Guidance",
          "full": "Full Harness", "reference": "参考解自检", "seed": "初始缺陷版本自检"}


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _key(item):
    return item["task_id"], item["variant"], item.get("repeat", 1)


def _validated(report):
    if not isinstance(report, Mapping) or report.get("schema_version") != 1:
        raise ValueError("benchmark report requires schema_version=1")
    mode = report.get("mode")
    if mode not in ("live", "selfcheck"):
        raise ValueError("benchmark report mode must be live or selfcheck")
    plan, results = report.get("plan"), report.get("results")
    if not isinstance(plan, list) or not isinstance(results, list):
        raise ValueError("benchmark plan and results must be lists")
    allowed = LIVE_VARIANTS if mode == "live" else SELFCHECK_VARIANTS
    planned = {}
    for item in plan:
        if not isinstance(item, Mapping) or any(not isinstance(item.get(k), str) or not item[k]
                                                for k in ("task_id", "category", "variant")):
            raise ValueError("each planned item requires task_id, category and variant")
        if item["variant"] not in allowed:
            raise ValueError("variant does not belong to this report mode")
        if type(item.get("repeat", 1)) is not int or item.get("repeat", 1) < 0:
            raise ValueError("repeat must be a non-negative integer")
        key = _key(item)
        if key in planned:
            raise ValueError("duplicate task/variant/repeat in benchmark plan")
        planned[key] = item
    observed = {}
    for item in results:
        if not isinstance(item, Mapping):
            raise ValueError("each result must be an object")
        try:
            key = _key(item)
        except (KeyError, TypeError):
            raise ValueError("result is missing a task identity") from None
        if key not in planned or item.get("category") != planned[key]["category"]:
            raise ValueError("result is outside its planned task/category/variant/repeat")
        if key in observed:
            raise ValueError("duplicate task/variant/repeat result")
        if type(item.get("success")) is not bool or not isinstance(item.get("status"), str):
            raise ValueError("each result requires boolean success and string status")
        observed[key] = item
    return mode, list(planned.values()), observed


def _attempted(item):
    return item is not None and item["status"].strip().lower() not in PENDING_STATUSES


def _successful(item, mode):
    evaluator = item.get("evaluator") or {}
    passed = isinstance(evaluator, Mapping) and evaluator.get("passed") is True
    return item["success"] is True and passed and (mode != "live" or item["status"] == "ready")


def _failure_then_success(item):
    """Observe ordering only: command identity and a repair cause are unknown."""
    metrics = item.get("metrics") or {}
    records = metrics.get("test_command_results", []) if isinstance(metrics, Mapping) else []
    seen_failure = False
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, Mapping):
            continue
        code, reason = record.get("exit_code"), record.get("reason")
        if reason or (type(code) is int and code != 0):
            seen_failure = True
        elif type(code) is int and code == 0 and seen_failure:
            return True
    return False


def _aggregate(plan, observed, mode):
    attempts = [observed[_key(p)] for p in plan if _attempted(observed.get(_key(p)))]
    successes = sum(_successful(item, mode) for item in attempts)
    complete = bool(plan) and len(attempts) == len(plan)
    values = {name: [] for name in (*METRICS, "latency_seconds")}
    token_complete = bool(attempts)
    compression_cases = 0
    for item in attempts:
        metrics = item.get("metrics") or {}
        metrics = metrics if isinstance(metrics, Mapping) else {}
        for name in METRICS:
            value = _number(metrics.get(name))
            if value is not None:
                values[name].append(value)
        latency = _number(item.get("latency_seconds"))
        if latency is not None:
            values["latency_seconds"].append(latency)
        token_complete = token_complete and metrics.get("usage_complete") is True and all(
            _number(metrics.get(name)) is not None
            for name in ("prompt_tokens", "completion_tokens", "reported_total_tokens"))
        compression_cases += bool((_number(metrics.get("context_compressions")) or 0) > 0)
    return {
        "planned": len(plan), "attempted": len(attempts), "pending": len(plan) - len(attempts),
        "successes": successes, "complete": complete,
        "success_rate": successes / len(plan) if complete and mode == "live" else None,
        "completed_success_rate": successes / len(attempts) if attempts and mode == "live" else None,
        "validation_success_rate": successes / len(attempts) if attempts and mode == "selfcheck" else None,
        "means": {name: mean(items) if items else None for name, items in values.items()},
        "sums": {name: sum(items) if items else None for name, items in values.items()},
        "metric_counts": {name: len(items) for name, items in values.items()},
        "usage_complete": token_complete if mode == "live" else None,
        "token_accounting": ("not_applicable" if mode == "selfcheck" else
                             "reported_nonbilling" if token_complete else
                             "partial_nonbilling" if attempts else "unavailable_nonbilling"),
        "compression_cases": compression_cases,
        "command_failure_then_success_cases": sum(_failure_then_success(item) for item in attempts),
    }


def summarize(report):
    """Aggregate planned denominators; incomplete groups have no main rate.

    A mean includes failed attempts with observed values. It never substitutes
    zero for a missing metric. Selfchecks validate fixtures, not Agent ability.
    """
    mode, plan, observed = _validated(report)
    result = _aggregate(plan, observed, mode)
    result.update(schema_version=1, mode=mode, is_agent_evaluation=mode == "live")
    variants = LIVE_VARIANTS if mode == "live" else SELFCHECK_VARIANTS
    result["variants"] = {}
    for variant in variants:
        subset = [item for item in plan if item["variant"] == variant]
        group = _aggregate(subset, observed, mode)
        group["categories"] = {
            category: _aggregate([item for item in subset if item["category"] == category], observed, mode)
            for category in sorted({item["category"] for item in subset})
        }
        result["variants"][variant] = group
    result["categories"] = {
        category: _aggregate([item for item in plan if item["category"] == category], observed, mode)
        for category in sorted({item["category"] for item in plan})
    }
    if mode == "selfcheck":
        reference, seed = result["variants"]["reference"], result["variants"]["seed"]
        seed_attempts = [item for item in observed.values()
                         if item["variant"] == "seed" and _attempted(item)]
        rejected = sum(isinstance(item.get("evaluator"), Mapping)
                       and item["evaluator"].get("passed") is False
                       and type(item["evaluator"].get("exit_code")) is int
                       and item["evaluator"]["exit_code"] != 0
                       and not item["evaluator"].get("reason") for item in seed_attempts)
        reference_keys = {(item["task_id"], item.get("repeat", 1)) for item in plan if item["variant"] == "reference"}
        seed_keys = {(item["task_id"], item.get("repeat", 1)) for item in plan if item["variant"] == "seed"}
        result["selfcheck"] = {
            "reference_passed": reference["successes"], "reference_planned": reference["planned"],
            "seed_rejected": rejected, "seed_planned": seed["planned"],
            "passed": (reference["complete"] and seed["complete"] and reference_keys == seed_keys
                       and reference["successes"] == reference["planned"] and rejected == seed["planned"]),
        }
    return result


def _percent(value):
    return "未完成" if value is None else f"{value:.1%}"


def _value(value):
    if value is None:
        return "—"
    return f"{value:.3f}".rstrip("0").rstrip(".") if type(value) is float else str(value)


def _cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _table(groups):
    lines = ["| 分组 | 已尝试 / 计划 | 待运行 | 成功 / 已尝试 | 完整成功率 | 已尝试成功率 |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for label, group in groups:
        lines.append(f"| {_cell(label)} | {group['attempted']}/{group['planned']} | {group['pending']} | "
                     f"{group['successes']}/{group['attempted']} | {_percent(group['success_rate'])} | "
                     f"{_percent(group['completed_success_rate'])} |")
    return lines


def _markdown(report, summary):
    lines = ["# XCode 自建 Benchmark 报告", ""]
    if summary["mode"] == "selfcheck":
        check = summary["selfcheck"]
        lines += ["本报告仅检验参考解能通过、初始缺陷版本能被验收器拒绝；未运行真实 Agent，不测模型成功率。", "",
                  f"自检状态：{'通过' if check['passed'] else '未通过或未完整完成'}。",
                  f"参考解通过：{check['reference_passed']}/{check['reference_planned']}；"
                  f"缺陷版本被拒绝：{check['seed_rejected']}/{check['seed_planned']}。", "",
                  "| 自检类型 | 已完成 / 计划 | 候选通过验收 | 含义 |", "| --- | ---: | ---: | --- |"]
        for variant, group in summary["variants"].items():
            meaning = "预期全部通过" if variant == "reference" else "预期全部失败；失败不算 Agent 失败率"
            lines.append(f"| {LABELS[variant]} | {group['attempted']}/{group['planned']} | "
                         f"{group['successes']}/{group['attempted']} | {meaning} |")
        public = [r["public_verification"] for r in report["results"]
                  if r["variant"] == "reference" and isinstance(r.get("public_verification"), Mapping)]
        if public:
            lines += ["", f"参考解公开测试通过：{sum(r.get('passed') is True for r in public)}/{check['reference_planned']}；要求实际发现测试。"]
    else:
        lines += ["成功由运行到 ready 且独立验收通过共同判定。未运行项保留在计划分母中；"
                  "整组未完成时不计算完整成功率。", ""]
        lines += _table([(LABELS[v], g) for v, g in summary["variants"].items()])
        lines += ["", "均值包含成功及失败的已尝试任务，仅聚合有值记录。括号 n 为有该指标的记录数。", "",
                  "| 分组 | Model Rounds 均值 | Tool Calls 均值 | Tool Errors 均值 | 已回报 Token 均值 | 耗时秒均值 | Token 口径 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | --- |"]
        for variant, group in summary["variants"].items():
            cells = [f"{_value(group['means'][name])} (n={group['metric_counts'][name]})"
                     for name in ("model_rounds", "tool_calls", "tool_errors", "reported_total_tokens", "latency_seconds")]
            lines.append("| " + " | ".join([LABELS[variant], *cells, group["token_accounting"]]) + " |")
        lines += ["", "Model Rounds 包含规划和执行的逻辑调用，不等于底层 HTTP 请求或重试次数。",
                  "Token 是接口已回报用量，并非精确计费；partial 表示存在用量缺失或调用失败，不能把缺失视作零消耗。",
                  "耗时为任务墙钟时间；外部验收是否计入由 config 记录，不能将并发调用耗时直接相加。"]
    for variant, group in summary["variants"].items():
        if not group["planned"]:
            continue
        lines += ["", "## " + LABELS[variant], ""]
        if summary["mode"] == "live":
            lines += _table(list(group["categories"].items()))
            lines += ["", f"上下文压缩触发总次数：{_value(group['sums']['context_compressions'])}；"
                      f"发生压缩的任务：{group['compression_cases']}/{group['attempted']}。",
                      f"命令结果出现先失败后成功的任务：{group['command_failure_then_success_cases']}。"
                      "该序列不保证是同一条命令，也不证明某一组件造成修复或成功率提升。"]
        else:
            lines += ["| 分类 | 已完成 / 计划 | 候选通过验收 |", "| --- | ---: | ---: |"]
            for category, category_group in group["categories"].items():
                lines.append(f"| {_cell(category)} | {category_group['attempted']}/{category_group['planned']} | "
                             f"{category_group['successes']}/{category_group['attempted']} |")
    lines += ["", "## 配置与说明", "", "```json", json.dumps(report.get("config", {}), ensure_ascii=False, indent=2), "```", "",
              "本地自建任务集用于开发验证，不应表述为公开通用编码基准的成绩。"]
    for note in report.get("notes", []):
        lines.append("- " + _cell(note))
    return "\n".join(lines) + "\n"


def _resume(report, summary):
    task_count = len({item["task_id"] for item in report["plan"]})
    category_count = len({item["category"] for item in report["plan"]})
    prefix = (f"Benchmark 与代码验证：自建覆盖 {category_count} 类任务的 {task_count}-task Benchmark 与独立验收器，"
              "记录任务验收、模型逻辑轮次、工具调用及错误、已回报 Token 和耗时。")
    if summary["mode"] == "selfcheck":
        state = "自检通过" if summary["selfcheck"]["passed"] else "自检尚未全部通过"
        return f"已搭建上述 Benchmark 与独立验收器，{state}，真实 Agent 结果未测。\n\n{prefix}\n"
    if not summary["complete"]:
        return (prefix + f"\n\n目前已尝试 {summary['attempted']}/{summary['planned']} 次计划运行，"
                f"尚有 {summary['pending']} 次待运行；完整对比未完成，暂不写总体成功率或提升结论。\n")
    baseline, full = summary["variants"]["baseline"], summary["variants"]["full"]
    baseline_keys = {(p["task_id"], p.get("repeat", 1)) for p in report["plan"] if p["variant"] == "baseline"}
    full_keys = {(p["task_id"], p.get("repeat", 1)) for p in report["plan"] if p["variant"] == "full"}
    if baseline["complete"] and full["complete"] and baseline_keys == full_keys:
        comparison = (f"在同一自建任务与运行配置下，Full Harness 独立验收通过 "
                      f"{full['successes']}/{full['planned']}（{_percent(full['success_rate'])}），"
                      f"Baseline 为 {baseline['successes']}/{baseline['planned']}（{_percent(baseline['success_rate'])}）。")
        if full["success_rate"] > baseline["success_rate"]:
            comparison += "该提升仅描述本轮自建任务对照结果。"
        else:
            comparison += "本轮未观察到 Full Harness 成功率高于 Baseline。"
        return prefix + "\n\n" + comparison + "\n"
    return prefix + f"\n\n已完成 {summary['attempted']}/{summary['planned']} 次计划运行；各组独立结果见评测报告，未作不对等组间的提升比较。\n"


def _csv(report):
    mode, plan, observed = _validated(report)
    stream = io.StringIO(newline="")
    columns = ["mode", "task_id", "category", "variant", "repeat", "attempted", "success", "status",
               "evaluator_passed", "evaluator_exit_code", *METRICS, "usage_complete", "latency_seconds",
               "command_failure_then_success"]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for planned in plan:
        item = observed.get(_key(planned))
        attempted = _attempted(item)
        item = item or {}
        metrics = item.get("metrics") or {}
        metrics = metrics if isinstance(metrics, Mapping) else {}
        evaluator = item.get("evaluator") or {}
        evaluator = evaluator if isinstance(evaluator, Mapping) else {}
        row = {"mode": mode, "task_id": planned["task_id"], "category": planned["category"],
               "variant": planned["variant"], "repeat": planned.get("repeat", 1), "attempted": attempted,
               "success": _successful(item, mode) if attempted else "",
               "status": item.get("status", "pending"), "evaluator_passed": evaluator.get("passed", ""),
               "evaluator_exit_code": evaluator.get("exit_code", ""),
               "usage_complete": metrics.get("usage_complete", "") if attempted else "",
               "latency_seconds": _number(item.get("latency_seconds")) if attempted else "",
               "command_failure_then_success": _failure_then_success(item) if attempted else ""}
        row.update({name: _number(metrics.get(name)) if attempted else "" for name in METRICS})
        writer.writerow(row)
    return stream.getvalue()


def write_reports(report, output_dir):
    """Write UTF-8 artifacts and return their absolute paths without API calls."""
    snapshot = copy.deepcopy(report)
    summary = summarize(snapshot)
    documents = {
        "report_json": ("report.json", json.dumps({**snapshot, "summary": summary}, ensure_ascii=False,
                                                  indent=2, allow_nan=False) + "\n"),
        "report_md": ("report.md", _markdown(snapshot, summary)),
        "results_csv": ("results.csv", _csv(snapshot)),
        "resume_excerpt_md": ("resume_excerpt.md", _resume(snapshot, summary)),
    }
    folder = Path(output_dir).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, (filename, content) in documents.items():
        target = folder / filename
        temporary = folder / (filename + ".tmp")
        temporary.write_text(content, encoding="utf-8", newline="")
        temporary.replace(target)
        paths[name] = str(target)
    return paths
