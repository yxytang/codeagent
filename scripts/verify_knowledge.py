"""Exercise real skill/memory APIs using only the fixed offline demo."""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    def api(path, data=None):
        headers = {"X-XCode-Client": "1"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(base + path, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def demo():
        run = api("/api/runs", {"objective": "实现带测试与文档的 Python 计算器", "mode": "demo"})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = api("/api/runs/" + run["id"])
            if run["status"] == "ready":
                if run["verification"]["status"] != "passed":
                    raise RuntimeError("Offline calculator tests did not pass")
                return run
            if run["status"] in {"failed", "cancelled", "conflict", "interrupted"}:
                raise RuntimeError(run.get("error") or run["status"])
            time.sleep(0.15)
        raise RuntimeError("Timed out waiting for the offline demo")

    state = api("/api/state")
    if any(run["status"] in {"planning", "running", "verifying", "cancelling"} for run in state["runs"]):
        raise RuntimeError("Wait for the active task before running this verification")
    first = demo()
    note = api("/api/memory", {
        "scope": first["workspace"],
        "title": "离线演示：测试约定",
        "summary": "计算器使用 unittest，测试放在 tests/；除数为零应抛出 ValueError。",
    })["memory"]
    second = demo()
    if first["workspace"] != second["workspace"]:
        raise RuntimeError("Demo tasks must share the same project memory scope")
    planner = next(item for item in second["knowledge"]["memory_recalls"] if item["source"] == "planning")
    recalled = {item["id"] for item in planner["items"]}
    previous = {item["id"] for item in first["knowledge"]["memory_saves"]}
    if note["id"] not in recalled or not recalled.intersection(previous):
        raise RuntimeError("The second task did not recall its project note and previous task history")
    detail = api("/api/memory/" + str(note["id"]))
    if detail["scope"] != second["workspace"] or detail["summary"] != note["summary"]:
        raise RuntimeError("Memory detail did not preserve its project and content")
    python_skill = next((skill for skill in state["skills"] if skill["name"] == "python-quality"), None)
    if python_skill:
        preview = api("/api/skills?" + urllib.parse.urlencode({"name": python_skill["name"]}))
        if not preview["body"]:
            raise RuntimeError("Skill preview was empty")
        if python_skill["enabled"] and not any(item["name"] == "python-quality" for item in second["knowledge"]["skill_loads"]):
            raise RuntimeError("Enabled demo skill was not actually loaded")
    print(json.dumps({
        "mode": "offline_fixed_script_no_model_calls",
        "runs": [first["id"], second["id"]],
        "verification": [first["verification"]["status"], second["verification"]["status"]],
        "loaded_skills": sorted({item["name"] for item in second["knowledge"]["skill_loads"]}),
        "project_note_id": note["id"],
        "reused_previous_memory_ids": sorted(recalled.intersection(previous)),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
