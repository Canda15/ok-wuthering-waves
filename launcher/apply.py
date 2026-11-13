"""校验固定官方源码并应用 NoNo 统一差异；重复应用和基线漂移均失败。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

LAUNCHER_ROOT = Path(__file__).resolve().parent
RUST_COMMIT = "31cd261eb8360ba2cf9ac63ec59fa5ef253cca2a"
ACTION_COMMIT = "6dcb02954bb5fab5b83e95f1af243078213ec1ab"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def adaptation_inputs() -> dict[str, str]:
    files = [*LAUNCHER_ROOT.glob("patches/*.patch"), *LAUNCHER_ROOT.glob("adapters/*.rs"),
             LAUNCHER_ROOT / "Cargo.lock"]
    return {path.relative_to(LAUNCHER_ROOT).as_posix(): sha256(path) for path in sorted(files)}


def record_pin() -> dict:
    baselines = json.loads((LAUNCHER_ROOT / "baselines.json").read_text(encoding="utf-8"))
    if baselines["rust_commit"] != RUST_COMMIT or baselines["action_commit"] != ACTION_COMMIT:
        raise RuntimeError("官方源码固定提交发生漂移。")
    identity = {"baselines_sha256": sha256(LAUNCHER_ROOT / "baselines.json"),
                "files": adaptation_inputs()}
    return {"schema_version": 1, **identity,
            "adaptation_sha256": hashlib.sha256(canonical(identity)).hexdigest()}


def verify_pin() -> tuple[dict, dict]:
    expected = json.loads((LAUNCHER_ROOT / "adaptation.json").read_text(encoding="utf-8"))
    if record_pin() != expected:
        raise RuntimeError("NoNo 启动器差异、载荷或固定基线未登记。")
    baselines = json.loads((LAUNCHER_ROOT / "baselines.json").read_text(encoding="utf-8"))
    if sha256(LAUNCHER_ROOT / "Cargo.lock") != baselines["rust_files"]["src-tauri/Cargo.lock"]["after_sha256"]:
        raise RuntimeError("固定 Rust lockfile 与统一差异结果不一致。")
    return expected, baselines


def run(arguments: list[str], cwd: Path) -> str:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False, **options)
    if result.returncode:
        raise RuntimeError(f"启动器源码检查或差异应用失败，退出码 {result.returncode}。")
    return result.stdout.strip()


def apply(root: Path, kind: str) -> None:
    pin, baselines = verify_pin()
    root = root.resolve(strict=True)
    commit = RUST_COMMIT if kind == "rust" else ACTION_COMMIT
    if run(["git", "rev-parse", "HEAD"], root) != commit:
        raise RuntimeError("启动器或构建 Action 的官方提交不匹配。")
    entries = baselines[f"{kind}_files"]
    for relative, entry in entries.items():
        path = root / relative
        if not path.is_file() or path.is_symlink() or sha256(path) != entry["before_sha256"]:
            raise RuntimeError(f"官方 {kind} 源码基线不匹配，禁止猜测应用。")
    additions = {}
    if kind == "rust":
        additions = {f"src-tauri/src/{path.name}": path
                     for path in sorted((LAUNCHER_ROOT / "adapters").glob("*.rs"))}
        if any((root / relative).exists() for relative in [*additions, "src-tauri/src/nono_build.rs"]):
            raise RuntimeError("启动器适配已存在，必须从新的固定源码检出构建。")
    patch = LAUNCHER_ROOT / "patches" / ("v1.2.7.patch" if kind == "rust" else "action.patch")
    # 哈希先通过，再要求 Git 的完整统一差异检查；不使用模糊上下文回退。
    run(["git", "apply", "--check", "--whitespace=error-all", str(patch)], root)
    run(["git", "apply", "--whitespace=error-all", str(patch)], root)
    for relative, entry in entries.items():
        if sha256(root / relative) != entry["after_sha256"]:
            raise RuntimeError("统一差异结果摘要不匹配。")
    for relative, source in additions.items():
        shutil.copy2(source, root / relative)
    if kind == "rust":
        source_files = {relative: sha256(root / relative) for relative in sorted([*entries, *additions])}
        source_digest = hashlib.sha256(canonical(source_files)).hexdigest()
        constants = {
            "SOURCE_SHA": RUST_COMMIT, "ACTION_SHA": ACTION_COMMIT,
            "ADAPTATION_SHA256": pin["adaptation_sha256"], "SOURCE_TREE_SHA256": source_digest,
        }
        content = "// 由固定源码适配 hook 生成，用于校验 EXE 与便携载荷是否配套。\n"
        content += "".join(f'pub(crate) const {key}: &str = "{value}";\n' for key, value in constants.items())
        (root / "src-tauri/src/nono_build.rs").write_text(content, encoding="utf-8", newline="\n")
        (root / ".nono-source-proof.json").write_text(
            json.dumps({"schema_version": 1, "source_files": source_files, **constants},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        # 官方配置声明资源目录，但 Git 不保存空目录；仅 EXE 构建也需要资源检查通过。
        (root / "src-tauri/data").mkdir(exist_ok=True)
    print(f"已核验并应用 NoNo {kind} 固定源码适配。", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="固定官方启动器和 Action 的 NoNo 源码适配。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--rust-root", type=Path, help="刚检出的官方 v1.2.7 根目录。")
    group.add_argument("--action-root", type=Path, help="刚检出的固定官方 Action 根目录。")
    group.add_argument("--record", action="store_true", help="维护时重新登记已审查差异摘要。")
    group.add_argument("--check", action="store_true", help="只校验已登记差异与载荷。")
    arguments = parser.parse_args()
    try:
        if arguments.record:
            (LAUNCHER_ROOT / "adaptation.json").write_text(
                json.dumps(record_pin(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n")
            print("已登记 NoNo 启动器适配摘要。")
        elif arguments.check:
            verify_pin()
            print("NoNo 启动器适配来源校验通过。")
        else:
            apply(arguments.rust_root or arguments.action_root,
                  "rust" if arguments.rust_root else "action")
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"启动器源码适配失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
