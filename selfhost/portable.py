"""构建便携交付；普通提交覆盖当前部署快照，正式发布沿用镜像标签。"""

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

LAUNCHER_URL = (
    "https://github.com/Canda15/ok-wuthering-waves/releases/download/launcher-1/NoNo-win32.zip"
)
PROFILE_NAME = "China"
VERSION_TAG = re.compile(r"v?\d+\.\d+\.\d+")
PRIVATE_ENVIRONMENT = {
    "GITHUB_TOKEN", "GH_TOKEN", "CNB_TOKEN", "OK_GH", "SIGNPATH_API_TOKEN",
    "GEMINI_API_KEY", "AI_API_KEY", "MIRRORCHYANUPLOADTOKEN", "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV",
}

PROBE_IMPORTS = ("ok", "selfhost", "pyappify", "ok.device.interaction_methods.foreground_input")


def process_environment() -> dict[str, str]:
    return {
        **{key: value for key, value in os.environ.items()
           if key.upper() not in PRIVATE_ENVIRONMENT and not key.upper().startswith(("PYAPPIFY_", "PIP_"))},
        "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull,
    }


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


def run(command: list[str], cwd: Path, *, timeout: int = 120) -> str:
    role = Path(command[0]).name
    try:
        result = subprocess.run(command, cwd=cwd, env=process_environment(), capture_output=True,
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
    if not VERSION_TAG.fullmatch(version):
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


def replace_deployment(snapshot: Path, destination: Path, build_root: Path, *, preserve_git: bool) -> None:
    require_path(build_root, destination)
    if preserve_git and not (destination / ".git").is_dir():
        raise RuntimeError("正式 setup 没有生成可供后续更新的 repo/.git。")
    for child in destination.iterdir():
        if preserve_git and child.name == ".git":
            continue
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


def fetch_launcher(destination: Path, app_name: str) -> Path:
    archive = destination / "launcher.zip"
    with urllib.request.urlopen(LAUNCHER_URL, timeout=120) as response, archive.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    checksum_url = LAUNCHER_URL.rsplit("/", 1)[0] + "/NoNo-win32-sha256.txt"
    with urllib.request.urlopen(checksum_url, timeout=120) as response:
        expected = response.read().decode("ascii").split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(archive) != expected:
        raise RuntimeError("固定启动器下载内容与发布的 SHA-256 不一致。")
    launcher = destination / f"{app_name}.exe"
    with zipfile.ZipFile(archive) as package:
        name = f"{app_name}/{app_name}.exe"
        if name not in package.namelist():
            raise RuntimeError(f"固定启动器包缺少 {name}。")
        launcher.write_bytes(package.read(name))
    literal = str(launcher).replace("'", "''")
    product_name = run(["powershell", "-NoProfile", "-Command",
                        f"(Get-Item -LiteralPath '{literal}').VersionInfo.ProductName"], destination).strip()
    if product_name != app_name:
        raise RuntimeError(f"启动器的产品名 {product_name!r} 与应用名 {app_name!r} 不一致。")
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
        raise RuntimeError("启动器应用元数据没有合法稳定更新版本。")
    return state


def assert_update_anchor(repo: Path, state: dict, version: str, commit: str) -> None:
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


def probe_executable(executable: Path, working: Path, runtime: Path, *, check_deployment: bool) -> dict:
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
        "'sources': sources}))\n"
    )
    with subprocess.Popen([str(executable), "-c", code], cwd=working, env=process_environment(),
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
    for name, path in identity["sources"].items():
        if not Path(path).resolve().is_relative_to(working.resolve()):
            raise RuntimeError(f"便携进程的 {name} 没有从当前部署树导入。")
    return identity


def required_bundle_files(app_name: str, metadata_file: str) -> set[str]:
    app = f"data/apps/{app_name}"
    return {
        f"{app_name}.exe", "pyappify.yml", metadata_file,
        f"{app}/python/python.exe", f"{app}/python/python3.dll",
        *(f"{app}/{folder}/{file}" for folder in ("repo", "working")
          for file in ("main.py", "requirements.txt", "selfhost/NoNo.exe", "selfhost/ok_pin.json")),
    }


def verify_archive(archive: Path, app_name: str, manifest: dict) -> None:
    prefix = f"{app_name}/"
    with zipfile.ZipFile(archive) as package:
        names = {entry.filename for entry in package.infolist() if not entry.is_dir()}
        required = required_bundle_files(app_name, manifest["app_metadata_file"]) | {"build-info.json"}
        if not {prefix + name for name in required} <= names:
            raise RuntimeError("便携归档缺少启动入口、运行环境或部署文件。")
        for name in names:
            if not name.startswith(prefix) or ".." in PurePosixPath(name).parts or "\\" in name:
                raise RuntimeError(f"便携归档包含越界路径：{name}")
        if json.loads(package.read(prefix + "build-info.json")) != manifest:
            raise RuntimeError("便携归档中的构建信息发生漂移。")
        for folder in ("repo", "working"):
            deployment_prefix = f"{prefix}data/apps/{app_name}/{folder}/"
            actual_files = {
                name[len(deployment_prefix):] for name in names
                if name.startswith(deployment_prefix)
                and ".git" not in PurePosixPath(name[len(deployment_prefix):]).parts
            }
            if actual_files != set(manifest["deployment_files"]):
                raise RuntimeError(f"便携归档中的 {folder} 包含缺失或过时的部署文件。")
            for relative, expected in manifest["deployment_files"].items():
                name = deployment_prefix + relative
                if name not in names:
                    raise RuntimeError(f"便携归档缺少当前部署文件：{name}")
                with package.open(name) as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != expected:
                    raise RuntimeError(f"便携归档中的当前部署文件发生漂移：{name}")
        if package.testzip() is not None:
            raise RuntimeError("便携归档的文件校验失败。")


def archive_bundle(bundle: Path, output: Path, manifest: dict, build_root: Path) -> Path:
    require_path(build_root, bundle)
    require_path(build_root, output)
    app_name = bundle.name
    for relative in required_bundle_files(app_name, manifest["app_metadata_file"]):
        path = require_path(build_root, bundle / relative)
        if not path.is_file():
            raise RuntimeError(f"便携目录缺少必要文件：{relative}")
    for folder in ("repo", "working"):
        actual = deployment_digests(bundle / "data" / "apps" / app_name / folder)
        if actual != manifest["deployment_files"]:
            raise RuntimeError(f"归档前 {folder} 与当前部署快照不一致。")
    (bundle / "build-info.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    archive = require_path(build_root, output / f"{app_name}-win32-portable.zip")
    archive.unlink(missing_ok=True)
    seven_zip = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe"
    if seven_zip.is_file():
        # 保留 Windows 资源管理器可直接解压的标准 ZIP 容器，复用发行的 7-Zip 压缩等级。
        run([str(seven_zip), "a", "-tzip", "-mx=5", "-bd", str(archive), app_name], bundle.parent,
            timeout=900)
    else:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as package:
            for path in iter_payload_files(bundle):
                package.write(path, f"{app_name}/{path.relative_to(bundle).as_posix()}")
    verify_archive(archive, app_name, manifest)
    checksum = require_path(build_root, output / f"{app_name}-win32-sha256.txt")
    checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive


def verify_portable_metadata(bundle: Path, stage: Path, app_name: str) -> None:
    app = bundle / "data" / "apps" / app_name
    paths = [app / "app.json", app / "repo" / ".git" / "config"]
    for pattern in ("*.pth", "*._pth", "pyvenv.cfg", "direct_url.json"):
        paths.extend((app / "python").rglob(pattern))
    temporary_names = (str(stage), stage.as_posix(), json.dumps(str(stage))[1:-1])
    for path in paths:
        content = path.read_text(encoding="utf-8").casefold()
        if any(name.casefold() in content for name in temporary_names) or "file://" in content:
            raise RuntimeError(f"便携元数据残留了构建机临时来源：{path.name}")


def build_portable(source: Path, *, source_sha: str | None = None, release_tag: str | None = None) -> Path:
    if sys.platform != "win32":
        raise RuntimeError("便携运行环境和原生进程验证需要在 Windows 上构建。")
    actual_sha = git(source, "rev-parse", "HEAD")
    if source_sha is not None and source_sha != actual_sha:
        raise RuntimeError("当前检出源码 SHA 与本次编译要求不一致。")
    if release_tag is not None and not VERSION_TAG.fullmatch(release_tag):
        raise RuntimeError("正式便携发布必须使用稳定版本标签。")
    if release_tag is None and not (source / "selfhost" / "NoNo.exe").is_file():
        raise RuntimeError("当前源码缺少已编译的原生宿主，请先运行 build_runtime.ps1。")
    configuration = (source / "pyappify.yml").read_bytes()
    match = re.search(rb'^name:\s*"([^"\r\n]+)"', configuration, re.MULTILINE)
    if not match:
        raise RuntimeError("pyappify.yml 没有明确的应用名。")
    app_name = match[1].decode("utf-8")
    if app_name != "NoNo":
        raise RuntimeError("便携交付需要与固定 NoNo 启动器同名的应用配置。")
    build_root = require_path(source, source / "build")
    build_root.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="portable-", dir=build_root)
    stage = require_path(build_root, Path(temporary.name))
    try:
        bundle = stage / app_name
        bundle.mkdir()
        print("下载并校验固定启动器。", flush=True)
        launcher = fetch_launcher(bundle, app_name)
        (bundle / "launcher.zip").unlink()
        (bundle / "pyappify.yml").write_bytes(configuration)
        arguments = [str(launcher), "-c", "setup", "-p", PROFILE_NAME]
        if release_tag:
            arguments += ["--setup-version", release_tag]
        # 普通提交也只连接正式镜像：先建立可被客户端长期识别的合法 Git 更新锚点。
        # 临时本地 tag 在首次 fetch 时会被清除并触发强制回退，不能用来安装当前源码。
        print("安装正式运行环境与合法更新底座。", flush=True)
        run(arguments, bundle, timeout=1800)
        state = read_app_state(bundle, app_name)
        base_version = state["current_version"]
        if release_tag and base_version != release_tag:
            raise RuntimeError("正式 setup 没有检出本次发布标签。")
        app = bundle / "data" / "apps" / app_name
        repo, working, runtime = app / "repo", app / "working", app / "python"
        base_commit = git(repo, "rev-parse", "HEAD")
        assert_update_anchor(repo, state, base_version, base_commit)
        if release_tag:
            digests = deployment_digests(repo)
        else:
            print(f"覆盖本次源码部署快照：{actual_sha}。", flush=True)
            snapshot = stage / "snapshot"
            digests = prepare_snapshot(source, snapshot, base_version, build_root)
            # 只保留 repo/.git 作为正式更新锚点；实际代码来自当前 SHA 的白名单快照。
            # 旧代码和资源必须删除，不能只覆盖同名文件后把镜像残留一起压入包。
            replace_deployment(snapshot, repo, build_root, preserve_git=True)
            replace_deployment(snapshot, working, build_root, preserve_git=False)
            (bundle / "pyappify.yml").write_bytes(configuration)
            # 已有 repo 的无标签 setup 只 fetch，不 checkout；从当前物理树重建 working，
            # 按当前 YML 安装依赖并合并完整 App。避免用非必要的版本列表入口刷新安装态。
            print("按当前部署快照重新安装依赖并刷新启动器元数据。", flush=True)
            run([str(launcher), "-c", "setup", "-p", PROFILE_NAME], bundle, timeout=1800)
            state = read_app_state(bundle, app_name)
        assert_update_anchor(repo, state, base_version, base_commit)
        selected_profile = next(profile for profile in state["profiles"] if profile["name"] == PROFILE_NAME)
        if selected_profile.get("main_script") != "selfhost/NoNo.exe":
            raise RuntimeError("启动器应用元数据没有使用当前原生业务入口。")
        if (bundle / "pyappify.yml").read_bytes() != configuration:
            raise RuntimeError("启动器根目录配置与当前源码配置不一致。")
        print("核对实际部署树、框架来源和便携进程。", flush=True)
        for deployment in (repo, working):
            if deployment_digests(deployment) != digests:
                raise RuntimeError("启动器处理后实际部署树与预期快照不一致。")
            run([sys.executable, str(source / "scripts" / "sync_ok.py"), "--check", "--root", str(deployment)],
                source)
        python_identity = probe_executable(runtime / "python.exe", working, runtime, check_deployment=True)
        probe_executable(working / "selfhost" / "NoNo.exe", working, runtime, check_deployment=True)
        if python_identity["python_version"] != selected_profile.get("requires_python"):
            raise RuntimeError("便携 Python 版本与当前部署配置不一致。")
        for folder in (bundle / "cache", bundle / "data" / "logs"):
            remove_tree(build_root, folder)
        clean_caches(build_root, bundle)
        manifest = {
            "schema_version": 1,
            "build_kind": "release_tag" if release_tag else "commit_snapshot",
            "source_sha": actual_sha,
            "base_update_version": base_version,
            "base_update_commit": base_commit,
            "app_metadata_file": f"data/apps/{app_name}/app.json",
            "deployment_tree_sha256": tree_digest(digests),
            "deployment_files": digests,
            "native_sha256": digests["selfhost/NoNo.exe"],
            "launcher_sha256": sha256(launcher),
            "framework_pin": json.loads((working / "selfhost" / "ok_pin.json").read_text(encoding="utf-8")),
        }
        if not release_tag:
            manifest["generated_sources"] = {
                ".gitignore": ".update_repo_gitignore",
                "selfhost/NoNo.exe": "scripts/build_runtime.ps1",
                "pyappify/": "现有发行准备函数内联的固定依赖",
                "config.py / requirements.txt": "ok.update.inline_ok_requirements",
            }
        # 安装态只允许正式来源，不把构建机目录、临时 URL 或 pip 安装记录里的凭据打进包。
        verify_portable_metadata(bundle, stage, app_name)
        archive = archive_bundle(bundle, build_root / "portable-dist", manifest, build_root)
        print(f"已验证完整便携包：{archive}", flush=True)
        return archive
    finally:
        require_path(build_root, stage)
        temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并验证完整 NoNo Windows 便携包。")
    parser.add_argument("--source-sha", help="普通编译需要交付的完整源码 SHA。")
    parser.add_argument("--release-tag", help="沿用正式镜像发布的稳定标签；省略时交付当前源码快照。")
    arguments = parser.parse_args()
    try:
        build_portable(Path(__file__).resolve().parents[1], source_sha=arguments.source_sha,
                       release_tag=arguments.release_tag)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"便携打包失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
