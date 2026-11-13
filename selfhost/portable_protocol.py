"""NoNo incoming 协议的文件、身份和 ZIP 校验；只依赖标准库。"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

APP_NAME = "NoNo"
ORIGIN = "https://github.com/Canda15/nono-update.git"
MANIFEST_KEYS = {
    "schema_version", "bundle_id", "app_name", "build_kind", "source_sha",
    "base_update_version", "base_update_commit", "origin", "requirements_sha256",
    "app_seed_file", "launcher", "working_files", "payload_files", "payload_directories",
}
LAUNCHER_KEYS = {
    "version", "source_sha", "action_sha", "adaptation_sha256", "source_tree_sha256", "exe_sha256",
}
PRIVATE_WORKING_DIRS = {
    "configs", "ok_tasks", "ok_import", "ok_templates", "screenshots", "logs",
}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def json_bytes(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError("便携路径格式无效。")
    for component in value.split("/"):
        base = component.split(".", 1)[0].upper()
        if (not component or component in {".", ".."} or component.endswith((".", " "))
                or any(ord(char) < 32 or char in ':<>"|?*' for char in component)
                or base in {"CON", "PRN", "AUX", "NUL"}
                or re.fullmatch(r"(?:COM|LPT)[1-9]", base)):
            raise RuntimeError("便携路径包含越界或 Windows 文件别名。")
    return PurePosixPath(value)


def assert_regular(path: Path) -> None:
    metadata = path.lstat()
    if (stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode))):
        raise RuntimeError("便携载荷不允许链接、重解析点或特殊文件。")
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise RuntimeError("便携载荷不允许硬链接。")


def file_record(path: Path) -> dict:
    assert_regular(path)
    with path.open("rb") as stream:
        return {"size": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def collect_tree(root: Path) -> tuple[dict[str, dict], list[str]]:
    assert_regular(root)
    files, directories = {}, []
    aliases = set()
    for path in sorted(root.rglob("*")):
        assert_regular(path)
        relative = path.relative_to(root).as_posix()
        validate_relative(relative)
        if relative.casefold() in aliases:
            raise RuntimeError("便携载荷包含大小写路径别名。")
        aliases.add(relative.casefold())
        if path.is_dir():
            directories.append(relative)
        else:
            files[relative] = file_record(path)
    # Windows 的 Path 排序忽略大小写；协议目录顺序必须与 Rust/JSON 的字符串顺序一致。
    return files, sorted(directories)


def launcher_identity(proof: dict) -> dict:
    return {key: proof[key] for key in LAUNCHER_KEYS}


def seal_manifest(manifest: dict) -> dict:
    manifest = {key: value for key, value in manifest.items() if key != "bundle_id"}
    return {**manifest, "bundle_id": digest_bytes(canonical_json(manifest))}


def validate_manifest(manifest: dict) -> None:
    if (not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS
            or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1
            or manifest.get("app_name") != APP_NAME
            or manifest.get("origin") != ORIGIN or manifest.get("app_seed_file") != "app-seed.json"
            or manifest.get("build_kind") not in ("working_install", "commit_snapshot", "release_tag")):
        raise RuntimeError("便携载荷协议不匹配。")
    for key, length in (("bundle_id", 64), ("source_sha", 40), ("base_update_commit", 40),
                        ("requirements_sha256", 64)):
        if not isinstance(manifest.get(key), str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", manifest[key]):
            raise RuntimeError("便携载荷身份摘要无效。")
    pattern = (r"v?\d+\.\d+\.\d+(?:[-.](?:alpha|beta|rc)(?:\.\d+)?)?"
               if manifest["build_kind"] == "release_tag" else r"v?\d+\.\d+\.\d+")
    if not isinstance(manifest["base_update_version"], str) or not re.fullmatch(pattern, manifest["base_update_version"]):
        raise RuntimeError("便携载荷缺少正式版本锚点。")
    if seal_manifest(manifest) != manifest:
        raise RuntimeError("便携载荷内容身份不匹配。")
    launcher = manifest.get("launcher")
    if not isinstance(launcher, dict) or set(launcher) != LAUNCHER_KEYS or launcher["version"] != "1.2.7":
        raise RuntimeError("便携载荷启动器身份无效。")
    for key in LAUNCHER_KEYS - {"version"}:
        length = 40 if key in {"source_sha", "action_sha"} else 64
        if not isinstance(launcher[key], str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", launcher[key]):
            raise RuntimeError("便携载荷启动器摘要无效。")
    for collection in ("working_files", "payload_files"):
        if not isinstance(manifest[collection], dict):
            raise RuntimeError("便携文件清单格式无效。")
        aliases = set()
        for relative, record in manifest[collection].items():
            validate_relative(relative)
            if relative.casefold() in aliases:
                raise RuntimeError("便携文件清单存在 Windows 路径别名。")
            aliases.add(relative.casefold())
            if (not isinstance(record, dict) or set(record) != {"size", "sha256"}
                    or type(record["size"]) is not int or record["size"] < 0
                    or not isinstance(record["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                raise RuntimeError("便携文件长度或摘要无效。")
            if collection == "payload_files":
                allowed = (relative.startswith(("repo/", "working/", "python/"))
                           or relative in {"app-seed.json", "root/pyappify.yml", "root/build-info.json"})
                if (not allowed or relative.casefold().endswith(".pip_update_needed.tmp")
                        or (relative.startswith("working/")
                            and relative.split("/")[1].casefold() in PRIVATE_WORKING_DIRS)):
                    raise RuntimeError("便携载荷包含用户数据或未授权路径。")
    if (not isinstance(manifest["payload_directories"], list)
            or not all(isinstance(relative, str) for relative in manifest["payload_directories"])
            or manifest["payload_directories"] != sorted(set(manifest["payload_directories"]))):
        raise RuntimeError("便携目录清单格式无效。")
    for relative in manifest["payload_directories"]:
        validate_relative(relative)
        if (not (relative in {"repo", "working", "python", "root"}
                 or relative.startswith(("repo/", "working/", "python/")))
                or relative in manifest["payload_files"]
                or (relative.startswith("working/")
                    and relative.split("/")[1].casefold() in PRIVATE_WORKING_DIRS)
                or relative.casefold().endswith(".pip_update_needed.tmp")):
            raise RuntimeError("便携目录包含用户状态或未授权路径。")


def make_manifest(*, source_sha: str, version: str, commit: str, working: Path,
                  launcher: dict, kind: str, payload: Path | None = None) -> dict:
    working_files, _ = collect_tree(working)
    files, directories = collect_tree(payload) if payload is not None else ({}, [])
    files.pop("manifest.json", None)
    manifest = seal_manifest({
        "schema_version": 1, "app_name": APP_NAME, "build_kind": kind,
        "source_sha": source_sha, "base_update_version": version, "base_update_commit": commit,
        "origin": ORIGIN, "requirements_sha256": file_record(working / "requirements.txt")["sha256"],
        "app_seed_file": "app-seed.json", "launcher": launcher_identity(launcher),
        "working_files": working_files, "payload_files": files, "payload_directories": directories,
    })
    validate_manifest(manifest)
    return manifest


def selected_package(manifest: dict) -> dict:
    return {"schema_version": 1, "bundle_id": manifest["bundle_id"],
            "manifest_sha256": digest_bytes(json_bytes(manifest))}


def required_files(app_name: str, bundle_id: str) -> set[str]:
    if app_name != APP_NAME or not re.fullmatch(r"[0-9a-f]{64}", bundle_id):
        raise RuntimeError("便携应用名或包标识无效。")
    incoming = f"data/incoming/{app_name}/{bundle_id}"
    return {
        f"{app_name}.exe", f"data/incoming/{app_name}/selected.json", f"{incoming}/manifest.json",
        *(f"{incoming}/{relative}" for relative in (
            "repo/HEAD", "repo/config", "working/main.py", "working/config.py",
            "working/requirements.txt", "working/pyappify.yml", "working/selfhost/NoNo.exe",
            "working/selfhost/ok_pin.json", "python/python.exe", "python/python3.dll",
            "python/python312.dll", "app-seed.json", "root/pyappify.yml", "root/build-info.json",
        )),
    }


def verify_archive(archive: Path, app_name: str, manifest: dict) -> None:
    validate_manifest(manifest)
    prefix = f"{app_name}/"
    incoming = f"{prefix}data/incoming/{app_name}/{manifest['bundle_id']}/"
    expected = {f"{prefix}{app_name}.exe", f"{prefix}data/incoming/{app_name}/selected.json",
                incoming + "manifest.json", *(incoming + name for name in manifest["payload_files"])}
    if not {prefix + relative for relative in required_files(app_name, manifest["bundle_id"])} <= expected:
        raise RuntimeError("便携归档缺少完整运行环境或安装种子。")
    allowed_directories = {parent.as_posix() + "/" for name in expected
                           for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
    allowed_directories.update(incoming + relative + "/" for relative in manifest["payload_directories"])
    with zipfile.ZipFile(archive) as package:
        aliases, files = set(), {}
        for entry in package.infolist():
            name = entry.filename
            validate_relative(name[:-1] if entry.is_dir() else name)
            key = name.rstrip("/").casefold()
            if key in aliases:
                raise RuntimeError("便携归档存在重复成员或 Windows 路径别名。")
            aliases.add(key)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise RuntimeError("便携归档不允许符号链接或特殊成员。")
            if entry.is_dir():
                if name not in allowed_directories:
                    raise RuntimeError("便携归档包含未授权目录。")
            else:
                files[name] = entry
        if set(files) != expected:
            raise RuntimeError("便携归档包含缺失、过时或用户状态文件。")
        reconstructed = set()
        for entry in package.infolist():
            path = PurePosixPath(entry.filename.rstrip("/"))
            if entry.is_dir():
                reconstructed.add(path.as_posix() + "/")
            reconstructed.update(parent.as_posix() + "/" for parent in path.parents
                                 if parent.as_posix() != ".")
        payload_directories = {name[len(incoming):].rstrip("/") for name in reconstructed
                               if name.startswith(incoming) and name != incoming}
        if payload_directories != set(manifest["payload_directories"]):
            raise RuntimeError("便携归档无法重建完整载荷目录。")
        if (package.read(incoming + "manifest.json") != json_bytes(manifest)
                or package.read(f"{prefix}data/incoming/{app_name}/selected.json") != json_bytes(selected_package(manifest))):
            raise RuntimeError("便携归档选择清单或内容身份发生漂移。")
        for relative, record in manifest["payload_files"].items():
            entry = files[incoming + relative]
            if entry.file_size != record["size"]:
                raise RuntimeError("便携归档文件长度发生漂移。")
            with package.open(entry) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                    raise RuntimeError("便携归档文件摘要发生漂移。")
        with package.open(prefix + f"{app_name}.exe") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["launcher"]["exe_sha256"]:
                raise RuntimeError("便携归档根启动器摘要发生漂移。")
        if package.testzip() is not None:
            raise RuntimeError("便携归档 ZIP CRC 校验失败。")
