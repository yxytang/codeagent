"""Exercise the real local HTTP API using a disposable, bounded project."""
import json
import time
import urllib.request
from pathlib import Path

base = "http://127.0.0.1:8765"


def request(path, data=None):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None, headers={"Content-Type":"application/json", "X-XCode-Client":"1"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


root = Path(__file__).resolve().parents[1] / ".xcode" / "smoke-projects" / str(int(time.time()))
root.mkdir(parents=True)
(root / "PROJECT.md").write_text("# Slug utility\nUse Python standard library only. Source: slug.py. Tests: tests/test_slug.py. Docs: README.md.\n", "utf-8")
run = request("/api/runs", {"workspace":str(root), "mode":"live", "objective":"用 Python 标准库实现一个小型 slug 工具。slug.py 中 normalize_slug(text) 把英文文本转为小写，将连续空白和标点替换为单个连字符，并去掉首尾连字符；空字符串返回空字符串，非字符串抛 TypeError。拆为 3 个叶子任务：core 只写 slug.py，tests 依赖 core 只写 tests/test_slug.py 用 unittest 覆盖空输入、连续标点、大小写、非字符串，docs 独立只写 README.md。不要运行命令或增加其他文件。完成后简要说明。", "allow_commands":False})
print("Live run:", run["id"], "workspace:", str(root), flush=True)
deadline = time.monotonic() + 360
last = ""
while time.monotonic() < deadline:
    run = request("/api/runs/" + run["id"])
    phase = run["status"] + " / " + ", ".join(a["id"] + ":" + a["status"] for a in run["agents"])
    if phase != last:
        print(phase, flush=True)
        last = phase
    if run["status"] in ("ready", "failed", "conflict", "cancelled"):
        print(json.dumps({"id":run["id"], "status":run["status"], "tokens":run["tokens"], "files":[p["path"] for p in run["patches"]], "error":run["error"]}, ensure_ascii=False), flush=True)
        raise SystemExit(0 if run["status"] == "ready" else 1)
    time.sleep(2)
request("/api/runs/" + run["id"] + "/cancel", {})
print("Smoke test timed out; cancellation requested.", flush=True)
raise SystemExit(1)
