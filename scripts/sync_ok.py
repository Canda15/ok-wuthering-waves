"""把内联在 ./ok 的 ok-script 同步到 pin 指定的版本。

为什么需要这个脚本:框架的源码以普通文件提交在仓库里(见 SELFHOST.md),
但它不在上游的 git 树里,所以上游发版后 `git merge` 只会带回 src/、config.py
这些应用层改动。只要上游动了框架版本,这里就必须重新从 PyPI 取一次 wheel,
否则会出现"合并干净、构建成功、启动 ImportError"——这是自动合并里唯一不会
自己暴露出来的失败,只能靠脚本兜住。

除了替换源码,它还把 wheel 元数据声明的运行时依赖重写进 pyproject.toml 的
生成块:内联之后 ok-script 不再是 pip 的解析来源,少写一条依赖不会报错,
只会等到某个功能跑起来才炸。

隔离解包后先重放 selfhost/framework_patches.py 的品牌字符串，再严格重放
selfhost/logic_patches.json 登记的逻辑差异，全部验证成功才替换 ok/。
原始 wheel、完整补丁后树和逻辑差异集合分别记录摘要，--check 同时验证来源契约。

用法:
    python scripts/sync_ok.py              # 版本取"上游 pyproject 的声明",没有声明才用 pin
    python scripts/sync_ok.py --dry-run    # 只报告差异,不落盘
    python scripts/sync_ok.py --version 3.0.2   # 明确指定版本(覆盖上面的推断)
    python scripts/sync_ok.py --check      # 离线校验 ./ok 与 pin 是否一致
    python scripts/sync_ok.py --check --root E:/deploy/NoNo  # 校验另一棵部署树
    python scripts/sync_ok.py --record-patches  # 从经核验 wheel 收集当前 ok/ 的全部自有差异
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tomllib
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
# 补丁清单住在 selfhost/ 里(与运行时配置覆盖同目录),脚本只是施加者。
sys.path.insert(0, str(REPO_ROOT))
from selfhost import framework_patches, logic_patches  # noqa: E402  (必须在 REPO_ROOT 之后)
from selfhost.framework_tree import (  # noqa: E402
    IGNORED_DIR_NAMES as IGNORED_DIR_NAMES,
    IGNORED_SUFFIXES as IGNORED_SUFFIXES,
    hash_tree,
    iter_payload_files as iter_payload_files,
    tree_digest,
)
OK_DIR = REPO_ROOT / "ok"
PIN_FILE = REPO_ROOT / "selfhost" / "ok_pin.json"
PATCH_MANIFEST = REPO_ROOT / "selfhost" / logic_patches.MANIFEST_NAME
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


def load_pin(path: Path | None = None) -> dict:
    path = path or PIN_FILE
    if not path.exists():
        raise SystemExit(f"缺少 pin 文件: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


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


def ensure_trailing_comma(lines: list[str], closing_index: int) -> None:
    """保证生成块之前那一项以逗号结尾(就地修改 lines)。

    上游的 dependencies 数组最后一项通常不写尾逗号(TOML 允许)。生成块要插在
    这一项和右括号之间,少了逗号整个 pyproject 就变成 "Unclosed array",
    而 pip 只在安装依赖时才报错——一次合并可以因此"测试全绿、包打不出来"。
    """
    index = closing_index - 1
    while index >= 0 and (not lines[index].strip() or lines[index].lstrip().startswith("#")):
        index -= 1
    if index < 0 or lines[index].rstrip().endswith(","):
        return
    lines[index] = lines[index].rstrip("\r\n") + ",\n"


def rewrite_pyproject(base_requires: list[str], package: str, version: str) -> tuple[bool, list[str]]:
    text = read_text(PYPROJECT_FILE)
    outside, _current = dependency_lines(text)
    skip = declared_names(outside)
    entries = [spec for spec in base_requires if package_name(spec) not in skip]

    lines = text.splitlines(keepends=True)
    begin = next(i for i, line in enumerate(lines) if line.strip().startswith(BEGIN_MARK))
    end = next(i for i, line in enumerate(lines) if line.strip().startswith(END_MARK))
    ensure_trailing_comma(lines, begin)
    new_lines = lines[: begin + 1] + render_block(entries, package, version) + lines[end + 1 :]
    new_text = "".join(new_lines)
    # 写盘前先解析一遍:pyproject 坏掉的表现是"装依赖失败",离这里很远,
    # 让它在产生的那一刻就炸,比事后从 CI 日志里倒推便宜得多。
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"改写后的 pyproject.toml 不是合法 TOML: {exc}") from exc
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
        ensure_trailing_comma(lines, end)
        lines[end:end] = [f"    {BEGIN_MARK}\n", f"    {END_MARK}\n"]
        pyproject_text = "".join(lines)
    pyproject_text, extra_dropped = drop_web_test_extra(pyproject_text)
    dropped.extend(extra_dropped)
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


def drop_web_test_extra(text: str) -> tuple[str, list[str]]:
    """删掉上游的 web-test extra(pyproject 里一个数组块)。

    上游这个 extra 只有一条 `ok-script[web]==x`。web 子系统在框架 3.0 里已经
    整个删除,extra 本身装不上东西;更糟的是它一旦被 cue 到,pip 会把 ok-script
    从 PyPI 装进 site-packages,与内联的 ./ok 并存——运行时用哪份取决于
    sys.path 顺序,是那种只在个别机器上复现的问题。删行之后这个 extra 会剩一个
    空数组,所以整块一起删。
    """
    lines = text.splitlines(keepends=True)
    dropped: list[str] = []
    kept: list[str] = []
    index = 0
    while index < len(lines):
        match = re.match(r"(web-test)\s*=\s*\[", lines[index])
        if not match:
            kept.append(lines[index])
            index += 1
            continue
        dropped.append(f"pyproject: {match.group(1)} extra")
        index += 1
        while index < len(lines) and not lines[index].strip().startswith("]"):
            index += 1
        index += 1  # 跳过右括号那一行
    return "".join(kept), dropped


def upstream_pinned_version() -> str | None:
    """读出上游 pyproject.toml 声明的 ok-script 版本(合并后、改写前)。

    这一行正是 reapply_upstream_overwrites() 要删掉的,所以必须抢在改写之前读。
    不跟随它就意味着:上游把框架升到 3.0.3,我们继续内联 3.0.1,而 src/ 已经按
    3.0.3 的 API 改了——冒烟测试只覆盖我们自己的页面,这类版本漂移只能靠
    "版本以对方声明为准"来消除,不能指望有人记得手动 --version。
    """
    match = re.search(r'"ok-script[^"]*==\s*([0-9][^",\s]*)"', read_text(PYPROJECT_FILE))
    return match.group(1) if match else None


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


def load_logic_patches(pin: dict, version: str, manifest_path: Path | None = None) -> logic_patches.PatchSet:
    patches = logic_patches.load(manifest_path or PATCH_MANIFEST, version)
    expected = pin.get("logic_patches_sha256")
    if patches.digest() != expected:
        raise SystemExit(
            f"逻辑差异集合与 pin 不一致:\n  pin  {expected}\n  实际 {patches.digest()}\n"
            "请用 --record-patches 收集完整来源并验证重放，不能只更新 pin"
        )
    return patches


def do_check(root: Path | None = None) -> int:
    ok_dir = root / "ok" if root is not None else OK_DIR
    pin_path = root / "selfhost" / "ok_pin.json" if root is not None else PIN_FILE
    manifest = root / "selfhost" / logic_patches.MANIFEST_NAME if root is not None else PATCH_MANIFEST
    if not ok_dir.is_dir():
        print("ok/ 不存在,先运行同步")
        return 1
    try:
        pin = load_pin(pin_path)
        patches = load_logic_patches(pin, pin["version"], manifest)
        current = hash_tree(ok_dir)
        patches.baseline_digests(current)
    except SystemExit as exc:
        print(str(exc))
        return 1
    # 逻辑差异可能重写或删除品牌所在文件；它们已由品牌后基线与精确后摘要保护。
    covered = {entry.path for entry in patches.entries}
    absent = [relative for relative in framework_patches.missing(ok_dir) if relative not in covered]
    if absent:
        print("这些源码补丁没有生效(ok/ 被手改或未重新施加):")
        for rel in absent:
            print(f"  ok/{rel}")
        print("跑一次 python scripts/sync_ok.py 会重新施加。")
        return 1
    actual = tree_digest(current)
    expected = pin.get("tree_sha256", "")
    if actual != expected:
        print(f"ok/ 与 pin 不一致:\n  pin  {expected}\n  实际 {actual}")
        print("请核对完整逻辑差异与重放结果，不能只登记新的树哈希(见 SELFHOST.md)。")
        return 1
    print(f"ok/、基线和逻辑差异与 pin 一致({pin['package']} {pin['version']},{len(current)} 个文件)")
    return 0


def prepare_staging() -> Path:
    staging = STAGING_DIR.resolve()
    if (
        staging.parent != REPO_ROOT.resolve() or staging.name != ".sync-ok-stage" or STAGING_DIR.is_symlink()
        or (hasattr(STAGING_DIR, "is_junction") and STAGING_DIR.is_junction())
    ):
        raise SystemExit("同步暂存目录必须是仓库内的 .sync-ok-stage，不能删除其它路径")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    return staging


def stage_wheel(pin: dict, version: str, staging: Path) -> tuple[Path, str, str]:
    url, pypi_sha256 = fetch_release(pin["package"], version)
    expected = pin.get("wheel_sha256", "") if version == pin["version"] else ""
    if expected and expected != pypi_sha256:
        raise SystemExit(f"wheel 哈希与 pin 不符(供应链告警):\n  pin  {expected}\n  PyPI {pypi_sha256}")
    payload = download(url)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != pypi_sha256:
        raise SystemExit(f"下载内容与 PyPI 摘要不符: {actual} != {pypi_sha256}")
    wheel_path = staging / "ok_script.whl"
    wheel_path.write_bytes(payload)
    return wheel_path, url, actual


def do_sync(
    version_override: str | None, dry_run: bool, upstream_version: str | None = None,
    *, reapply: bool = False,
) -> int:
    pin = load_pin()
    package = pin["package"]
    # 优先级:--version(人手指定)> 上游 pyproject 的声明 > pin 文件。
    version = version_override or upstream_version or pin["version"]
    if version != pin["version"]:
        print(f"目标: {package} {version}(pin 里是 {pin['version']},跟随上游声明)")
    else:
        print(f"目标: {package} {version}")

    patches = load_logic_patches(pin, version)
    staging = prepare_staging()
    try:
        wheel_path, url, actual_sha256 = stage_wheel(pin, version, staging)
        package_root = extract_ok_package(wheel_path, staging / "unpacked")
        applied, already = framework_patches.apply_to(package_root)
        print(f"  品牌补丁: 新施加 {len(applied)} 处,已生效 {len(already)} 处")
        applied, already = patches.apply_to(package_root)
        print(f"  逻辑差异: 新施加 {len(applied)} 个文件,已生效 {len(already)} 个文件")
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

        if reapply:
            dropped = reapply_upstream_overwrites()
            if dropped:
                print("已重新移除上游带回来的 pin:")
                for line in dropped:
                    print(f"  {line}")
            else:
                print("没有需要移除的 pin")

        if changed:
            # 整树替换而不是逐文件覆盖:上游删掉的文件必须真的消失,否则旧的
            # __pycache__ 与残留模块会让"已删除的 API"继续可导入。最坏情况
            # (move 中途失败)可从 git 恢复。
            if (
                OK_DIR.resolve() != REPO_ROOT.resolve() / "ok" or OK_DIR.is_symlink()
                or (hasattr(OK_DIR, "is_junction") and OK_DIR.is_junction())
            ):
                raise SystemExit("整体替换目标必须是仓库内的 ok/ 实目录")
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
                "logic_patches_sha256": patches.digest(),
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


def do_record_patches(version_override: str | None, dry_run: bool) -> int:
    """显式收集完整自有差异，绝不替换已编辑的 ok/ 或改写依赖文件。"""
    if not OK_DIR.is_dir():
        raise SystemExit("收集差异前必须有完整 ok/ 工作树")
    pin = load_pin()
    version = version_override or pin["version"]
    print(f"收集逻辑差异: {pin['package']} {version}")
    current = hash_tree(OK_DIR)
    staging = prepare_staging()
    try:
        wheel_path, url, wheel_sha256 = stage_wheel(pin, version, staging)
        base_root = extract_ok_package(wheel_path, staging / "unpacked")
        framework_patches.apply_to(base_root)
        summarize(hash_tree(base_root), current)
        staged_manifest = staging / "captured" / logic_patches.MANIFEST_NAME
        patches = logic_patches.capture(base_root, OK_DIR, version, staged_manifest)
        applied, _already = patches.apply_to(base_root)
        if hash_tree(base_root) != current or hash_tree(OK_DIR) != current:
            raise SystemExit("差异重放与工作树不一致或收集期间发生编辑，未写入来源和 pin")
        print(f"  已验证 {len(applied)} 个逻辑差异文件，集合摘要 {patches.digest()}")
        if dry_run:
            print("dry-run:未落盘")
            return 0
        target = PATCH_MANIFEST.parent / "patches"
        if (
            not target.resolve().is_relative_to(REPO_ROOT.resolve()) or target.is_symlink()
            or (hasattr(target, "is_junction") and target.is_junction())
        ):
            raise SystemExit("逻辑差异载荷目录必须位于仓库内，不能删除其它路径")
        shutil.rmtree(target, ignore_errors=True)
        source = staged_manifest.parent / "patches"
        if source.is_dir():
            shutil.copytree(source, target)
        PATCH_MANIFEST.write_bytes(patches.manifest_content)
        save_pin({
            "package": pin["package"], "version": version, "wheel_sha256": wheel_sha256,
            "tree_sha256": tree_digest(current), "logic_patches_sha256": patches.digest(), "source_url": url,
        })
        print("已登记差异清单、载荷与 pin；请审查源码 diff 和差异文件")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步内联在 ./ok 的 ok-script")
    parser.add_argument("--version", help="同步到指定版本(默认取 selfhost/ok_pin.json)")
    parser.add_argument("--dry-run", action="store_true", help="只报告差异,不写文件")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="离线校验框架、基线与差异集合")
    modes.add_argument("--record-patches", action="store_true", help="收集当前 ok/ 的完整差异并隔离验证后登记")
    parser.add_argument("--root", type=Path, help="--check 的目标根目录，包含 ok/ 与 selfhost/")
    parser.add_argument(
        "--reapply",
        action="store_true",
        help="合并冲突后先重新删除上游带回来的 ok-script pin,再正常同步",
    )
    args = parser.parse_args(argv)

    if args.root is not None and not args.check:
        parser.error("--root 只能与 --check 一起使用")
    if args.check:
        if args.version or args.reapply or args.dry_run:
            parser.error("--check 不能与 --version、--reapply 或 --dry-run 一起使用")
        return do_check(args.root.resolve() if args.root is not None else None)
    if args.record_patches:
        if args.reapply:
            parser.error("--record-patches 不能改写上游依赖，不能与 --reapply 一起使用")
        return do_record_patches(args.version, args.dry_run)
    # 顺序不能反:reapply 会把上游那行 pin 删掉,之后再读就没有版本依据了。
    upstream_version = upstream_pinned_version()
    return do_sync(args.version, args.dry_run, upstream_version=upstream_version, reapply=args.reapply)


if __name__ == "__main__":
    sys.exit(main())
