import argparse
import glob
import hashlib
import os
import re
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from ok.update.copy_ok_folder import find_and_copy_site_package, get_file_in_path_or_cwd
from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

INLINED_REQUIREMENTS = {
    'ok-script': 'ok',
    'pyappify': 'pyappify',
}

PYAPPIFY_VERSION = '1.0.20'
PYAPPIFY_UPDATE_MODULES = ('updater_ui.py', '_tk_updater.py', 'changelog.py', 'i18n.py')


def _pyappify_source():
    """声明升级不足以更新内联包，构建解释器必须实际提供本轮锁定版本。"""
    try:
        installed = distribution('pyappify')
    except PackageNotFoundError:
        raise RuntimeError('构建环境缺少 PyAppify（PYAPPIFY_SOURCE_MISSING）') from None
    if installed.version != PYAPPIFY_VERSION:
        raise RuntimeError('构建环境的 PyAppify 版本不符（PYAPPIFY_VERSION_MISMATCH）')
    source = Path(installed.locate_file('pyappify'))
    if not source.is_dir() or any(not (source / name).is_file() for name in PYAPPIFY_UPDATE_MODULES):
        raise RuntimeError('构建环境的 PyAppify 更新模块缺失（PYAPPIFY_SOURCE_INCOMPLETE）')
    return source


def _package_digests(folder):
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob('*'))
        if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo')
    }


def verify_inlined_pyappify(repo_dir):
    """内联结果必须与本次构建源逐文件相同，已有旧目录不能靠跳过复制蒙混通过。"""
    source = _pyappify_source()
    destination = Path(repo_dir) / 'pyappify'
    if not destination.is_dir() or _package_digests(destination) != _package_digests(source):
        raise RuntimeError('PyAppify 内联来源不一致（PYAPPIFY_INLINE_MISMATCH）')
    return PYAPPIFY_VERSION


def _read_deploy_entries(repo_dir):
    deploy_file = os.path.join(repo_dir, 'deploy.txt')
    if not os.path.exists(deploy_file):
        return []

    with open(deploy_file, 'r', encoding='utf-8') as file:
        return [
            line.strip().replace('\\', '/').strip('/')
            for line in file
            if line.strip() and not line.lstrip().startswith('#')
        ]


def _deploy_includes_folder(deploy_entries, folder):
    folder = folder.replace('\\', '/').strip('/')
    return any(entry == folder or entry.startswith(f'{folder}/') for entry in deploy_entries)


def _add_folders_to_deploy(repo_dir, folders):
    deploy_file = os.path.join(repo_dir, 'deploy.txt')
    deploy_entries = _read_deploy_entries(repo_dir)
    missing_folders = []
    for folder in folders:
        normalized_folder = folder.replace('\\', '/').strip('/')
        if (
            normalized_folder not in missing_folders
            and not _deploy_includes_folder(deploy_entries, normalized_folder)
        ):
            missing_folders.append(normalized_folder)
    if not missing_folders:
        return

    existing_content = ''
    if os.path.exists(deploy_file):
        with open(deploy_file, 'r', encoding='utf-8') as file:
            existing_content = file.read()
    with open(deploy_file, 'a', encoding='utf-8') as file:
        if existing_content and not existing_content.endswith(('\n', '\r')):
            file.write('\n')
        file.writelines(f'{folder}\n' for folder in missing_folders)


def _parse_inlined_requirement(value):
    package_name, separator, package_folder = value.partition('=')
    package_name = package_name.strip()
    package_folder = package_folder.strip().replace('\\', '/').strip('/')
    if not separator or not package_name or not package_folder:
        raise argparse.ArgumentTypeError(
            f"invalid inlined requirement '{value}'; expected PACKAGE=FOLDER"
        )
    return package_name, package_folder


def inline_site_packages(repo_dir, package_folders=None):
    if package_folders is None:
        package_folders = INLINED_REQUIREMENTS.values()

    for package_folder in package_folders:
        if package_folder == 'pyappify':
            _pyappify_source()
        if os.path.exists(os.path.join(repo_dir, package_folder)):
            if package_folder == 'pyappify':
                verify_inlined_pyappify(repo_dir)
            logger.info('内联目录已存在，来源检查完成')
            continue
        exit_code = find_and_copy_site_package(package_folder, repo_dir)
        if exit_code != 0:
            raise RuntimeError('从构建环境内联依赖失败（INLINE_COPY_FAILED）')
        if package_folder == 'pyappify':
            verify_inlined_pyappify(repo_dir)


def _remove_inlined_requirements(repo_dir, package_names):
    package_names = tuple(package_names)
    if not package_names:
        return
    requirement_pattern = re.compile(
        r'^\s*({})\b'.format('|'.join(re.escape(name) for name in package_names)),
        re.IGNORECASE,
    )
    for file_path in glob.glob(os.path.join(repo_dir, 'requirements*.txt')):
        with open(file_path, 'r', encoding='utf-8') as file:
            lines = file.readlines()
        filtered_lines = [line for line in lines if not requirement_pattern.match(line)]
        with open(file_path, 'w', encoding='utf-8') as file:
            file.writelines(filtered_lines)


def remove_ok_requirements(repo_dir, tag, additional_inlined_requirements=None):
    inlined_requirements = dict(INLINED_REQUIREMENTS)
    if additional_inlined_requirements:
        inlined_requirements.update(additional_inlined_requirements)

    package_folders = inlined_requirements.values()
    _add_folders_to_deploy(repo_dir, package_folders)
    inline_site_packages(repo_dir, package_folders)

    config_file = get_file_in_path_or_cwd(repo_dir, 'config.py')
    with open(config_file, 'r', encoding='utf-8') as file:
        content = file.read()
    new_content = re.sub(r'version = ".+"', f'version = "{tag}"', content)
    with open(config_file, 'w', encoding='utf-8') as file:
        file.write(new_content)

    _remove_inlined_requirements(repo_dir, inlined_requirements.keys())


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inline deployment requirements.')
    parser.add_argument('--tag', help='Set the application version and remove inlined packages from requirements files.')
    parser.add_argument(
        '--add-inlined-requirement',
        action='append',
        default=[],
        metavar='PACKAGE=FOLDER',
        type=_parse_inlined_requirement,
        help='Add an inlined package mapping. Repeat this option to add multiple packages.',
    )
    args = parser.parse_args(argv)
    additional_inlined_requirements = dict(args.add_inlined_requirement)
    repo_dir = os.getcwd()

    if args.tag:
        print(f'remove ok_requirements from tag {args.tag} cwd {repo_dir}')
        remove_ok_requirements(repo_dir, args.tag, additional_inlined_requirements)
    else:
        inlined_requirements = dict(INLINED_REQUIREMENTS)
        inlined_requirements.update(additional_inlined_requirements)
        _add_folders_to_deploy(repo_dir, inlined_requirements.values())
        inline_site_packages(repo_dir, inlined_requirements.values())


if __name__ == "__main__":
    main()
