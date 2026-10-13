from __future__ import annotations

import json
import re
from pathlib import Path


def frontmatter(text):
    """A deliberately small YAML frontmatter subset: scalars, inline lists, |/>.

    No executable YAML tags, implicit object construction or third-party dependency.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 缺少 YAML frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("frontmatter 缺少结束分隔符") from None
    meta = {}
    i = 1
    while i < end:
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z_]+):\s*(.*)", line)
        if not match:
            raise ValueError("frontmatter 仅支持顶层标量、行内数组及多行文本")
        key, value = match.groups()
        if value in ("|", ">"):
            chunks = []
            while i < end and (not lines[i].strip() or lines[i].startswith(" ")):
                chunks.append(lines[i].strip())
                i += 1
            meta[key] = ("\n" if value == "|" else " ").join(chunks)
        elif value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        elif value in ("true", "false"):
            meta[key] = value == "true"
        else:
            meta[key] = value.strip("\"'")
    return meta, "\n".join(lines[end + 1:]).strip()


class SkillRegistry:
    def __init__(self, directories, disabled=()):
        self.entries = []
        self.disabled = set(disabled)
        seen = set()
        for directory in directories:
            if not directory.exists() or directory.is_symlink():
                continue
            for path in sorted(directory.glob("*/SKILL.md")):
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                try:
                    if path.stat().st_size > 64000:
                        raise ValueError("技能文件超过 64KB")
                    meta, body = frontmatter(path.read_text("utf-8"))
                    name = meta.get("name", path.parent.name)
                    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name in seen:
                        raise ValueError("技能名无效或重复")
                    seen.add(name)
                    self.entries.append({"name": name, "description": str(meta.get("description", "")), "tools": meta.get("tools", []), "enabled": name not in self.disabled and meta.get("enabled", True) is not False, "body": body, "path": str(path)})
                except (ValueError, OSError) as exc:
                    self.entries.append({"name": path.parent.name, "description": "加载失败", "enabled": False, "error": str(exc), "tools": [], "body": "", "path": str(path)})

    def catalog(self):
        return [{k: v for k, v in e.items() if k != "body"} for e in self.entries]

    def prompt_catalog(self):
        return json.dumps([{ "name": e["name"], "description": e["description"]} for e in self.entries if e["enabled"]], ensure_ascii=False)

    def load(self, name):
        for entry in self.entries:
            if entry["name"] == name and entry["enabled"]:
                return entry["body"]
        raise ValueError("技能不存在或已禁用")
