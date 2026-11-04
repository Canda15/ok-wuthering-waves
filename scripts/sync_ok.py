"""把内联在 ./ok 的 ok-script 同步到 pin 指定的版本。

为什么需要这个脚本:框架的源码以普通文件提交在仓库里(见 SELFHOST.md),
但它不在上游的 git 树里,所以上游发版后 `git merge` 只会带回 src/、config.py
这些应用层改动。只要上游动了框架版本,这里就必须重新从 PyPI 取一次 wheel,
否则会出现"合并干净、构建成功、启动 ImportError"——这是自动合并里唯一不会
自己暴露出来的失败,只能靠脚本兜住。

除了替换源码,它还把 wheel 元数据声明的运行时依赖重写进 pyproject.toml 的
生成块:内联之后 ok-script 不再是 pip 的解析来源,少写一条依赖不会报错,
只会等到某个功能跑起来才炸。

用法:
    python scripts/sync_ok.py              # 按 selfhost/ok_pin.json 同步
    python scripts/sync_ok.py --dry-run    # 只报告差异,不落盘
    python scripts/sync_ok.py --version 3.0.2
    python scripts/sync_ok.py --check      # 离线校验 ./ok 与 pin 是否一致
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

# CI 的 Windows runner 控制台是 cp1252,直接 print 中文会抛 UnicodeEncodeError
# 而让整个同步静默失败(步骤看起来仍然成功)。输出改成 UTF-8 并允许替换,
# 保证"脚本失败"一定以退出码的形式暴露,而不是靠运气。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 被重定向到不支持 reconfigure 的对象
        pass

REPO_ROOT = Path(__file__).resolve().parents[1]
OK_DIR = REPO_ROOT / "ok"
PIN_FILE = REPO_ROOT / "selfhost" / "ok_pin.json"
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"
REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"
# 暂存目录刻意用显式 mkdir 建在仓库内:tempfile 在 Windows 上会创建 0o700 的
# 受限目录,普通进程连自己刚建的目录都写不进去;而且整树替换要与 ok/ 同盘,
# 跨盘 shutil.move 会退化成逐文件拷贝。
STAGING_DIR = REPO_ROOT / ".sync-ok-stage"

# 生成块的定界标记。块内内容每次同步整体重写,块外由人维护。
BEGIN_MARK = "# >>> generated: vendored ok-script dependencies (scripts/sync_ok.py)"
END_MARK = "# <<< generated: vendored ok-script dependencies"

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/{version}/json"

# 编译产物不属于源码树;__pycache__ 若混进 deploy.txt 的打包树会污染更新包。
IGNORED_DIR_NAMES = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def normalize(name: str) -> str:
    """按 PEP 503 归一化包名,用于跨大小写/横线比较。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def package_name(spec: str) -> str:
    """从 PEP 508 依赖串里取出包名:去掉 extras 与版本约束。"""
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec.strip())
    if not match:
        raise SystemExit(f"无法解析依赖声明: {spec!r}")
    return normalize(match.group(0))


