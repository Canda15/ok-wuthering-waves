"""同步、差异重放与部署校验共用的框架源码树摘要算法。"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 仅保留原同步脚本的编译缓存例外；输入源码和生成资源都参与摘要。
IGNORED_DIR_NAMES = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def iter_payload_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise SystemExit(f"源码树不允许符号链接或目录联接: {path}")
        if not path.is_file():
            continue
        if IGNORED_DIR_NAMES & set(path.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        files.append(path)
    return files


def hash_tree(root: Path) -> dict[str, str]:
    """返回相对 POSIX 路径到文件 SHA-256 的映射，保持既有跨平台契约。"""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in iter_payload_files(root)
    }


def tree_digest(digests: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(digests):
        digest.update(f"{relative}\0{digests[relative]}\n".encode())
    return digest.hexdigest()
