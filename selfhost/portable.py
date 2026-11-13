"""构建 incoming 完整便携交付；当前工作树与正式裸 Git 更新锚点分别验证。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from selfhost.framework_tree import IGNORED_DIR_NAMES, IGNORED_SUFFIXES, iter_payload_files, tree_digest
from selfhost import portable_protocol as protocol

LAUNCHER_URL = (
    "https://github.com/Canda15/ok-wuthering-waves/releases/download/launcher-2/NoNo-win32.zip"
)
PROFILE_NAME = "China"
STABLE_VERSION_TAG = re.compile(r"v?\d+\.\d+\.\d+")
VERSION_TAG = re.compile(r"v?\d+\.\d+\.\d+(?:[-.](?:alpha|beta|rc)(?:\.\d+)?)?")
PRIVATE_ENVIRONMENT = {
    "GITHUB_TOKEN", "GH_TOKEN", "CNB_TOKEN", "OK_GH", "SIGNPATH_API_TOKEN",
    "GEMINI_API_KEY", "AI_API_KEY", "MIRRORCHYANUPLOADTOKEN", "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV",
    "NONO_USER_DATA_ROOT", "__COMPAT_LAYER",
}

PROBE_IMPORTS = ("ok", "selfhost", "pyappify", "ok.device.interaction_methods.foreground_input")


def process_environment(temporary_root: Path | None = None) -> dict[str, str]:
    environment = {
        **{key: value for key, value in os.environ.items()
           if key.upper() not in PRIVATE_ENVIRONMENT and not key.upper().startswith(("PYAPPIFY_", "PIP_"))},
        "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull,
        # 只用于隔离构建子进程；核验仍要求产物保留正式 requireAdministrator 资源。
        "__COMPAT_LAYER": "RunAsInvoker",
    }
    if temporary_root is not None:
        # 官方下载器和 pip 共用系统临时变量；完整构建只允许写入本次隔离目录。
        environment.update({key: str(temporary_root) for key in ("TMP", "TEMP", "RUNNER_TEMP")})
    return environment


def hidden_process_options() -> dict:
    options = {}
    if sys.platform == "win32":
        # 启动器和宿主是 GUI 子系统；显式等待退出，并隐藏构建用的辅助窗口。
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def diagnostic_tail(output: str | bytes | None) -> str:
    """只保留受控日志尾部；不同 Python 版本的超时输出可能是文本、字节或空值。"""
    if output is None:
        return "（无输出）"
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    for key, value in os.environ.items():
        if value and (key.upper() in PRIVATE_ENVIRONMENT - {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
                      or re.search(r"TOKEN|PASSWORD|SECRET|API_KEY", key.upper())):
            text = text.replace(value, "[已隐藏]")
    text = re.sub(r"(https?://)[^\s/@]+@", r"\1[已隐藏]@", text)
    text = re.sub(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:token|password|secret|api_key)=)[^\s&]+", r"\1[已隐藏]", text)
    return "\n".join(text.splitlines()[-12:])[-2000:] or "（无输出）"


def run(command: list[str], cwd: Path, *, timeout: int = 120,
        temporary_root: Path | None = None) -> str:
    role = Path(command[0]).name
    try:
        result = subprocess.run(command, cwd=cwd, env=process_environment(temporary_root), capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                **hidden_process_options())
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"{role} 超时（{timeout} 秒）。\n标准输出尾部：\n{diagnostic_tail(error.stdout)}"
            f"\n标准错误尾部：\n{diagnostic_tail(error.stderr)}"
        ) from None
    if result.returncode:
        raise RuntimeError(
            f"{role} 构建命令失败（退出码 {result.returncode}）。\n标准输出尾部：\n{diagnostic_tail(result.stdout)}"
            f"\n标准错误尾部：\n{diagnostic_tail(result.stderr)}"
        )
    return result.stdout


def git(root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(root), *arguments], root).strip()


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_path(root: Path, path: Path) -> Path:
    """所有递归清理和覆盖先核对实路径，拒绝符号链接、目录联接和越界。"""
    resolved_root = root.resolve()
    target = path.absolute()
    resolved_target = target.resolve()
    if resolved_target == resolved_root or not resolved_target.is_relative_to(resolved_root):
        raise RuntimeError(f"构建路径不在预期目录内：{target}")
    # .. 不能掩盖词法路径中的联接；边界和最终删除目标则统一使用规范实路径。
    for candidate in (target, *target.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise RuntimeError(f"构建路径不允许符号链接或目录联接：{candidate}")
    return resolved_target


def remove_tree(build_root: Path, path: Path) -> None:
    target = require_path(build_root, path)
    if target.exists():
        shutil.rmtree(target)


def deployment_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in iter_payload_files(root)
        if ".git" not in path.relative_to(root).parts
    }


def clean_caches(build_root: Path, root: Path) -> None:
    for name in IGNORED_DIR_NAMES:
        for path in root.rglob(name):
            if path.is_dir():
                remove_tree(build_root, path)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in IGNORED_SUFFIXES:
            require_path(build_root, path).unlink()


def prepare_snapshot(source: Path, destination: Path, version: str, build_root: Path) -> dict[str, str]:
    if not STABLE_VERSION_TAG.fullmatch(version):
        raise RuntimeError(f"更新底座没有合法版本标签：{version}")
    require_path(build_root, destination)
    native = source / "selfhost" / "NoNo.exe"
    if not native.is_file():
        raise RuntimeError("当前源码缺少已编译的 selfhost/NoNo.exe，请先运行 build_runtime.ps1。")
    entries = [
        line.strip().replace("\\", "/")
        for line in (source / "deploy.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for entry in entries:
        relative = PurePosixPath(entry)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or ":" in entry:
            raise RuntimeError(f"部署白名单路径越界：{entry}")
        require_path(source, source / entry)
        if entry != "pyappify" and not (source / entry).exists():
            raise RuntimeError(f"部署白名单的当前源码缺失：{entry}")
    # 只读取白名单中的已跟踪文件当前字节；不复制开发机的未跟踪配置和缓存。
    tracked = run(["git", "-C", str(source), "ls-files", "-z", "--", *entries], source)
    selected = {
        relative for relative in tracked.split("\0") if relative
        and not IGNORED_DIR_NAMES & set(PurePosixPath(relative).parts)
        and PurePosixPath(relative).suffix not in IGNORED_SUFFIXES
    }
    framework_tracked = run(["git", "-C", str(source), "ls-files", "-z", "--", "ok",
                             ".update_repo_gitignore"], source).split("\0")
    required_sources = {"main.py", "config.py", "requirements.txt", "pyappify.yml", "ok/__init__.py"}
    required_framework = {
        relative for relative in framework_tracked if relative.startswith("ok/")
        and not IGNORED_DIR_NAMES & set(PurePosixPath(relative).parts)
        and PurePosixPath(relative).suffix not in IGNORED_SUFFIXES
    }
    missing = (required_sources | required_framework) - selected
    if missing:
        raise RuntimeError(f"部署白名单未包含必要已跟踪文件：{', '.join(sorted(missing))}")
    # 旧准备函数会向 cwd 回退配置或向 site-packages 回退框架；必须先完整验证集合和实文件。
    files = {}
    for relative in sorted(selected):
        path = require_path(source, source / relative)
        if not path.is_file():
            raise RuntimeError(f"已跟踪部署文件缺失：{relative}")
        files[relative] = path
    native = require_path(source, native)
    ignore_source = require_path(source, source / ".update_repo_gitignore")
    if not ignore_source.is_file() or ".update_repo_gitignore" not in framework_tracked:
        raise RuntimeError("缺少已跟踪的部署忽略规则 .update_repo_gitignore。")

    destination.mkdir(parents=True, exist_ok=False)
    for relative, path in files.items():
        target = require_path(build_root, destination / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    target_native = destination / "selfhost" / "NoNo.exe"
    target_native.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(native, target_native)
    # 镜像发布使用同一份忽略规则；保留缓存、日志边界，但不采用用户根目录的 .gitignore。
    shutil.copy2(ignore_source, destination / ".gitignore")

    # 与标签发布共用内联准备逻辑；版本、依赖过滤和 pyappify 副本只写入隔离快照。
    from ok.update.inline_ok_requirements import remove_ok_requirements
    remove_ok_requirements(str(destination), version)
    if "deploy.txt" not in entries:
        (destination / "deploy.txt").unlink(missing_ok=True)
    clean_caches(build_root, destination)
    digests = deployment_digests(destination)
    for required in required_sources | {"selfhost/NoNo.exe"}:
        if required not in digests:
            raise RuntimeError(f"部署白名单未包含必要入口：{required}")
    return digests


def replace_deployment(snapshot: Path, destination: Path, build_root: Path, *, preserve_git: bool = False) -> None:
    require_path(build_root, destination)
    if preserve_git or (destination / "HEAD").is_file() or (destination / ".git").exists():
        raise RuntimeError("新版裸仓库只作为更新锚点，不能用部署快照覆盖。")
    for child in destination.iterdir():
        require_path(build_root, child)
        if child.is_dir():
            remove_tree(build_root, child)
        else:
            child.unlink()
    for child in snapshot.iterdir():
        target = require_path(build_root, destination / child.name)
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    if deployment_digests(destination) != deployment_digests(snapshot):
        raise RuntimeError("实际部署树与当前源码快照不一致。")


def fetch_launcher(destination: Path, app_name: str, candidate: Path | None = None,
                   proof_path: Path | None = None, *, published: bool = False) -> Path:
    """默认消费已构建候选；新固定 tag 只有明确选择发布来源时才下载。"""
    if app_name != "NoNo":
        raise RuntimeError("便携交付仅接受 NoNo 启动器。")
    launcher = destination / f"{app_name}.exe"
    if published:
        if candidate is not None or proof_path is not None:
            raise RuntimeError("候选启动器和固定发布来源不能同时选择。")
        archive = destination / "launcher.zip"
        with urllib.request.urlopen(LAUNCHER_URL, timeout=120) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        base = LAUNCHER_URL.rsplit("/", 1)[0]
        with urllib.request.urlopen(base + "/NoNo-win32-sha256.txt", timeout=120) as response:
            expected = response.read().decode("ascii").split()[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(archive) != expected:
            raise RuntimeError("固定启动器下载内容与发布的 SHA-256 不一致。")
        with zipfile.ZipFile(archive) as package:
            if {entry.filename for entry in package.infolist() if not entry.is_dir()} != {"NoNo/NoNo.exe"}:
                raise RuntimeError("固定启动器 ZIP 成员不匹配。")
            launcher.write_bytes(package.read("NoNo/NoNo.exe"))
        proof_path = destination / "launcher-build.json"
        with urllib.request.urlopen(base + "/launcher-build.json", timeout=120) as response:
            proof_path.write_bytes(response.read())
        candidate = launcher
        archive.unlink()
    else:
        candidate = candidate or Path(__file__).resolve().parents[1] / "build/launcher-dist/NoNo.exe"
        proof_path = proof_path or candidate.parent / "launcher-build.json"
        if not candidate.is_file() or not proof_path.is_file():
            raise RuntimeError("缺少新版候选启动器，请先运行 build_launcher.ps1 或传入 --launcher-exe。")
    from launcher.verify import inspect_executable
    proof = json.loads(proof_path.read_bytes())
    inspected = inspect_executable(candidate, Path(__file__).resolve().parents[1] / "icons/icon.ico")
    if proof != inspected:
        raise RuntimeError("候选启动器源码身份、PE、图标或摘要与构建清单不一致。")
    if candidate.resolve() != launcher.resolve():
        shutil.copy2(candidate, launcher)
    if sha256(launcher) != proof["exe_sha256"]:
        raise RuntimeError("候选启动器复制后摘要发生漂移。")
    (destination / "launcher-build.json").write_bytes(protocol.json_bytes(proof))
    return launcher


def read_app_state(bundle: Path, app_name: str) -> dict:
    path = bundle / "data" / "apps" / app_name / "app.json"
    if not path.is_file():
        raise RuntimeError("启动器 setup 没有生成同名应用的 app.json。")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("name") != app_name or not state.get("installed"):
        raise RuntimeError("启动器应用元数据的名字或安装状态不正确。")
    if state.get("current_profile") != PROFILE_NAME:
        raise RuntimeError("启动器应用元数据没有保留 China profile。")
    if not isinstance(state.get("current_version"), str) or not VERSION_TAG.fullmatch(state["current_version"]):
        raise RuntimeError("启动器应用元数据没有合法更新版本。")
    return state


def assert_update_anchor(repo: Path, state: dict, version: str, commit: str) -> None:
    if (repo / ".git").exists() or git(repo, "rev-parse", "--is-bare-repository") != "true":
        raise RuntimeError("正式更新底座必须是独立裸 Git 仓库。")
    if state["current_version"] != version:
        raise RuntimeError("当前部署文件覆盖后，启动器的正式更新版本发生漂移。")
    if git(repo, "rev-parse", "HEAD") != commit or git(repo, "rev-parse", f"{version}^{{commit}}") != commit:
        raise RuntimeError("当前部署文件覆盖后，正式 Git HEAD 或合法标签发生漂移。")
    urls = {profile["git_url"] for profile in state["profiles"] if profile.get("name") == PROFILE_NAME}
    if len(urls) != 1 or git(repo, "remote", "get-url", "origin") not in urls:
        raise RuntimeError("启动器应用元数据与 Git 更新来源不一致。")
    for url in urls:
        if not url.startswith("https://github.com/") or "@" in url:
            raise RuntimeError("便携包更新来源不能包含临时路径、地址或凭据。")


def probe_executable(executable: Path, working: Path, runtime: Path, *, check_deployment: bool,
                     provide_user_root: bool = False, temporary_root: Path | None = None) -> dict:
    code = (
        "import ctypes, importlib, importlib.metadata, json, os, pathlib, re, sys; "
        "image = ctypes.create_unicode_buffer(32768); "
        "ctypes.windll.kernel32.GetModuleFileNameW(None, image, len(image)); "
        "sources = {}\n"
    )
    if check_deployment:
        code += (
            f"for name in {PROBE_IMPORTS!r}:\n"
            "    sources[name] = importlib.import_module(name).__file__\n"
            "for name in ('PySide6.QtCore', 'cv2', 'numpy', 'openvino'):\n"
            "    importlib.import_module(name)\n"
            "for line in pathlib.Path('requirements.txt').read_text(encoding='utf-8').splitlines():\n"
            "    match = re.fullmatch(r'([\\w.-]+)(?:\\[[^]]+\\])?==([^\\s;]+)', line.strip())\n"
            "    if match:\n"
            "        assert importlib.metadata.version(match[1]) == match[2], match[1]\n"
        )
    code += (
        "print(json.dumps({'pid': os.getpid(), 'executable': sys.executable, 'image': image.value, "
        "'prefix': sys.prefix, 'python_version': f'{sys.version_info.major}.{sys.version_info.minor}', "
        "'sources': sources, 'user_data_root': os.getenv('NONO_USER_DATA_ROOT')}))\n"
    )
    environment = process_environment(temporary_root)
    portable_layout = (working.name == "working" and working.parent.name == "NoNo"
                       and working.parents[1].name == "apps" and working.parents[2].name == "data")
    portable_root = working.parents[3] if portable_layout else None
    if provide_user_root:
        if portable_root is None:
            raise RuntimeError("稳定用户根探针需要正式便携布局。")
        environment["NONO_USER_DATA_ROOT"] = str(portable_root / "data/user/NoNo")
    with subprocess.Popen([str(executable), "-c", code], cwd=working, env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace", **hidden_process_options()) as process:
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("便携运行时探针超时。") from None
        if process.returncode:
            raise RuntimeError(f"便携运行时探针失败（退出码 {process.returncode}）：{stdout}\n{stderr}")
        identity = json.loads(stdout.strip().splitlines()[-1])
        if identity["pid"] != process.pid:
            raise RuntimeError("业务没有在当前宿主自身进程中运行。")
    if any(Path(identity[key]).resolve() != executable.resolve() for key in ("image", "executable")):
        raise RuntimeError("便携进程没有使用预期的 EXE 入口。")
    if Path(identity["prefix"]).resolve() != runtime.resolve():
        raise RuntimeError("便携进程没有加载便携目录中的 Python 运行环境。")
    if (portable_root is not None and (not identity.get("user_data_root")
            or Path(identity["user_data_root"]).resolve() != (portable_root / "data/user/NoNo").resolve())):
        raise RuntimeError("便携进程没有使用统一稳定用户目录。")
    for name, path in identity["sources"].items():
        if not Path(path).resolve().is_relative_to(working.resolve()):
            raise RuntimeError(f"便携进程的 {name} 没有从当前部署树导入。")
    return identity


def required_bundle_files(app_name: str, bundle_id: str) -> set[str]:
    return protocol.required_files(app_name, bundle_id)


def verify_archive(archive: Path, app_name: str, manifest: dict) -> None:
    protocol.verify_archive(archive, app_name, manifest)


def incoming_payload(bundle: Path, manifest: dict) -> Path:
    protocol.validate_manifest(manifest)
    return bundle / "data/incoming/NoNo" / manifest["bundle_id"]


def verify_payload_tree(bundle: Path, manifest: dict) -> None:
    payload = incoming_payload(bundle, manifest)
    files, directories = protocol.collect_tree(payload)
    files.pop("manifest.json", None)
    if files != manifest["payload_files"] or directories != manifest["payload_directories"]:
        raise RuntimeError("完整 incoming 载荷发生漂移。")
    if (protocol.file_record(bundle / "NoNo.exe")["sha256"] != manifest["launcher"]["exe_sha256"]
            or (payload / "manifest.json").read_bytes() != protocol.json_bytes(manifest)
            or (bundle / "data/incoming/NoNo/selected.json").read_bytes()
            != protocol.json_bytes(protocol.selected_package(manifest))):
        raise RuntimeError("incoming 选择清单或启动器身份发生漂移。")


def archive_bundle(bundle: Path, output: Path, manifest: dict, build_root: Path) -> Path:
    require_path(build_root, bundle)
    require_path(build_root, output)
    verify_payload_tree(bundle, manifest)
    for relative in required_bundle_files(bundle.name, manifest["bundle_id"]):
        if not require_path(build_root, bundle / relative).is_file():
            raise RuntimeError("便携目录缺少必要环境、入口或安装种子。")
    # 避免把验证接收后产生的 live、用户配置或私有备份意外归档。
    files, directories = protocol.collect_tree(bundle)
    prefix = f"data/incoming/NoNo/{manifest['bundle_id']}/"
    expected = {"NoNo.exe", "data/incoming/NoNo/selected.json", prefix + "manifest.json",
                *(prefix + name for name in manifest["payload_files"])}
    if set(files) != expected:
        raise RuntimeError("归档根包含 live 或用户私有文件。")
    output.mkdir(parents=True, exist_ok=True)
    archive = require_path(build_root, output / "NoNo-win32-portable.zip")
    archive.unlink(missing_ok=True)
    seven_zip = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe"
    if seven_zip.is_file():
        run([str(seven_zip), "a", "-tzip", "-mx=5", "-bd", str(archive), bundle.name],
            bundle.parent, timeout=900)
    else:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as package:
            # 没有 7-Zip 时也保留裸库和 Python 的空目录，接收器按完整目录身份校验。
            for relative in directories:
                package.write(bundle / relative, f"{bundle.name}/{relative}/")
            for relative in sorted(files):
                package.write(bundle / relative, f"{bundle.name}/{relative}")
    verify_archive(archive, bundle.name, manifest)
    checksum = require_path(build_root, output / "NoNo-win32-sha256.txt")
    checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive


def verify_portable_metadata(bundle: Path, stage: Path, app_name: str) -> None:
    app = bundle / "data/apps" / app_name
    paths = [app / "app.json", app / "repo/config"]
    for pattern in ("*.pth", "*._pth", "pyvenv.cfg", "direct_url.json"):
        paths.extend((app / "python").rglob(pattern))
    temporary_names = (str(stage), stage.as_posix(), json.dumps(str(stage))[1:-1])
    for path in paths:
        if not path.is_file():
            raise RuntimeError("便携安装状态或裸仓库配置缺失。")
        content = path.read_text(encoding="utf-8").casefold()
        if any(name.casefold() in content for name in temporary_names) or "file://" in content:
            raise RuntimeError("便携元数据残留构建机临时来源。")


def package_incoming(installation: Path, destination: Path, build_info: dict,
                     launcher_proof: dict, build_root: Path) -> tuple[Path, dict]:
    require_path(build_root, installation)
    require_path(build_root, destination)
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(installation / "NoNo.exe", destination / "NoNo.exe")
    incoming = destination / "data/incoming/NoNo"
    incoming.mkdir(parents=True)
    payload = incoming / "preparing"
    payload.mkdir()
    app = installation / "data/apps/NoNo"
    for folder in ("repo", "working", "python"):
        # Git 对象常带只读属性；新交付副本采用普通文件，保证同包再次解压可覆盖 incoming。
        # 只复制新载荷字节，原安装和正式裸仓库的属性保持不变。
        shutil.copytree(app / folder, payload / folder, copy_function=shutil.copyfile)
    seed = read_app_state(installation, "NoNo")
    # 构建环境没有用户偏好，seed 只提供缺项默认；升级时由 Rust 合并旧 raw。
    seed.update({"running": False, "auto_start": False, "update_state": "idle",
                 "update_target_version": None, "update_error": None,
                 "app_starting_version": None, "update_note": []})
    seed.pop("current_version_missing", None)
    (payload / "app-seed.json").write_bytes(protocol.json_bytes(seed))
    (payload / "root").mkdir()
    shutil.copy2(installation / "pyappify.yml", payload / "root/pyappify.yml")
    (payload / "root/build-info.json").write_bytes(protocol.json_bytes(build_info))
    manifest = protocol.make_manifest(
        source_sha=build_info["source_sha"], version=build_info["base_update_version"],
        commit=build_info["base_update_commit"], working=payload / "working",
        launcher=launcher_proof, kind=build_info["build_kind"], payload=payload)
    (payload / "manifest.json").write_bytes(protocol.json_bytes(manifest))
    final = require_path(build_root, incoming / manifest["bundle_id"])
    payload.rename(final)
    (incoming / "selected.json").write_bytes(protocol.json_bytes(protocol.selected_package(manifest)))
    verify_payload_tree(destination, manifest)
    return destination, manifest


def verify_received_state(root: Path, manifest: dict, expected_preferences: dict | None = None) -> None:
    state = read_app_state(root, "NoNo")
    app = root / "data/apps/NoNo"
    assert_update_anchor(app / "repo", state, manifest["base_update_version"], manifest["base_update_commit"])
    if protocol.collect_tree(app / "working")[0] != manifest["working_files"]:
        raise RuntimeError("接收后的工作树没有保持本次源码。")
    for marker in ("repo.git.migrating", "repo.checkout.old", ".pip_update_needed.tmp"):
        if (app / marker).exists():
            raise RuntimeError("接收后仍有旧库或失败标记。")
    if state.get("update_state") != "idle" or state.get("update_target_version") is not None or state.get("update_error") is not None:
        raise RuntimeError("接收后没有隔离旧更新操作态。")
    if expected_preferences and any(state.get(key) != value for key, value in expected_preferences.items()):
        raise RuntimeError("接收后用户偏好或未知字段丢失。")
    receipt = json.loads((root / "data/state/NoNo/accepted.json").read_bytes())
    if receipt["bundle_id"] != manifest["bundle_id"] or receipt["source_sha"] != manifest["source_sha"]:
        raise RuntimeError("接收收据与源码身份不一致。")


def verify_package_reception(bundle: Path, manifest: dict, build_root: Path) -> None:
    """只运行接收 CLI；用合成旧文件验证实际 Windows 覆盖，不启动业务界面。"""
    check_root = bundle.parent / "reception"
    require_path(build_root, check_root)
    check_root.mkdir()
    expected = {}
    for legacy in (False, True):
        target = check_root / ("legacy" if legacy else "fresh") / "NoNo"
        target.parent.mkdir()
        shutil.copytree(bundle, target)
        if legacy:
            app = target / "data/apps/NoNo"
            (app / "working/configs").mkdir(parents=True)
            account = protocol.json_bytes({"accounts": [{"id": "合成账号"}], "enabled": False, "unknown": {"value": 0}})
            (app / "working/configs/MultiAccountConfig.json").write_bytes(account)
            (app / "working/custom-source.py").write_bytes("自定义 = '合成源码'\n".encode())
            (app / "repo/.git").mkdir(parents=True)
            (app / "repo/.git/HEAD").write_bytes(b"old non-bare head")
            (app / "repo.git.migrating").mkdir()
            (app / "working/.pip_update_needed.tmp").write_bytes(b"old marker")
            (app / "python").mkdir()
            (app / "python/old-user-file.bin").write_bytes(b"synthetic original")
            seed = json.loads((incoming_payload(bundle, manifest) / "app-seed.json").read_bytes())
            seed.update({"auto_start": False, "update_method": "AUTO_UPDATE_PRE_RELEASE",
                         "update_state": "failed", "update_target_version": "v0.0.1",
                         "update_error": "合成旧错误", "unknown": {"preserved": False}})
            (app / "app.json").write_bytes(protocol.json_bytes(seed))
            (target / "data/config").mkdir()
            (target / "data/config/app_config.json").write_bytes(protocol.json_bytes({"Language": "zh-CN", "unknown": {"preserved": True}}))
            expected = {"auto_start": False, "update_method": "AUTO_UPDATE_PRE_RELEASE", "unknown": {"preserved": False}}
        run([str(target / "NoNo.exe"), "--nono-receive-only"], target, timeout=1200)
        verify_received_state(target, manifest, expected if legacy else None)
        if legacy and (target / "data/user/NoNo/configs/MultiAccountConfig.json").read_bytes() != account:
            raise RuntimeError("实际旧目录迁移没有保持合成账号原件。")
        # 同包重新解压只覆盖 incoming；后来的用户偏好不能被 seed 重新播种。
        app_json = target / "data/apps/NoNo/app.json"
        state = json.loads(app_json.read_bytes())
        state["auto_start"] = True
        state["update_method"] = "MANUAL_UPDATE"
        app_json.write_bytes(protocol.json_bytes(state))
        before = app_json.read_bytes()
        shutil.copytree(bundle / "data/incoming", target / "data/incoming", dirs_exist_ok=True)
        run([str(target / "NoNo.exe"), "--nono-receive-only"], target, timeout=1200)
        if app_json.read_bytes() != before:
            raise RuntimeError("同包重复接收覆盖了后来修改的用户偏好。")


def build_portable(source: Path, *, source_sha: str | None = None, release_tag: str | None = None,
                   launcher_exe: Path | None = None, launcher_proof: Path | None = None,
                   published_launcher: bool = False) -> Path:
    if sys.platform != "win32":
        raise RuntimeError("便携环境、根接收和原生进程验证需要 Windows。")
    actual_sha = git(source, "rev-parse", "HEAD")
    if source_sha is not None and source_sha != actual_sha:
        raise RuntimeError("当前检出源码 SHA 与本次编译要求不一致。")
    if release_tag is not None and not VERSION_TAG.fullmatch(release_tag):
        raise RuntimeError("正式便携发布必须使用合法的正式或 alpha/beta/rc 标签。")
    if release_tag is None and not (source / "selfhost/NoNo.exe").is_file():
        raise RuntimeError("缺少已编译的原生宿主，请先运行 build_runtime.ps1。")
    configuration = (source / "pyappify.yml").read_bytes()
    if not re.search(rb'^name:\s*"NoNo"\s*$', configuration, re.MULTILINE):
        raise RuntimeError("便携交付需要明确的 NoNo 应用名。")
    build_root = require_path(source, source / "build")
    build_root.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="portable-", dir=build_root)
    stage = require_path(build_root, Path(temporary.name))
    try:
        process_temp = stage / "process-temp"
        process_temp.mkdir()
        installation = stage / "installation/NoNo"
        installation.mkdir(parents=True)
        print("核验本次新版候选启动器。", flush=True)
        launcher = fetch_launcher(installation, "NoNo", launcher_exe, launcher_proof, published=published_launcher)
        proof = json.loads((installation / "launcher-build.json").read_bytes())
        (installation / "pyappify.yml").write_bytes(configuration)
        setup = [str(launcher), "-c", "setup", "-p", PROFILE_NAME]
        if release_tag:
            setup += ["--setup-version", release_tag]
        print("安装正式运行环境与裸 Git 更新底座。", flush=True)
        run(setup, installation, timeout=1800, temporary_root=process_temp)
        state = read_app_state(installation, "NoNo")
        version = state["current_version"]
        app = installation / "data/apps/NoNo"
        repo, working, runtime = app / "repo", app / "working", app / "python"
        commit = git(repo, "rev-parse", "HEAD")
        if release_tag and version != release_tag:
            raise RuntimeError("正式 setup 没有检出本次发布标签。")
        assert_update_anchor(repo, state, version, commit)
        if not release_tag:
            snapshot = stage / "snapshot"
            digests = prepare_snapshot(source, snapshot, version, build_root)
            replace_deployment(snapshot, working, build_root)
            (installation / "pyappify.yml").write_bytes(configuration)
            install_manifest = protocol.make_manifest(
                source_sha=actual_sha, version=version, commit=commit, working=working,
                launcher=proof, kind="working_install")
            manifest_path = installation / "working-install.json"
            manifest_path.write_bytes(protocol.json_bytes(install_manifest))
            print("安装当前工作树依赖并严格保存完整 App。", flush=True)
            run([str(launcher), "--nono-install-working", str(manifest_path), "-p", PROFILE_NAME],
                installation, timeout=1800, temporary_root=process_temp)
            record = json.loads((installation / "data/state/NoNo/install-working.json").read_bytes())
            if record.get("state") != "completed":
                raise RuntimeError("当前工作树安装没有提交完成记录。")
            state = read_app_state(installation, "NoNo")
        else:
            digests = deployment_digests(working)
        assert_update_anchor(repo, state, version, commit)
        profile = next(item for item in state["profiles"] if item["name"] == PROFILE_NAME)
        if profile.get("main_script") != "selfhost/NoNo.exe":
            raise RuntimeError("完整 App 未使用当前原生业务入口。")
        if (installation / "pyappify.yml").read_bytes() != configuration:
            raise RuntimeError("根配置与本次源码配置不一致。")
        if deployment_digests(working) != digests:
            raise RuntimeError("安装后当前工作树源码发生漂移。")
        if (working / ".pip_update_needed.tmp").exists():
            raise RuntimeError("当前工作树残留未完成依赖标记。")
        run([sys.executable, str(source / "scripts/sync_ok.py"), "--check", "--root", str(working)], source)
        from ok.update.inline_ok_requirements import verify_inlined_pyappify
        verify_inlined_pyappify(str(working))
        print("验证便携 Python、原生 PID 与稳定用户根。", flush=True)
        identity = probe_executable(runtime / "python.exe", working, runtime, check_deployment=True,
                                    provide_user_root=True, temporary_root=process_temp)
        probe_executable(working / "selfhost/NoNo.exe", working, runtime, check_deployment=True,
                         temporary_root=process_temp)
        if identity["python_version"] != profile.get("requires_python"):
            raise RuntimeError("便携 Python 版本与当前运行配置不一致。")
        for folder in (installation / "cache", installation / "data/logs", installation / "logs"):
            remove_tree(build_root, folder)
        clean_caches(build_root, installation)
        if deployment_digests(working) != digests:
            raise RuntimeError("缓存清理改变了业务部署文件。")
        verify_portable_metadata(installation, stage, "NoNo")
        build_info = {
            "schema_version": 1, "build_kind": "release_tag" if release_tag else "commit_snapshot",
            "source_sha": actual_sha, "base_update_version": version, "base_update_commit": commit,
            "deployment_tree_sha256": tree_digest(digests), "deployment_files": digests,
            "framework_pin": json.loads((working / "selfhost/ok_pin.json").read_bytes()),
            "launcher": proof, "app_metadata_file": "app-seed.json",
            "verification": {"python": True, "native_pid": True, "stable_user_root": True},
        }
        bundle, manifest = package_incoming(installation, stage / "package/NoNo", build_info, proof, build_root)
        print("验证新目录、旧非裸目录和同包重复覆盖接收。", flush=True)
        verify_package_reception(bundle, manifest, build_root)
        archive = archive_bundle(bundle, build_root / "portable-dist", manifest, build_root)
        print(f"完整便携包已生成并验证：{archive}", flush=True)
        return archive
    finally:
        require_path(build_root, stage)
        temporary.cleanup()

def main() -> int:
    parser = argparse.ArgumentParser(description="构建并验证完整 NoNo Windows 便携包。")
    parser.add_argument("--source-sha", help="普通编译需要交付的完整源码 SHA。")
    parser.add_argument("--release-tag", help="沿用正式镜像发布标签（支持 alpha/beta/rc）；省略时交付当前源码快照。")
    parser.add_argument("--launcher-exe", type=Path, help="本次已构建并核验的 NoNo 1.2.7 候选 EXE。")
    parser.add_argument("--launcher-proof", type=Path, help="候选源码与 PE 清单；默认取 EXE 同目录 launcher-build.json。")
    parser.add_argument("--published-launcher", action="store_true", help="明确使用固定 launcher-2 发布来源；该资产需已发布。")
    arguments = parser.parse_args()
    try:
        build_portable(Path(__file__).resolve().parents[1], source_sha=arguments.source_sha,
                       release_tag=arguments.release_tag, launcher_exe=arguments.launcher_exe,
                       launcher_proof=arguments.launcher_proof, published_launcher=arguments.published_launcher)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"便携打包失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