def iter_payload_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if IGNORED_DIR_NAMES & set(path.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        files.append(path)
    return files


def hash_tree(root: Path) -> dict[str, str]:
    """返回 {相对 posix 路径: sha256}。路径一律用 / 以保持跨平台可比。"""
    digests: dict[str, str] = {}
    for path in iter_payload_files(root):
        rel = path.relative_to(root).as_posix()
        digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def tree_digest(digests: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(digests):
        h.update(f"{rel}\0{digests[rel]}\n".encode())
    return h.hexdigest()


def load_pin() -> dict:
    if not PIN_FILE.exists():
        raise SystemExit(f"缺少 pin 文件: {PIN_FILE}")
    return json.loads(PIN_FILE.read_text(encoding="utf-8"))


def save_pin(pin: dict) -> None:
    PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_text(PIN_FILE, json.dumps(pin, indent=2, ensure_ascii=False) + "\n")


def fetch_release(package: str, version: str) -> tuple[str, str]:
    """返回 (wheel 下载地址, PyPI 记录的 sha256)。"""
    url = PYPI_JSON_URL.format(package=package, version=version)
    with urllib.request.urlopen(url, timeout=60) as resp:
        meta = json.loads(resp.read().decode("utf-8"))

    wheels = [item for item in meta["urls"] if item["filename"].endswith(".whl")]
    # ok-script 只发 universal wheel;若上游哪天改成多平台包,这里直接停下让人决定。
    universal = [item for item in wheels if item["filename"].endswith("none-any.whl")]
    candidates = universal or wheels
    if len(candidates) != 1:
        names = ", ".join(item["filename"] for item in wheels) or "无 wheel"
        raise SystemExit(f"{package} {version} 的 wheel 不唯一({names}),需要人工指定")
    return candidates[0]["url"], candidates[0]["digests"]["sha256"]


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def extract_ok_package(wheel_path: Path, dest: Path) -> Path:
    """只取出 wheel 里的 ok/ 子树:diagnostics/、examples/、dist-info 不内联。"""
    with zipfile.ZipFile(wheel_path) as archive:
        members = [name for name in archive.namelist() if name.startswith("ok/")]
        if not members:
            raise SystemExit("wheel 里没有 ok/ 目录,上游布局可能变了")
        for name in members:
            archive.extract(name, dest)
    return dest / "ok"


def parse_requires(metadata_text: str) -> tuple[list[str], dict[str, list[str]]]:
    """从 wheel METADATA 拆出基础依赖与 extra 依赖。"""
    base: list[str] = []
    extras: dict[str, list[str]] = {}
    for line in metadata_text.splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        spec = line.split(":", 1)[1].strip()
        match = re.search(r'extra\s*==\s*"([^"]+)"', spec)
        if match:
            extras.setdefault(match.group(1), []).append(spec.split(";")[0].strip())
        else:
            base.append(spec)
    return base, extras


def read_metadata(archive: zipfile.ZipFile) -> str:
    for name in archive.namelist():
        if name.endswith(".dist-info/METADATA"):
            return archive.read(name).decode("utf-8")
    raise SystemExit("wheel 里没有 METADATA,无法读取依赖声明")


def dependency_lines(text: str) -> tuple[list[str], list[str]]:
    """把 pyproject 的 dependencies 数组拆成(生成块之外的行, 生成块内的行)。"""
    lines = text.splitlines(keepends=True)
    begin = end = None
    for index, line in enumerate(lines):
        if line.strip().startswith(BEGIN_MARK):
            begin = index
        elif line.strip().startswith(END_MARK):
            end = index
    if begin is None or end is None or end < begin:
        raise SystemExit(
            f"pyproject.toml 里找不到生成块标记,请先补上:\n{BEGIN_MARK}\n{END_MARK}"
        )
    return lines[: begin + 1] + lines[end:], lines[begin + 1 : end]


def declared_names(outside_lines: list[str]) -> set[str]:
    """生成块之外由人声明的包名,生成块不重复声明它们。"""
    names: set[str] = set()
    for line in outside_lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for spec in re.findall(r'"([^"]+)"', stripped):
            match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec)
            if match:
                names.add(normalize(match.group(0)))
    return names


def render_block(entries: list[str], package: str, version: str) -> list[str]:
    body = [f"    # 来源: {package} {version} 的 wheel 元数据,由 scripts/sync_ok.py 生成。\n"]
    body += [f'    "{entry}",\n' for entry in entries]
    body.append(f"    {END_MARK}\n")
    return body


def rewrite_pyproject(base_requires: list[str], package: str, version: str) -> tuple[bool, list[str]]:
    text = read_text(PYPROJECT_FILE)
    outside, _current = dependency_lines(text)
    skip = declared_names(outside)
    entries = [spec for spec in base_requires if package_name(spec) not in skip]

    lines = text.splitlines(keepends=True)
    begin = next(i for i, line in enumerate(lines) if line.strip().startswith(BEGIN_MARK))
    end = next(i for i, line in enumerate(lines) if line.strip().startswith(END_MARK))
    new_lines = lines[: begin + 1] + render_block(entries, package, version) + lines[end + 1 :]
    new_text = "".join(new_lines)
    if new_text == text:
        return False, entries
    write_text(PYPROJECT_FILE, new_text)
    return True, entries


def reapply_upstream_overwrites() -> list[str]:
    """把上游版本里被我们删掉的那几行重新删掉。

    上游每次 bump 框架版本都会改 pyproject.toml 的 `ok-script[ocr,qt]==x` 与
    requirements.txt 的同名 pin。这两个文件在我们这里是被删掉那几行的状态,
    git 因此判定 "deleted by us / modified by them" 并停下——这是自动合并里
    唯一会周期性出现的冲突。既然我们对这两个文件的改动完全可脚本复现
    (删 pin、加生成块),冲突就能自动解开:先取上游版本,再跑这个函数。
    """
    dropped: list[str] = []

    pyproject_text = read_text(PYPROJECT_FILE)
    kept_lines = []
    for line in pyproject_text.splitlines(keepends=True):
        match = re.match(r'\s*"([^"]+)"', line)
        if match and package_name(match.group(1)) == "ok-script":
            dropped.append(f"pyproject: {line.strip().rstrip(',')}")
            continue
        kept_lines.append(line)
    pyproject_text = "".join(kept_lines)

    if BEGIN_MARK not in pyproject_text:
        # 上游版本里没有生成块标记,插到 dependencies 数组的右括号之前。
        lines = pyproject_text.splitlines(keepends=True)
        start = next(
            (i for i, line in enumerate(lines) if line.strip().startswith("dependencies = [")),
            None,
        )
        if start is None:
            raise SystemExit("pyproject.toml 里没有 [project] dependencies,布局可能变了")
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].strip() == "]"),
            None,
        )
        if end is None:
            raise SystemExit("pyproject.toml 的 dependencies 数组没有闭合括号")
        lines[end:end] = [f"    {BEGIN_MARK}\n", f"    {END_MARK}\n"]
        pyproject_text = "".join(lines)
    write_text(PYPROJECT_FILE, pyproject_text)

    requirements_text = read_text(REQUIREMENTS_FILE)
    lines = requirements_text.splitlines(keepends=True)
    kept_lines = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if re.match(r"ok-script\s*[=<>!\[~]", line) or line.strip() == "ok-script":
            dropped.append(f"requirements: {line.strip()}")
            index += 1
            # 紧跟的 "# via ..." 是这条 pin 的来源说明,一起删掉。
            while index < len(lines) and re.match(r"\s+#\s*via\b", lines[index]):
                dropped.append(f"requirements: {lines[index].strip()}")
                index += 1
            continue
        kept_lines.append(line)
        index += 1
    write_text(REQUIREMENTS_FILE, "".join(kept_lines))
    return dropped


