"""Run evaluator-owned checks only after the Agent has stopped."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import signal
import subprocess
import sys
import time

from xcode.mcp import process_env
from xcode.workspace import inventory, safe_path


def evaluate_public(case, candidate: Path, directory: Path, timeout: float = 45):
    """Preflight the same unittest discovery used by the Agent, without a model."""
    source = (
        "import sys, unittest\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path.cwd()))\n"
        "suite = unittest.defaultTestLoader.discover('.', pattern='test*.py')\n"
        "if suite.countTestCases() == 0:\n"
        "    raise SystemExit('No public tests discovered')\n"
        "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "raise SystemExit(0 if result.wasSuccessful() else 1)\n"
    )
    return evaluate(replace(case, grader_source=source), candidate, directory, timeout)


def _stop_process_tree(child):
    """Terminate only the evaluator-owned process group/tree, never a named app."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            confirmed = result.returncode == 0
        else:
            os.killpg(child.pid, signal.SIGKILL)
            confirmed = True
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        return confirmed
    except (OSError, subprocess.TimeoutExpired):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        return False


def write_files(root: Path, files: dict[str, str]):
    root.mkdir(parents=True, exist_ok=False)
    for name, source in files.items():
        target = safe_path(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8", newline="")


def evaluate(case, candidate: Path, directory: Path, timeout: float = 45):
    """Never feed these private results back into an unfinished Agent run.

    This is process separation and workspace isolation, not an OS sandbox.
    """
    started = time.perf_counter()
    directory.mkdir(parents=True, exist_ok=False)
    work = directory / "candidate"
    work.mkdir()
    files = inventory(candidate)
    for name, data in files.items():
        target = safe_path(work, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    private = directory / "acceptance.py"
    private.write_text(case.grader_source, encoding="utf-8")
    temp = directory / "tmp"
    temp.mkdir()
    env = process_env()
    env.pop("PYTHONPATH", None)
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", TEMP=str(temp), TMP=str(temp))
    log = directory / "evaluator.log"
    reason = None
    cleanup_confirmed = True
    with log.open("wb") as stream:
        process_options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
        child = subprocess.Popen(
            [sys.executable, "-I", "-B", str(private)], cwd=work,
            stdout=stream, stderr=subprocess.STDOUT, env=env, shell=False,
            **process_options,
        )
        try:
            while child.poll() is None:
                if time.perf_counter() - started > timeout:
                    reason = "timeout"
                    break
                if log.stat().st_size > 2 * 1024 * 1024:
                    reason = "output_limit"
                    break
                time.sleep(0.025)
        finally:
            if child.poll() is None:
                cleanup_confirmed = _stop_process_tree(child)
            child.wait()
    with log.open("rb") as stream:
        output = stream.read(12000).decode("utf-8", errors="replace")
    return {
        "passed": child.returncode == 0 and reason is None and cleanup_confirmed,
        "exit_code": child.returncode, "reason": reason, "output": output,
        "output_truncated": log.stat().st_size > 12000,
        "latency_seconds": round(time.perf_counter() - started, 4),
        "candidate_files": len(files),
        "cleanup_confirmed": cleanup_confirmed,
    }
