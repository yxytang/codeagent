from __future__ import annotations

import difflib
import hashlib
import os
import shutil
from pathlib import Path


IGNORED = {".git", ".xcode", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build", ".next", ".idea"}
SECRET_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "id_rsa", "id_ed25519"}
MAX_FILES = 800
MAX_BYTES = 24 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024


def blocked(path: Path):
    return any(p.casefold() in IGNORED or p.casefold() in SECRET_NAMES or p.casefold().startswith(".env.") or p.casefold().endswith((".pem", ".key", ".p12", ".pfx")) for p in path.parts)


def safe_path(root: Path, name: str):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("文件路径必须是相对路径，使用 / 分隔")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or blocked(relative):
        raise ValueError("路径越界或指向受保护文件")
    for part in relative.parts:
        stem = part.split(".", 1)[0].upper()
        if part.endswith((" ", ".")) or stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("路径包含保留名称或末尾空格/点")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("不允许访问符号链接或 junction")
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("路径越界")
    return resolved


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def inventory(root: Path):
    result = {}
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not blocked((base / d).relative_to(root)) and not (base / d).is_symlink() and not (hasattr(base / d, "is_junction") and (base / d).is_junction()))
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root)
            if blocked(relative) or path.is_symlink():
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f"文件 {relative} 超过 1MB；请选择更小的项目目录")
            total += size
            if len(result) >= MAX_FILES or total > MAX_BYTES:
                raise ValueError("项目快照超过 800 个文件或 24MB；第一版请选择较小的代码目录")
            result[relative.as_posix()] = path.read_bytes()
    return result


class Workspace:
    def __init__(self, source: Path, base: Path):
        if not source.is_dir() or source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
            raise ValueError("工作目录不存在，或是链接目录")
        self.source = source.resolve()
        self.base = base
        self.original = inventory(self.source)
        base.mkdir(parents=True, exist_ok=False)
        for name, data in self.original.items():
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def clone(self, target: Path, overlays=None):
        shutil.copytree(self.base, target)
        for name, content in (overlays or {}).items():
            path = safe_path(target, name)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, "utf-8", newline="")
        return target

    def diffs(self, changes):
        result = []
        for name, content in sorted(changes.items()):
            old_bytes = self.original.get(name)
            old = (old_bytes or b"").decode("utf-8")
            if content is not None and old_bytes == content.encode("utf-8"):
                continue
            if content is None and old_bytes is None:
                continue
            diff = "".join(difflib.unified_diff(old.splitlines(True), (content or "").splitlines(True), fromfile="a/" + name, tofile="b/" + name))
            result.append({"path": name, "kind": "deleted" if content is None else ("added" if old_bytes is None else "modified"), "diff": diff, "content": content, "base_hash": digest(old_bytes)})
        return result


def apply_patches(root: Path, patches):
    """Validate all hashes first; restore touched files if a write fails."""
    backups = {}
    paths = {}
    for patch in patches:
        path = safe_path(root, patch["path"])
        if path.exists() and not path.is_file():
            raise ValueError(f"{patch['path']} 已变成目录")
        data = path.read_bytes() if path.exists() else None
        if digest(data) != patch["base_hash"]:
            raise ValueError(f"{patch['path']} 已被外部修改，请重新运行任务")
        backups[patch["path"]] = data
        paths[patch["path"]] = path
    touched = []
    try:
        for patch in patches:
            path = paths[patch["path"]]
            # Recheck immediately before writing to reduce the external-editor race window.
            if digest(path.read_bytes() if path.exists() else None) != patch["base_hash"]:
                raise ValueError(f"{patch['path']} 在合并时被修改")
            touched.append(patch["path"])
            if patch["content"] is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".xcode-tmp")
                if temp.exists():
                    raise ValueError(f"临时文件已存在：{temp.name}")
                try:
                    temp.write_text(patch["content"], "utf-8", newline="")
                    temp.replace(path)
                finally:
                    temp.unlink(missing_ok=True)
    except Exception:
        for name in reversed(touched):
            path = paths[name]
            if backups[name] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(backups[name])
        raise
