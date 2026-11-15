"""NoNo 应用标签合同；同一文件也内联到 PyAppify，避免更新器各自解释品牌版本。"""

from __future__ import annotations

import argparse
import json
import re
from typing import NamedTuple


VERSION_TAG = re.compile(
    r'v?([0-9]+)\.([0-9]+)\.([0-9]+)'
    r'(?:[-.](alpha|beta|rc)(?:\.([0-9]+))?)?'
    r'(-xmod(?:\.([1-9][0-9]*))?)?'
)
_CHANNEL_RANK = {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}
_MAX_COMPONENT = (1 << 64) - 1


class VersionTag(NamedTuple):
    release: tuple[int, int, int]
    channel: str | None
    number: int | None
    branded: bool
    revision: int

    def key(self):
        return (*self.release, _CHANNEL_RANK[self.channel],
                -1 if self.number is None else self.number, int(self.branded), self.revision)


def parse_version_tag(tag):
    match = VERSION_TAG.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        return None
    major, minor, patch, channel, number, brand, revision = match.groups()
    try:
        components = [int(value) for value in (major, minor, patch, number, revision) if value is not None]
    except ValueError:
        return None
    if any(value > _MAX_COMPONENT for value in components):
        return None
    return VersionTag((int(major), int(minor), int(patch)), channel,
                      None if number is None else int(number), brand is not None, int(revision or 0))


def require_version_tag(tag, *, branded=None):
    version = parse_version_tag(tag)
    if version is None or (branded is not None and version.branded != branded):
        raise ValueError('版本标签不符合 NoNo 的正式、预览或 xmod 格式')
    return version


def version_tag_key(tag):
    version = parse_version_tag(tag)
    return None if version is None else version.key()


def is_version_tag(tag):
    return parse_version_tag(tag) is not None


def is_release_version(tag):
    version = parse_version_tag(tag)
    return version is not None and version.channel is None


def is_greater_version(left, right):
    left_key, right_key = version_tag_key(left), version_tag_key(right)
    return left_key is not None and right_key is not None and left_key > right_key


def sort_version_tags(tags):
    keyed = [(version_tag_key(tag), tag) for tag in tags]
    return [tag for key, tag in sorted((item for item in keyed if item[0] is not None),
                                      key=lambda item: item[0], reverse=True)]


def unbranded_tag(tag):
    require_version_tag(tag)
    return tag.split('-xmod', 1)[0]


def branded_tag(upstream_tag, revision=0):
    require_version_tag(upstream_tag, branded=False)
    if type(revision) is not int or not 0 <= revision <= _MAX_COMPONENT:
        raise ValueError('xmod 修订序号必须是有效的非负整数')
    return f'{upstream_tag}-xmod' + (f'.{revision}' if revision else '')


def same_release_family(tag, upstream_tag):
    candidate, source = parse_version_tag(tag), require_version_tag(upstream_tag, branded=False)
    return (candidate is not None and candidate.branded
            and candidate[:3] == source[:3])


def next_revision(upstream_tag, tags):
    revisions = [require_version_tag(tag).revision for tag in tags if same_release_family(tag, upstream_tag)]
    return branded_tag(upstream_tag, max(revisions) + 1 if revisions else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--new-release', action='store_true', help='新发布必须使用 xmod 标签。')
    mode.add_argument('--upstream', action='store_true', help='官方来源不能使用本项目品牌后缀。')
    arguments = parser.parse_args()
    try:
        branded = True if arguments.new_release else False if arguments.upstream else None
        version = require_version_tag(arguments.tag, branded=branded)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({'tag': arguments.tag, 'is_release': version.channel is None,
                      'upstream_tag': unbranded_tag(arguments.tag)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
