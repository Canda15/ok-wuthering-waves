"""Compare release versions using only the standard library.

Supports numeric releases, an optional ``v`` prefix/epoch, alpha/beta/RC,
development/post releases, and local version labels. Invalid tags raise
ValueError so update checks cannot mistake arbitrary text for a release.
"""

import re


_VERSION = re.compile(
    r'v?(?:(?P<epoch>[0-9]+)!)?'
    r'(?P<release>[0-9]+(?:\.[0-9]+)*)'
    r'(?:[-_.]?(?P<pre>alpha|beta|preview|pre|rc|a|b|c)[-_.]?(?P<pre_number>[0-9]+)?)?'
    r'(?:(?:-(?P<post_number>[0-9]+))|'
    r'(?:[-_.]?(?P<post>post|rev|r)[-_.]?(?P<post_label_number>[0-9]+)?))?'
    r'(?:[-_.]?(?P<dev>dev)[-_.]?(?P<dev_number>[0-9]+)?)?'
    r'(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?',
    re.IGNORECASE | re.ASCII,
)
_PRE_ORDER = {'alpha': 0, 'a': 0, 'beta': 1, 'b': 1,
              'preview': 2, 'pre': 2, 'rc': 2, 'c': 2}


def _version_key(version):
    # xmod 是本应用正式发行标识；只为该格式接线，保留插件原有的通用版本语义。
    if isinstance(version, str) and '-xmod' in version:
        from selfhost.versioning import parse_version_tag, unbranded_tag
        branded = parse_version_tag(version)
        if branded is not None and branded.branded:
            base = _version_key(unbranded_tag(version))
            return (*base[:-1], (1, branded.revision))
    match = _VERSION.fullmatch(version.strip()) if isinstance(version, str) else None
    if match is None:
        raise ValueError(f'Invalid version: {version!r}')
    parts = match.groupdict()
    release = [int(part) for part in parts['release'].split('.')]
    # Missing release components are zero: 1.2 == 1.2.0.
    while release and release[-1] == 0:
        release.pop()
    post = -1
    if parts['post'] is not None or parts['post_number'] is not None:
        post = int(parts['post_number'] or parts['post_label_number'] or 0)
    if parts['pre'] is not None:
        pre = (_PRE_ORDER[parts['pre'].lower()], int(parts['pre_number'] or 0))
    elif parts['dev'] is not None and post == -1:
        pre = (-1, 0)  # A development snapshot precedes even the first alpha.
    else:
        pre = (3, 0)  # Final releases follow prereleases.
    dev = (1, 0) if parts['dev'] is None else (0, int(parts['dev_number'] or 0))
    local = ()
    if parts['local'] is not None:
        local = tuple((1, int(part)) if part.isdigit() else (0, part.lower())
                      for part in re.split(r'[-_.]', parts['local']))
    return int(parts['epoch'] or 0), tuple(release), pre, post, dev, local, (0, 0)


def compare_versions(left, right):
    """Return -1, 0, or 1 for two valid release versions; raise on invalid tags."""
    if any(isinstance(value, str) and '-xmod' in value for value in (left, right)):
        from selfhost.versioning import version_tag_key
        left_tag, right_tag = version_tag_key(left), version_tag_key(right)
        if left_tag is not None and right_tag is not None:
            # 应用标签遵循启动器合同（例如 rc 与 rc.0），普通插件仍走下方通用比较。
            return (left_tag > right_tag) - (left_tag < right_tag)
    left_key, right_key = _version_key(left), _version_key(right)
    return (left_key > right_key) - (left_key < right_key)