def check_requirements(base_requires: list[str]) -> list[str]:
    """requirements.txt 是 pip-compile 产物,新依赖不在这里就装不上。"""
    if not REQUIREMENTS_FILE.exists():
        return []
    text = read_text(REQUIREMENTS_FILE)
    present = set()
    for line in text.splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line.strip())
        if match:
            present.add(normalize(match.group(1)))
    return [spec for spec in base_requires if package_name(spec) not in present]


def summarize(current: dict[str, str], incoming: dict[str, str]) -> list[str]:
    added = sorted(set(incoming) - set(current))
    removed = sorted(set(current) - set(incoming))
    modified = sorted(rel for rel in set(current) & set(incoming) if current[rel] != incoming[rel])
    print(f"  ok/ 文件: {len(incoming)} 个(新增 {len(added)},删除 {len(removed)},改动 {len(modified)})")
    for label, paths in (("删", removed), ("改", modified), ("增", added)):
        for rel in paths[:15]:
            print(f"    {label} {rel}")
        if len(paths) > 15:
            print(f"    {label} ... 另有 {len(paths) - 15} 个")
    return added + removed + modified


def do_check() -> int:
    pin = load_pin()
    if not OK_DIR.is_dir():
        print("ok/ 不存在,先运行同步")
        return 1
    actual = tree_digest(hash_tree(OK_DIR))
    expected = pin.get("tree_sha256", "")
    if actual != expected:
        print(f"ok/ 与 pin 不一致:\n  pin  {expected}\n  实际 {actual}")
        print("说明 ./ok 被手改过或未重新同步;手改需在此登记新哈希(见 SELFHOST.md)。")
        return 1
    print(f"ok/ 与 pin 一致({pin['package']} {pin['version']},{len(hash_tree(OK_DIR))} 个文件)")
    return 0


