"""仅规范本项目受管字段，再用 Git 三方合并保留上游与本地的其余改动。"""

import argparse
import ast
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selfhost.versioning import require_version_tag


MANAGED_FILES = {'config.py', 'pyproject.toml', 'requirements.txt'}
FRAMEWORK_PIN = re.compile(r'ok-script(?:\[[a-z,]+\])?\s*==\s*([0-9][0-9A-Za-z.!+_-]*)')


def git(root, *arguments, accepted=(0,)):
    try:
        result = subprocess.run(['git', *arguments], cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'无法完成 Git {arguments[0]}') from error
    if result.returncode not in accepted:
        # 保留失败命令与状态；Git 原始日志可能包含带凭据的远端地址。
        raise RuntimeError(f'Git {arguments[0]} 失败，退出码 {result.returncode}')
    return result


def is_framework(spec):
    return isinstance(spec, str) and re.match(r'^ok-script(?:$|[\s\[=<>!~;@])', spec) is not None


def pinned_version(spec):
    match = FRAMEWORK_PIN.fullmatch(spec)
    if match is None:
        raise RuntimeError('ok-script 依赖不再是已支持的固定版本，需人工适配')
    return match.group(1)


def project_framework(document, *, required=False):
    project = document.get('project', {})
    dependencies = project.get('dependencies', [])
    if not isinstance(dependencies, list) or not all(isinstance(spec, str) for spec in dependencies):
        raise RuntimeError('pyproject.toml 的依赖布局已改变，需人工合并')
    pins = [pinned_version(spec) for spec in dependencies if is_framework(spec)]
    if len(pins) > 1 or (required and not pins):
        raise RuntimeError('上游必须声明唯一的 ok-script 固定版本')
    version = pins[0] if pins else None
    for name, specs in project.get('optional-dependencies', {}).items():
        if name == 'web-test':
            if not isinstance(specs, list) or len(specs) != 1 or not is_framework(specs[0]):
                raise RuntimeError('web-test 已包含未受管依赖，需人工合并')
            if version is not None and pinned_version(specs[0]) != version:
                raise RuntimeError('上游 web-test 与主依赖的框架版本不一致')
            pinned_version(specs[0])
        elif any(is_framework(spec) for spec in specs):
            raise RuntimeError(f'额外依赖 {name} 中出现 ok-script，需人工合并')
    return version


def normalize_config(content):
    tree = ast.parse(content)
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'config' for target in node.targets)]
    if len(assignments) != 1:
        raise RuntimeError('config.py 不再只有一个配置字典，需人工合并')

    def field(dictionary, name):
        if not isinstance(dictionary, ast.Dict) or any(key is None for key in dictionary.keys):
            raise RuntimeError('config.py 的受管配置布局已改变，需人工合并')
        matches = [value for key, value in zip(dictionary.keys, dictionary.values)
                   if isinstance(key, ast.Constant) and key.value == name]
        if len(matches) != 1:
            raise RuntimeError(f'config.py 必须包含唯一的 {name} 字段')
        return matches[0]

    interaction = field(field(assignments[0].value, 'windows'), 'interaction')
    value = ast.literal_eval(interaction)
    if value not in ('PostMessage', 'nono') and value not in (
            ['PostMessage', 'Pynput'], ['nono', 'post_message_test']):
        raise RuntimeError('上游输入方式出现未知值，需人工核对输入模式适配')
    # AST 的列号是 UTF-8 字节偏移；同一行前面有中文时也只替换这个值。
    lines = content.splitlines(keepends=True)
    start = sum(map(len, lines[:interaction.lineno - 1])) + interaction.col_offset
    end = sum(map(len, lines[:interaction.end_lineno - 1])) + interaction.end_col_offset
    return content[:start] + b"['nono', 'post_message_test']" + content[end:]


def normalize_pyproject(content):
    text = content.decode('utf-8')
    document = tomllib.loads(text)
    project_framework(document)
    expected = deepcopy(document)
    project = expected['project']
    framework_specs = {spec for spec in project['dependencies'] if is_framework(spec)}
    project['dependencies'] = [spec for spec in project['dependencies'] if not is_framework(spec)]
    lines = text.splitlines(keepends=True)
    extras = project.get('optional-dependencies', {})
    if 'web-test' in extras:
        expected_extra = extras.pop('web-test')
        starts = [index for index, line in enumerate(lines)
                  if re.fullmatch(r'web-test\s*=\s*\[\s*(?:#.*)?', line.strip())]
        if len(starts) != 1:
            raise RuntimeError('web-test 的源码布局已改变，需人工合并')
        start = starts[0]
        end = next((index for index in range(start + 1, len(lines))
                    if lines[index].split('#', 1)[0].strip() == ']'), None)
        if end is None or tomllib.loads(''.join(lines[start:end + 1])) != {'web-test': expected_extra}:
            raise RuntimeError('web-test 不再是独立的受管数组，需人工合并')
        del lines[start:end + 1]
    removed = []
    kept = []
    for line in lines:
        match = re.fullmatch(r'''\s*(['"])(.*?)\1\s*,?\s*(?:#.*)?''', line.rstrip('\r\n'))
        if match and match.group(2) in framework_specs:
            removed.append(match.group(2))
        else:
            kept.append(line)
    normalized = ''.join(kept)
    if set(removed) != framework_specs or len(removed) != len(framework_specs) or tomllib.loads(normalized) != expected:
        raise RuntimeError('框架 pin 无法独立移除，需人工合并 pyproject.toml')
    return normalized.encode('utf-8')


