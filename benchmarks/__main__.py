from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .reporting import summarize
from .runner import BenchmarkConfig, VARIANTS, connection_from_env, run_suite, select_cases, self_check


def main(argv=None):
    parser = argparse.ArgumentParser(description="XCode 20-task benchmark; live calls require explicit --live.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="List task contracts, no requests")
    mode.add_argument("--self-check", action="store_true", help="Run evaluator calibration only, no model")
    mode.add_argument("--live", action="store_true", help="Run actual Agent; consumes model quota")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tasks", nargs="+", help="Fixed task IDs to evaluate")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=list(VARIANTS))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--max-model-calls", type=int, default=18)
    parser.add_argument("--max-tool-calls", type=int, default=48)
    parser.add_argument("--max-tokens", type=int, default=32000, help="Reported-token budget per cell, soft at last response")
    parser.add_argument("--max-batch-tokens", type=int, default=500000)
    parser.add_argument("--task-timeout", type=int, default=180)
    parser.add_argument("--context-chars", type=int, default=12000)
    args = parser.parse_args(argv)
    try:
        cases = select_cases(args.tasks, args.limit)
        if args.list:
            for case in cases:
                print(f"{case.id:16} {case.category:20} {case.objective.splitlines()[0]}")
            print(dict(Counter(c.category for c in cases)))
            return 0
        config = BenchmarkConfig(variants=tuple(args.variants), repeats=args.repeat,
                                 max_model_calls=args.max_model_calls, max_tool_calls=args.max_tool_calls,
                                 max_total_tokens=args.max_tokens, max_batch_tokens=args.max_batch_tokens,
                                 task_timeout=args.task_timeout, context_chars=args.context_chars)
        config.validate()
        connection = connection_from_env(args.base_url, args.model, args.api_key_env) if args.live else None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = args.output or Path(".xcode") / "benchmarks" / stamp
        def progress(record):
            result = "PASS" if record["success"] else "FAIL"
            if args.self_check:
                evaluator = record["evaluator"]
                matched = record["success"] and evaluator["passed"] if record["selfcheck_expected_pass"] else (
                    evaluator["passed"] is False
                    and type(evaluator.get("exit_code")) is int
                    and evaluator["exit_code"] != 0
                    and not evaluator.get("reason")
                )
                result = "CHECK OK" if matched else "CHECK FAILED"
            print(f"{record['task_id']} / {record['variant']} / {result}", flush=True)
        report = run_suite(output, connection, cases, config, progress) if args.live else self_check(output, cases, config, progress)
        print("Reports: " + str(output.resolve()), flush=True)
        if args.self_check:
            return 0 if len(report["results"]) == len(report["plan"]) and summarize(report)["selfcheck"]["passed"] else 1
        return 0 if len(report["results"]) == len(report["plan"]) else 2
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