def do_sync(version_override: str | None, dry_run: bool) -> int:
    pin = load_pin()
    package = pin["package"]
    version = version_override or pin["version"]
    print(f"目标: {package} {version}")

    url, pypi_sha256 = fetch_release(package, version)
    expected = pin.get("wheel_sha256", "") if version == pin["version"] else ""
    if expected and expected != pypi_sha256:
        raise SystemExit(
            f"wheel 哈希与 pin 不符(供应链告警):\n  pin  {expected}\n  PyPI {pypi_sha256}"
        )

    staging = STAGING_DIR
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        wheel_path = staging / "ok_script.whl"
        payload = download(url)
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != pypi_sha256:
            raise SystemExit(f"下载内容与 PyPI 摘要不符: {actual_sha256} != {pypi_sha256}")
        wheel_path.write_bytes(payload)

        package_root = extract_ok_package(wheel_path, staging / "unpacked")
        incoming = hash_tree(package_root)
        current = hash_tree(OK_DIR) if OK_DIR.is_dir() else {}

        changed = summarize(current, incoming)
        with zipfile.ZipFile(wheel_path) as archive:
            base_requires, extras = parse_requires(read_metadata(archive))

        missing = check_requirements(base_requires)
        if missing:
            print("  requirements.txt 缺少这些新依赖(需重跑 pip-compile):")
            for spec in missing:
                print(f"    {spec}")
        for extra, specs in sorted(extras.items()):
            print(f"  extra[{extra}]: {', '.join(specs)}")

        if dry_run:
            print("dry-run:未落盘")
            return 0

        if changed:
            # 整树替换而不是逐文件覆盖:上游删掉的文件必须真的消失,否则旧的
            # __pycache__ 与残留模块会让"已删除的 API"继续可导入。最坏情况
            # (move 中途失败)可从 git 恢复。
            shutil.rmtree(OK_DIR, ignore_errors=True)
            shutil.move(str(package_root), str(OK_DIR))

        pyproject_changed, entries = rewrite_pyproject(base_requires, package, version)
        print(f"  pyproject 生成块: {len(entries)} 条依赖,{'已更新' if pyproject_changed else '无变化'}")

        save_pin(
            {
                "package": package,
                "version": version,
                "wheel_sha256": actual_sha256,
                "tree_sha256": tree_digest(incoming),
                "source_url": url,
            }
        )
        print("  pin 已更新: selfhost/ok_pin.json")
        if not changed and not pyproject_changed:
            print("同步完成:源码与 pyproject 均无变化(幂等)")
        else:
            print("同步完成:请检查 git diff 后再提交")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步内联在 ./ok 的 ok-script")
    parser.add_argument("--version", help="同步到指定版本(默认取 selfhost/ok_pin.json)")
    parser.add_argument("--dry-run", action="store_true", help="只报告差异,不写文件")
    parser.add_argument("--check", action="store_true", help="离线校验 ./ok 与 pin 是否一致")
    parser.add_argument(
        "--reapply",
        action="store_true",
        help="合并冲突后先重新删除上游带回来的 ok-script pin,再正常同步",
    )
    args = parser.parse_args(argv)

    if args.check:
        return do_check()
    if args.reapply:
        dropped = reapply_upstream_overwrites()
        if dropped:
            print("已重新移除上游带回来的 pin:")
            for line in dropped:
                print(f"  {line}")
        else:
            print("没有需要移除的 pin")
    return do_sync(args.version, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
