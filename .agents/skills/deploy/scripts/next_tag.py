#!/usr/bin/env python3
"""基于实际已集成的官方版本计算 NoNo 品牌标签，不递增上游基础号。"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from selfhost.versioning import next_revision, parse_version_tag, require_version_tag
from scripts.upstream_release import find_integrated_upstream


def repository_tags() -> list[str]:
    result = subprocess.run(["git", "tag", "--list", "v*"], cwd=ROOT, check=True,
                            capture_output=True, text=True, encoding="utf-8")
    return result.stdout.splitlines()


def remote_tags(remote: str) -> list[str]:
    result = subprocess.run(["git", "ls-remote", "--refs", "--tags", remote, "refs/tags/v*"],
                            cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8")
    return [ref.removeprefix("refs/tags/")
            for _, ref in (line.split("\t", 1) for line in result.stdout.splitlines())]


def next_tag(channel: str, tags: list[str], upstream_tag: str | None = None) -> str:
    if upstream_tag is None:
        raise ValueError("必须提供已验证的官方基础标签，不能从本地缓存标签猜测版本")
    source = require_version_tag(upstream_tag, branded=False)
    if channel == "release":
        if source.channel is not None:
            raise ValueError("官方预览基础不能自动计算为正式版本")
        return next_revision(upstream_tag, tags)
    if channel not in ("alpha", "beta", "rc"):
        raise ValueError("未知发布渠道")
    versions = [parsed for tag in tags if (parsed := parse_version_tag(tag)) is not None
                and parsed.branded and parsed.release == source.release]
    if any(version.channel is None for version in versions):
        raise ValueError("此基础已发布正式版；修复应使用 xmod 修订，不能再创建更早的预览版")
    numbers = [version.number or 0 for version in versions if version.channel == channel]
    start = (source.number or 1) if source.channel == channel else 1
    number = max(numbers) + 1 if numbers else start
    return "v" + ".".join(str(part) for part in source.release) + f"-{channel}.{number}-xmod"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", choices=("release", "beta", "alpha", "rc"))
    parser.add_argument("--tag", action="append", dest="tags", help="仅用于离线计算的已有标签，可重复。")
    parser.add_argument("--upstream-tag", help="已核验的官方基础标签；正常发布仍检查真实提交祖先。")
    parser.add_argument("--remote", help="同时考虑此发布远端的标签，例如 origin。")
    arguments = parser.parse_args()
    try:
        if arguments.tags is None:
            integrated = find_integrated_upstream(ROOT)["upstream_tag"]
            if (arguments.upstream_tag is not None
                    and require_version_tag(arguments.upstream_tag) != require_version_tag(integrated)):
                raise ValueError("指定基础不是 HEAD 实际已集成的最新官方版本")
            upstream_tag = integrated
            tags = repository_tags()
        else:
            upstream_tag, tags = arguments.upstream_tag, arguments.tags
        if arguments.remote:
            tags.extend(remote_tags(arguments.remote))
        print(next_tag(arguments.channel, tags, upstream_tag))
    except subprocess.CalledProcessError:
        print("无法读取发布标签，停止计算版本", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
