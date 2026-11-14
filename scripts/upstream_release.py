"""按实际提交核对上游同号发布，拒绝复用旧源码或来源不明的镜像标签。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TAG_PATTERN = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(?:[-.](alpha|beta|rc)(?:\.(\d+))?)?$')
REQUIRED_ASSETS = {'NoNo-win32-portable.zip', 'NoNo-win32-sha256.txt'}
SOURCE_RECORD = 'selfhost/build-source.json'


def validate_tag(tag):
    match = TAG_PATTERN.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError('上游标签不是支持的版本格式')
    return match


def git(root, *arguments, accepted=(0,)):
    try:
        result = subprocess.run(['git', *arguments], cwd=root, capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'无法完成 Git {arguments[0]}，请检查 Git 或网络状态') from error
    if result.returncode not in accepted:
        # Git 的原始错误可能包含带凭据的远端 URL，不能原样写入工作流日志。
        raise RuntimeError(f'Git {arguments[0]} 失败，退出码 {result.returncode}')
    return result


def is_ancestor(root, ancestor, descendant):
    return git(root, 'merge-base', '--is-ancestor', ancestor, descendant,
               accepted=(0, 1)).returncode == 0


def remote_tags(root, remote, tag):
    lines = git(root, 'ls-remote', '--refs', '--tags', remote, f'refs/tags/{tag}').stdout.splitlines()
    return {ref.removeprefix('refs/tags/'): oid for oid, ref in
            (line.split('\t', 1) for line in lines) if ref.startswith('refs/tags/')}


def source_tag_commit(root, tag, remote_refs):
    if tag not in remote_refs:
        return None
    ref = f'refs/tags/{tag}'
    local = git(root, 'rev-parse', '--verify', '--quiet', ref, accepted=(0, 1))
    if local.returncode or local.stdout.strip() != remote_refs.get(tag):
        # checkout 之后新增或变动的远端标签不能靠本地旧引用证明覆盖关系。
        raise RuntimeError(f'源码远端与本地的 {tag} 标签不一致，请刷新并核对后重试')
    commit = git(root, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', accepted=(0, 1))
    if commit.returncode:
        raise RuntimeError(f'源码标签 {tag} 没有指向有效提交')
    return commit.stdout.strip()


def get_release(repository, tag):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
        raise ValueError('发布仓库必须使用 owner/repository 格式')
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'NoNo-upstream-sync'}
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = Request(f'https://api.github.com/repos/{repository}/releases/tags/{tag}', headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            release = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f'查询发布状态失败（HTTP {error.code}），停止本次同步') from error
    except (URLError, OSError, ValueError) as error:
        raise RuntimeError('无法读取有效发布状态，停止本次同步') from error
    if not isinstance(release, dict) or release.get('tag_name') != tag:
        raise RuntimeError('发布状态格式或版本不正确，停止本次同步')
    return release


def release_complete(release):
    if not release or release.get('draft') is not False:
        return False
    if not isinstance(release.get('assets'), list):
        raise RuntimeError('发布附件列表格式不正确，停止本次同步')
    assets = {item.get('name') for item in release['assets']
              if isinstance(item, dict) and item.get('state') == 'uploaded'
              and type(item.get('size')) is int and item['size'] > 0}
    return REQUIRED_ASSETS <= assets


def check_release(root, upstream_tag, repository, force=False):
    requested = validate_tag(upstream_tag)
    upstream = git(root, 'rev-parse', f'refs/upstream-tags/{upstream_tag}^{{commit}}').stdout.strip()
    head = git(root, 'rev-parse', 'HEAD').stdout.strip()
    refs = remote_tags(root, 'origin', upstream_tag)
    commit = source_tag_commit(root, upstream_tag, refs)
    if commit:
        if not is_ancestor(root, upstream, commit):
            raise RuntimeError(f'源码已有的 {upstream_tag} 不包含目标上游提交，请先处理同号版本冲突')
        if not is_ancestor(root, commit, head):
            raise RuntimeError(f'源码标签 {upstream_tag} 不在当前 HEAD 历史中，请先同步并核对源码')
    if not is_ancestor(root, upstream, head) or force:
        return {'needed': True}
    release = get_release(repository, upstream_tag)
    if (commit and release_complete(release)
            and (requested.group(4) or release.get('prerelease') is False)):
        return {'needed': False, 'covered_tag': upstream_tag}
    return {'needed': True}


def source_record(tag, commit):
    return {'schema_version': 1, 'release_tag': tag, 'source_commit': commit}


def record_source(root, tag, mirror):
    validate_tag(tag)
    head = git(root, 'rev-parse', 'HEAD').stdout.strip()
    tagged = git(root, 'rev-parse', f'refs/tags/{tag}^{{commit}}').stdout.strip()
    if tagged != head:
        raise RuntimeError('构建来源记录必须对应当前检出的发布标签提交')
    # 手动派发 build.yml 也可能绕过自动同步，写镜像前仍须拒绝同号旧源码。
    verify_mirror_source(root, mirror, tag, head)
    record = source_record(tag, head)
    destination = root / SOURCE_RECORD
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record


def verify_mirror_source(root, mirror, tag, commit):
    refs = remote_tags(root, mirror, tag)
    if tag not in refs:
        return
    # 镜像是另一棵部署仓库，提交 SHA 不同；只能核对该标签内实际保存的源码来源。
    # 临时裸仓库不会检出运行文件，也不会改写本仓库或镜像的任何引用。
    with tempfile.TemporaryDirectory(prefix='nono-release-source-') as directory:
        mirror_root = Path(directory)
        git(mirror_root, 'init', '--bare')
        git(mirror_root, 'fetch', '--no-tags', '--depth=1', mirror, f'refs/tags/{tag}')
        fetched = git(mirror_root, 'rev-parse', 'FETCH_HEAD').stdout.strip()
        if fetched != refs[tag]:
            raise RuntimeError(f'核对期间更新镜像的 {tag} 标签发生变化，请重新检查')
        marker = git(mirror_root, 'show', f'FETCH_HEAD:{SOURCE_RECORD}', accepted=(0, 128))
        if marker.returncode:
            raise RuntimeError(f'更新镜像已有 {tag}，但缺少源码来源记录，拒绝自动覆盖旧版本')
        try:
            record = json.loads(marker.stdout)
        except ValueError as error:
            raise RuntimeError(f'更新镜像 {tag} 的源码来源记录损坏，拒绝自动覆盖') from error
        if (not isinstance(record, dict) or type(record.get('schema_version')) is not int
                or record != source_record(tag, commit)):
            raise RuntimeError(f'更新镜像 {tag} 的源码来源与本次发布不一致，拒绝自动覆盖')


def plan_release(root, upstream_tag, mirror):
    validate_tag(upstream_tag)
    head = git(root, 'rev-parse', 'HEAD').stdout.strip()
    upstream = git(root, 'rev-parse', f'refs/upstream-tags/{upstream_tag}^{{commit}}').stdout.strip()
    if not is_ancestor(root, upstream, head):
        raise RuntimeError('待发布提交尚未包含目标上游，拒绝发布')
    refs = remote_tags(root, 'origin', upstream_tag)
    source_tag_commit(root, upstream_tag, refs)
    local = git(root, 'rev-parse', '--verify', '--quiet', f'refs/tags/{upstream_tag}', accepted=(0, 1))
    if local.returncode == 0:
        tagged = git(root, 'rev-parse', f'refs/tags/{upstream_tag}^{{commit}}').stdout.strip()
        if tagged != head:
            raise RuntimeError(f'已有 {upstream_tag} 没有指向本次验证 HEAD，拒绝构建旧源码或移动历史标签')
    verify_mirror_source(root, mirror, upstream_tag, head)
    return {'release_tag': upstream_tag, 'source_commit': head, 'upstream_commit': upstream,
            'reuse': local.returncode == 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('check', 'plan', 'record'))
    parser.add_argument('--upstream-tag', required=True)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--mirror', default='')
    parser.add_argument('--force', action='store_true')
    arguments = parser.parse_args()
    try:
        if arguments.mode == 'check':
            result = check_release(Path.cwd(), arguments.upstream_tag, arguments.repository, arguments.force)
        elif arguments.mode == 'plan':
            if not arguments.mirror:
                raise ValueError('核对发布标签需要指定更新镜像')
            result = plan_release(Path.cwd(), arguments.upstream_tag, arguments.mirror)
        else:
            if not arguments.mirror:
                raise ValueError('记录发布来源需要指定更新镜像')
            result = record_source(Path.cwd(), arguments.upstream_tag, arguments.mirror)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f'上游发布决策失败：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
