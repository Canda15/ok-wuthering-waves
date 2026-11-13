"""只读核验 NoNo PE、图标、UAC 与编译时源码身份，不启动界面或业务。"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path
from ctypes import wintypes


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_hidden(command: list[str], cwd: Path) -> str:
    environment = {key: value for key, value in os.environ.items()
                   if not re.search(r"TOKEN|PASSWORD|SECRET|API_KEY", key.upper())
                   and not key.upper().startswith(("PYAPPIFY_", "PYTHON", "PIP_"))}
    environment.update({"__COMPAT_LAYER": "RunAsInvoker", "PYTHONNOUSERSITE": "1"})
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120,
                            creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)
    if result.returncode:
        raise RuntimeError(f"只读启动器探针失败，退出码 {result.returncode}。")
    return result.stdout


def inspect_resources(executable: Path, icon: Path) -> dict:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel.FindResourceW.restype = ctypes.c_void_p
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.LoadResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel.LoadResource.restype = ctypes.c_void_p
    kernel.LockResource.argtypes = [ctypes.c_void_p]
    kernel.LockResource.restype = ctypes.c_void_p
    module = kernel.LoadLibraryExW(str(executable), None, 0x2 | 0x20)
    if not module:
        raise RuntimeError("无法只读加载 PE 资源。")

    def resource(kind: int, identifier: int) -> bytes:
        handle = kernel.FindResourceW(module, ctypes.c_void_p(identifier), ctypes.c_void_p(kind))
        if not handle:
            raise RuntimeError("PE 缺少必要资源。")
        size = kernel.SizeofResource(module, handle)
        address = kernel.LockResource(kernel.LoadResource(module, handle))
        if not address or not size:
            raise RuntimeError("PE 资源不完整。")
        return ctypes.string_at(address, size)

    try:
        manifest = resource(24, 1)
        if b'level="requireAdministrator"' not in manifest:
            raise RuntimeError("启动器 UAC 资源不符合配置。")
        # 固定 Tauri/Winres 把主图标组编译为 IDI_APPLICATION（32512），不是图像自身的编号 1。
        group = resource(14, 32512)
        _, kind, count = struct.unpack_from("<HHH", group)
        if kind != 1 or len(group) != 6 + count * 14:
            raise RuntimeError("启动器图标组格式无效。")
        embedded = []
        for index in range(count):
            entry = 6 + index * 14
            size, identifier = struct.unpack_from("<IH", group, entry + 8)
            data = resource(3, identifier)
            if len(data) != size:
                raise RuntimeError("启动器图标资源长度不匹配。")
            embedded.append(hashlib.sha256(data).hexdigest())
        original = icon.read_bytes()
        _, kind, count = struct.unpack_from("<HHH", original)
        if kind != 1:
            raise RuntimeError("源图标格式无效。")
        expected = []
        for index in range(count):
            size, offset = struct.unpack_from("<II", original, 6 + index * 16 + 8)
            expected.append(hashlib.sha256(original[offset:offset + size]).hexdigest())
        if Counter(embedded) != Counter(expected):
            raise RuntimeError("PE 内嵌图标与本仓库 NoNo 图标不一致。")
        return {"uac": "requireAdministrator", "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "icon_sha256": sha256(icon), "icon_images_sha256": sorted(embedded)}
    finally:
        kernel.FreeLibrary(module)


def inspect_executable(executable: Path, icon: Path) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("启动器 PE 核验需要 Windows。")
    executable = executable.resolve(strict=True)
    literal = str(executable).replace("'", "''")
    script = (
        f"$i = (Get-Item -LiteralPath '{literal}').VersionInfo; "
        "[ordered]@{ProductName=$i.ProductName; FileDescription=$i.FileDescription; "
        "CompanyName=$i.CompanyName; OriginalFilename=$i.OriginalFilename; "
        "ProductVersion=$i.ProductVersion; FileVersion=$i.FileVersion} | ConvertTo-Json -Compress"
    )
    pe = json.loads(run_hidden(["powershell", "-NoProfile", "-Command", script], executable.parent))
    if pe["ProductName"] != "NoNo" or pe["FileDescription"] != "NoNo":
        raise RuntimeError("PE 产品名或文件描述不是 NoNo。")
    for field in ("ProductVersion", "FileVersion"):
        if not re.fullmatch(r"1\.2\.7(?:\.0)?", pe[field]):
            raise RuntimeError("PE 版本不是 1.2.7。")
    if any(re.search(r"ok-ww|ok-script|pyappify", pe[field], re.I)
           for field in ("ProductName", "FileDescription", "CompanyName", "OriginalFilename")):
        raise RuntimeError("PE 元数据仍有原版名称。")
    info = json.loads(run_hidden([str(executable), "--nono-build-info"], executable.parent).strip().splitlines()[-1])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from launcher.apply import ACTION_COMMIT, RUST_COMMIT, verify_pin

    pin, _ = verify_pin()
    if (info.get("app_name"), info.get("version"), info.get("source_sha"), info.get("action_sha"),
            info.get("adaptation_sha256")) != (
            "NoNo", "1.2.7", RUST_COMMIT, ACTION_COMMIT, pin["adaptation_sha256"]):
        raise RuntimeError("编译时启动器身份与受审查源码不一致。")
    if not re.fullmatch(r"[0-9a-f]{64}", info.get("source_tree_sha256", "")):
        raise RuntimeError("启动器缺少实际源码树摘要。")
    return {"schema_version": 1, **info, "exe_sha256": sha256(executable), "pe": pe,
            "resources": inspect_resources(executable, icon)}


def main() -> int:
    parser = argparse.ArgumentParser(description="只读核验新版 NoNo 启动器工件。")
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--icon", type=Path, default=Path(__file__).resolve().parents[1] / "icons/icon.ico")
    parser.add_argument("--output", type=Path, required=True, help="输出可审阅源码与 PE 身份清单。")
    arguments = parser.parse_args()
    try:
        proof = inspect_executable(arguments.exe, arguments.icon)
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8", newline="\n")
        print("NoNo 1.2.7 的源码身份、PE、图标与 UAC 核验通过。")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"启动器工件核验失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
