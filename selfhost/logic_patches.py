"""严格重放自有逻辑差异；数据独立于品牌字符串清单。

文本使用带精确行号和上下文的统一 diff，二进制资源使用明确标记的结果载荷。
重放先验证整个品牌后基线与所有文件结果，再写入隔离树，不尝试模糊匹配。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from selfhost.framework_tree import (
    IGNORED_DIR_NAMES,
    IGNORED_SUFFIXES,
    hash_tree,
    iter_payload_files,
    tree_digest,
)

SCHEMA_VERSION = 1
MANIFEST_NAME = "logic_patches.json"
PYTHON_SUFFIXES = {".py", ".pyi", ".pyw"}
NO_NEWLINE = b"\\ No newline at end of file\n"
HUNK_HEADER = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: [^\n]*)?\n")


def _sha256(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _digest(value: object, name: str, *, absent: bool = False) -> str | None:
    if absent and value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SystemExit(f"逻辑差异清单的 {name} 必须是小写 SHA-256")
    return value


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SystemExit("逻辑差异路径必须是非空相对路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or any(part in {".", ".."} or part.endswith((".", " ")) for part in path.parts)
        or any(re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.IGNORECASE) for part in path.parts)
        or re.search(r'[\\:\x00-\x1f<>"|?*]', value)
        or IGNORED_DIR_NAMES & set(path.parts)
        or path.suffix in IGNORED_SUFFIXES
    ):
        raise SystemExit(f"逻辑差异路径不规范或越界: {value!r}")
    return value


def _require_root(root: Path) -> None:
    if not root.is_dir() or root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise SystemExit(f"逻辑差异源树必须是实目录: {root}")


def _file_path(root: Path, relative: str) -> Path:
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise SystemExit(f"逻辑差异路径不允许符号链接或目录联接: {relative}")
        if path.exists() and path != root / relative and not path.is_dir():
            raise SystemExit(f"逻辑差异父路径已变成文件: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise SystemExit(f"逻辑差异路径越界: {relative}")
    if path.exists() and not path.is_file():
        raise SystemExit(f"逻辑差异目标已变成目录: {relative}")
    return path


def _split_lines(content: bytes) -> list[bytes]:
    # 只按 LF 切分，保留 CRLF、中文及源文件末尾没有换行的原始字节。
    parts = content.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


@dataclass(frozen=True)
class Hunk:
    before_start: int
    after_start: int
    before: tuple[bytes, ...]
    after: tuple[bytes, ...]


@dataclass(frozen=True)
class LogicPatch:
    path: str
    operation: str
    format: str
    payload: str
    before_sha256: str | None
    after_sha256: str | None
    content: bytes
    hunks: tuple[Hunk, ...]


def _parse_unified(content: bytes, path: str, operation: str) -> tuple[Hunk, ...]:
    before = "/dev/null" if operation == "add" else f"a/{path}"
    after = "/dev/null" if operation == "delete" else f"b/{path}"
    lines = _split_lines(content)
    if not content.endswith(b"\n") or lines[:2] != [f"--- {before}\n".encode(), f"+++ {after}\n".encode()]:
        raise SystemExit(f"统一差异头与清单路径或操作不一致: ok/{path}")
    hunks: list[Hunk] = []
    index = 2
    while index < len(lines):
        match = HUNK_HEADER.fullmatch(lines[index])
        if match is None:
            raise SystemExit(f"统一差异片段头无效: ok/{path}")
        before_start, before_count, after_start, after_count = (
            int(value) if value is not None else 1 for value in match.groups()
        )
        if (before_count and not before_start) or (after_count and not after_start):
            raise SystemExit(f"统一差异行号无效: ok/{path}")
        index += 1
        changes: list[tuple[bytes, bytes]] = []
        marked = False
        while index < len(lines) and not lines[index].startswith(b"@@ "):
            line = lines[index]
            if line == NO_NEWLINE:
                if not changes or marked:
                    raise SystemExit(f"统一差异末尾换行标记无效: ok/{path}")
                tag, data = changes[-1]
                changes[-1] = (tag, data[:-1])
                marked = True
            elif line[:1] in {b" ", b"+", b"-"} and line.endswith(b"\n"):
                changes.append((line[:1], line[1:]))
                marked = False
            else:
                raise SystemExit(f"统一差异包含无效内容: ok/{path}")
            index += 1
        old = tuple(data for tag, data in changes if tag != b"+")
        new = tuple(data for tag, data in changes if tag != b"-")
        if len(old) != before_count or len(new) != after_count:
            raise SystemExit(f"统一差异片段行数不符: ok/{path}")
        hunks.append(Hunk(
            before_start - 1 if before_count else before_start,
            after_start - 1 if after_count else after_start,
            old, new,
        ))
    if operation == "modify" and not hunks:
        raise SystemExit(f"修改文件的统一差异不能为空: ok/{path}")
    return tuple(hunks)


def _apply_unified(before: bytes, patch: LogicPatch) -> bytes:
    source = _split_lines(before)
    result: list[bytes] = []
    cursor = 0
    for hunk in patch.hunks:
        start = hunk.before_start
        if start < cursor or start > len(source):
            raise SystemExit(f"逻辑差异片段位置漂移: ok/{patch.path}")
        result.extend(source[cursor:start])
        if len(result) != hunk.after_start or tuple(source[start:start + len(hunk.before)]) != hunk.before:
            raise SystemExit(f"逻辑差异上下文漂移: ok/{patch.path}")
        result.extend(hunk.after)
        cursor = start + len(hunk.before)
    result.extend(source[cursor:])
    return b"".join(result)


@dataclass(frozen=True)
class PatchSet:
    framework_version: str
    baseline_tree_sha256: str
    entries: tuple[LogicPatch, ...]
    manifest_content: bytes
    source_digests: dict[str, str]

    def digest(self) -> str:
        return tree_digest(self.source_digests)

    def baseline_digests(self, current: dict[str, str], *, allow_before: bool = False) -> dict[str, str]:
        """逆向还原文件摘要，守住未写进清单的改动，避免只更新 pin 即通过。"""
        baseline = current.copy()
        for entry in self.entries:
            actual = current.get(entry.path)
            if actual != entry.after_sha256 and not (allow_before and actual == entry.before_sha256):
                raise SystemExit(f"逻辑差异文件摘要漂移: ok/{entry.path}")
            if entry.before_sha256 is None:
                baseline.pop(entry.path, None)
            else:
                baseline[entry.path] = entry.before_sha256
        if tree_digest(baseline) != self.baseline_tree_sha256:
            raise SystemExit("品牌处理后的框架基线树不一致；存在未登记改动或上游版本漂移")
        return baseline

    def apply_to(self, root: Path) -> tuple[list[str], list[str]]:
        """完整预检后重放，返回实际施加与已经满足后摘要的文件。"""
        _require_root(root)
        current = hash_tree(root)
        self.baseline_digests(current, allow_before=True)
        final_paths = set(current)
        for entry in self.entries:
            if entry.operation == "delete":
                final_paths.discard(entry.path)
            else:
                final_paths.add(entry.path)
        if any(parent.as_posix() in final_paths for relative in final_paths for parent in PurePosixPath(relative).parents):
            raise SystemExit("逻辑差异结果存在文件与父目录路径冲突")
        planned: list[tuple[Path, bytes | None]] = []
        applied: list[str] = []
        already: list[str] = []
        for entry in self.entries:
            path = _file_path(root, entry.path)
            before = path.read_bytes() if path.is_file() else None
            actual = _sha256(before)
            if actual == entry.after_sha256:
                already.append(entry.path)
                continue
            if actual != entry.before_sha256:
                raise SystemExit(f"逻辑差异基线摘要漂移: ok/{entry.path}")
            result = _apply_unified(before or b"", entry) if entry.format == "unified_diff" else entry.content
            if entry.operation == "delete":
                if result:
                    raise SystemExit(f"删除差异仍留下内容: ok/{entry.path}")
                output = None
            else:
                output = result
            if _sha256(output) != entry.after_sha256:
                raise SystemExit(f"逻辑差异修改后摘要不符: ok/{entry.path}")
            planned.append((path, output))
            applied.append(entry.path)
        # 任一文件的路径、基线、上下文或结果失败时，前面的文件也不能先被改写。
        for path, output in planned:
            if output is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(output)
        return applied, already


def load(manifest_path: Path, version: str) -> PatchSet:
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取逻辑差异清单 {manifest_path}: {exc}") from exc
    fields = {"schema_version", "framework_version", "baseline_tree_sha256", "entries"}
    if (
        not isinstance(manifest, dict) or set(manifest) != fields
        or type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION
    ):
        raise SystemExit("逻辑差异清单结构或 schema_version 不受支持")
    if manifest["framework_version"] != version:
        raise SystemExit(f"逻辑差异仅支持框架 {manifest['framework_version']}，目标是 {version}，需人工重审基线")
    baseline_sha = _digest(manifest["baseline_tree_sha256"], "baseline_tree_sha256")
    if not isinstance(manifest["entries"], list):
        raise SystemExit("逻辑差异 entries 必须是数组")
    entries: list[LogicPatch] = []
    seen_paths: set[str] = set()
    payloads: set[str] = set()
    sources = {manifest_path.name: hashlib.sha256(raw).hexdigest()}
    for item in manifest["entries"]:
        required = {"path", "operation", "format", "payload", "before_sha256", "after_sha256"}
        if not isinstance(item, dict) or set(item) != required:
            raise SystemExit("逻辑差异条目字段不完整或包含未知字段")
        path = _relative_path(item["path"])
        payload = _relative_path(item["payload"])
        if not payload.startswith("patches/") or path.casefold() in seen_paths or payload.casefold() in payloads:
            raise SystemExit(f"逻辑差异路径重复或载荷不在 patches/ 中: ok/{path}")
        seen_paths.add(path.casefold())
        payloads.add(payload.casefold())
        operation = item["operation"]
        before_sha = _digest(item["before_sha256"], f"{path}.before_sha256", absent=True)
        after_sha = _digest(item["after_sha256"], f"{path}.after_sha256", absent=True)
        valid = (
            operation == "add" and before_sha is None and after_sha is not None
            or operation == "delete" and before_sha is not None and after_sha is None
            or operation == "modify" and before_sha is not None and after_sha is not None and before_sha != after_sha
        )
        if not valid:
            raise SystemExit(f"逻辑差异操作与前后摘要不一致: ok/{path}")
        format_name = item["format"]
        if not isinstance(format_name, str) or format_name not in {"unified_diff", "binary"}:
            raise SystemExit(f"逻辑差异格式不受支持: ok/{path}")
        if format_name == "binary" and PurePosixPath(path).suffix.lower() in PYTHON_SUFFIXES:
            raise SystemExit(f"Python 源码必须使用可审查统一差异，不能保存二进制副本: ok/{path}")
        try:
            content = _file_path(manifest_path.parent, payload).read_bytes()
        except OSError as exc:
            raise SystemExit(f"逻辑差异载荷缺失或不可读: {payload}") from exc
        hunks = _parse_unified(content, path, operation) if format_name == "unified_diff" else ()
        if format_name == "binary" and (
            operation == "delete" and content or operation != "delete" and _sha256(content) != after_sha
        ):
            raise SystemExit(f"二进制载荷与修改后摘要不符: ok/{path}")
        sources[payload] = hashlib.sha256(content).hexdigest()
        entries.append(LogicPatch(path, operation, format_name, payload, before_sha, after_sha, content, hunks))
    patch_root = manifest_path.parent / "patches"
    if patch_root.is_symlink() or (hasattr(patch_root, "is_junction") and patch_root.is_junction()):
        raise SystemExit("逻辑差异 patches 路径不能是符号链接或目录联接")
    if patch_root.exists() and not patch_root.is_dir():
        raise SystemExit("逻辑差异 patches 路径必须是目录")
    actual_payloads = {path.relative_to(manifest_path.parent).as_posix().casefold() for path in iter_payload_files(patch_root)}
    if actual_payloads != payloads:
        raise SystemExit("逻辑差异目录存在未登记或缺失的载荷，不能跳过")
    return PatchSet(version, baseline_sha, tuple(entries), raw, sources)


def _is_text(content: bytes | None) -> bool:
    if content is None:
        return True
    if b"\0" in content:
        return False
    try:
        content.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _unified_payload(path: str, before: bytes | None, after: bytes | None) -> bytes:
    old_name = b"/dev/null" if before is None else f"a/{path}".encode()
    new_name = b"/dev/null" if after is None else f"b/{path}".encode()
    lines = list(difflib.diff_bytes(
        difflib.unified_diff, _split_lines(before or b""), _split_lines(after or b""),
        fromfile=old_name, tofile=new_name, n=3,
    ))
    # 空文件的新增／删除没有文本行，仍保留路径头以记录存在性变化。
    if not lines:
        return b"--- " + old_name + b"\n+++ " + new_name + b"\n"
    return b"".join(line if line.endswith(b"\n") else line + b"\n" + NO_NEWLINE for line in lines)


def capture(base_root: Path, modified_root: Path, version: str, manifest_path: Path) -> PatchSet:
    """把经品牌处理的 wheel 基线与工作树比较，只在指定隔离目录生成差异。"""
    _require_root(base_root)
    _require_root(modified_root)
    if manifest_path.exists() or (manifest_path.parent / "patches").exists():
        raise SystemExit("收集目标必须是空隔离目录，不能覆盖已有差异")
    baseline = hash_tree(base_root)
    modified = hash_tree(modified_root)
    records: list[dict] = []
    for relative in sorted(set(baseline) | set(modified)):
        if baseline.get(relative) == modified.get(relative):
            continue
        _relative_path(relative)
        before_path = _file_path(base_root, relative)
        after_path = _file_path(modified_root, relative)
        before = before_path.read_bytes() if before_path.is_file() else None
        after = after_path.read_bytes() if after_path.is_file() else None
        if _sha256(before) != baseline.get(relative) or _sha256(after) != modified.get(relative):
            raise SystemExit(f"收集期间源码发生变化，请完成编辑后重试: ok/{relative}")
        operation = "add" if before is None else "delete" if after is None else "modify"
        format_name = "unified_diff" if _is_text(before) and _is_text(after) else "binary"
        if PurePosixPath(relative).suffix.lower() in PYTHON_SUFFIXES and (
            format_name == "binary" or after is not None and after.startswith(b"\xef\xbb\xbf")
        ):
            raise SystemExit(f"Python 源码必须使用 UTF-8 无 BOM 文本: ok/{relative}")
        payload = f"patches/{relative}.{'patch' if format_name == 'unified_diff' else 'bin'}"
        content = _unified_payload(relative, before, after) if format_name == "unified_diff" else after or b""
        payload_path = _file_path(manifest_path.parent, payload)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(content)
        records.append({
            "path": relative, "operation": operation, "format": format_name, "payload": payload,
            "before_sha256": _sha256(before), "after_sha256": _sha256(after),
        })
    manifest = {
        "schema_version": SCHEMA_VERSION, "framework_version": version,
        "baseline_tree_sha256": tree_digest(baseline), "entries": records,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes((json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return load(manifest_path, version)
