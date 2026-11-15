"""启动前保护和迁移用户数据；只依赖标准库，不导入应用或执行旧源码。"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from functools import cmp_to_key, lru_cache
from pathlib import Path, PurePosixPath

FIXED_USER_ROOTS = ("configs", "ok_tasks", "ok_import", "ok_templates", "screenshots", "logs", ".ok_cache")
APP_PATH = "data/apps/NoNo"
STATE_PATH = "data/state/NoNo"
USER_PATH = "data/user/NoNo"
BACKUP_PATH = "data/backups/NoNo"
_CONTEXTS = {}
_THREAD_LOCK = threading.RLock()


class UserDataError(RuntimeError):
    def __init__(self, code, path=""):
        super().__init__(f"用户数据迁移失败，原件已保留（{code}）：{path}")
        self.code = code
        self.path = path


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise UserDataError("USER_PATH_INVALID")
    for part in value.split("/"):
        base = part.split(".", 1)[0].upper()
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or any(ord(char) < 32 or char in ':<>"|?*' for char in part)
                or base in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", base)):
            raise UserDataError("USER_PATH_INVALID")
    return PurePosixPath(value)


def _logical_path(path):
    name = os.fspath(path)
    if name.startswith("\\\\?\\UNC\\"):
        name = "\\\\" + name[8:]
    elif name.startswith("\\\\?\\"):
        name = name[4:]
    return Path(name)


def _io_path(path):
    # 只在受检绝对路径的 IO 边界使用扩展前缀，不依赖进程清单或系统 LongPathsEnabled。
    path = Path(path)
    if not path.is_absolute():
        raise UserDataError("USER_PATH_INVALID")
    name = str(path)
    if name.startswith("\\\\?\\"):
        return name
    if name.startswith("\\\\"):
        return "\\\\?\\UNC\\" + name[2:]
    return "\\\\?\\" + name


def _resolved_path(path):
    return _logical_path(Path(_io_path(path)).resolve())


def _ordinary(path, *, missing=False):
    try:
        item = os.lstat(_io_path(path))
    except FileNotFoundError:
        if missing:
            return None
        raise
    if (stat.S_ISLNK(item.st_mode) or getattr(item, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode))
            or (stat.S_ISREG(item.st_mode) and item.st_nlink != 1)):
        raise UserDataError("USER_PATH_LINK", str(path))
    return item


def _path(root, relative):
    path = root.joinpath(*_relative(relative).parts)
    for ancestor in reversed((path, *path.parents)):
        _ordinary(ancestor, missing=True)
    return path


def _canonical_root(root):
    # Rust canonicalize 使用 Win32 扩展前缀；协议路径仍按可搬迁的普通绝对路径比较。
    root = Path(os.path.abspath(_logical_path(root)))
    for ancestor in reversed((root, *root.parents)):
        item = _ordinary(ancestor)
        if not stat.S_ISDIR(item.st_mode):
            raise UserDataError("USER_ROOT_INVALID")
    return _resolved_path(root)


@lru_cache(maxsize=1)
def _kernel():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetStdHandle.argtypes = (wintypes.DWORD,)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    kernel.MoveFileExW.restype = wintypes.BOOL
    kernel.CompareStringOrdinal.argtypes = (wintypes.LPCWSTR, ctypes.c_int,
                                           wintypes.LPCWSTR, ctypes.c_int, wintypes.BOOL)
    kernel.CompareStringOrdinal.restype = ctypes.c_int
    return kernel


def _compare_names(left, right):
    # 文件名按 Windows 非语言学规则比较；casefold 会把 ß 展开成 ss，二者实际是不同目录。
    result = _kernel().CompareStringOrdinal(left, -1, right, -1, True)
    if result == 0:
        raise UserDataError("USER_NAME_COMPARE")
    return result - 2


def _find_name(names, requested):
    return next((name for name in names if _compare_names(name, requested) == 0), None)


def _distinct_names(names, error_code):
    ordered = sorted(names, key=cmp_to_key(_compare_names))
    for previous, current in zip(ordered, ordered[1:]):
        if _compare_names(previous, current) == 0:
            raise UserDataError(error_code, current)


def _handle_path(handle, *, directory=False):
    from ctypes import wintypes
    kernel = _kernel()
    # BY_HANDLE_FILE_INFORMATION 全部为 DWORD；固定 13 项同时检查链接数和重解析点。
    information = (wintypes.DWORD * 13)()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise UserDataError("USER_LEASE_HANDLE")
    attributes = information[0]
    if (bool(attributes & 0x10) != directory or attributes & 0x400
            or (not directory and information[10] != 1)):
        raise UserDataError("USER_LEASE_HANDLE")
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise UserDataError("USER_LEASE_HANDLE")
    return _logical_path(buffer.value)


def _open_handle(path, *, shared=False, directory=False, create=False):
    if os.name != "nt":
        raise UserDataError("USER_WINDOWS_REQUIRED")
    kernel = _kernel()
    access = 0x80000000 if shared or directory else 0xC0000000
    share = 3 if directory else (1 if shared else 0)
    flags = 0x200000 | (0x2000000 if directory else 0x80)
    handle = kernel.CreateFileW(_io_path(path), access, share, None, 4 if create else 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = str(path)
        raise error
    try:
        if _handle_path(handle, directory=directory) != _resolved_path(path):
            raise UserDataError("USER_LEASE_HANDLE")
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return handle


@contextmanager
def _pins(root, *paths):
    handles = []
    try:
        parents = {root, *root.parents}
        for path in paths:
            if not path.is_relative_to(root):
                raise UserDataError("USER_PATH_BOUNDARY")
            parents.update(path.parents)
        for parent in sorted(parents, key=lambda item: (len(item.parts), str(item))):
            metadata = _ordinary(parent, missing=True)
            if metadata is not None:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise UserDataError("USER_DIRECTORY_REQUIRED")
                handles.append(_open_handle(parent, directory=True))
        yield
    finally:
        for handle in reversed(handles):
            _kernel().CloseHandle(handle)


def _mkdir(root, path):
    if not path.is_relative_to(root):
        raise UserDataError("USER_PATH_BOUNDARY")
    current = root
    for part in path.relative_to(root).parts:
        child = current / part
        with _pins(root, child):
            item = _ordinary(child, missing=True)
            if item is None:
                os.mkdir(_io_path(child))
            elif not stat.S_ISDIR(item.st_mode):
                raise UserDataError("USER_DIRECTORY_REQUIRED", child.relative_to(root).as_posix())
        current = child


class FileLease:
    """Windows 共享模式与原生宿主、Rust 的文件租约完全相同。"""

    def __init__(self, root, name, *, shared=False, wait=False):
        self.handle = None
        self.pins = ExitStack()
        directory = _path(root, STATE_PATH)
        _mkdir(root, directory)
        path = directory / name
        self.pins.enter_context(_pins(root, path))
        deadline = time.monotonic() + (30 if wait else 0)
        try:
            while True:
                try:
                    self.handle = _open_handle(path, shared=shared, create=True)
                    break
                except OSError as error:
                    if getattr(error, "winerror", None) not in (32, 33) or time.monotonic() >= deadline:
                        raise UserDataError("USER_LEASE_BUSY", f"{STATE_PATH}/{name}") from error
                    time.sleep(0.03)
        except BaseException:
            self.pins.close()
            raise

    def close(self):
        if self.handle is not None:
            _kernel().CloseHandle(self.handle)
            self.handle = None
        self.pins.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


@contextmanager
def _reader(root, path):
    import msvcrt
    with _pins(root, path):
        handle = _open_handle(path, shared=True)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _kernel().CloseHandle(handle)
            raise
        with os.fdopen(descriptor, "rb") as stream:
            yield stream


def _file_record(root, path):
    with _reader(root, path) as stream:
        return {"size": os.fstat(stream.fileno()).st_size,
                "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def _children(root, directory):
    with _pins(root, directory / "__entry__"), os.scandir(_io_path(directory)) as entries:
        return [directory / name for name in sorted(entry.name for entry in entries)]


def _tree(root, directory):
    files, directories = {}, []

    def visit(parent):
        for path in _children(root, parent):
            relative = path.relative_to(directory).as_posix()
            _relative(relative)
            item = _ordinary(path)
            if stat.S_ISDIR(item.st_mode):
                directories.append(relative)
                visit(path)
            else:
                files[relative] = _file_record(root, path)

    visit(directory)
    _distinct_names([*files, *directories], "USER_PATH_ALIAS")
    return {"files": files, "directories": sorted(directories)}


def _stamp(root, path):
    item = _ordinary(path, missing=True)
    if item is None:
        return None
    if stat.S_ISDIR(item.st_mode):
        return {"kind": "directory", "stamp": _tree(root, path)}
    return {"kind": "file", "stamp": _file_record(root, path)}


def _move(root, source, target, *, replace=False):
    with _pins(root, source, target):
        _ordinary(source)
        _ordinary(target, missing=True)
        if not _kernel().MoveFileExW(_io_path(source), _io_path(target), 8 | (1 if replace else 0)):
            error = ctypes.WinError(ctypes.get_last_error())
            error.filename = str(source)
            error.filename2 = str(target)
            raise error


def _write_json(root, path, value):
    content = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False) + "\n").encode("utf-8")
    _mkdir(root, path.parent)
    with _pins(root, path):
        _ordinary(path, missing=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".user-data-", suffix=".tmp", dir=_io_path(path.parent))
        temporary = _logical_path(temporary)
        identity = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _move(root, temporary, path, replace=True)
        finally:
            current = _ordinary(temporary, missing=True)
            if current is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                os.unlink(_io_path(temporary))


def _read_object(root, path, *, missing=False):
    if _ordinary(path, missing=missing) is None:
        return None

    def invalid_constant(unused):
        raise ValueError()

    try:
        with _reader(root, path) as stream:
            value = json.loads(stream.read().decode("utf-8-sig"), parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise UserDataError("USER_JSON_INVALID", path.relative_to(root).as_posix()) from None
    return value


def _copy(root, source, destination, expected):
    if expected["kind"] == "directory":
        _mkdir(root, destination)
        for name in expected["stamp"]["directories"]:
            _mkdir(root, destination.joinpath(*_relative(name).parts))
        for name, record in expected["stamp"]["files"].items():
            _copy(root, source.joinpath(*_relative(name).parts), destination.joinpath(*_relative(name).parts),
                  {"kind": "file", "stamp": record})
    else:
        _mkdir(root, destination.parent)
        actual = _stamp(root, destination)
        if actual is not None:
            if actual != expected:
                raise UserDataError("USER_COPY_COLLISION", destination.relative_to(root).as_posix())
            return
        with _pins(root, source, destination), _reader(root, source) as incoming:
            temporary_root = _path(root, f"{STATE_PATH}/copy-temporary")
            _mkdir(root, temporary_root)
            # 临时副本不能落入来源快照，强制结束进程后残片也不能改变快照的完整文件集合。
            descriptor, temporary = tempfile.mkstemp(prefix=".user-copy-", suffix=".tmp", dir=_io_path(temporary_root))
            temporary = _logical_path(temporary)
            identity = os.fstat(descriptor)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    while block := incoming.read(1024 * 1024):
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if _stamp(root, temporary) != expected:
                    raise UserDataError("USER_SOURCE_CHANGED", source.relative_to(root).as_posix())
                _move(root, temporary, destination)
            finally:
                current = _ordinary(temporary, missing=True)
                if current is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(_io_path(temporary))
    if _stamp(root, destination) != expected:
        raise UserDataError("USER_COPY_INCOMPLETE", destination.relative_to(root).as_posix())


def _new_state():
    return {"schema_version": 1, "initialized": False, "sources": [], "roots": {},
            "conflicts": [], "capture": None, "pending": None}


def _validate_tree(tree):
    if (not isinstance(tree, dict) or set(tree) != {"files", "directories"}
            or not isinstance(tree["files"], dict) or not isinstance(tree["directories"], list)
            or not all(isinstance(name, str) for name in tree["directories"])
            or tree["directories"] != sorted(set(tree["directories"]))):
        raise UserDataError("USER_RECEIPT_INVALID")
    directories = set(tree["directories"])
    for name in [*tree["files"], *tree["directories"]]:
        path = _relative(name)
        for parent in path.parents:
            if str(parent) != "." and str(parent) not in directories:
                raise UserDataError("USER_RECEIPT_INVALID")
    _distinct_names([*tree["files"], *tree["directories"]], "USER_RECEIPT_INVALID")
    for record in tree["files"].values():
        if (not isinstance(record, dict) or set(record) != {"size", "sha256"}
                or type(record["size"]) is not int or record["size"] < 0
                or not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise UserDataError("USER_RECEIPT_INVALID")


def _validate_stamp(stamp):
    if stamp is None:
        return
    if (not isinstance(stamp, dict) or set(stamp) != {"kind", "stamp"}
            or stamp["kind"] not in ("directory", "file")):
        raise UserDataError("USER_RECEIPT_INVALID")
    _validate_tree(stamp["stamp"] if stamp["kind"] == "directory" else
                   {"files": {"file": stamp["stamp"]}, "directories": []})


def _validate_conflicts(conflicts):
    if not isinstance(conflicts, list):
        raise UserDataError("USER_RECEIPT_INVALID")
    for entry in conflicts:
        if (not isinstance(entry, dict) or set(entry) != {"root", "source", "unit", "destination"}
                or len(_relative(entry["root"]).parts) != 1
                or not isinstance(entry["source"], str) or not entry["source"].startswith(BACKUP_PATH + "/")
                or entry["destination"] != f"{USER_PATH}/{entry['root']}"
                or not isinstance(entry["unit"], list) or not entry["unit"]):
            raise UserDataError("USER_RECEIPT_INVALID")
        _relative(entry["source"])
        for part in entry["unit"]:
            if part != "":
                _relative(part)


def _validate_operation_state(state):
    _distinct_names(state["roots"], "USER_RECEIPT_INVALID")
    for name, record in state["roots"].items():
        if (len(_relative(name).parts) != 1 or not isinstance(record, dict)
                or set(record) not in ({"transaction_id"}, {"adopted_transaction"})):
            raise UserDataError("USER_RECEIPT_INVALID")
        identifier = next(iter(record.values()))
        if (len(_relative(identifier).parts) != 1
                or ("transaction_id" in record and not re.fullmatch(r"user-data-[0-9a-f]{32}", identifier))):
            raise UserDataError("USER_RECEIPT_INVALID")
    for captured, source in [(False, source) for source in state["sources"]] + [
            (True, source) for source in state["capture"] or []]:
        if not isinstance(source, dict):
            raise UserDataError("USER_SOURCE_INVALID")
        if (not isinstance(source, dict) or set(source) != ({"path", "kind", "tree", "source"} if captured else
                                                          {"path", "kind", "tree"})
                or not isinstance(source["path"], str) or not source["path"].startswith(BACKUP_PATH + "/")
                or source["kind"] not in ("working", "repo", "repo.checkout.old")):
            raise UserDataError("USER_SOURCE_INVALID")
        parts = _relative(source["path"]).parts
        prefix = "/".join(parts[:4])
        if source["path"] not in (f"{prefix}/original/{source['kind']}",
                                  f"{prefix}/original/{APP_PATH}/{source['kind']}"):
            raise UserDataError("USER_SOURCE_INVALID")
        _validate_tree(source["tree"])
        if captured and source["source"] != f"{APP_PATH}/{source['kind']}":
            raise UserDataError("USER_SOURCE_INVALID")
    pending = state["pending"]
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != {"id", "roots", "operations", "conflicts"}
                or not isinstance(pending["id"], str) or not re.fullmatch(r"user-data-[0-9a-f]{32}", pending["id"])
                or not isinstance(pending["roots"], list) or not isinstance(pending["operations"], list)
                or any(not isinstance(name, str) for name in pending["roots"])
                or len(set(pending["roots"])) != len(pending["roots"])
                or len(pending["operations"]) != len(pending["roots"])):
            raise UserDataError("USER_RECEIPT_INVALID")
        _distinct_names(pending["roots"], "USER_RECEIPT_INVALID")
        for name, operation in zip(pending["roots"], pending["operations"]):
            if (len(_relative(name).parts) != 1
                    or not isinstance(operation, dict)
                    or set(operation) != {"root", "original", "replacement", "staged", "backup"}
                    or operation["root"] != name
                    or operation["staged"] != f"{BACKUP_PATH}/{pending['id']}/prepared/{name}"
                    or operation["backup"] != f"{BACKUP_PATH}/{pending['id']}/previous/{name}"):
                raise UserDataError("USER_RECEIPT_INVALID")
            _validate_stamp(operation["original"])
            _validate_stamp(operation["replacement"])
        _validate_conflicts(pending["conflicts"])
    _validate_conflicts(state["conflicts"])


def _load_state(root):
    value = _read_object(root, _path(root, f"{STATE_PATH}/user-data.json"), missing=True)
    if value is None:
        return _new_state()
    _validate_state(value)
    return value


def _validate_state(value):
    if (not isinstance(value, dict) or set(value) != set(_new_state()) or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or type(value["initialized"]) is not bool
            or not isinstance(value["sources"], list) or not isinstance(value["roots"], dict)
            or (value["capture"] is not None and not isinstance(value["capture"], list))
            or (not value["initialized"] and (value["roots"] or value["sources"] or value["pending"] is not None))
            or (value["initialized"] and value["capture"] is not None)):
        raise UserDataError("USER_RECEIPT_INVALID")
    _validate_operation_state(value)


def _save_state(root, state):
    # 所有入口共享写入不变量，不能提交下一次启动会拒绝的成功收据。
    _validate_state(state)
    _write_json(root, _path(root, f"{STATE_PATH}/user-data.json"), state)


def _is_bare(root, path):
    return (_ordinary(path / ".git", missing=True) is None
            and _ordinary(path / "HEAD", missing=True) is not None
            and _ordinary(path / "objects", missing=True) is not None)


def _capture_live_sources(root, state):
    if state["initialized"]:
        return
    capture = state["capture"]
    if capture is None:
        identifier = "user-data-" + uuid.uuid4().hex
        capture = []
        for kind in ("working", "repo", "repo.checkout.old"):
            source = _path(root, f"{APP_PATH}/{kind}")
            item = _ordinary(source, missing=True)
            if item is None or (kind != "working" and _is_bare(root, source)):
                continue
            if not stat.S_ISDIR(item.st_mode):
                raise UserDataError("USER_SOURCE_INVALID", f"{APP_PATH}/{kind}")
            capture.append({"source": f"{APP_PATH}/{kind}", "kind": kind,
                            "path": f"{BACKUP_PATH}/{identifier}/original/{kind}",
                            "tree": _tree(root, source)})
        state["capture"] = capture
        _save_state(root, state)
    for source in capture:
        original, protected = _path(root, source["source"]), _path(root, source["path"])
        expected = {"kind": "directory", "stamp": source["tree"]}
        # 中断后只能接续同一份清单；新 working 字节不能冒充已经封存的来源。
        if _stamp(root, protected) != expected:
            if _stamp(root, original) != expected:
                raise UserDataError("USER_SOURCE_CHANGED", source["source"])
            _copy(root, original, protected, expected)
        if _stamp(root, original) != expected:
            raise UserDataError("USER_SOURCE_CHANGED", source["source"])
        record = {key: source[key] for key in ("path", "kind", "tree")}
        if record not in state["sources"]:
            state["sources"].append(record)
    state["capture"] = None
    state["initialized"] = True
    _save_state(root, state)


def _subtree(tree, prefix):
    prefix += "/"
    return {"files": {name[len(prefix):]: value for name, value in tree["files"].items()
                      if name.startswith(prefix)},
            "directories": [name[len(prefix):] for name in tree["directories"] if name.startswith(prefix)]}


def _register_originals(root, state, transaction_id, originals):
    if state["capture"] is not None:
        raise UserDataError("USER_CAPTURE_PENDING")
    if len(_relative(transaction_id).parts) != 1:
        raise UserDataError("USER_SOURCE_INVALID")
    app = originals.get(APP_PATH)
    if app is not None:
        if app.get("kind") != "directory":
            raise UserDataError("USER_SOURCE_INVALID")
        for kind in ("working", "repo", "repo.checkout.old"):
            if kind not in app["stamp"]["directories"]:
                continue
            path = f"{BACKUP_PATH}/{transaction_id}/original/{APP_PATH}/{kind}"
            source = _path(root, path)
            expected = _subtree(app["stamp"], kind)
            if _stamp(root, source) != {"kind": "directory", "stamp": expected}:
                raise UserDataError("USER_SOURCE_CHANGED", path)
            if kind != "working" and _is_bare(root, source):
                continue
            record = {"path": path, "kind": kind, "tree": expected}
            if record not in state["sources"]:
                state["sources"].append(record)
    state["initialized"] = True


def _adopt_accepted(root, state):
    accepted = _read_object(root, _path(root, f"{STATE_PATH}/accepted.json"), missing=True)
    if accepted is None:
        return False
    if accepted.get("user_data_schema_version") is not None:
        raise UserDataError("USER_RECEIPT_MISSING")
    transaction = accepted.get("transaction_id")
    if (type(accepted.get("schema_version")) is not int or accepted["schema_version"] != 1
            or not isinstance(transaction, str) or len(_relative(transaction).parts) != 1):
        raise UserDataError("USER_PREVIOUS_RECEIPT_INVALID")
    _relative(transaction)
    proof = _read_object(root, _path(root, f"{BACKUP_PATH}/{transaction}/protection.json"))
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1 or proof.get("complete") is not True
            or proof.get("transaction_id") != transaction or proof.get("bundle_id") != accepted.get("bundle_id")
            or not isinstance(proof.get("originals"), dict)):
        raise UserDataError("USER_PREVIOUS_RECEIPT_INVALID")
    _register_originals(root, state, transaction, proof["originals"])
    # 旧完整包的合法保护凭据证明七根已处理，不能因引入新收据重新复活用户删除的作品。
    state["roots"].update({name: {"adopted_transaction": transaction} for name in FIXED_USER_ROOTS})
    _save_state(root, state)
    return True


def _source_root(root, source, name):
    names = {path.split("/", 1)[0] for path in (*source["tree"]["files"], *source["tree"]["directories"])}
    matches = [candidate for candidate in names if _compare_names(candidate, name) == 0]
    if len(matches) > 1:
        raise UserDataError("USER_PATH_ALIAS", name)
    if not matches:
        return None
    relative = matches[0]
    path = _path(root, source["path"] + "/" + relative)
    expected = ({"kind": "file", "stamp": source["tree"]["files"][relative]}
                if relative in source["tree"]["files"] else
                {"kind": "directory", "stamp": _subtree(source["tree"], relative)})
    if _stamp(root, path) != expected:
        raise UserDataError("USER_SOURCE_CHANGED", source["path"] + "/" + relative)
    return path


def _validate_json(root, path, name):
    stamp = _stamp(root, path)
    if stamp is None:
        return
    if stamp["kind"] != "directory":
        if name in FIXED_USER_ROOTS:
            raise UserDataError("USER_DIRECTORY_REQUIRED", path.relative_to(root).as_posix())
        return
    for relative in stamp["stamp"]["files"]:
        parts = tuple(part.casefold() for part in _relative(relative).parts)
        known = ((name == "configs" and ((len(parts) == 1 and parts[0].endswith(".json"))
                  or (len(parts) == 3 and parts[0] == "custom_teams" and parts[2] == "team.json")
                  or parts == ("custom_chars", "custom_chars.json")))
                 or (name == "ok_tasks" and parts == ("manifest.json",))
                 or (name == "ok_templates" and parts == ("coco_annotations.json",))
                 or (name == ".ok_cache" and parts == ("schedule_tasks_cache.json",))
                 or (name == "ok_import" and ((len(parts) == 2 and parts[1] == "manifest.json")
                     or (len(parts) == 3 and parts[0] == ".plugin-staging" and parts[2] == "operation.json")
                     or (len(parts) == 4 and parts[0] == ".plugin-staging"
                         and parts[2] in ("incoming", "previous") and parts[3] == "manifest.json"))))
        if known:
            _read_object(root, path.joinpath(*_relative(relative).parts))


def _units(root, source, name):
    stamp = _stamp(root, source)
    if stamp["kind"] == "file" or name == "ok_templates":
        return [[""]]
    names = [path.name for path in _children(root, source)]
    if name == "ok_import":
        namespaces = [entry for entry in names if _compare_names(entry, ".plugin-staging") != 0]
        staging = source / ".plugin-staging"
        if _ordinary(staging, missing=True) is not None:
            for path in _children(root, staging):
                if _find_name(namespaces, path.name) is None:
                    namespaces.append(path.name)
        # 成员缺失本身也是事务状态，先按两端检测冲突，复制时才跳过来源没有的成员。
        return [[entry, ".plugin-staging/" + entry] for entry in namespaces]
    result = []
    for entry in names:
        path = source / entry
        directory = stat.S_ISDIR(_ordinary(path).st_mode)
        if name == "configs" and entry.casefold() == "custom_teams" and directory:
            children = _children(root, path)
            result.extend([[child.relative_to(source).as_posix()] for child in children] or [[entry]])
        elif name in ("configs", "screenshots", "logs", ".ok_cache") and directory and entry.casefold() != "custom_chars":
            tree = _tree(root, path)
            result.extend([[f"{entry}/{relative}"] for relative in tree["files"]])
            result.extend([[f"{entry}/{relative}"] for relative in tree["directories"]
                           if not any(candidate.startswith(relative + "/") for candidate in tree["files"]
                                      | {directory: None for directory in tree["directories"]})])
            if not tree["files"] and not tree["directories"]:
                result.append([entry])
        else:
            result.append([entry])
    return result


def _merge(root, source, staged, name, conflicts):
    for group in _units(root, source, name):
        existing = []
        for relative in group:
            try:
                existing.append(_stamp(root, staged / relative))
            except (NotADirectoryError, UserDataError) as error:
                if isinstance(error, UserDataError) and error.code != "USER_DIRECTORY_REQUIRED":
                    raise
                existing.append({"blocked": True})
        incoming = [_stamp(root, source / relative) for relative in group]
        empty_root = group == [""] and existing == [{"kind": "directory", "stamp": {"files": {}, "directories": []}}]
        if any(existing) and not empty_root:
            if existing != incoming:
                conflicts.append({"root": name, "source": source.relative_to(root).as_posix(),
                                  "unit": group, "destination": f"{USER_PATH}/{name}"})
            continue
        for relative, stamp in zip(group, incoming):
            if stamp is not None:
                _copy(root, source / relative, staged / relative, stamp)


def _resume_pending(root, state):
    pending = state["pending"]
    if pending is None:
        return
    for operation in pending["operations"]:
        target = _path(root, f"{USER_PATH}/{operation['root']}")
        staged = _path(root, operation["staged"])
        backup = _path(root, operation["backup"])
        current, saved, prepared = _stamp(root, target), _stamp(root, backup), _stamp(root, staged)
        old, new = operation["original"], operation["replacement"]
        if current == new and (old == new or saved == old or old is None) and prepared is None:
            continue
        if old == new and current == old:
            continue
        if current == old and saved is None and prepared == new:
            if old is not None:
                _mkdir(root, backup.parent)
                _move(root, target, backup)
            current, saved = None, old
        if current is None and saved == old and prepared == new:
            if new is not None:
                _mkdir(root, target.parent)
                _move(root, staged, target)
        else:
            raise UserDataError("USER_RECOVERY_IDENTITY", f"{USER_PATH}/{operation['root']}")
    for name in pending["roots"]:
        state["roots"][name] = {"transaction_id": pending["id"]}
    state["conflicts"].extend(pending["conflicts"])
    state["pending"] = None
    _save_state(root, state)


def _migrate_roots(root, state, names):
    _resume_pending(root, state)
    requested = []
    for name in names:
        if _find_name(state["roots"], name) is None and _find_name(requested, name) is None:
            requested.append(name)
    names = requested
    if not names:
        return
    sources = {name: [] for name in names}
    # 所有已知 JSON 都在任何目标发布前验证，坏队伍不能留下看似成功的半份配置。
    for name in names:
        _validate_json(root, _path(root, f"{USER_PATH}/{name}"), name)
        for source in state["sources"]:
            path = _source_root(root, source, name)
            if path is not None:
                _validate_json(root, path, name)
                sources[name].append(path)
    identifier = "user-data-" + uuid.uuid4().hex
    pending = {"id": identifier, "roots": names, "operations": [], "conflicts": []}
    for name in names:
        target = _path(root, f"{USER_PATH}/{name}")
        staged = _path(root, f"{BACKUP_PATH}/{identifier}/prepared/{name}")
        before = _stamp(root, target)
        if before is not None:
            _copy(root, target, staged, before)
        for source in sources[name]:
            if _ordinary(staged, missing=True) is None:
                _copy(root, source, staged, _stamp(root, source))
            elif _stamp(root, staged)["kind"] != "directory" or _stamp(root, source)["kind"] != "directory":
                if _stamp(root, staged) != _stamp(root, source):
                    pending["conflicts"].append({"root": name, "source": source.relative_to(root).as_posix(),
                                                "unit": [""], "destination": f"{USER_PATH}/{name}"})
            else:
                _merge(root, source, staged, name, pending["conflicts"])
        if _stamp(root, target) != before:
            raise UserDataError("USER_TARGET_CHANGED", f"{USER_PATH}/{name}")
        pending["operations"].append({"root": name, "original": before, "replacement": _stamp(root, staged),
                                      "staged": staged.relative_to(root).as_posix(),
                                      "backup": f"{BACKUP_PATH}/{identifier}/previous/{name}"})
    state["pending"] = pending
    _save_state(root, state)
    _resume_pending(root, state)


class RuntimeContext:
    def __init__(self, root, project, lease=None, native_handle=None, completed_roots=()):
        self.root, self.project, self.lease, self.native_handle = root, project, lease, native_handle
        self.closed = False
        self.completed_roots = set(completed_roots)

    def close(self):
        if self.lease is not None:
            self.lease.close()
        self.closed = True
        if _CONTEXTS.get(str(self.root / USER_PATH)) is self:
            _CONTEXTS.pop(str(self.root / USER_PATH), None)


def _portable_project(project):
    project = Path(os.path.abspath(project))
    if tuple(part.casefold() for part in project.parts[-4:]) != ("data", "apps", "nono", "working"):
        return None
    root = _canonical_root(project.parents[3])
    if _path(root, APP_PATH + "/working") != _resolved_path(project):
        raise UserDataError("USER_PROJECT_INVALID")
    return root


def _record_error(root, error):
    try:
        relative = getattr(error, "path", "")
        filename = getattr(error, "filename2", None) or getattr(error, "filename", None)
        if not relative and filename:
            path = _logical_path(filename)
            if path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
        _write_json(root, _path(root, f"{STATE_PATH}/user-data-error.json"), {
            "schema_version": 1, "code": getattr(error, "code", "USER_IO_ERROR"),
            "path": relative, "winerror": getattr(error, "winerror", None),
        })
    except (OSError, UserDataError):
        pass


def bootstrap_user_data(project_root=None, *, _native_helper=False):
    """在任何 ok/config 导入前调用，成功后把运行读租约持有至进程退出。"""
    project = Path(project_root) if project_root is not None else Path(__file__).absolute().parent.parent
    root = _portable_project(project)
    if root is None:
        return None
    user = root / USER_PATH
    supplied = os.environ.get("NONO_USER_DATA_ROOT")
    # 测试入口明确指定了隔离用户根时，不扫描与它无关的便携安装。
    if supplied and Path(os.path.abspath(supplied)) != user and not _native_helper:
        return None
    existing = _CONTEXTS.get(str(user))
    if existing is not None and not existing.closed:
        return existing
    native = ("" if _native_helper else os.environ.get("NONO_RUNTIME_LEASE", "")).split(":")
    native_handle = None
    if len(native) == 2 and native[0] == str(os.getpid()):
        try:
            native_handle = int(native[1])
            if _handle_path(native_handle) != _path(root, f"{STATE_PATH}/runtime.lock"):
                raise UserDataError("USER_NATIVE_LEASE_INVALID")
        except (ValueError, OSError):
            raise UserDataError("USER_NATIVE_LEASE_INVALID") from None
    with _THREAD_LOCK, ExitStack() as stack:
        existing = _CONTEXTS.get(str(user))
        if existing is not None and not existing.closed:
            return existing
        lease = None
        try:
            if native_handle is None:
                stack.enter_context(FileLease(root, "receive.lock", wait=True))
            if _ordinary(_path(root, f"{STATE_PATH}/receive.pending"), missing=True) is not None:
                raise UserDataError("USER_RECEIVE_PENDING")
            with FileLease(root, "user-data.lock", wait=True):
                state = _load_state(root)
                complete = state["initialized"] and state["pending"] is None and all(
                    _find_name(state["roots"], name) is not None for name in FIXED_USER_ROOTS)
            if native_handle is not None:
                if not complete:
                    raise UserDataError("USER_NATIVE_RECEIPT_MISSING")
            else:
                lease = FileLease(root, "runtime.lock", shared=complete)
                if not complete:
                    with FileLease(root, "user-data.lock", wait=True):
                        state = _load_state(root)
                        if not state["initialized"] and (state["capture"] is not None or not _adopt_accepted(root, state)):
                            _capture_live_sources(root, state)
                        _migrate_roots(root, state, FIXED_USER_ROOTS)
                    # receive 仍独占，释放写租约和取得读租约之间不允许接收器交换目录。
                    lease.close()
                    lease = FileLease(root, "runtime.lock", shared=True)
            os.environ["NONO_USER_DATA_ROOT"] = str(user)
            context = RuntimeContext(root, _resolved_path(project), lease, native_handle, state["roots"])
            _CONTEXTS[str(user)] = context
            if _native_helper:
                sys.stdout.buffer.write(b"R")
                sys.stdout.buffer.flush()
                if sys.stdin.buffer.read(1) != b"A":
                    raise UserDataError("USER_NATIVE_HANDOFF")
            return context
        except BaseException as error:
            if lease is not None:
                lease.close()
            _record_error(root, error)
            raise


def current_project_path():
    """向共享路径消费者提供已取得运行租约的本实例项目目录。"""
    user = os.environ.get("NONO_USER_DATA_ROOT")
    context = _CONTEXTS.get(str(Path(user))) if user else None
    return context.project if context is not None and not context.closed else None


def prepare_requested_root(relative, *, base_path=None):
    """只补迁已启动实例实际请求的首段；外部项目和越界路径由调用者保留原语义。"""
    user = os.environ.get("NONO_USER_DATA_ROOT")
    context = _CONTEXTS.get(str(Path(user))) if user else None
    if context is None or context.closed:
        return
    if base_path is not None and _resolved_path(Path(os.path.abspath(base_path))) != context.project:
        return
    name = Path(relative).parts[0] if Path(relative).parts else None
    if not name or name in (".", ".."):
        return
    _relative(name)
    if _find_name(context.completed_roots, name) is not None:
        return
    with _THREAD_LOCK, FileLease(context.root, "user-data.lock", wait=True):
        try:
            state = _load_state(context.root)
            if not state["initialized"]:
                raise UserDataError("USER_RECEIPT_MISSING")
            _migrate_roots(context.root, state, [name])
            context.completed_roots.update(state["roots"])
        except BaseException as error:
            _record_error(context.root, error)
            raise


def migrate_protected_sources(portable_root, transaction_id):
    """完整接收专用：stdin/stdout 必须继承外层两个真实独占文件句柄。"""
    root = _canonical_root(portable_root)
    kernel = _kernel()
    for standard, name in ((-10, "receive.lock"), (-11, "runtime.lock")):
        if _handle_path(kernel.GetStdHandle(standard)) != _path(root, f"{STATE_PATH}/{name}"):
            raise UserDataError("USER_RECEIVER_LEASE_INVALID")
        try:
            probe = _open_handle(_path(root, f"{STATE_PATH}/{name}"), shared=True)
        except OSError as error:
            if getattr(error, "winerror", None) not in (32, 33):
                raise UserDataError("USER_RECEIVER_LEASE_INVALID") from error
        else:
            kernel.CloseHandle(probe)
            raise UserDataError("USER_RECEIVER_LEASE_INVALID")
    with _THREAD_LOCK, FileLease(root, "user-data.lock", wait=True):
        try:
            journal = _read_object(root, _path(root, f"{STATE_PATH}/receive.json"))
            if (type(journal.get("schema_version")) is not int or journal["schema_version"] != 1
                    or journal.get("transaction_id") != transaction_id
                    or journal.get("stage") != "originals_protected"):
                raise UserDataError("USER_RECEIVER_JOURNAL_INVALID")
            state = _load_state(root)
            if not state["initialized"]:
                if state["capture"] is not None:
                    # 先完成此前封存计划；来源已变化则保留原计划失败，不让完整包假报成功。
                    _capture_live_sources(root, state)
                else:
                    _adopt_accepted(root, state)
            _register_originals(root, state, transaction_id, journal["originals"])
            _save_state(root, state)
            _migrate_roots(root, state, FIXED_USER_ROOTS)
        except BaseException as error:
            _record_error(root, error)
            raise


def _close_contexts():
    for context in list(_CONTEXTS.values()):
        context.close()


atexit.register(_close_contexts)


def main():
    parser = argparse.ArgumentParser(description="NoNo 启动前用户数据保护")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--native-bootstrap", type=Path)
    group.add_argument("--receive", type=Path)
    parser.add_argument("--transaction")
    arguments = parser.parse_args()
    try:
        if arguments.native_bootstrap is not None:
            context = bootstrap_user_data(arguments.native_bootstrap, _native_helper=True)
            if context is None:
                raise UserDataError("USER_PROJECT_INVALID")
            context.close()
        else:
            migrate_protected_sources(arguments.receive, arguments.transaction)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        # 标准输出是锁或握手通道，错误只输出固定提示，不泄露配置内容。
        print("用户数据迁移失败，请检查 data/state/NoNo/user-data-error.json（"
              + getattr(error, "code", "USER_IO_ERROR") + "）。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