def normalize_requirements(content):
    lines = content.decode('utf-8').splitlines(keepends=True)
    kept = []
    index = count = 0
    while index < len(lines):
        line = lines[index]
        if is_framework(line.strip()):
            pinned_version(line.strip())
            count += 1
            index += 1
            # pip-compile 的缩进来源注释只属于紧邻的这条依赖。
            while index < len(lines) and re.match(r'^\s+#', lines[index]):
                index += 1
        else:
            kept.append(line)
            index += 1
    if count > 1:
        raise RuntimeError('requirements.txt 出现多条框架依赖，需人工合并')
    return ''.join(kept).encode('utf-8')


def resolve_conflicts(root):
    """全部候选合并成功后才写盘、暂存，保留未知冲突供人工检查。"""
    names = git(root, 'diff', '--name-only', '--diff-filter=U', '-z').stdout
    conflicts = [name.decode('utf-8') for name in names.split(b'\0') if name]
    unknown = sorted(set(conflicts) - MANAGED_FILES)
    if unknown:
        raise RuntimeError(f'需要人工合并：{", ".join(unknown)}')
    normalizers = {'config.py': normalize_config, 'pyproject.toml': normalize_pyproject,
                   'requirements.txt': normalize_requirements}
    candidates = {}
    with tempfile.TemporaryDirectory(prefix='nono-merge-') as directory:
        temporary = Path(directory)
        for name in conflicts:
            records = git(root, 'ls-files', '--unmerged', '--', name).stdout.splitlines()
            stages = [record.split(b'\t', 1)[0].split() for record in records]
            if len(stages) != 3 or {item[2] for item in stages} != {b'1', b'2', b'3'} or any(
                    item[0] != b'100644' for item in stages):
                raise RuntimeError(f'{name} 存在新增、删除或非普通文件冲突，需人工合并')
            for stage, label in ((1, 'base'), (2, 'ours'), (3, 'theirs')):
                source = git(root, 'show', f':{stage}:{name}').stdout
                (temporary / label).write_bytes(normalizers[name](source))
            merged = git(root, 'merge-file', '-p', str(temporary / 'ours'), str(temporary / 'base'),
                         str(temporary / 'theirs'), accepted=tuple(range(128)))
            if merged.returncode:
                raise RuntimeError(f'{name} 的非受管字段仍有冲突，需人工合并')
            candidates[name] = merged.stdout
    for name, content in candidates.items():
        (Path(root) / name).write_bytes(content)
    if candidates:
        git(root, 'add', '--', *candidates)
    return sorted(candidates)


def run_merge(root, upstream_tag):
    require_version_tag(upstream_tag, branded=False)
    reference = f'refs/upstream-tags/{upstream_tag}'
    upstream_commit = git(root, 'rev-parse', '--verify', f'{reference}^{{commit}}').stdout.decode().strip()
    # 规范化会移除 pin，必须先从官方对象取得版本；已合入时也不能回落到旧 pin。
    source = git(root, 'show', f'{upstream_commit}:pyproject.toml').stdout.decode('utf-8')
    framework_version = project_framework(tomllib.loads(source), required=True)
    if git(root, 'status', '--porcelain', '--untracked-files=no').stdout:
        raise RuntimeError('合并上游前需要干净的已跟踪工作树')
    if git(root, 'rev-parse', '--verify', 'MERGE_HEAD', accepted=(0, 128)).returncode == 0:
        raise RuntimeError('已有未完成合并，请先处理原合并')
    merged = git(root, 'merge', '--no-ff', '--no-commit', upstream_commit, accepted=(0, 1))
    resolved = resolve_conflicts(root)
    if merged.returncode and not resolved:
        raise RuntimeError('Git 合并失败且没有可处理的受管冲突')
    pending = git(root, 'rev-parse', '--verify', 'MERGE_HEAD', accepted=(0, 128)).returncode == 0
    if pending:
        git(root, 'commit', '-m', f'other: 同步上游 {upstream_tag}\n\n保留本项目的 nono 输入配置和自维护依赖。')
    git(root, 'merge-base', '--is-ancestor', upstream_commit, 'HEAD')
    return {'upstream_tag': upstream_tag, 'upstream_commit': upstream_commit,
            'framework_version': framework_version, 'merged': pending, 'resolved': resolved}


def main():
    parser = argparse.ArgumentParser(description='保留 NoNo 受管配置与依赖的上游合并入口。')
    parser.add_argument('--upstream-tag', required=True)
    arguments = parser.parse_args()
    try:
        result = run_merge(Path.cwd(), arguments.upstream_tag)
    except (RuntimeError, ValueError, SyntaxError, OSError) as error:
        print(f'合并上游失败：{error}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
