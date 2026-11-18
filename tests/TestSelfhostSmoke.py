"""守住 selfhost 补丁面的最小冒烟集。

上游的测试覆盖的是"没有我们补丁"的树,测不到四件事:随 pin 重新内联的 ok/
是否还认这些 API、被我们重新注册的两个页面是否还能实例化、改名相关的 override
是否还压得住上游的默认值、以及施加在 ok/ 源码里的品牌补丁是否还在。
框架冒烟由 .github/workflows/sync-upstream.yml 在每次自动合并后执行，
红了就不许推送。原生宿主用同一测试文件验证实际进程和参数，发布时编译后执行。
严格逻辑差异用隔离树验证来源、重放与部署，不初始化游戏设备或发送通知。
便携交付用小型部署仓库验证当前快照、更新锚点和归档，不在开发机执行启动器 setup。
"""

import inspect
import importlib
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import CancelledError
from contextlib import ExitStack, chdir, redirect_stdout
from importlib import util as importlib_util
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def load_sync_script(filename='sync_ok.py'):
    script = Path(__file__).resolve().parents[1] / 'scripts' / filename
    spec = importlib_util.spec_from_file_location(f'selfhost_{script.stem}', script)
    sync_ok = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(sync_ok)
    return sync_ok


class TestInferenceMemoryDefaults(unittest.TestCase):
    def test_cache_default_precedes_configuration_and_preserves_environment(self):
        from selfhost.portable import diagnostic_tail, process_environment, hidden_process_options

        repo = Path(__file__).resolve().parents[1]
        names = ('ONEDNN_PRIMITIVE_CACHE_CAPACITY', 'DNNL_PRIMITIVE_CACHE_CAPACITY')
        # 在真实入口的首个配置导入处停止，验证默认值不会等到加载模型后才设置。
        observer = (
            'import importlib,json,os,sys\nfrom pathlib import Path\n'
            'selfhost=sys.modules["selfhost"]\n'
            f'names={names!r}\n'
            'before=[os.environ.get(name) for name in names]\n'
            'importlib.reload(selfhost)\n'
            'print(json.dumps({"before":before,"after":[os.environ.get(name) for name in names],'
            '"source":str(Path(selfhost.__file__).resolve()),'
            '"heavy_imports":any(name in sys.modules for name in ("ok","cv2","openvino","onnxocr"))}))\n'
            'raise SystemExit(0)\n'
        )
        scenarios = (
            (None, None, '64', None),
            ('128', None, '128', None),
            ('0', None, '0', None),
            (None, '128', None, '128'),
            (None, '0', None, '0'),
            ('16', '128', '16', '128'),
            ('', '0', '', '0'),
            ('', '', '64', ''),
        )
        with tempfile.TemporaryDirectory() as directory:
            working = Path(directory).resolve()
            package = working / 'selfhost'
            package.mkdir()
            shutil.copy2(repo / 'selfhost/__init__.py', package / '__init__.py')
            (package / 'user_data.py').write_text('def bootstrap_user_data():\n    pass\n', encoding='utf-8')
            (working / 'config.py').write_text(observer, encoding='utf-8')
            for entrypoint in ('main.py', 'main_debug.py', 'main_web.py', 'main_web_debug.py'):
                shutil.copy2(repo / entrypoint, working / entrypoint)
                for supplied_new, supplied_old, expected_new, expected_old in scenarios:
                    with self.subTest(entrypoint=entrypoint, supplied=(supplied_new, supplied_old)):
                        environment = process_environment(working)
                        for name, value in zip(names, (supplied_new, supplied_old)):
                            environment.pop(name, None)
                            if value is not None:
                                environment[name] = value
                        result = subprocess.run(
                            [sys.executable, '-X', 'utf8', '-B', str(working / entrypoint)],
                            cwd=working, env=environment, capture_output=True, text=True,
                            encoding='utf-8', timeout=10, **hidden_process_options(),
                        )
                        self.assertEqual(result.returncode, 0,
                                         f'标准输出尾部：\n{diagnostic_tail(result.stdout)}\n'
                                         f'标准错误尾部：\n{diagnostic_tail(result.stderr)}')
                        observed = json.loads(result.stdout)
                        self.assertEqual(observed['before'], [expected_new, expected_old])
                        self.assertEqual(observed['after'], observed['before'])
                        self.assertEqual(observed['source'], str((package / '__init__.py').resolve()))
                        self.assertFalse(observed['heavy_imports'])


class TestSelfhostSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 原生宿主验证只依赖标准库，不应提前加载 Qt 和游戏框架。
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        # Windows runner 的 TEMP 可能使用 RUNNER~1；同一夹具统一规范根，避免解析接口间误判。
        self.user_root = Path(self.temp_dir.name).resolve() / 'user'
        # 路径消费者导入前隔离便携环境，不能加载继承环境里的用户脚本或诊断内容。
        self.user_data_patch = patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(self.user_root),
                                                      'OK_DISABLE_FILE_LOG': '1'})
        self.user_data_patch.start()
        from ok import og
        from ok.util.config import Config
        # 两个页面在构造时会读 og.app 的翻译和配置目录,这里给最小替身,
        # 避免冒烟测试碰到用户真实的 configs/。
        self.app_patch = patch.object(og, 'app', SimpleNamespace(tr=lambda text: text,
                                                               start_controller=Mock()))
        self.app_patch.start()
        self.config_class = Config
        self.old_config_folder = Config.config_folder
        Config.config_folder = str(self.user_root / 'configs')

    def tearDown(self):
        self.config_class.config_folder = self.old_config_folder
        self.temp_dir.cleanup()
        self.app.processEvents()
        self.app_patch.stop()
        self.user_data_patch.stop()

    def test_apply_registers_both_tabs_and_is_idempotent(self):
        from selfhost.overrides import CUSTOM_TABS, apply
        config = {}
        self.assertEqual(apply(config), list(CUSTOM_TABS))
        self.assertEqual([tuple(tab) for tab in config['custom_tabs']], list(CUSTOM_TABS))
        # 重复调用或上游把页面自己注册回来时,都不应出现重复项。
        self.assertEqual(apply(config), list(CUSTOM_TABS))
        self.assertEqual(len(config['custom_tabs']), len(CUSTOM_TABS))

    def test_xmod_versions_sort_as_branded_releases_in_both_python_consumers(self):
        from ok.util.version import compare_versions
        from ok.update import inline_ok_requirements as inliner
        from types import ModuleType

        ordered = ['v3.8.1-alpha.1-xmod', 'v3.8.1-beta.1-xmod', 'v3.8.1-rc.1-xmod',
                   'v3.8.1', 'v3.8.1-xmod', 'v3.8.1-xmod.1', 'v3.8.1-xmod.2',
                   'v3.8.1-xmod.10', 'v3.8.2-xmod']
        for left, right in zip(ordered, ordered[1:]):
            with self.subTest(left=left, right=right):
                self.assertLess(compare_versions(left, right), 0)
        # 应用品牌不能改变框架、插件已有的通用版本比较。
        self.assertLess(compare_versions('3.0.5b8', '3.0.5b9'), 0)
        self.assertLess(compare_versions('1.0.dev1', '1.0'), 0)
        self.assertLess(compare_versions('v3.8.1-rc-xmod', 'v3.8.1-rc.0-xmod'), 0)
        # 插件本地标签中出现品牌文字，也不能冒充应用的完整版本格式。
        for local, base in (('1.2+team-xmod', '1.2'), ('3.0.5b8+local-xmod', '3.0.5b8')):
            with self.subTest(local=local):
                self.assertGreater(compare_versions(local, base), 0)

        destination = Path(self.temp_dir.name) / 'xmod-inline'
        destination.mkdir()
        with redirect_stdout(StringIO()):
            inliner.inline_site_packages(destination, ('pyappify',))
        package = ModuleType('_xmod_pyappify_probe')
        package.__path__ = [str(destination / 'pyappify')]
        with patch.dict(sys.modules, {package.__name__: package}):
            versions = importlib.import_module(f'{package.__name__}.versions')
            self.assertEqual(versions.sort_version_tags(ordered), ordered[::-1])
            for left, right in zip(ordered, ordered[1:]):
                self.assertTrue(versions.is_greater_version(right, left))
            for tag in ordered[3:]:
                self.assertTrue(versions.is_release_version(tag), tag)
            for tag in ordered[:3]:
                self.assertFalse(versions.is_release_version(tag), tag)
            for invalid in ('dev', 'v3.8.1-xmod.0', 'v3.8.1-xmod.bad', 'v3.8.1-other'):
                self.assertFalse(versions.is_version_tag(invalid), invalid)

        self.assertEqual(inliner.verify_inlined_pyappify(destination), '1.0.20')

    def test_xmod_tag_generation_requires_integrated_base_and_ignores_future_cached_tags(self):
        release_script = load_sync_script('upstream_release.py')
        fixture = self.make_upstream_release_fixture()
        from runpy import run_path
        generator = run_path(str(Path(__file__).resolve().parents[1]
                                / '.agents/skills/deploy/scripts/next_tag.py'))['next_tag']
        with self.assertRaisesRegex(ValueError, '官方基础'):
            generator('release', ['v3.8.1', 'v3.8.3'])
        self.assertEqual(generator('release', ['v3.8.1', 'v3.8.3'], 'v3.8.1'), 'v3.8.1-xmod')
        self.assertEqual(generator('release', ['v3.8.1-xmod.2', 'v3.8.1-xmod.10'], 'v3.8.1'),
                         'v3.8.1-xmod.11')
        self.assertEqual(generator('beta', ['v3.8.1-beta.2-xmod'], 'v3.8.1'), 'v3.8.1-beta.3-xmod')
        with self.assertRaisesRegex(ValueError, '已发布正式版'):
            generator('beta', ['v3.8.1-xmod'], 'v3.8.1')
        with patch.dict(os.environ, fixture.environment, clear=True):
            # 新官方标签的对象已在本地，仍未合入 HEAD，不能把“能找到对象”当作已集成。
            future = fixture.git('commit-tree', fixture.git('rev-parse', 'HEAD^{tree}'),
                                 '-p', fixture.upstream, '-m', '未合入的未来上游')
            fixture.git('tag', '-a', 'v3.8.3', future, '-m', '仅缓存的未来版本')
            fixture.git('push', 'upstream', 'refs/tags/v3.8.3')
            self.assertEqual(release_script.find_integrated_upstream(fixture.checkout),
                             {'upstream_tag': 'v3.8.1', 'upstream_commit': fixture.upstream})
            fixture.git('tag', '-a', 'v3.8.3-xmod', '-m', '错误基础标签')
            with self.assertRaisesRegex(RuntimeError, '实际包含的官方基础版本'):
                release_script.record_source(fixture.checkout, 'v3.8.3-xmod', str(fixture.mirror))

    def test_xmod_inline_rejects_extra_or_changed_contract_and_altered_source(self):
        from ok.update import inline_ok_requirements as inliner

        destination = Path(self.temp_dir.name) / 'xmod-inline-protection'
        destination.mkdir()
        source = inliner._pyappify_source()
        before = inliner._package_digests(source)
        with redirect_stdout(StringIO()):
            inliner.inline_site_packages(destination, ('pyappify',))
            inliner.inline_site_packages(destination, ('pyappify',))
        self.assertEqual(inliner._package_digests(source), before)
        contract = destination / 'pyappify/_nono_versions.py'
        original = contract.read_bytes()
        contract.write_bytes(original + b'\naltered = True\n')
        with self.assertRaisesRegex(RuntimeError, 'PYAPPIFY_INLINE_MISMATCH'):
            inliner.verify_inlined_pyappify(destination)
        contract.write_bytes(original)
        extra = destination / 'pyappify/stale_module.py'
        extra.write_bytes(b'stale = True\n')
        with self.assertRaisesRegex(RuntimeError, 'PYAPPIFY_INLINE_MISMATCH'):
            inliner.inline_site_packages(destination, ('pyappify',))
        extra.unlink()
        self.assertEqual(inliner.verify_inlined_pyappify(destination), '1.0.20')
        altered = Path(self.temp_dir.name) / 'altered-source'
        shutil.copytree(source, altered, ignore=shutil.ignore_patterns('__pycache__'))
        (altered / 'versions.py').write_bytes(b'wrong = True\n')
        with patch.object(inliner, '_pyappify_source', return_value=altered):
            with self.assertRaisesRegex(RuntimeError, 'PYAPPIFY_VERSIONS_BASELINE_MISMATCH'):
                inliner.verify_inlined_pyappify(destination)

    def make_upstream_release_fixture(self, directory_name='upstream-release'):
        from selfhost.portable import process_environment

        root = Path(self.temp_dir.name).resolve() / directory_name
        root.mkdir()
        checkout, remote, mirror = root / 'checkout', root / 'remote.git', root / 'mirror.git'
        checkout.mkdir()
        environment = {key: value for key, value in process_environment().items()
                       if not key.upper().startswith('GIT_')}
        environment.update({'GIT_CONFIG_GLOBAL': str(root / 'empty-git-config'), 'GIT_CONFIG_NOSYSTEM': '1',
                            'NONO_USER_DATA_ROOT': str(self.user_root), 'OK_DISABLE_FILE_LOG': '1'})

        def git(*arguments, cwd=checkout):
            result = subprocess.run(['git', *arguments], cwd=cwd, env=environment, capture_output=True,
                                    text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        def commit(message):
            (checkout / 'source.txt').write_text(message + '\n', encoding='utf-8')
            git('add', 'source.txt')
            git('commit', '-m', message)
            return git('rev-parse', 'HEAD')

        git('init', '--bare', str(remote), cwd=root)
        git('init', '--bare', str(mirror), cwd=root)
        git('init', '-b', 'master')
        git('config', 'user.name', '发布测试')
        git('config', 'user.email', 'release-test@example.invalid')
        git('remote', 'add', 'origin', str(remote))
        base = commit('已有本地版本')
        git('tag', '-a', 'v3.8.0', '-m', '已有正式发布')
        git('push', 'origin', 'HEAD:master', 'refs/tags/v3.8.0')
        upstream = commit('新上游版本')
        git('update-ref', 'refs/upstream-tags/v3.8.1', upstream)
        head = commit('保留本地适配')
        (checkout / 'scripts').mkdir()
        shutil.copy2(Path(__file__).resolve().parents[1] / 'scripts/upstream_release.py', checkout / 'scripts')
        (checkout / 'selfhost').mkdir()
        for name in ('__init__.py', 'versioning.py'):
            shutil.copy2(Path(__file__).resolve().parents[1] / 'selfhost' / name, checkout / 'selfhost' / name)
        upstream_remote = root / 'upstream.git'
        git('init', '--bare', str(upstream_remote), cwd=root)
        git('remote', 'add', 'upstream', str(upstream_remote))
        git('tag', '-a', 'v3.8.1', upstream, '-m', '真实官方基础')
        git('push', 'upstream', 'refs/tags/v3.8.1')
        return SimpleNamespace(root=root, checkout=checkout, remote=remote, mirror=mirror,
                               upstream_remote=upstream_remote,
                               environment=environment, git=git, commit=commit, base=base,
                               upstream=upstream, head=head)

    def make_upstream_merge_fixture(self, variant='normal'):
        fixture = self.make_upstream_release_fixture(f'upstream-merge-{variant}')
        base_config = (
            "config = {\n"
            "    'gui_title': '官方标题',\n"
            "    'version': 'v3.8.1',\n"
            "    'debug': False,\n"
            "    'windows': {'标题': '中文偏移', 'interaction': 'PostMessage'},\n"
            "    'screen_ratio': 1,\n"
            "    'keep_setting': '保留',\n"
            "    'trigger_tasks': [\n"
            "        ['src.task.MouseResetTask', 'MouseResetTask'],\n"
            "        ['src.task.KeepTask', 'KeepTask'],\n"
            "    ],\n"
            "    'update_only_game_exit': True,\n"
            "}\n"
        )
        web_extra = 'web-test = [\n    "ok-script[web]==3.0.5b8",\n]\n'
        base_project = (
            '[project]\nname = "merge-fixture"\ndependencies = [\n'
            '    "mcp==2.3.0",\n'
            '    "ok-script[ocr,qt]==3.0.5b8",\n'
            '    "onnxocr-ppocrv5>=0.0.23",\n'
            '    "pyappify>=1.0.20",\n]\n\n'
            '[project.optional-dependencies]\ndev = [\n    "pytest",\n    "polib",\n]\n'
            + web_extra + 'docs = ["mkdocs"]\n'
        )
        framework_requirement = 'ok-script==3.0.5b8\n    # via\n    #   ok-ww (pyproject.toml)\n'
        base_requirements = (
            'mcp==2.3.0\n    # via ok-ww\n' + framework_requirement
            + 'onnxocr-ppocrv5==0.0.23\n    # via\n    #   ok-script\n    #   ok-ww (pyproject.toml)\n'
            'pyappify==1.0.20\n    # via ok-ww\n'
        )
        base_files = {'config.py': base_config, 'pyproject.toml': base_project,
                      'requirements.txt': base_requirements, 'notes.txt': '共同说明\n'}

        def save(files, message):
            for name, content in files.items():
                (fixture.checkout / name).write_text(content, encoding='utf-8', newline='')
            fixture.git('add', '-A')
            fixture.git('commit', '-m', message)
            return fixture.git('rev-parse', 'HEAD')

        shutil.copy2(Path(__file__).resolve().parents[1] / 'scripts/merge_upstream.py', fixture.checkout / 'scripts')
        save(base_files, '官方合并共同基线')
        fixture.git('checkout', '-b', 'upstream-v383')
        theirs = {name: content.replace('3.0.5b8', '3.0.5b9').replace('ppocrv5', 'ppocrv6')
                  .replace('0.0.23', '0.0.1') for name, content in base_files.items()}
        theirs['config.py'] = base_config.replace("'PostMessage'", "['PostMessage', 'Pynput']")
        theirs['config.py'] = theirs['config.py'].replace('官方标题', '上游新标题')
        if variant == 'interaction':
            theirs['config.py'] = theirs['config.py'].replace("'Pynput'", "'未知新方式'")
        if variant == 'other_file':
            theirs['notes.txt'] = '上游说明\n'
        if variant == 'web_extra':
            theirs['pyproject.toml'] = theirs['pyproject.toml'].replace(
                '"ok-script[web]==3.0.5b9",', '"ok-script[web]==3.0.5b9",\n    "new-web-runtime",')
        fixture.upstream_v383 = save(theirs, '官方框架和输入选项升级')
        fixture.git('update-ref', 'refs/upstream-tags/v3.8.3', fixture.upstream_v383)
        fixture.git('checkout', 'master')
        ours = dict(base_files)
        ours['config.py'] = base_config.replace("'PostMessage'", "['nono', 'post_message_test']")
        ours['config.py'] = ours['config.py'].replace("    'update_only_game_exit': True,\n", '')
        if variant == 'config_field':
            ours['config.py'] = ours['config.py'].replace('官方标题', '本地修改的标题')
        if variant == 'other_file':
            ours['notes.txt'] = '本地说明\n'
        ours['pyproject.toml'] = base_project.replace('    "ok-script[ocr,qt]==3.0.5b8",\n', '').replace(web_extra, '')
        ours['pyproject.toml'] = ours['pyproject.toml'].replace('    "polib",\n', '    "polib",\n    "PyYAML==6.0.3",\n')
        ours['pyproject.toml'] = ours['pyproject.toml'].replace(
            '    "pyappify>=1.0.20",\n',
            '    "pyappify>=1.0.20",\n'
            '    # >>> generated: vendored ok-script dependencies (scripts/sync_ok.py)\n'
            '    "pywin32!=312,>=306",\n'
            '    # <<< generated: vendored ok-script dependencies\n')
        ours['requirements.txt'] = base_requirements.replace(framework_requirement, '')
        fixture.head = save(ours, '本地前台输入与内联依赖')
        return fixture

    def test_upstream_merge_preserves_policies_dependencies_and_official_framework_version(self):
        import tomllib
        merger = load_sync_script('merge_upstream.py')
        fixture = self.make_upstream_merge_fixture()
        with patch.dict(os.environ, fixture.environment, clear=True):
            result = merger.run_merge(fixture.checkout, 'v3.8.3')
        self.assertEqual(result['resolved'], ['config.py', 'pyproject.toml', 'requirements.txt'])
        self.assertEqual(result['framework_version'], '3.0.5b9')
        config = ast.literal_eval(ast.parse((fixture.checkout / 'config.py').read_bytes()).body[0].value)
        self.assertEqual(config['windows'], {'标题': '中文偏移', 'interaction': ['nono', 'post_message_test']})
        self.assertEqual(config['gui_title'], '上游新标题')
        self.assertEqual(config['trigger_tasks'], [
            ['src.task.MouseResetTask', 'MouseResetTask'], ['src.task.KeepTask', 'KeepTask'],
        ])
        self.assertNotIn('update_only_game_exit', config)
        text = (fixture.checkout / 'pyproject.toml').read_text(encoding='utf-8')
        project = tomllib.loads(text)['project']
        self.assertIn('PyYAML==6.0.3', project['optional-dependencies']['dev'])
        self.assertIn('onnxocr-ppocrv6>=0.0.1', project['dependencies'])
        self.assertIn('pywin32!=312,>=306', project['dependencies'])
        self.assertNotIn('web-test', project['optional-dependencies'])
        self.assertFalse(any(spec.startswith('ok-script') for spec in project['dependencies']))
        self.assertIn('generated: vendored ok-script dependencies', text)
        requirements = (fixture.checkout / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('onnxocr-ppocrv6==0.0.1', requirements)
        self.assertNotRegex(requirements, r'(?m)^ok-script')
        parents = fixture.git('rev-list', '--parents', '-n', '1', 'HEAD').split()
        self.assertEqual(parents[1:], [fixture.head, fixture.upstream_v383])
        self.assertEqual(fixture.git('status', '--porcelain'), '')
        with patch.dict(os.environ, fixture.environment, clear=True):
            repeated = merger.run_merge(fixture.checkout, 'v3.8.3')
        self.assertFalse(repeated['merged'])
        self.assertEqual(repeated['framework_version'], '3.0.5b9')
        self.assertEqual(fixture.git('rev-parse', 'HEAD'), parents[0])

    def test_upstream_merge_rejects_unknown_conflicts_without_partially_resolving_files(self):
        merger = load_sync_script('merge_upstream.py')
        for variant, message in (('interaction', '未知值'), ('config_field', '非受管字段'),
                                 ('other_file', '需要人工合并')):
            with self.subTest(variant=variant):
                fixture = self.make_upstream_merge_fixture(variant)
                merge = subprocess.run(['git', 'merge', '--no-ff', '--no-commit', 'refs/upstream-tags/v3.8.3'],
                                       cwd=fixture.checkout, env=fixture.environment, capture_output=True, timeout=30)
                self.assertEqual(merge.returncode, 1)
                names = fixture.git('diff', '--name-only', '--diff-filter=U').splitlines()
                before = {name: (fixture.checkout / name).read_bytes() for name in names}
                index = fixture.git('ls-files', '--unmerged')
                with patch.dict(os.environ, fixture.environment, clear=True):
                    with self.assertRaisesRegex(RuntimeError, message):
                        merger.resolve_conflicts(fixture.checkout)
                self.assertEqual(fixture.git('ls-files', '--unmerged'), index)
                self.assertEqual({name: (fixture.checkout / name).read_bytes() for name in names}, before)
                self.assertEqual(fixture.git('rev-parse', 'HEAD'), fixture.head)

    def test_upstream_merge_rejects_new_web_dependencies_before_mutating_the_checkout(self):
        merger = load_sync_script('merge_upstream.py')
        fixture = self.make_upstream_merge_fixture('web_extra')
        with patch.dict(os.environ, fixture.environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'web-test 已包含未受管依赖'):
                merger.run_merge(fixture.checkout, 'v3.8.3')
        self.assertEqual(fixture.git('rev-parse', 'HEAD'), fixture.head)
        self.assertEqual(fixture.git('status', '--porcelain'), '')

    def test_upstream_workflow_passes_the_official_framework_version_after_merge(self):
        import yaml
        fixture = self.make_upstream_merge_fixture()
        repo_root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((repo_root / '.github/workflows/sync-upstream.yml').read_text(encoding='utf-8'))
        steps = workflow['jobs']['sync']['steps']
        merge_step = next(step for step in steps if step.get('name') == 'Merge upstream')
        vendor_step = next(step for step in steps if step.get('name') == 'Re-vendor ok-script')
        self.assertEqual(merge_step['id'], 'merge')
        self.assertEqual(vendor_step['env']['FRAMEWORK_VERSION'], '${{ steps.merge.outputs.framework_version }}')
        output = fixture.root / 'step-output.txt'
        script = fixture.root / 'merge-step.ps1'
        python_path = sys.executable.replace("'", "''")
        bootstrap = "$ErrorActionPreference = 'Stop'\nfunction python { & '" + python_path + "' @args }\n"
        script.write_text(bootstrap + merge_step['run'].replace('${{ steps.resolve.outputs.tag }}', 'v3.8.3'),
                          encoding='utf-8')
        environment = {**fixture.environment, 'GITHUB_OUTPUT': str(output)}
        result = subprocess.run(['pwsh', '-NoProfile', '-File', str(script)], cwd=fixture.checkout,
                                env=environment, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.read_text(encoding='utf-8').strip(), 'framework_version=3.0.5b9')
        arguments = fixture.root / 'vendor-arguments.json'
        stub = (
            "$ErrorActionPreference = 'Stop'\n"
            "function python {\n"
            "  ConvertTo-Json -Compress -InputObject @($args) | Set-Content -LiteralPath $env:VENDOR_ARGUMENTS\n"
            "  $global:LASTEXITCODE = 0\n"
            "}\n"
        )
        script.write_text(stub + vendor_step['run'], encoding='utf-8')
        environment.update({'FRAMEWORK_VERSION': '3.0.5b9', 'VENDOR_ARGUMENTS': str(arguments)})
        result = subprocess.run(['pwsh', '-NoProfile', '-File', str(script)], cwd=fixture.checkout,
                                env=environment, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(arguments.read_text(encoding='utf-8-sig')),
                         ['scripts/sync_ok.py', '--reapply', '--version', '3.0.5b9'])

    def set_mirror_release_record(self, fixture, content):
        # 镜像必须有独立提交历史；用源码仓库自己的 tag 充当镜像会掩盖跨仓库身份错误。
        checkout = fixture.root / 'mirror-checkout'
        if not checkout.exists():
            checkout.mkdir()
            fixture.git('init', '-b', 'master', cwd=checkout)
            fixture.git('config', 'user.name', '镜像测试', cwd=checkout)
            fixture.git('config', 'user.email', 'mirror-test@example.invalid', cwd=checkout)
        marker = checkout / 'selfhost/build-source.json'
        marker.parent.mkdir(exist_ok=True)
        if content is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(content, encoding='utf-8')
        fixture.git('add', '-A', cwd=checkout)
        fixture.git('commit', '--allow-empty', '-m', '独立部署镜像', cwd=checkout)
        fixture.git('tag', '-af', 'v3.8.1-xmod', '-m', '测试镜像标签', cwd=checkout)
        fixture.git('push', '--force', str(fixture.mirror), 'HEAD:master', 'refs/tags/v3.8.1-xmod', cwd=checkout)
        return fixture.git('rev-parse', 'HEAD', cwd=checkout)

    def test_upstream_sync_retries_missing_release_and_rejects_query_errors(self):
        release_script = load_sync_script('upstream_release.py')
        fixture = self.make_upstream_release_fixture()
        fixture.git('tag', '-a', 'v3.8.1-xmod', '-m', '已包含上游')
        fixture.git('push', 'origin', 'refs/tags/v3.8.1-xmod')
        assets = [{'name': name, 'state': 'uploaded', 'size': 100}
                  for name in ('NoNo-win32-portable.zip', 'NoNo-win32-sha256.txt')]
        complete = {'tag_name': 'v3.8.1-xmod', 'draft': False, 'prerelease': False, 'assets': assets}
        scenarios = (
            ('尚未发布', HTTPError('https://example.invalid', 404, '不存在', {}, None), True),
            ('完整发布', complete, False),
            ('仍是草稿', {**complete, 'draft': True}, True),
            ('缺少校验文件', {**complete, 'assets': assets[:1]}, True),
            ('附件为空', {**complete, 'assets': [assets[0], {**assets[1], 'size': 0}]}, True),
            ('附件上传中', {**complete, 'assets': [assets[0], {**assets[1], 'state': 'new'}]}, True),
            ('正式号误标为预发布', {**complete, 'prerelease': True}, True),
            ('服务错误', HTTPError('https://example.invalid', 503, '暂不可用', {}, None), None),
            ('连接错误', URLError('连接中断'), None),
            ('损坏 JSON', '{', None),
            ('错误响应结构', [], None),
            ('返回了另一版本', {**complete, 'tag_name': 'v3.8.0'}, None),
        )
        with patch.dict(os.environ, fixture.environment, clear=True):
            for label, payload, expected in scenarios:
                with self.subTest(label=label), patch.object(release_script, 'urlopen') as request:
                    if isinstance(payload, Exception):
                        request.side_effect = payload
                    else:
                        request.return_value = BytesIO((payload if isinstance(payload, str)
                                                        else json.dumps(payload)).encode('utf-8'))
                    if expected is None:
                        with self.assertRaises(RuntimeError):
                            release_script.check_release(fixture.checkout, 'v3.8.1', 'synthetic/repository')
                    else:
                        result = release_script.check_release(fixture.checkout, 'v3.8.1', 'synthetic/repository')
                        self.assertEqual(result['needed'], expected)
                    self.assertTrue(request.call_args.args[0].full_url.endswith('/releases/tags/v3.8.1-xmod'))

            with patch.object(release_script, 'urlopen', side_effect=AssertionError('强制模式无需查询附件')) as request:
                self.assertEqual(release_script.check_release(fixture.checkout, 'v3.8.1',
                                                              'synthetic/repository', force=True), {'needed': True})
                request.assert_not_called()
            # 发布后新增本地文档提交不等于有新的上游，计划同步仍应幂等跳过。
            fixture.commit('发布后的本地说明')
            with patch.object(release_script, 'urlopen', return_value=BytesIO(json.dumps(complete).encode())):
                self.assertEqual(release_script.check_release(fixture.checkout, 'v3.8.1', 'synthetic/repository'),
                                 {'needed': False, 'covered_tag': 'v3.8.1-xmod'})

    def test_upstream_sync_checks_real_tag_ancestry_and_identity(self):
        release_script = load_sync_script('upstream_release.py')
        fixture = self.make_upstream_release_fixture()
        with patch.dict(os.environ, fixture.environment, clear=True), patch.object(release_script, 'urlopen') as request:
            fixture.git('tag', '-a', 'v3.8.1-xmod', fixture.base, '-m', '旧同号版本')
            fixture.git('push', 'origin', 'refs/tags/v3.8.1-xmod')
            for force in (False, True):
                with self.subTest(force=force), self.assertRaisesRegex(RuntimeError, '不包含目标上游'):
                    release_script.check_release(fixture.checkout, 'v3.8.1', 'synthetic/repository', force=force)

            tree = fixture.git('rev-parse', 'HEAD^{tree}')
            later = fixture.git('commit-tree', tree, '-p', fixture.head, '-m', '尚未检出的后续发布')
            fixture.git('tag', '-af', 'v3.8.1-xmod', later, '-m', '远端较新版本')
            fixture.git('push', '--force', 'origin', 'refs/tags/v3.8.1-xmod')
            with self.assertRaisesRegex(RuntimeError, '不在当前 HEAD 历史'):
                release_script.check_release(fixture.checkout, 'v3.8.1', 'synthetic/repository')

            fixture.git('update-ref', 'refs/tags/v3.8.1-xmod', fixture.base, cwd=fixture.remote)
            with self.assertRaisesRegex(RuntimeError, '远端与本地'):
                release_script.check_release(fixture.checkout, 'v3.8.1', 'synthetic/repository')
            request.assert_not_called()

            fixture.git('tag', '-d', 'v3.8.1-xmod')
            fixture.git('push', 'origin', ':refs/tags/v3.8.1-xmod')
            fixture.git('reset', '--hard', fixture.base)
            self.assertEqual(release_script.check_release(fixture.checkout, 'v3.8.1', 'synthetic/repository'),
                             {'needed': True})
            with self.assertRaisesRegex(RuntimeError, '尚未包含目标上游'):
                release_script.plan_release(fixture.checkout, 'v3.8.1', str(fixture.mirror))
            request.assert_not_called()

    def test_upstream_release_mirror_requires_matching_build_source(self):
        release_script = load_sync_script('upstream_release.py')
        fixture = self.make_upstream_release_fixture()
        with patch.dict(os.environ, fixture.environment, clear=True):
            plan = release_script.plan_release(fixture.checkout, 'v3.8.1', str(fixture.mirror))
            self.assertEqual(plan, {'release_tag': 'v3.8.1-xmod', 'source_commit': fixture.head,
                                    'upstream_tag': 'v3.8.1', 'upstream_commit': fixture.upstream, 'reuse': False})
            # 源仓库没有标签也不能覆盖镜像留下的旧同号发布。
            self.set_mirror_release_record(fixture, None)
            with self.assertRaisesRegex(RuntimeError, '缺少源码来源记录'):
                release_script.plan_release(fixture.checkout, 'v3.8.1', str(fixture.mirror))

            fixture.git('tag', '-a', 'v3.8.1-xmod', '-m', '本次验证提交')
            fixture.git('push', 'origin', 'refs/tags/v3.8.1-xmod')
            fixture.git('update-ref', '-d', 'refs/tags/v3.8.1-xmod', cwd=fixture.mirror)
            record = release_script.record_source(fixture.checkout, 'v3.8.1-xmod', str(fixture.mirror))
            marker_path = fixture.checkout / 'selfhost/build-source.json'
            marker = marker_path.read_text(encoding='utf-8')
            self.assertEqual(record, {'schema_version': 1, 'release_tag': 'v3.8.1-xmod', 'source_commit': fixture.head})
            self.assertFalse(marker_path.read_bytes().startswith(b'\xef\xbb\xbf'))
            mirror_commit = self.set_mirror_release_record(fixture, marker)
            self.assertNotEqual(mirror_commit, fixture.head)
            self.assertEqual(release_script.plan_release(fixture.checkout, 'v3.8.1', str(fixture.mirror)),
                             {**plan, 'reuse': True})

            for label, content in (
                ('缺少记录', None), ('损坏记录', '{'),
                ('错误源码', json.dumps({**record, 'source_commit': fixture.base})),
                ('错误版本', json.dumps({**record, 'release_tag': 'v3.8.0'})),
                ('错误格式版本', json.dumps({**record, 'schema_version': True})),
            ):
                with self.subTest(label=label):
                    self.set_mirror_release_record(fixture, content)
                    old_tag = fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod', cwd=fixture.mirror)
                    with self.assertRaisesRegex(RuntimeError, '更新镜像'):
                        release_script.plan_release(fixture.checkout, 'v3.8.1', str(fixture.mirror))
                    self.assertEqual(fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod', cwd=fixture.mirror), old_tag)
                    self.assertEqual(fixture.git('tag', '--list', 'v3.8.2'), '')

            with self.assertRaisesRegex(RuntimeError, 'Git ls-remote'):
                release_script.plan_release(fixture.checkout, 'v3.8.1', str(fixture.root / 'missing.git'))
            changed = fixture.commit('验证后新增改动')
            revision = release_script.plan_release(fixture.checkout, 'v3.8.1', str(fixture.mirror))
            self.assertEqual(revision['release_tag'], 'v3.8.1-xmod.1')
            self.assertEqual(revision['source_commit'], changed)
            self.assertFalse(revision['reuse'])
            with self.assertRaisesRegex(RuntimeError, '当前检出'):
                release_script.record_source(fixture.checkout, 'v3.8.1-xmod', str(fixture.mirror))
            self.assertEqual(marker_path.read_text(encoding='utf-8'), marker)

    def test_upstream_publish_creates_annotated_tag_and_preserves_it_on_retry(self):
        import yaml

        fixture = self.make_upstream_release_fixture()
        repo_root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((repo_root / '.github/workflows/sync-upstream.yml').read_text(encoding='utf-8'))
        step = next(item for item in workflow['jobs']['sync']['steps'] if item.get('name') == 'Push and tag')
        script_path = fixture.root / 'publish.ps1'
        dispatches = fixture.root / 'dispatches.jsonl'
        # 仅替换实际远端派发；发布决策、CLI、标签与推送均经过真实临时 Git 仓库。
        bootstrap = '''
$ErrorActionPreference = 'Stop'
function python { & $env:TEST_PYTHON -B -X utf8 @args }
function gh {
    ConvertTo-Json -InputObject @($args) -Compress | Add-Content -LiteralPath $env:TEST_DISPATCH_FILE
    $global:LASTEXITCODE = [int]$env:TEST_DISPATCH_EXIT
}
'''
        script_path.write_text(bootstrap + step['run'].replace('${{ steps.resolve.outputs.tag }}', 'v3.8.1'),
                               encoding='utf-8')
        environment = {**fixture.environment, 'TEST_PYTHON': sys.executable, 'UPDATE_MIRROR': str(fixture.mirror),
                       'GITHUB_REPOSITORY': 'synthetic/repository', 'TEST_DISPATCH_FILE': str(dispatches),
                       'TEST_DISPATCH_EXIT': '23', 'RELEASE_TAG': 'v3.8.1-xmod'}

        def run(script=script_path):
            return subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(script)],
                                  cwd=fixture.checkout, env=environment, capture_output=True,
                                  text=True, encoding='utf-8', timeout=30)

        result = run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('派发 build.yml 失败', result.stderr)
        tag_object = fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod', cwd=fixture.remote)
        self.assertEqual(fixture.git('cat-file', '-t', tag_object, cwd=fixture.remote), 'tag')
        self.assertEqual(fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod^{commit}', cwd=fixture.remote), fixture.head)

        environment['TEST_DISPATCH_EXIT'] = '0'
        fixture.git('merge', '--no-ff', '--no-edit', 'refs/upstream-tags/v3.8.1')
        self.assertEqual(fixture.git('rev-parse', 'HEAD'), fixture.head)
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod', cwd=fixture.remote), tag_object)

        build = yaml.safe_load((repo_root / '.github/workflows/build.yml').read_text(encoding='utf-8'))
        marker_step = next(item for item in build['jobs']['synchronize']['steps']
                           if item.get('name') == '记录镜像对应的源码')
        marker_script = fixture.root / 'record-source.ps1'
        marker_script.write_text(bootstrap + marker_step['run'], encoding='utf-8')
        result = run(marker_script)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = (fixture.checkout / 'selfhost/build-source.json').read_text(encoding='utf-8')
        self.set_mirror_release_record(fixture, marker)
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod', cwd=fixture.remote), tag_object)
        self.assertEqual(fixture.git('rev-parse', 'refs/heads/master', cwd=fixture.remote), fixture.head)
        calls = [json.loads(line) for line in dispatches.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(calls, [['workflow', 'run', 'build.yml', '--ref', 'v3.8.1-xmod',
                                 '--repo', 'synthetic/repository']] * 3)

        self.set_mirror_release_record(fixture, json.dumps({**json.loads(marker), 'source_commit': fixture.base}))
        for script in (marker_script, script_path):
            with self.subTest(script=script.name):
                result = run(script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('源码来源与本次发布不一致', result.stderr)
        self.assertEqual((fixture.checkout / 'selfhost/build-source.json').read_text(encoding='utf-8'), marker)
        revision_head = fixture.commit('待发布的新修复')
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.git('rev-parse', 'refs/heads/master', cwd=fixture.remote), revision_head)
        self.assertEqual(fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod', cwd=fixture.remote), tag_object)
        self.assertEqual(fixture.git('rev-parse', 'refs/tags/v3.8.1-xmod.1^{commit}', cwd=fixture.remote),
                         revision_head)
        self.assertEqual(len(dispatches.read_text(encoding='utf-8').splitlines()), 4)
        self.assertEqual(fixture.git('tag', '--list', 'v3.8.2'), '')

    def test_main_window_still_consumes_custom_tabs(self):
        """上游换掉注册机制时会静默失效——这是最该被拦住的一种漂移。"""
        from ok.ui.qt.MainWindow import MainWindow
        self.assertIn('custom_tabs', inspect.getsource(MainWindow),
                      '上游改了自定义页面的注册机制,需要同步更新 selfhost/overrides.py')

    def test_branding_matches_pyappify_name_and_disables_upstream_hooks(self):
        """改名是自维护分支的核心差异,两条都是"上游一动就白改"的点。"""
        from selfhost.overrides import APP_NAME, apply
        config = {
            'gui_title': 'OK-WW',
            'links': {'default': {'github': 'https://github.com/ok-oldking/ok-wuthering-waves'}},
            # 上游重新塞回这个键,首次显示窗口时会把启动器换成上游版本。
            'update_pyappify': {'to_version': '1.2.3', 'zip_url': 'https://example.invalid/ok-ww-win32.zip'},
        }
        apply(config)
        self.assertEqual(config['gui_title'], APP_NAME)
        self.assertEqual(Path(config['log_file']), self.user_root / 'logs' / f'{APP_NAME}.log')
        self.assertEqual(Path(config['error_log_file']), self.user_root / 'logs' / f'{APP_NAME}_error.log')
        self.assertEqual(config['links'], {})
        self.assertNotIn('update_pyappify', config)

        # pyappify.yml 的 name 决定 exe 名与 data/apps/<name>,必须与 APP_NAME 一致,
        # 否则用户会看到"程序叫 NoNo,目录却叫别的"。
        pyappify = (Path(__file__).resolve().parents[1] / 'pyappify.yml').read_text(encoding='utf-8')
        declared = re.search(r'^name:\s*"?([^"\n]+)"?', pyappify, re.MULTILINE)
        self.assertIsNotNone(declared, 'pyappify.yml 里找不到 name 字段')
        self.assertEqual(declared.group(1).strip('"'), APP_NAME)

    def test_framework_branding_patches_are_applied(self):
        """ok/ 每次发版都会被整树替换,补丁只可能靠 sync_ok.py 重新施加。

        补丁丢了的后果是"品牌标识悄悄回来了",没有任何运行时错误,所以只能在这里拦。
        """
        from selfhost import framework_patches
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(framework_patches.missing(repo_root / 'ok'), [],
                         'ok/ 里的品牌补丁没生效,跑一次 python scripts/sync_ok.py')

    def test_default_input_policy_and_background_mouse_reset_registration(self):
        """前台默认与后台测试均可选，鼠标保护注册不再被同步策略删除。"""
        from config import config
        from ok.device.DeviceManager import DeviceManager
        from ok.ui.qt.shell.connection import interaction_options
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(config['windows']['interaction'], ['nono', 'post_message_test'])
        self.assertFalse(config['windows']['start_exe'])
        self.assertNotIn('launch_arguments', config['windows'])
        manager = DeviceManager.__new__(DeviceManager)
        manager.windows_capture_config = config['windows']
        options, index, names = interaction_options(manager, {'device': 'windows'})
        self.assertEqual(names, manager.available_interaction_methods({'device': 'windows'}))
        self.assertEqual(names, ['nono', 'post_message_test'])
        self.assertEqual(index, 0)
        self.assertEqual(options, [('nono（前台系统键鼠）',
                                   '使用真实键盘和鼠标；失焦释放并等待，回到游戏后自动继续', None),
                                   ('后台测试（启动时单次激活）',
                                    '每次开始或恢复时仅激活一次；运行中不补发，最小化时等待恢复', None)])
        self.assertIn(('src.task.MouseResetTask', 'MouseResetTask'),
                      [tuple(task) for task in config['trigger_tasks']])
        self.assertTrue((repo_root / 'src' / 'task' / 'MouseResetTask.py').exists())

    def test_upstream_input_overwrite_preserves_foreground_policy_and_other_settings(self):
        """上游覆盖保留可选模式和捕获参数，但绝不恢复自动启动入口。"""
        from selfhost.overrides import apply
        kept_task = ['src.task.AutoPickTask', 'AutoPickTask']
        for old_name in ('PostMessage', 'Genshin', 'ForegroundPostMessage', 'PyDirect', 'Pynput', 'nono',
                         'post_message_test', ['nono', 'post_message_test']):
            with self.subTest(old_name=old_name):
                config = {
                    'windows': {'interaction': old_name, 'exe': 'fixture.exe', 'capture_method': ['WGC'],
                                'start_exe': True, 'start_method': 'shell', 'launch_arguments': '-old'},
                    'trigger_tasks': [kept_task, ['src.task.MouseResetTask', 'MouseResetTask']],
                }
                apply(config)
                apply(config)
                self.assertEqual(config['windows'], {
                    'interaction': ['nono', 'post_message_test'], 'exe': 'fixture.exe', 'capture_method': ['WGC'],
                    'start_exe': False,
                })
                self.assertEqual(config['trigger_tasks'], [
                    kept_task, ['src.task.MouseResetTask', 'MouseResetTask'],
                ])

    def test_connection_inactive_notification_button_is_available_while_running(self):
        from PySide6.QtWidgets import QWidget
        from ok import og
        from ok.core.start_request import StartRequest
        from ok.ui.qt.shell.connection import ConnectionPopover
        device = {'device': 'windows', 'connected': True}
        manager = SimpleNamespace(
            get_preferred_device=lambda: device,
            hwnd_window=SimpleNamespace(foreground_target=SimpleNamespace(hwnd=10)),
            config={'interaction': 'nono'}, available_capture_methods=lambda device: ['WGC'],
            get_preferred_capture=lambda: 'WGC',
            available_interaction_methods=lambda device: ['nono', 'post_message_test'])
        request = StartRequest('运行中的原请求')
        task = SimpleNamespace(running=True, request=request)
        executor = SimpleNamespace(paused=False, current_task=task,
                                   pause=Mock(), stop=Mock(), release_inputs=Mock())
        parent = QWidget()
        self.addCleanup(parent.close)
        with patch.object(og, 'device_manager', manager), patch.object(og, 'executor', executor), \
                patch('ok.device.interaction_methods.post_message_test.Win32MessageBackend') as backend:
            popover = ConnectionPopover(parent)
            for mode in ('nono', 'post_message_test'):
                manager.config['interaction'] = mode
                popover._refresh_device_state()
                self.assertTrue(popover.inactive_notification_button.isEnabled())
                popover.inactive_notification_button.click()
            self.assertEqual(backend.return_value.send_inactive_notification.call_args_list,
                             [unittest.mock.call(10)] * 2)
        self.assertIs(executor.current_task, task)
        self.assertFalse(executor.paused)
        self.assertFalse(request.cancelled)
        for action in (executor.pause, executor.stop, executor.release_inputs):
            action.assert_not_called()

    def test_connection_inactive_notification_button_rejects_unavailable_targets_and_reports_failure(self):
        from PySide6.QtWidgets import QWidget
        from ok import og
        from ok.device.interaction_methods.foreground_input import ForegroundInputError
        from ok.ui.qt.shell.connection import ConnectionPopover
        device = {'device': 'windows', 'connected': True}
        manager = SimpleNamespace(
            get_preferred_device=Mock(return_value=device),
            hwnd_window=SimpleNamespace(foreground_target=SimpleNamespace(hwnd=10)),
            config={'interaction': 'nono'}, available_capture_methods=lambda device: ['WGC'],
            get_preferred_capture=lambda: 'WGC',
            available_interaction_methods=lambda device: ['nono', 'post_message_test'])
        executor = SimpleNamespace(paused=False, current_task=object())
        parent = QWidget()
        self.addCleanup(parent.close)
        with patch.object(og, 'device_manager', manager), patch.object(og, 'executor', executor), \
                patch('ok.device.interaction_methods.post_message_test.Win32MessageBackend') as backend, \
                patch('ok.ui.qt.util.Alert.alert_error') as alert:
            popover = ConnectionPopover(parent)
            for selected, hwnd in ((None, 10), ({'device': 'windows', 'connected': False}, 10),
                                   ({'device': 'browser', 'connected': True}, 10), (device, 0)):
                manager.get_preferred_device.return_value = selected
                manager.hwnd_window.foreground_target.hwnd = hwnd
                popover._refresh_device_state()
                self.assertFalse(popover.inactive_notification_button.isEnabled())
                popover.inactive_notification_button.click()
            backend.assert_not_called()
            manager.get_preferred_device.return_value = device
            manager.hwnd_window.foreground_target.hwnd = 10
            popover._refresh_device_state()
            device['connected'] = False
            popover.inactive_notification_button.click()
            backend.assert_not_called()
            alert.assert_called_once()
            device['connected'] = True
            popover._refresh_device_state()
            alert.reset_mock()
            backend.return_value.send_inactive_notification.side_effect = ForegroundInputError('受控投递失败')
            popover.inactive_notification_button.click()
            backend.return_value.send_inactive_notification.assert_called_once_with(10)
            alert.assert_called_once()

    def test_foreground_sources_remain_inline_and_are_deployed(self):
        """部署白名单或导入来源漂移会让已通过的前台实现在用户包中消失。"""
        import ok
        from ok.device import interaction_methods
        from ok.device.interaction_methods import foreground_input, nono, post_message_test
        from selfhost.portable import process_environment, hidden_process_options
        repo_root = Path(__file__).resolve().parents[1]
        self.assertTrue(Path(ok.__file__).resolve().is_relative_to(repo_root / 'ok'))
        self.assertTrue(Path(foreground_input.__file__).resolve().is_relative_to(repo_root / 'ok'))
        self.assertTrue(Path(nono.__file__).resolve().is_relative_to(repo_root / 'ok'))
        self.assertTrue(Path(post_message_test.__file__).resolve().is_relative_to(repo_root / 'ok'))
        self.assertIs(ok.NonoInteraction, nono.NonoInteraction)
        self.assertIs(ok.PynputInteraction, nono.NonoInteraction)
        self.assertIs(ok.PostMessageTestInteraction, post_message_test.PostMessageTestInteraction)
        deploy_entries = {
            line.strip() for line in (repo_root / 'deploy.txt').read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')
        }
        self.assertTrue({'ok', 'selfhost'} <= deploy_entries)
        for old_module, old_class in (
            ('post_message', 'PostMessageInteraction'), ('genshin', 'GenshinInteraction'),
            ('foreground_post_message', 'ForegroundPostMessageInteraction'), ('pydirect', 'PyDirectInteraction'),
        ):
            self.assertFalse((repo_root / 'ok' / 'device' / 'interaction_methods' / f'{old_module}.py').exists())
            self.assertFalse(hasattr(interaction_methods, old_class))

        # 部署树缺失的新模块必须由已登记差异严格重放，不能借用源码目录补齐导入。
        sync = load_sync_script()
        pin = sync.load_pin()
        patches = sync.load_logic_patches(pin, pin['version'])
        deployment = Path(self.temp_dir.name) / 'deployment'
        ignored = shutil.ignore_patterns('__pycache__', '*.pyc', 'NoNo.exe')
        shutil.copytree(repo_root / 'ok', deployment / 'ok', ignore=ignored)
        shutil.copytree(repo_root / 'selfhost', deployment / 'selfhost', ignore=ignored)
        (deployment / 'ok/device/interaction_methods/nono.py').unlink()
        (deployment / 'ok/device/interaction_methods/post_message_test.py').unlink()
        applied, _already = patches.apply_to(deployment / 'ok')
        self.assertEqual(applied, ['device/interaction_methods/nono.py', 'device/interaction_methods/post_message_test.py'])
        self.assertEqual(sync.hash_tree(deployment / 'ok'), sync.hash_tree(repo_root / 'ok'))
        self.assertEqual(sync.do_check(deployment), 0)
        code = (
            'import importlib; from pathlib import Path; '
            'from ok import NonoInteraction, PynputInteraction, PostMessageTestInteraction; '
            'from ok.device.DeviceManager import normalize_pc_interaction; '
            'assert NonoInteraction is PynputInteraction; '
            'assert normalize_pc_interaction("Pynput") == "nono"; '
            'assert normalize_pc_interaction(NonoInteraction) == "nono"; '
            'assert normalize_pc_interaction(PostMessageTestInteraction) == "post_message_test"; '
            'module = importlib.import_module(NonoInteraction.__module__); '
            'assert Path(module.__file__).resolve().is_relative_to((Path.cwd() / "ok").resolve()); '
            'assert all(importlib.import_module(name).PynputInteraction is NonoInteraction '
            'for name in ("ok.device.interaction", "ok.device.intercation", '
            '"ok.device.interaction_methods", "ok.device.interaction_methods.pynput"))'
        )
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', code],
                                cwd=deployment, env=process_environment(), capture_output=True, text=True,
                                timeout=60, **hidden_process_options())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_script_keeps_pyproject_parseable(self):
        """上游的 dependencies 数组结尾不带逗号,把生成块插进去会让 pyproject 变成非法 TOML。

        这个坑踩过一次:构建跑到"装依赖"才炸,而测试全绿——因为它们只需要 requirements.txt。
        所以这里按上游的真实形状造一份文件,要求改写之后仍然可解析,且该删的都删了。
        """
        import tomllib
        sync_ok = load_sync_script()

        upstream_shape = (
            '[project]\n'
            'dependencies = [\n'
            '    "mcp==2.3.0",\n'
            '    "ok-script[ocr,qt]==9.9.9",\n'
            '    "pyappify>=1.0.13"\n'   # 上游最后一项没有尾逗号
            ']\n'
            '\n'
            '[project.optional-dependencies]\n'
            'dev = [\n'
            '    "pytest",\n'
            ']\n'
            'web-test = [\n'
            '    "ok-script[web]==9.9.9",\n'
            ']\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            pyproject = Path(folder) / 'pyproject.toml'
            requirements = Path(folder) / 'requirements.txt'
            pyproject.write_text(upstream_shape, encoding='utf-8')
            requirements.write_text('mcp==2.3.0\nok-script[ocr,qt]==9.9.9\n    # via ok-ww\n',
                                    encoding='utf-8')
            with patch.object(sync_ok, 'PYPROJECT_FILE', pyproject), \
                    patch.object(sync_ok, 'REQUIREMENTS_FILE', requirements):
                # 版本必须在 reapply 删掉那行之前读出来,否则永远停在旧 pin 上。
                self.assertEqual(sync_ok.upstream_pinned_version(), '9.9.9')
                self.assertTrue(sync_ok.reapply_upstream_overwrites())
                rewritten, _entries = sync_ok.rewrite_pyproject(['pywin32!=312,>=306'],
                                                                'ok-script', '9.9.9')
                text = pyproject.read_text(encoding='utf-8')
                requirements_text = requirements.read_text(encoding='utf-8')

            self.assertTrue(rewritten)
            tomllib.loads(text)  # 解析失败就是这里要拦的那种回归
            self.assertNotIn('"ok-script', text, '上游的 pin 行必须被删掉')
            self.assertNotIn('web-test', text, '空掉的 web-test extra 不能留在 pyproject 里')
            self.assertIn('"pywin32!=312,>=306"', text, '生成块应该写进依赖数组')
            self.assertNotIn('ok-script', requirements_text)

    def test_registered_tabs_instantiate_against_current_framework(self):
        """页面能实例化,才说明框架契约(name/icon/add_after_default_tabs)没变。"""
        from ok.util.clazz import init_class_by_name
        from selfhost.overrides import apply
        for module_name, class_name in apply({}):
            tab = init_class_by_name(module_name, class_name)
            try:
                self.assertTrue(tab.name, f'{class_name} 没有可用的 name')
                self.assertIsNotNone(tab.icon, f'{class_name} 没有可用的 icon')
                self.assertIsInstance(tab.add_after_default_tabs, bool)
            finally:
                tab.close()
                tab.deleteLater()

    def test_config_preserves_unknown_fields_and_explicit_values_without_rewriting(self):
        from ok.util import config as config_module
        path = Path(self.config_class.config_folder) / '扩展配置.json'
        path.parent.mkdir(parents=True)
        original = b'{ "enabled": false, "hotkey": "F10", "unknown": {"false": false, "zero": 0, "null": null} }'
        path.write_bytes(original)
        with patch.object(config_module, 'write_json_file', wraps=config_module.write_json_file) as writer:
            loaded = self.config_class('扩展配置', {'enabled': True, 'hotkey': 'F9'})
            writer.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
            self.assertIs(loaded['enabled'], False)
            self.assertEqual(loaded['hotkey'], 'F10')
            loaded['enabled'] = True
            loaded['nullable_extension'] = None
        saved = json.loads(path.read_bytes())
        self.assertEqual(saved['unknown'], {'false': False, 'zero': 0, 'null': None})
        self.assertIn('nullable_extension', saved)
        self.assertIsNone(saved['nullable_extension'])

    def test_config_only_fills_missing_defaults_once(self):
        from ok.util import config as config_module
        path = Path(self.config_class.config_folder) / '缺省配置.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"enabled": false, "extension": {"active": true}}', encoding='utf-8')
        with patch.object(config_module, 'write_json_file', wraps=config_module.write_json_file) as writer:
            loaded = self.config_class('缺省配置', {'enabled': True, 'new_option': False})
            writer.assert_called_once()
        self.assertIs(loaded['enabled'], False)
        self.assertIs(loaded['new_option'], False)
        self.assertEqual(json.loads(path.read_bytes())['extension'], {'active': True})

    def test_corrupt_or_non_object_config_fails_without_overwriting_original(self):
        path = Path(self.config_class.config_folder) / '损坏配置.json'
        path.parent.mkdir(parents=True)
        for original in (b'{"private":', b'\xff', b'[]', b'null', b'false', b'1',
                         b'{"private": NaN}', b'{"private": Infinity}', b'{"private": -Infinity}'):
            with self.subTest(original_kind=original[:1]):
                path.write_bytes(original)
                with self.assertRaises(ValueError):
                    self.config_class('损坏配置', {'enabled': False})
                if original.startswith(b'{"private": ') and original.endswith(b'}'):
                    from ok.util.file import read_json_file
                    with self.assertRaisesRegex(ValueError, 'CONFIG_JSON_INVALID'):
                        read_json_file(path, strict=True)
                    self.assertIsInstance(read_json_file(path), dict)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_nonfinite_nested_config_writes_keep_valid_original_and_no_temporary_files(self):
        from ok.util.file import write_json_file
        path = Path(self.config_class.config_folder) / '非有限配置.json'
        path.parent.mkdir(parents=True)
        original = b'{"enabled": false, "extension": {"zero": 0}}'
        for value in (float('nan'), float('inf'), -float('inf')):
            path.write_bytes(original)
            with self.assertRaisesRegex(ValueError, 'CONFIG_JSON_INVALID'):
                write_json_file(path, {'extension': {'nested': [value]}})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_real_ui_config_preserves_unknown_false_and_rejects_corrupt_original(self):
        from ok.ui.qt.common.config import UiConfig
        from selfhost.portable import process_environment, hidden_process_options
        path = self.user_root / 'configs' / '界面配置.json'
        path.parent.mkdir(parents=True)
        original = b'{ "mica": false, "theme": "dark", "extension": {"flag": false, "count": 0, "null": null} }'
        path.write_bytes(original)
        config = UiConfig(str(path))
        self.assertEqual(config.backdrop, 'none')
        self.assertEqual(path.read_bytes(), original)
        config.set_theme_mode('light')
        stored = json.loads(path.read_bytes())
        self.assertEqual(stored['extension'], {'flag': False, 'count': 0, 'null': None})
        self.assertIs(stored['mica'], False)
        for malformed in (b'{partial', b'[]', b'{"theme":NaN}', b'{"theme":Infinity}', b'{"theme":-Infinity}'):
            path.write_bytes(malformed)
            with self.assertRaises(ValueError):
                UiConfig(str(path))
            self.assertEqual(path.read_bytes(), malformed)
        # 在独立进程真实导入全局 cfg，避免已导入模块缓存影响环境切换断言。
        environment = {**process_environment(), 'NONO_USER_DATA_ROOT': str(self.user_root)}
        code = ('from pathlib import Path; from ok.ui.qt.common.config import cfg; '
                'import os; assert Path(cfg.path).resolve() == '
                '(Path(os.environ["NONO_USER_DATA_ROOT"])/"configs"/"ui_config.json").resolve()')
        result = subprocess.run([sys.executable, '-B', '-c', code],
                                cwd=Path(__file__).resolve().parents[1], env=environment,
                                capture_output=True, text=True, timeout=60, **hidden_process_options())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_atomic_config_writes_keep_original_on_serialization_flush_or_replace_failure(self):
        from ok.util import file as file_module
        path = Path(self.config_class.config_folder) / '原子配置.json'
        path.parent.mkdir(parents=True)
        original = b'{"enabled": false, "extension": {"zero": 0}}'
        for failure in ('serialize', 'fsync', 'replace'):
            with self.subTest(failure=failure):
                path.write_bytes(original)
                loaded = self.config_class('原子配置', {'enabled': False})
                with ExitStack() as stack:
                    if failure == 'serialize':
                        value = object()
                        exception = TypeError
                    else:
                        value = True
                        exception = OSError
                        stack.enter_context(patch.object(file_module.os, failure, side_effect=OSError('合成写入失败')))
                    with self.assertRaises(exception):
                        loaded['enabled'] = value
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_config_logs_do_not_expose_values_keys_or_write_error_details(self):
        from ok.util import config as config_module
        marker = '合成私密字段'
        with patch.object(config_module, 'logger') as logger:
            loaded = self.config_class('日志配置', {'value': marker})
            with self.assertRaises(KeyError):
                loaded[marker]
            with patch.object(config_module, 'write_json_file', side_effect=OSError(marker)):
                with self.assertRaises(OSError):
                    loaded['value'] = '变更值'
            self.assertNotIn(marker, str(logger.mock_calls))
            self.assertNotIn('变更值', str(logger.mock_calls))

    def test_shortcut_defaults_and_explicit_user_choices_survive_loading(self):
        from ok.util import GlobalConfig as global_config_module
        from ok.util import shortcut
        menu = global_config_module.CREATE_START_MENU_SHORTCUT
        desktop = global_config_module.CREATE_DESKTOP_SHORTCUT
        path = Path(self.config_class.config_folder) / 'Basic Options.json'
        path.parent.mkdir(parents=True)
        choices = ({}, {menu: True, desktop: False}, {menu: False, desktop: True})
        for choice in choices:
            with self.subTest(choice=choice), patch.object(global_config_module, 'shortcut_supported', return_value=True):
                path.write_text(json.dumps({**choice, 'extension': {'enabled': False}}), encoding='utf-8')
                global_config = global_config_module.GlobalConfig([])
                basic = global_config_module.register_basic_options(global_config)
                self.assertIs(basic[menu], choice.get(menu, False))
                self.assertIs(basic[desktop], choice.get(desktop, False))
                self.assertEqual(basic['extension'], {'enabled': False})
                with patch.object(shortcut.threading, 'Thread') as worker:
                    shortcut.create_app_shortcut(basic)
                    if any(choice.values()):
                        self.assertEqual(worker.call_args.kwargs['args'], (basic[menu], basic[desktop]))
                        worker.return_value.start.assert_called_once()
                    else:
                        worker.assert_not_called()

    def test_user_paths_reach_tasks_plugins_templates_features_and_screenshots(self):
        from ok import og
        from ok.core import screenshot as screenshot_module
        from ok.core.mcp_runner import McpTaskRunner
        from ok.core.script_packager import get_ok_import_folder, get_ok_tasks_folder
        from ok.core.task_manager import TaskManager
        from ok.core.template_store import CocoTemplateStore
        from ok.feature.FeatureSet import FeatureSet
        from ok.ui.qt.tasks.TemplateTab import get_coco_path
        from ok.util.clazz import generate_label_enum
        from selfhost.overrides import apply
        config = {}
        apply(config)
        self.assertEqual(Path(config['config_folder']), self.user_root / 'configs')
        self.assertEqual(Path(get_ok_tasks_folder()), self.user_root / 'ok_tasks')
        self.assertEqual(Path(get_ok_import_folder()), self.user_root / 'ok_import')
        with patch.object(og, 'config', {'custom_tasks': True}), \
                patch.object(TaskManager, 'load_user_tasks'), patch.object(TaskManager, 'load_imported_tasks'):
            manager = TaskManager(SimpleNamespace(), SimpleNamespace(debug=False))
        self.assertEqual(Path(manager.task_folder), self.user_root / 'ok_tasks')
        plugin = self.user_root / 'ok_import' / 'synthetic_plugin'
        plugin.mkdir(parents=True)
        sources = list(FeatureSet._feature_sources(SimpleNamespace(coco_json='assets/coco.json',
                                                                 template_folder='assets/templates')))
        self.assertEqual(sources[0], ('assets/coco.json', 'assets/templates', None))
        self.assertEqual(Path(sources[1][0]), self.user_root / 'ok_tasks/assets/coco_annotations.json')
        self.assertEqual(Path(sources[2][1]), plugin / 'assets/templates')
        self.assertEqual(CocoTemplateStore().folder, (self.user_root / 'ok_templates').resolve())
        self.assertEqual(Path(get_coco_path()), self.user_root / 'ok_templates/coco_annotations.json')
        generate_label_enum('ok_tasks.SyntheticLabels', ['synthetic_feature'])
        self.assertTrue((self.user_root / 'ok_tasks/SyntheticLabels.py').is_file())
        external = Path(self.temp_dir.name) / 'external'
        self.assertEqual(CocoTemplateStore(external).folder, external.resolve())
        signals = SimpleNamespace(draw_box=Mock(), clear_box=Mock(), screenshot=Mock())
        for configured in (None, str(external)):
            with patch.object(og, 'ok', SimpleNamespace(config={'screenshots_folder': configured})), \
                    patch.object(screenshot_module, 'communicate', signals), \
                    patch.object(screenshot_module.threading, 'Thread'), \
                    patch.object(screenshot_module, 'find_first_existing_file', return_value=''), \
                    patch.object(screenshot_module.ImageFont, 'load_default'):
                screenshot = screenshot_module.Screenshot(Mock(), False)
            self.assertEqual(Path(screenshot.screenshot_folder), external if configured else self.user_root / 'screenshots')
        executor = Mock()
        runner = McpTaskRunner(executor, SimpleNamespace(), SimpleNamespace())
        self.assertEqual(runner.artifact_directory, self.user_root / 'logs/mcp')
        executor.enable_frame_snapshots.assert_called_once()

    def test_user_path_resolver_keeps_source_mode_and_explicit_external_paths(self):
        from ok.core.script_packager import get_ok_tasks_folder
        from ok.util.file import get_user_data_path
        from selfhost.overrides import apply
        external = Path(self.temp_dir.name) / 'external'
        self.assertEqual(Path(get_user_data_path(external)), external)
        with chdir(self.temp_dir.name):
            self.assertEqual(Path(get_user_data_path('..', 'outside')), Path(self.temp_dir.name).parent / 'outside')
            with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': ''}):
                self.assertEqual(Path(get_ok_tasks_folder()), Path(self.temp_dir.name) / 'ok_tasks')
                config = {'config_folder': str(external)}
                apply(config)
                self.assertEqual(config['config_folder'], str(external))
                self.assertEqual(config['log_file'], 'logs/NoNo.log')
        with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': 'relative-root'}):
            with self.assertRaises(ValueError):
                get_user_data_path('configs')

    def test_user_tasks_import_only_the_stable_package_and_bare_helpers(self):
        from ok import og
        from ok.core.task_manager import TaskManager
        from ok.util.clazz import generate_label_enum

        repo = Path(__file__).resolve().parents[1]
        search_paths = [value for value in sys.path if Path(value or os.getcwd()).resolve() != repo]
        for regular_package in (False, True):
            for legacy_state in ('missing', 'present', 'cached'):
                with self.subTest(regular_package=regular_package, legacy_state=legacy_state):
                    case = self.user_root / f'{regular_package}-{legacy_state}'
                    stable = case / 'user'
                    tasks = stable / 'ok_tasks'
                    working = case / 'working'
                    legacy = case / 'previous' / 'ok_tasks'
                    working.mkdir(parents=True)
                    (stable / 'ApplicationImportProbe.py').parent.mkdir(parents=True)
                    (stable / 'ApplicationImportProbe.py').write_text("VALUE = '用户根遮蔽'\n", encoding='utf-8')
                    (working / 'ApplicationImportProbe.py').write_text("VALUE = '应用模块'\n", encoding='utf-8')
                    for root, marker in ((tasks, '稳定'), (legacy, '旧版')):
                        if root == legacy and legacy_state == 'missing':
                            continue
                        (root / 'helpers').mkdir(parents=True)
                        if regular_package:
                            (root / 'helpers/__init__.py').write_text('from .nested import VALUE\n', encoding='utf-8')
                        (root / 'helpers/nested.py').write_text(f'VALUE = {marker!r}\n', encoding='utf-8')
                        (root / 'BareTaskHelper.py').write_text(f'VALUE = {marker!r}\n', encoding='utf-8')
                        if regular_package:
                            (root / '__init__.py').write_text('from .helpers import VALUE\n', encoding='utf-8')
                    if legacy_state != 'missing':
                        (legacy / 'SyntheticLabels.py').write_text(
                            "from enum import Enum\nclass SyntheticLabels(Enum):\n    stable_feature = '旧标签'\n",
                            encoding='utf-8')
                        (legacy / 'RetiredTaskHelper.py').write_text("VALUE = '旧版残留'\n", encoding='utf-8')
                    (tasks / 'UserDataTaskProbe.py').write_text(
                        'from ok.task.task import BaseTask\n'
                        'from ok_tasks.SyntheticLabels import SyntheticLabels\n'
                        'from ok_tasks.helpers.nested import VALUE as nested_value\n'
                        'from BareTaskHelper import VALUE as bare_value\n'
                        'from ApplicationImportProbe import VALUE as application_value\n'
                        'import ok_tasks\n'
                        'class UserDataTaskProbe(BaseTask):\n'
                        '    observed = (SyntheticLabels.stable_feature.value, nested_value, bare_value,\n'
                        "                getattr(ok_tasks, 'VALUE', 'namespace'), application_value)\n",
                        encoding='utf-8')
                    paths = [str(working), *search_paths]
                    if legacy_state != 'missing':
                        paths[1:1] = [str(legacy.parent), str(legacy)]
                    # 只替代实例化与插件注册，真实加载器负责执行任务源码和 Python 导入。
                    with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(stable)}), \
                            patch.object(sys, 'path', paths), patch.dict(sys.modules), chdir(working), \
                            patch.object(og, 'config', {'custom_tasks': True}), \
                            patch.object(TaskManager, 'load_imported_tasks'), \
                            patch.object(TaskManager, '_instantiate_task', return_value=None) as instantiate:
                        names = {'SyntheticLabels', 'BareTaskHelper', 'UserDataTaskProbe',
                                 'RetiredTaskHelper', 'ApplicationImportProbe'}
                        for name in tuple(sys.modules):
                            if name in names or name == 'ok_tasks' or name.startswith('ok_tasks.'):
                                sys.modules.pop(name)
                        importlib.invalidate_caches()
                        if legacy_state == 'cached':
                            importlib.import_module('ok_tasks.SyntheticLabels')
                            importlib.import_module('ok_tasks.helpers.nested')
                            importlib.import_module('BareTaskHelper')
                        generate_label_enum('ok_tasks.SyntheticLabels', ['stable_feature'])
                        manager = TaskManager(SimpleNamespace(), SimpleNamespace(debug=False))
                        self.assertEqual(manager.task_errors, {})
                        instantiate.assert_called_once()
                        self.assertEqual(instantiate.call_args.args[0].observed,
                                         ('stable_feature', '稳定', '稳定',
                                          '稳定' if regular_package else 'namespace', '应用模块'))
                        self.assertNotIn(str(stable), sys.path)
                        for name in ('ok_tasks.RetiredTaskHelper', 'RetiredTaskHelper'):
                            with self.assertRaises(ModuleNotFoundError):
                                importlib.import_module(name)

    def test_template_store_rejects_bad_annotations_before_saving_an_image(self):
        import cv2
        import numpy as np
        from ok.core.template_store import CocoTemplateStore

        store = CocoTemplateStore()
        for original in (b'{', b'[]', b'{"images": NaN}', b'{"images": {"lost": 1}}',
                         b'{"annotations": [null]}'):
            with self.subTest(original=original):
                store.coco_path.write_bytes(original)
                with self.assertRaises(ValueError):
                    store.load()
                with patch.object(cv2, 'imwrite') as write_image:
                    with self.assertRaises(ValueError):
                        store.save_frame(np.zeros((8, 8, 3), dtype=np.uint8))
                    write_image.assert_not_called()
                self.assertEqual(store.coco_path.read_bytes(), original)
                self.assertEqual(list(store.folder.iterdir()), [store.coco_path])

    def test_template_store_keeps_annotations_on_io_and_serialization_failure(self):
        from ok.core.template_store import CocoTemplateStore

        store = CocoTemplateStore()
        original = b'{"images": [], "annotations": [], "categories": [], "info": {"enabled": false}}'
        store.coco_path.write_bytes(original)
        with patch('builtins.open', side_effect=PermissionError('合成读取拒绝')), \
                patch('io.open', side_effect=PermissionError('合成读取拒绝')):
            with self.assertRaises(PermissionError):
                store.load()
        with self.assertRaises(ValueError):
            store.save({'images': [], 'annotations': [], 'categories': [], 'info': {'number': float('nan')}})
        self.assertEqual(store.coco_path.read_bytes(), original)
        with patch('os.replace', side_effect=PermissionError('合成原子提交失败')):
            with self.assertRaises(PermissionError):
                store.save(store.load())
        self.assertEqual(store.coco_path.read_bytes(), original)
        self.assertEqual(list(store.folder.iterdir()), [store.coco_path])

    def test_template_store_preserves_existing_metadata_when_saving_a_frame(self):
        import numpy as np
        from ok.core.template_store import CocoTemplateStore

        store = CocoTemplateStore()
        metadata = {'enabled': False, 'count': 0, 'extra': None}
        original = {'images': [], 'annotations': [], 'categories': [], 'info': metadata}
        store.coco_path.write_text(json.dumps(original), encoding='utf-8')
        saved = store.save_frame(np.zeros((8, 8, 3), dtype=np.uint8))
        data = store.load()
        self.assertTrue(saved.is_file())
        self.assertEqual(data['images'][0]['file_name'], saved.name)
        self.assertEqual((data['images'][0]['width'], data['images'][0]['height']), (8, 8))
        self.assertEqual(data['info'], metadata)

    def test_task_manifest_rejects_bad_data_and_failed_writes_without_overwriting(self):
        from ok.core.script_packager import export_script, get_ok_tasks_folder, load_manifest, save_manifest

        tasks = Path(get_ok_tasks_folder())
        tasks.mkdir(parents=True)
        path = tasks / 'manifest.json'
        output = self.user_root / 'exports'
        for original in (b'{', b'[]', b'{"extension": NaN}'):
            with self.subTest(original=original):
                path.write_bytes(original)
                with self.assertRaises(ValueError):
                    load_manifest()
                with self.assertRaises(ValueError):
                    export_script([], 'probe', '合成任务', '1.0', output_folder=output)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(output.exists())
        original = b'{"extension": {"enabled": false, "count": 0, "value": null}}'
        path.write_bytes(original)
        with patch('builtins.open', side_effect=PermissionError('合成读取拒绝')):
            with self.assertRaises(PermissionError):
                load_manifest()
        with self.assertRaises(TypeError):
            save_manifest({'invalid': object()})
        self.assertEqual(path.read_bytes(), original)
        with patch('os.replace', side_effect=PermissionError('合成原子提交失败')):
            with self.assertRaises(PermissionError):
                save_manifest({'version': '2.0'})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(tasks.iterdir()), [path])

    def test_task_manifest_keeps_legacy_bom_and_unknown_fields_through_export(self):
        from ok.core.script_packager import export_script, get_ok_tasks_folder, load_manifest

        tasks = Path(get_ok_tasks_folder())
        tasks.mkdir(parents=True)
        extension = {'enabled': False, 'count': 0, 'value': None}
        (tasks / 'manifest.json').write_text(json.dumps({'extension': extension}), encoding='utf-8-sig')
        self.assertEqual(load_manifest()['extension'], extension)
        success, message, path = export_script([], 'probe', '合成任务', '1.0', output_folder=self.user_root / 'exports')
        self.assertTrue(success, message)
        with zipfile.ZipFile(path) as package:
            self.assertEqual(json.loads(package.read('manifest.json'))['extension'], extension)
        self.assertEqual(load_manifest()['extension'], extension)

    def test_diagnostics_export_uses_stable_folders_and_relative_archive_names(self):
        from ok import og
        from ok.ui.qt.shell import diagnostics
        downloads = Path(self.temp_dir.name) / 'downloads'
        for folder in ('logs', 'screenshots'):
            directory = self.user_root / folder
            directory.mkdir(parents=True)
            (directory / 'synthetic.txt').write_text('合成诊断', encoding='utf-8')
        with patch.object(og, 'config', {'gui_title': 'NoNo'}), \
                patch('ok.util.file.get_downloads_folder', return_value=str(downloads)), \
                patch.object(diagnostics, 'reveal_in_explorer') as reveal:
            diagnostics.export_logs()
        reveal.assert_called_once_with(downloads / 'NoNo-log.zip')
        with zipfile.ZipFile(downloads / 'NoNo-log.zip') as archive:
            self.assertEqual(set(archive.namelist()), {'logs/synthetic.txt', 'screenshots/synthetic.txt'})

    def test_pyappify_inline_requires_actual_version_and_rejects_stale_directory(self):
        from ok.update import inline_ok_requirements as inliner
        destination = Path(self.temp_dir.name) / 'inline'
        destination.mkdir()
        with redirect_stdout(StringIO()):
            inliner.inline_site_packages(destination, ('pyappify',))
        self.assertEqual(inliner.verify_inlined_pyappify(destination), '1.0.20')
        for name in inliner.PYAPPIFY_UPDATE_MODULES:
            self.assertTrue((destination / 'pyappify' / name).is_file())
        stale = destination / 'pyappify/updater_ui.py'
        stale.write_bytes('# 合成旧模块\n'.encode('utf-8'))
        with patch.object(inliner, 'find_and_copy_site_package') as copier:
            with self.assertRaisesRegex(RuntimeError, 'PYAPPIFY_INLINE_MISMATCH'):
                inliner.inline_site_packages(destination, ('pyappify',))
            copier.assert_not_called()
        self.assertEqual(stale.read_bytes(), '# 合成旧模块\n'.encode('utf-8'))
        with patch.object(inliner, 'distribution', return_value=SimpleNamespace(version='1.0.19')), \
                patch.object(inliner, 'find_and_copy_site_package') as copier:
            with self.assertRaisesRegex(RuntimeError, 'PYAPPIFY_VERSION_MISMATCH'):
                inliner.inline_site_packages(destination, ('pyappify',))
            copier.assert_not_called()

    def test_framework_resources_keep_foreground_messages_and_removed_update_source_keys(self):
        from PySide6 import QtCore
        repo = Path(__file__).resolve().parents[1]
        qt_folder = repo / 'ok/ui/qt'
        current = {
            ('SelectInteractionListView', '前台系统键鼠'),
            ('SelectInteractionListView', '使用真实键盘和鼠标；失焦释放并等待，回到游戏后自动继续'),
            ('SelectInteractionListView', '后台测试（启动时单次激活）'),
            ('SelectInteractionListView', '每次开始或恢复时仅激活一次；运行中不补发，最小化时等待恢复'),
            ('app', '需要已打开的本地 QQ 客户端；发送时占用真实键鼠和前台，长任务或用户失焦期间可能延后'),
            ('app', '需要已打开的本地微信客户端；发送时占用真实键鼠和前台，长任务或用户失焦期间可能延后'),
        }
        removed = {('SelectInteractionListView', name) for name in
                   ('Genshin', 'PostMessage', 'PyDirect', 'Pynput', 'WindowsInteraction')}
        removed.update({('app', 'Requires the local QQ client window to be open and running'),
                        ('app', 'Requires the local WeChat client window to be open and running')})
        removed.update({('UpdateCard', name) for name in
                        ('Aliyun', 'Failed to change pip source: {error}', 'Huawei Cloud', 'Pip source', 'PyPI',
                         'System default', 'Tencent Cloud', 'Tsinghua', 'USTC')})
        locales = ('en_US', 'es_ES', 'ja_JP', 'ko_KR', 'zh_CN', 'zh_TW')
        captions = {}
        caption_keys = (('SelectInteractionListView', 'Default Interaction'), ('UpdateCard', 'Switch'))

        def check_translator(translator, caption):
            # 通用选择标题和版本切换按钮仍保留；撤出的只是旧后端和下载源选项。
            for key, expected in caption.items():
                self.assertEqual(translator.translate(*key), expected)
            for context, source in current:
                self.assertEqual(translator.translate(context, source), source)
            for context, source in removed:
                self.assertEqual(translator.translate(context, source), '')

        for locale in locales:
            with self.subTest(locale=locale):
                messages = {(context.findtext('name'), message.findtext('source')): message
                            for context in ET.parse(qt_folder / f'i18n/{locale}.ts').getroot().findall('context')
                            for message in context.findall('message')
                            if message.find('translation') is not None
                            and message.find('translation').get('type') not in ('vanished', 'obsolete')}
                self.assertTrue(current <= messages.keys())
                self.assertFalse(removed & messages.keys())
                captions[locale] = {key: ''.join(messages[key].find('translation').itertext()) for key in caption_keys}
                translator = QtCore.QTranslator()
                self.assertTrue(translator.load(str(qt_folder / f'i18n/{locale}.qm')))
                check_translator(translator, captions[locale])
        tree = ast.parse((qt_folder / 'resources.py').read_bytes())
        names = {'qt_resource_struct', 'qt_resource_name', 'qt_resource_data'}
        data = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names}
        self.assertEqual(data.keys(), names)
        arguments = (3, data['qt_resource_struct'], data['qt_resource_name'], data['qt_resource_data'])
        self.assertTrue(QtCore.qRegisterResourceData(*arguments))
        try:
            self.assertFalse(QtCore.QFile.exists(':/i18n/en_US.qm'))
            for locale in locales[1:]:
                with self.subTest(embedded_locale=locale):
                    resource_name = f':/i18n/{locale}.qm'
                    resource = QtCore.QFile(resource_name)
                    self.assertTrue(resource.open(QtCore.QIODevice.OpenModeFlag.ReadOnly))
                    content = bytes(resource.readAll())
                    resource.close()
                    self.assertEqual(content, (qt_folder / f'i18n/{locale}.qm').read_bytes())
                    translator = QtCore.QTranslator()
                    self.assertTrue(translator.load(resource_name))
                    check_translator(translator, captions[locale])
        finally:
            self.assertTrue(QtCore.qUnregisterResourceData(*arguments))
        manifest = json.loads((repo / 'selfhost/logic_patches.json').read_bytes())
        pin = json.loads((repo / 'selfhost/ok_pin.json').read_bytes())
        # 资源约束跟随已登记的框架来源，升级补丁版本不应被旧版本常量误报阻断。
        self.assertEqual(manifest['framework_version'], pin['version'])
        paths = {entry['path'] for entry in manifest['entries']}
        self.assertIn('ui/qt/MainWindow.py', paths)
        self.assertNotIn('ui/qt/about/UpdateCard.py', paths)

    def test_update_card_passes_language_and_supports_legacy_notes(self):
        from ok.ui.qt.about import UpdateCard as update_module
        from pyappify.versions import entry_notes
        entries = [{'version': 'v3.8.0', 'update_note': ['旧协议说明']}]
        exit_event = threading.Event()
        for modern in (True, False):
            calls = []

            def calculate_modern(versions, current, target, language=None):
                calls.append(('notes', language))
                return ['合成更新说明']

            def calculate_legacy(versions, current, target):
                calls.append(('notes', None))
                return ['合成更新说明']

            def update_modern(version, exit_event=None, language=None):
                calls.append(('update', version, exit_event, language))
                return {'mocked': True}

            def update_legacy(version, exit_event=None):
                calls.append(('update', version, exit_event, None))
                return {'mocked': True}

            api = SimpleNamespace(get_version_list=lambda release_only, exit_event: entries,
                                  calculate_update_notes=calculate_modern if modern else calculate_legacy,
                                  update_to_version=update_modern if modern else update_legacy,
                                  is_greater_version=lambda left, right: left > right)
            card = update_module.UpdateCard('v3.7.20', api, exit_event=exit_event)
            try:
                with patch.object(update_module, '_ui_locale', return_value='zh_CN'), \
                        patch.object(card, '_run_in_background',
                                     side_effect=lambda operation, signal, name: signal.emit((True, operation()))):
                    card.check_for_updates()
                    card.update_to_version('v3.8.0')
                    self.assertIn(('notes', 'zh_CN' if modern else None), calls)
                    self.assertIn(('update', 'v3.8.0', exit_event, 'zh_CN' if modern else None), calls)
                    self.assertFalse(card._busy)
                    self.assertEqual(card._entry_notes(entries[0]), ['旧协议说明'])
                    api.entry_notes = entry_notes
                    localized = {**entries[0], 'localized_notes': {'zh-CN': ['中文说明']}}
                    self.assertEqual(card._entry_notes(localized), ['中文说明'])
            finally:
                card.close()
                card.deleteLater()

    def test_update_card_cancelled_background_check_clears_busy_without_quitting(self):
        from ok.ui.qt.about import UpdateCard as update_module
        exit_event = threading.Event()

        def cancelled(release_only, exit_event):
            self.assertTrue(exit_event.is_set())
            raise CancelledError('合成取消')

        card = update_module.UpdateCard('v3.8.0', SimpleNamespace(get_version_list=cancelled), exit_event=exit_event)
        try:
            with patch.object(update_module.threading, 'Thread') as worker, \
                    patch.object(update_module, 'communicate', SimpleNamespace(quit=Mock())) as signals:
                card.check_for_updates()
                card.check_for_updates()
                worker.assert_called_once()
                self.assertTrue(card._busy)
                exit_event.set()
                worker.call_args.kwargs['target']()
                self.assertFalse(card._busy)
                self.assertTrue(card.check_button.isEnabled())
                self.assertIn('合成取消', card.status_label.text())
                signals.quit.emit.assert_not_called()
        finally:
            card.close()
            card.deleteLater()

    def test_main_window_startup_update_check_only_runs_while_idle(self):
        from ok import og
        from ok.ui.qt.MainWindow import MainWindow
        executor = SimpleNamespace(paused=True)
        controller = SimpleNamespace(starting=False)
        about = SimpleNamespace(update_card=object(), check_for_updates=Mock())
        window = SimpleNamespace(basic_global_config={}, executor=executor, about_tab=about)
        window._game_starting_or_started = lambda: MainWindow._game_starting_or_started(window)
        cases = ((False, False, False, True), (True, False, False, True),
                 (False, True, False, True), (False, False, True, True), (False, False, False, False))
        for auto_start, requested, starting, paused in cases:
            with self.subTest(auto_start=auto_start, requested=requested, starting=starting, paused=paused), \
                    patch.object(og, 'app', SimpleNamespace(start_controller=controller)), \
                    patch.object(og, 'ok', SimpleNamespace(args={'task': requested})):
                window.basic_global_config['Auto Start Game When App Starts'] = auto_start
                controller.starting = starting
                executor.paused = paused
                about.check_for_updates.reset_mock()
                MainWindow._startup_update_check(window)
                # 已废弃的游戏自动启动值不能影响 NoNo 自身的空闲更新检查。
                if not requested and not starting and paused:
                    about.check_for_updates.assert_called_once_with(automatic=True)
                else:
                    about.check_for_updates.assert_not_called()


class TestLogicPatchReplay(unittest.TestCase):
    """仅使用标准库与隔离目录，守住覆盖前失败、完整来源及部署一致性。"""

    @classmethod
    def setUpClass(cls):
        cls.sync = load_sync_script()
        from selfhost import logic_patches
        cls.logic = logic_patches
        cls.version = '9.9.9'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='逻辑差异验证-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = StringIO()
        self.stack.enter_context(redirect_stdout(self.output))

    @staticmethod
    def write_files(root, files):
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def capture(self, baseline, modified, folder):
        self.write_files(folder / 'base', baseline)
        self.write_files(folder / 'modified', modified)
        manifest = folder / 'captured' / 'logic_patches.json'
        patches = self.logic.capture(folder / 'base', folder / 'modified', self.version, manifest)
        return patches, manifest

    def test_text_and_binary_add_modify_delete_replay_exactly_and_repeat_safely(self):
        lines = [f'计数_{index} = {index}\r\n' for index in range(500)]
        source = (''.join(lines) + "末尾 = '保留'").encode('utf-8')
        updated = source.replace('计数_100 = 100'.encode(), '计数_100 = 101'.encode())
        updated = updated.replace("末尾 = '保留'".encode(), "末尾 = '更新'".encode())
        baseline = {
            'a.py': source, 'obsolete.py': b'old = 1\n', 'stable.txt': b'unchanged\n',
            'resources/changed.qm': b'\x00\xff' + '旧资源'.encode(),
            'resources/deleted.mo': b'\x00\xfe' + '旧资源'.encode(),
        }
        modified = {
            'a.py': updated, '新目录/模块.py': '新值 = 1\n'.encode(), 'empty.py': b'',
            'stable.txt': baseline['stable.txt'], 'resources/changed.qm': b'\x00\xfe' + '新资源'.encode(),
            'resources/added.mo': b'\x00\xff' + '新资源'.encode(),
        }
        patches, _manifest = self.capture(baseline, modified, self.root)
        entries = {entry.path: entry for entry in patches.entries}
        self.assertEqual(entries['resources/changed.qm'].format, 'binary')
        self.assertEqual(entries['resources/deleted.mo'].operation, 'delete')
        self.assertEqual(entries['obsolete.py'].format, 'unified_diff')
        self.assertEqual(entries['empty.py'].before_sha256, None)
        self.assertIn(self.logic.NO_NEWLINE, entries['a.py'].content)
        self.assertLess(len(entries['a.py'].content), len(source) // 4, '单行修改不能保存整份 Python 副本')
        applied, already = patches.apply_to(self.root / 'base')
        self.assertEqual(set(applied), set(entries))
        self.assertEqual(already, [])
        expected = self.sync.hash_tree(self.root / 'modified')
        self.assertEqual(self.sync.hash_tree(self.root / 'base'), expected)
        self.assertTrue((self.root / 'base' / 'empty.py').is_file())
        applied, already = patches.apply_to(self.root / 'base')
        self.assertEqual(applied, [])
        self.assertEqual(set(already), set(entries))
        self.assertEqual(self.sync.hash_tree(self.root / 'base'), expected)
        self.assertEqual(patches.baseline_digests(expected), {
            relative: hashlib.sha256(content).hexdigest() for relative, content in baseline.items()
        })

    def test_baseline_context_result_and_path_drift_fail_before_any_file_write(self):
        baseline = {'a.py': b'value = 1\n', 'z.py': b'value = 1\n'}
        modified = {'a.py': b'value = 2\n', 'z.py': b'value = 2\n'}
        for failure in ('baseline', 'context', 'result', 'path', 'path_layout'):
            with self.subTest(failure=failure):
                folder = self.root / failure
                target = {**modified, 'new.py': b'value = 2\n'} if failure == 'path_layout' else modified
                _patches, manifest = self.capture(baseline, target, folder)
                if failure == 'baseline':
                    (folder / 'base' / 'z.py').write_bytes(b'value = 9\n')
                elif failure in ('context', 'result'):
                    payload = manifest.parent / 'patches' / 'z.py.patch'
                    old = b'-value = 1\n' if failure == 'context' else b'+value = 2\n'
                    new = b'-value = 9\n' if failure == 'context' else b'+value = 9\n'
                    payload.write_bytes(payload.read_bytes().replace(old, new))
                elif failure == 'path':
                    data = json.loads(manifest.read_bytes())
                    data['entries'][1]['path'] = '../z.py'
                    manifest.write_bytes(json.dumps(data).encode())
                else:
                    (folder / 'base' / 'new.py').mkdir()
                before = self.sync.hash_tree(folder / 'base')
                with self.assertRaisesRegex(SystemExit, '漂移|摘要不符|越界|目录'):
                    self.logic.load(manifest, self.version).apply_to(folder / 'base')
                self.assertEqual(self.sync.hash_tree(folder / 'base'), before)
                self.assertEqual((folder / 'base' / 'a.py').read_bytes(), baseline['a.py'])

    def test_invalid_manifest_or_payload_is_rejected_without_fuzzy_application(self):
        _patches, manifest = self.capture({'a.py': b'value = 1\n'}, {'a.py': b'value = 2\n'}, self.root)
        original = json.loads(manifest.read_bytes())
        for path in ('../a.py', '/a.py', 'dir\\a.py', '.', 'dir/../a.py', 'NUL.py'):
            with self.subTest(path=path):
                data = json.loads(json.dumps(original))
                data['entries'][0]['path'] = path
                manifest.write_bytes(json.dumps(data).encode())
                with self.assertRaisesRegex(SystemExit, '路径'):
                    self.logic.load(manifest, self.version)
        for failure in ('version', 'duplicate', 'python_binary', 'header', 'orphan'):
            with self.subTest(failure=failure):
                data = json.loads(json.dumps(original))
                if failure == 'version':
                    data['framework_version'] = '0.0.0'
                elif failure == 'duplicate':
                    data['entries'].append(data['entries'][0].copy())
                elif failure == 'python_binary':
                    data['entries'][0]['format'] = 'binary'
                manifest.write_bytes(json.dumps(data).encode())
                payload = manifest.parent / 'patches' / 'a.py.patch'
                content = payload.read_bytes()
                orphan = manifest.parent / 'patches' / 'orphan.patch'
                if failure == 'header':
                    payload.write_bytes(content.replace(b'a/a.py', b'a/other.py'))
                elif failure == 'orphan':
                    orphan.write_bytes(b'\n')
                try:
                    with self.assertRaises(SystemExit):
                        self.logic.load(manifest, self.version)
                finally:
                    payload.write_bytes(content)
                    orphan.unlink(missing_ok=True)
        self.assertEqual((self.root / 'base' / 'a.py').read_bytes(), b'value = 1\n')
        folder = self.root / 'binary'
        _patches, binary_manifest = self.capture(
            {'resource.qm': b'\x00\xffold'}, {'resource.qm': b'\x00\xffnew'}, folder,
        )
        payload = binary_manifest.parent / 'patches' / 'resource.qm.bin'
        payload.write_bytes(b'\x00\xffcorrupt')
        with self.assertRaisesRegex(SystemExit, '二进制载荷与修改后摘要不符'):
            self.logic.load(binary_manifest, self.version)
        self.assertEqual((folder / 'base' / 'resource.qm').read_bytes(), b'\x00\xffold')

    def setup_sync_project(self):
        project = self.root / 'project'
        raw = {
            'a.py': "品牌 = '上游'\n数值 = 1\n".encode(), 'obsolete.py': b'old = 1\n',
            'resources/changed.qm': b'\x00\xffold', 'resources/deleted.mo': b'\x00\xfeold',
            'stable.txt': b'unchanged\n',
        }
        baseline = {**raw, 'a.py': raw['a.py'].replace('上游'.encode(), b'NoNo')}
        modified = {
            'a.py': baseline['a.py'].replace('数值 = 1'.encode(), '数值 = 2'.encode()),
            'new.py': '新值 = 1\n'.encode(), 'resources/changed.qm': b'\x00\xffnew',
            'stable.txt': baseline['stable.txt'],
        }
        self.write_files(project / 'ok', modified)
        self.write_files(self.root / 'baseline', baseline)
        manifest = project / 'selfhost' / 'logic_patches.json'
        empty = self.logic.capture(self.root / 'baseline', self.root / 'baseline', self.version, manifest)
        wheel_buffer = BytesIO()
        with zipfile.ZipFile(wheel_buffer, 'w') as archive:
            for relative, content in raw.items():
                archive.writestr(f'ok/{relative}', content)
            archive.writestr('ok_script.dist-info/METADATA', 'Metadata-Version: 2.1\nName: ok-script\n')
        wheel = wheel_buffer.getvalue()
        wheel_sha = hashlib.sha256(wheel).hexdigest()
        pin = {
            'package': 'ok-script', 'version': self.version, 'wheel_sha256': wheel_sha,
            'tree_sha256': self.sync.tree_digest(self.sync.hash_tree(self.root / 'baseline')),
            'logic_patches_sha256': empty.digest(), 'source_url': 'https://example.invalid/framework.whl',
        }
        pin_path = project / 'selfhost' / 'ok_pin.json'
        pin_path.write_bytes((json.dumps(pin, indent=2) + '\n').encode())
        for name, value in {
            'REPO_ROOT': project, 'STAGING_DIR': project / '.sync-ok-stage', 'OK_DIR': project / 'ok',
            'PIN_FILE': pin_path, 'PATCH_MANIFEST': manifest, 'PYPROJECT_FILE': project / 'pyproject.toml',
            'REQUIREMENTS_FILE': project / 'requirements.txt',
        }.items():
            self.stack.enter_context(patch.object(self.sync, name, value))
        self.stack.enter_context(patch.object(self.sync, 'fetch_release', return_value=(pin['source_url'], wheel_sha)))
        self.stack.enter_context(patch.object(self.sync, 'download', return_value=wheel))

        def apply_branding(root):
            path = root / 'a.py'
            path.write_bytes(path.read_bytes().replace('上游'.encode(), b'NoNo'))
            return ['a.py'], []

        self.stack.enter_context(patch.object(self.sync.framework_patches, 'apply_to', side_effect=apply_branding))
        self.stack.enter_context(patch.object(self.sync.framework_patches, 'missing', return_value=[]))
        return project, pin, baseline, modified

    def test_record_replays_verified_wheel_before_registering_source_and_pin(self):
        project, original_pin, baseline, _modified = self.setup_sync_project()
        current = self.sync.hash_tree(project / 'ok')
        pin_path = project / 'selfhost' / 'ok_pin.json'
        manifest = project / 'selfhost' / 'logic_patches.json'
        old_pin, old_manifest = pin_path.read_bytes(), manifest.read_bytes()
        with patch.object(self.sync, 'download', return_value=b'corrupt wheel'):
            with self.assertRaisesRegex(SystemExit, '下载内容与 PyPI 摘要不符'):
                self.sync.do_record_patches(None, False)
        self.assertEqual(pin_path.read_bytes(), old_pin)
        self.assertEqual(manifest.read_bytes(), old_manifest)
        self.assertEqual(self.sync.hash_tree(project / 'ok'), current)
        self.assertEqual(self.sync.do_record_patches(None, True), 0)
        self.assertEqual(pin_path.read_bytes(), old_pin)
        self.assertEqual(manifest.read_bytes(), old_manifest)
        self.assertEqual(self.sync.hash_tree(project / 'ok'), current)
        self.assertEqual(self.sync.do_record_patches(None, False), 0)
        pin = json.loads(pin_path.read_bytes())
        patches = self.logic.load(manifest, self.version)
        self.assertEqual(pin['wheel_sha256'], original_pin['wheel_sha256'], 'wheel 摘要不能变成补丁后树摘要')
        self.assertEqual(pin['tree_sha256'], self.sync.tree_digest(current))
        self.assertEqual(pin['logic_patches_sha256'], patches.digest())
        self.assertEqual(patches.baseline_tree_sha256, self.sync.tree_digest({
            relative: hashlib.sha256(content).hexdigest() for relative, content in baseline.items()
        }), '必须先施加品牌补丁再记录逻辑基线')
        self.assertEqual(self.sync.main(['--check', '--root', str(project)]), 0)
        rewrite = self.stack.enter_context(patch.object(self.sync, 'rewrite_pyproject', return_value=(False, [])))
        self.assertEqual(self.sync.do_sync(None, False), 0)
        rewrite.assert_called_once()
        self.assertEqual(self.sync.hash_tree(project / 'ok'), current)
        self.assertFalse((project / '.sync-ok-stage').exists())

    def test_deployment_check_rejects_unrecorded_tree_change_even_if_only_pin_is_updated(self):
        project, _pin, _baseline, _modified = self.setup_sync_project()
        self.sync.do_record_patches(None, False)
        stable = project / 'ok' / 'stable.txt'
        stable.write_bytes(b'unrecorded change\n')
        pin_path = project / 'selfhost' / 'ok_pin.json'
        pin = json.loads(pin_path.read_bytes())
        pin['tree_sha256'] = self.sync.tree_digest(self.sync.hash_tree(project / 'ok'))
        pin_path.write_bytes(json.dumps(pin).encode())
        self.assertEqual(self.sync.do_check(project), 1)
        self.assertIn('未登记改动', self.output.getvalue())
        stable.write_bytes(b'unchanged\n')
        pin['tree_sha256'] = self.sync.tree_digest(self.sync.hash_tree(project / 'ok'))
        pin_path.write_bytes(json.dumps(pin).encode())
        manifest = project / 'selfhost' / 'logic_patches.json'
        manifest.write_bytes(manifest.read_bytes() + b'\n')
        self.assertEqual(self.sync.do_check(project), 1)
        self.assertIn('集合与 pin 不一致', self.output.getvalue())

    def test_sync_context_drift_cannot_replace_tree_or_reapply_dependency_files(self):
        project, _pin, _baseline, _modified = self.setup_sync_project()
        self.sync.do_record_patches(None, False)
        manifest = project / 'selfhost' / 'logic_patches.json'
        payload = manifest.parent / 'patches' / 'a.py.patch'
        payload.write_bytes(payload.read_bytes().replace('-数值 = 1'.encode(), '-数值 = 9'.encode()))
        pin_path = project / 'selfhost' / 'ok_pin.json'
        pin = json.loads(pin_path.read_bytes())
        # 模拟审查遗漏的错误差异；即使集合摘要被登记，真实重放仍必须拒绝错误上下文。
        pin['logic_patches_sha256'] = self.logic.load(manifest, self.version).digest()
        pin_path.write_bytes(json.dumps(pin).encode())
        before, old_pin = self.sync.hash_tree(project / 'ok'), pin_path.read_bytes()
        reapply = self.stack.enter_context(patch.object(self.sync, 'reapply_upstream_overwrites'))
        rewrite = self.stack.enter_context(patch.object(self.sync, 'rewrite_pyproject'))
        with self.assertRaisesRegex(SystemExit, '上下文漂移'):
            self.sync.do_sync(None, False, reapply=True)
        self.assertEqual(self.sync.hash_tree(project / 'ok'), before)
        self.assertEqual(pin_path.read_bytes(), old_pin)
        reapply.assert_not_called()
        rewrite.assert_not_called()
        self.assertFalse((project / '.sync-ok-stage').exists())


class TestPortableBundle(unittest.TestCase):
    """普通编译必须交付当前物理部署树，并保留启动器认可的正式更新锚点。"""

    def setUp(self):
        from selfhost import portable
        self.portable = portable
        self.build_root = Path(__file__).resolve().parents[1] / 'build'
        self.build_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='便携交付验证-', dir=self.build_root)
        self.root = portable.require_path(self.build_root, Path(self.temporary.name))
        self.source = self.root / 'source'
        self.source.mkdir()
        self.version = 'v3.7.21'
        self.configuration = (
            'name: "NoNo"\nprofiles:\n  - name: "China"\n'
            '    git_url: "https://github.com/Canda15/nono-update.git"\n'
            '    main_script: "selfhost/NoNo.exe"\n    requires_python: "3.12"\n'
            '    requirements: "requirements.txt"\n    pip_args: "--no-deps"\n'
        )
        self.files = {
            'main.py': 'TOKEN = "原始提交"\n'.encode(),
            'config.py': b'version = "dev"\n',
            'requirements.txt': b'pyappify[standalone]==1.0.20\nexample==1.0.0\n',
            'pyappify.yml': self.configuration.encode(),
            'src/task.py': '业务值 = "当前源码"\n'.encode(),
            'ok/__init__.py': b'FRAMEWORK = "current"\n',
            'ok/core/foreground_fixture.py': b'FRAMEWORK_STATE = "current"\n',
            'selfhost/__init__.py': b'\n',
            'selfhost/ok_pin.json': b'{"package": "ok-script", "version": "test"}\n',
            'selfhost/user_data.py': (Path(__file__).resolve().parents[1] / 'selfhost/user_data.py').read_bytes(),
            'selfhost/versioning.py': (Path(__file__).resolve().parents[1] / 'selfhost/versioning.py').read_bytes(),
            'assets/current.bin': b'current asset',
            '.update_repo_gitignore': b'__pycache__/\n*.pyc\nlogs/\n',
            'deploy.txt': b'src\nok\nselfhost\npyappify\nmain.py\nconfig.py\nrequirements.txt\npyappify.yml\nassets\n',
        }
        for relative, content in self.files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.initialize_repository(self.source)
        # 宿主是编译期产物，不受源码仓库跟踪，但必须显式随当前部署树交付。
        (self.source / 'selfhost' / 'NoNo.exe').write_bytes(b'MZ-current-native-host')
        (self.source / 'selfhost' / 'local-account.json').write_bytes(b'private account')
        (self.source / 'assets' / '__pycache__').mkdir()
        (self.source / 'assets' / '__pycache__' / 'private.pyc').write_bytes(b'cache')
        (self.source / 'tests').mkdir()
        (self.source / 'tests' / 'private.bin').write_bytes(b'large developer fixture')
        (self.source / 'ok_templates').mkdir()
        (self.source / 'ok_templates' / 'private.bin').write_bytes(b'large developer template')

    def tearDown(self):
        self.portable.require_path(self.build_root, self.root)
        self.temporary.cleanup()

    def initialize_repository(self, root):
        self.portable.git(root, 'init', '--initial-branch=master')
        self.portable.git(root, 'add', '.')
        self.portable.git(root, '-c', 'user.name=便携交付测试', '-c',
                          'user.email=portable-test@example.invalid', 'commit', '-m', '准备隔离部署树')
        self.portable.git(root, 'tag', self.version)

    def prepare_snapshot(self):
        snapshot = self.root / 'snapshot'
        digests = self.portable.prepare_snapshot(self.source, snapshot, self.version, self.build_root)
        return snapshot, digests

    def prepare_bundle(self):
        installation, info = self.prepare_installation()
        return self.portable.package_incoming(installation, self.root / 'package/NoNo', info,
                                              self.launcher_proof, self.build_root)

    def test_snapshot_uses_current_checkout_and_explicit_host_without_private_or_developer_files(self):
        self.version = 'v3.8.1-xmod.10'
        (self.source / 'main.py').write_text('TOKEN = "本次构建"\n', encoding='utf-8')
        snapshot, digests = self.prepare_snapshot()
        self.assertEqual((snapshot / 'main.py').read_bytes(), (self.source / 'main.py').read_bytes())
        self.assertEqual((snapshot / 'selfhost' / 'NoNo.exe').read_bytes(), b'MZ-current-native-host')
        self.assertEqual((snapshot / 'config.py').read_text(encoding='utf-8'), f'version = "{self.version}"\n')
        self.assertEqual((self.source / 'config.py').read_bytes(), self.files['config.py'])
        self.assertEqual((self.source / 'requirements.txt').read_bytes(), self.files['requirements.txt'])
        self.assertEqual((snapshot / '.gitignore').read_bytes(), self.files['.update_repo_gitignore'])
        self.assertNotIn('pyappify', (snapshot / 'requirements.txt').read_text(encoding='utf-8'))
        self.assertEqual((snapshot / 'pyappify/_nono_versions.py').read_bytes(),
                         (snapshot / 'selfhost/versioning.py').read_bytes())
        self.assertFalse(any(
            'private' in path or path.startswith(('tests/', 'ok_templates/', '.git/'))
            or '__pycache__' in path or path == 'deploy.txt'
            for path in digests
        ))

    def test_overlay_replaces_both_trees_deletes_stale_files_and_preserves_official_git_anchor(self):
        installation, info = self.prepare_installation()
        app = installation / 'data/apps/NoNo'
        working, repo = app / 'working', app / 'repo'
        snapshot = self.root / 'snapshot'
        (working / 'removed.py').write_bytes(b'obsolete source')
        before = self.portable.deployment_digests(repo)
        self.portable.replace_deployment(snapshot, working, self.build_root)
        self.assertEqual(self.portable.deployment_digests(working), info['deployment_files'])
        self.assertFalse((working / 'removed.py').exists())
        self.assertEqual(self.portable.deployment_digests(repo), before)
        self.assertEqual(self.portable.git(repo, 'rev-parse', 'HEAD'), info['base_update_commit'])
        with self.assertRaisesRegex(RuntimeError, '裸仓库'):
            self.portable.replace_deployment(snapshot, repo, self.build_root, preserve_git=True)
        self.assertEqual(self.portable.deployment_digests(repo), before)

    def test_missing_compiled_host_and_escaping_deploy_entry_are_rejected_before_copy(self):
        native = self.source / 'selfhost' / 'NoNo.exe'
        native.unlink()
        with self.assertRaisesRegex(RuntimeError, '缺少已编译'):
            self.prepare_snapshot()
        self.assertFalse((self.root / 'snapshot').exists())
        native.write_bytes(b'MZ-current-native-host')
        (self.source / 'deploy.txt').write_text('../outside\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '越界'):
            self.prepare_snapshot()
        self.assertFalse((self.root / 'snapshot').exists())

        from ok.update import inline_ok_requirements
        source_config = self.source / 'config.py'
        source_requirements = self.source / 'requirements.txt'
        source_deploy = self.source / 'deploy.txt'
        cwd = Path.cwd()
        original_deploy = self.files['deploy.txt']
        missing_entries = ('config.py', 'requirements.txt', 'main.py', 'pyappify.yml', 'ok')
        for index, missing in enumerate((*missing_entries, 'partial_ok', 'untracked_config', 'untracked_ok')):
            with self.subTest(missing=missing):
                deploy = original_deploy.decode()
                untracked = None
                if missing in missing_entries:
                    deploy = deploy.replace(f'{missing}\n', '')
                elif missing == 'partial_ok':
                    deploy = deploy.replace('ok\n', 'ok/__init__.py\n')
                else:
                    untracked = 'config.py' if missing == 'untracked_config' else 'ok/__init__.py'
                    self.portable.git(self.source, 'rm', '--cached', '--', untracked)
                source_deploy.write_text(deploy, encoding='utf-8')
                destination = self.root / f'rejected-snapshot-{index}'
                try:
                    with patch.object(inline_ok_requirements, 'remove_ok_requirements',
                                      wraps=inline_ok_requirements.remove_ok_requirements) as inline, \
                            patch.object(inline_ok_requirements, 'find_and_copy_site_package',
                                         return_value=1) as site_package:
                        try:
                            # 专门复现旧 helper 向调用者 cwd 回退；这里只进入隔离源码树。
                            os.chdir(self.source)
                            with self.assertRaises(RuntimeError):
                                self.portable.prepare_snapshot(self.source, destination, self.version,
                                                               self.build_root)
                        finally:
                            os.chdir(cwd)
                        self.assertEqual(source_config.read_bytes(), self.files['config.py'])
                        self.assertEqual(source_requirements.read_bytes(), self.files['requirements.txt'])
                        self.assertFalse(destination.exists(), '无效部署集合不能开始复制或内联')
                        inline.assert_not_called()
                        site_package.assert_not_called()
                finally:
                    os.chdir(cwd)
                    source_config.write_bytes(self.files['config.py'])
                    source_requirements.write_bytes(self.files['requirements.txt'])
                    source_deploy.write_bytes(original_deploy)
                    if untracked:
                        self.portable.git(self.source, 'add', '--', untracked)

        child = self.root / 'child'
        child.mkdir()
        with patch.object(self.portable.shutil, 'rmtree') as delete:
            for candidate in (self.build_root.parent, self.root, child / '..'):
                with self.subTest(candidate=candidate):
                    with self.assertRaisesRegex(RuntimeError, '预期目录'):
                        self.portable.remove_tree(self.root, candidate)
            delete.assert_not_called()
        self.assertTrue(self.source.exists())

    def test_subprocess_environment_removes_pip_targets_and_host_python_prefixes_without_mutating_parent(self):
        inherited = {
            'PIP_TARGET': str(self.root / 'external'), 'PIP_PREFIX': str(self.root / 'prefix'),
            'PIP_ROOT': str(self.root / 'pip-root'), 'PIP_USER': '1',
            'PIP_FIND_LINKS': 'file:///fixture', 'PIP_REQUIRE_VIRTUALENV': '1',
            'VIRTUAL_ENV': str(self.root / 'host-venv'), 'PYTHONHOME': 'fixture-host-home',
            'PYTHONPATH': 'fixture-host-path', 'HTTP_PROXY': 'http://proxy.example.invalid:3128',
            'HTTPS_PROXY': 'http://proxy.example.invalid:3128', 'NO_PROXY': 'example.invalid',
        }
        with patch.dict(os.environ, inherited):
            parent = dict(os.environ)
            process_temp = self.root / 'process-temp'
            process_temp.mkdir()
            environment = self.portable.process_environment(process_temp)
            self.assertTrue(dict(os.environ) == parent, '环境隔离不能修改调用者进程')
            self.assertEqual({key for key in environment if key.upper().startswith('PIP_')},
                             {'PIP_CONFIG_FILE'})
            self.assertEqual(environment['PIP_CONFIG_FILE'], os.devnull)
            for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
                self.assertNotIn(key, environment)
            for key in ('PATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'):
                self.assertEqual(environment.get(key), parent.get(key))
            for key in ('TEMP', 'TMP', 'RUNNER_TEMP'):
                self.assertEqual(environment[key], str(process_temp))
            # 真实子进程只读取环境，不执行 pip，也不会向伪造的安装目录写入文件。
            code = (
                'import json, os, sys; print(json.dumps({"environment": '
                '{key: value for key, value in os.environ.items() if key.upper().startswith("PIP_") '
                'or key.upper() in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}, "prefix": sys.prefix, '
                '"temporary": {key: os.environ[key] for key in ("TEMP", "TMP", "RUNNER_TEMP")}}))'
            )
            child = json.loads(self.portable.run([sys.executable, '-c', code], self.root,
                                                temporary_root=process_temp))
            self.assertEqual(Path(child['prefix']).resolve(), Path(sys.prefix).resolve())
            self.assertEqual({key for key in child['environment'] if key.upper().startswith('PIP_')},
                             {'PIP_CONFIG_FILE'})
            for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
                self.assertNotIn(key, child['environment'])
            self.assertEqual(set(child['temporary'].values()), {str(process_temp)})
            self.assertFalse((self.root / 'external').exists())
            self.assertTrue(dict(os.environ) == parent, '环境隔离不能修改调用者进程')

    def test_actual_archive_contains_runtime_current_sources_pin_and_update_metadata(self):
        bundle, manifest = self.prepare_bundle()
        archive = self.portable.archive_bundle(bundle, self.root / 'artifacts', manifest, self.build_root)
        payload = f"NoNo/data/incoming/NoNo/{manifest['bundle_id']}/"
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(package.read(payload + 'working/main.py'), self.files['main.py'])
            self.assertEqual(package.read(payload + 'working/selfhost/NoNo.exe'), b'MZ-current-native-host')
            self.assertEqual(package.read(payload + 'working/selfhost/ok_pin.json'), self.files['selfhost/ok_pin.json'])
            self.assertEqual(package.read(payload + 'python/python.exe'), b'MZ-python')
            self.assertEqual(package.read(payload + 'python/python3.dll'), b'portable python library')
            self.assertEqual(json.loads(package.read(payload + 'app-seed.json'))['current_version'], self.version)
            self.assertEqual(json.loads(package.read(payload + 'manifest.json')), manifest)
            self.assertIn(payload + 'repo/HEAD', package.namelist())
            self.assertFalse(any('/data/apps/' in name or '/data/config/' in name or
                                 '/data/user/' in name or '/data/backups/' in name for name in package.namelist()))
            self.assertNotIn('NoNo/pyappify.yml', package.namelist())
        expected_checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual((archive.parent / 'NoNo-win32-sha256.txt').read_text().strip(),
                         f'{expected_checksum}  NoNo-win32-portable.zip')

    def test_build_uses_setup_to_refresh_current_snapshot_and_keeps_release_on_one_tag_setup(self):
        template, base_info = self.prepare_installation()
        original_run = self.portable.run
        source_sha = self.portable.git(self.source, 'rev-parse', 'HEAD')
        (self.source / 'main.py').write_bytes(b'current source snapshot')
        (self.source / 'requirements.txt').write_bytes(b'example==2.0.0\n')
        for release_tag in (None, self.version, 'v3.8.1-beta.1', 'v3.8.1-xmod', 'v3.8.1-xmod.10',
                            'v3.8.1-beta.1-xmod'):
            with self.subTest(release_tag=release_tag):
                calls, checks = [], []
                def fetch(destination, app_name, candidate=None, proof_path=None, *, published=False):
                    shutil.copy2(template / 'NoNo.exe', destination / 'NoNo.exe')
                    shutil.copy2(template / 'launcher-build.json', destination / 'launcher-build.json')
                    return destination / 'NoNo.exe'
                def execute(command, cwd, *, timeout=120, temporary_root=None):
                    if command[0] == str(cwd / 'NoNo.exe'):
                        self.assertTrue(temporary_root.is_relative_to(cwd.parents[1]))
                        self.assertTrue(temporary_root.is_dir())
                        app = cwd / 'data/apps/NoNo'
                        if command[1:5] == ['-c', 'setup', '-p', 'China']:
                            calls.append('setup')
                            shutil.copytree(template, cwd, dirs_exist_ok=True)
                            if release_tag:
                                state = json.loads((app / 'app.json').read_bytes())
                                state['current_version'] = release_tag
                                (app / 'app.json').write_text(json.dumps(state), encoding='utf-8')
                                self.portable.git(app / 'repo', 'tag', release_tag) if release_tag != self.version else None
                        elif command[1] == '--nono-install-working':
                            calls.append('working')
                            install = json.loads(Path(command[2]).read_bytes())
                            self.assertEqual(install['build_kind'], 'working_install')
                            self.assertEqual((app / 'working/main.py').read_bytes(), b'current source snapshot')
                            state_dir = cwd / 'data/state/NoNo'
                            state_dir.mkdir(parents=True, exist_ok=True)
                            (state_dir / 'install-working.json').write_text('{"state":"completed"}', encoding='utf-8')
                        else:
                            self.fail('调用了未声明的启动器安装入口。')
                        (app / 'python/installed-requirements.txt').write_bytes((app / 'working/requirements.txt').read_bytes())
                        return '合成安装完成'
                    if '--check' in command and '--root' in command:
                        checks.append(Path(command[-1]).name)
                        return '合成来源检查完成'
                    return original_run(command, cwd, timeout=timeout, temporary_root=temporary_root)
                with patch.object(self.portable, 'fetch_launcher', side_effect=fetch), \
                        patch.object(self.portable, 'run', side_effect=execute), \
                        patch.object(self.portable, 'probe_executable', return_value={'python_version': '3.12'}) as probe, \
                        patch.object(self.portable, 'verify_package_reception') as reception, \
                        patch.object(self.portable, 'prepare_snapshot', wraps=self.portable.prepare_snapshot) as snapshot:
                    archive = self.portable.build_portable(self.source, source_sha=source_sha, release_tag=release_tag)
                    self.assertEqual(calls, ['setup'] if release_tag else ['setup', 'working'])
                    self.assertEqual(snapshot.call_count, 0 if release_tag else 1)
                    self.assertEqual(checks, ['working'])
                    self.assertEqual(probe.call_count, 2)
                    reception.assert_called_once()
                with zipfile.ZipFile(archive) as package:
                    selected = json.loads(package.read('NoNo/data/incoming/NoNo/selected.json'))
                    prefix = f"NoNo/data/incoming/NoNo/{selected['bundle_id']}/"
                    info = json.loads(package.read(prefix + 'root/build-info.json'))
                    self.assertEqual(info['source_sha'], source_sha)
                    self.assertEqual(info['base_update_commit'], base_info['base_update_commit'])
                    self.assertEqual(info['base_update_version'], release_tag or self.version)
                    self.assertEqual(package.read(prefix + 'python/installed-requirements.txt'),
                                     package.read(prefix + 'working/requirements.txt'))
                    expected_source = self.files['main.py'] if release_tag else b'current source snapshot'
                    self.assertEqual(package.read(prefix + 'working/main.py'), expected_source)

    def test_command_timeout_retains_bounded_text_bytes_and_empty_tails_without_credentials(self):
        stdout = ('不保留的日志头\n' + '中间日志\n' * 100
                  + '受控输出末尾 fixture-secret-value https://user:password@example.invalid\n')
        stderr = '受控错误末尾 token=fixture-query-secret\n'
        for kind in ('text', 'bytes', 'empty'):
            with self.subTest(kind=kind), patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret-value'}):
                output = stdout.encode() if kind == 'bytes' else stdout if kind == 'text' else None
                error = stderr.encode() if kind == 'bytes' else stderr if kind == 'text' else None
                timeout = subprocess.TimeoutExpired(['fixture-launcher.exe'], 1, output=output, stderr=error)
                with patch.object(self.portable.subprocess, 'run', side_effect=timeout):
                    with self.assertRaisesRegex(RuntimeError, 'fixture-launcher.exe 超时') as caught:
                        self.portable.run(['fixture-launcher.exe'], self.root, timeout=1)
                message = str(caught.exception)
                self.assertLess(len(message), 4200)
                self.assertNotIn('fixture-secret-value', message)
                self.assertNotIn('user:password', message)
                self.assertNotIn('fixture-query-secret', message)
                self.assertNotIn('不保留的日志头', message)
                if kind != 'empty':
                    self.assertIn('受控输出末尾', message)
                    self.assertIn('受控错误末尾', message)
                else:
                    self.assertIn('无输出', message)

    def test_real_short_command_timeout_preserves_controlled_stdout_and_stderr(self):
        code = (
            'import sys, time; print("受控中文输出", flush=True); '
            'print("受控中文错误", file=sys.stderr, flush=True); time.sleep(5)'
        )
        with self.assertRaisesRegex(RuntimeError, '超时') as caught:
            self.portable.run([sys.executable, '-u', '-c', code], self.root, timeout=1)
        self.assertIn('受控中文输出', str(caught.exception))
        self.assertIn('受控中文错误', str(caught.exception))

    def test_archive_rejects_missing_runtime_host_or_changed_current_deployment(self):
        bundle, manifest = self.prepare_bundle()
        output = self.root / 'artifacts'
        payload = self.portable.incoming_payload(bundle, manifest)
        for relative in ('python/python3.dll', 'working/selfhost/NoNo.exe'):
            with self.subTest(relative=relative):
                missing = payload / relative
                content = missing.read_bytes()
                missing.unlink()
                with self.assertRaisesRegex(RuntimeError, '载荷发生漂移'):
                    self.portable.archive_bundle(bundle, output, manifest, self.build_root)
                self.assertFalse(output.exists())
                missing.write_bytes(content)
        (payload / 'working/main.py').write_bytes(b'stale mirror source')
        with self.assertRaisesRegex(RuntimeError, '载荷发生漂移'):
            self.portable.archive_bundle(bundle, output, manifest, self.build_root)

    def test_archive_verification_rejects_stale_files_and_local_install_metadata(self):
        bundle, manifest = self.prepare_bundle()
        archive = self.portable.archive_bundle(bundle, self.root / 'artifacts', manifest, self.build_root)
        with zipfile.ZipFile(archive, 'a') as package:
            package.writestr('NoNo/data/apps/NoNo/working/obsolete.py', b'old mirror source')
        with self.assertRaisesRegex(RuntimeError, '用户状态文件'):
            self.portable.verify_archive(archive, 'NoNo', manifest)
        installation = self.root / 'installation/NoNo'
        metadata = installation / 'data/apps/NoNo/python/Lib/site-packages/sample.dist-info'
        metadata.mkdir(parents=True)
        (metadata / 'direct_url.json').write_text('{"url": "file:///runner/private/source"}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '临时来源'):
            self.portable.verify_portable_metadata(installation, self.root, 'NoNo')

    def test_same_name_installed_metadata_and_current_version_are_required(self):
        installation, info = self.prepare_installation()
        state = self.portable.read_app_state(installation, 'NoNo')
        repo = installation / 'data/apps/NoNo/repo'
        self.portable.assert_update_anchor(repo, state, self.version, info['base_update_commit'])
        metadata = installation / 'data/apps/NoNo/app.json'
        for changes, expected in (({'name': 'other'}, '名字或安装状态'), ({'current_version': 'dev'}, '合法')):
            with self.subTest(changes=changes):
                metadata.write_text(json.dumps({**state, **changes}), encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, expected):
                    self.portable.read_app_state(installation, 'NoNo')

    def prepare_installation(self):
        snapshot, digests = self.prepare_snapshot()
        installation = self.root / 'installation' / 'NoNo'
        app = installation / 'data/apps/NoNo'
        working, runtime = app / 'working', app / 'python'
        shutil.copytree(snapshot, working)
        runtime.mkdir()
        # 真实 wheel 同时存在大写包名和下划线目录，不能只用全小写空树掩盖 Windows 排序差异。
        (runtime / 'Lib/site-packages/PySide6').mkdir(parents=True)
        (runtime / 'Lib/site-packages/_fixture').mkdir()
        anchor = self.root / 'anchor'
        anchor.mkdir()
        (anchor / 'main.py').write_bytes(b'official base source')
        self.initialize_repository(anchor)
        self.portable.run(['git', 'clone', '--bare', '--no-hardlinks', str(anchor), str(app / 'repo')], self.root)
        self.portable.git(app / 'repo', 'remote', 'set-url', 'origin', 'https://github.com/Canda15/nono-update.git')
        # Git pack/对象通常只读，用受控HEAD覆盖同一属性边界，不读取真实安装。
        (app / 'repo/HEAD').chmod(stat.S_IRUSR)
        (installation / 'NoNo.exe').write_bytes(b'MZ-launcher')
        (installation / 'pyappify.yml').write_bytes(self.files['pyappify.yml'])
        for name, content in {'python.exe': b'MZ-python', 'python3.dll': b'portable python library',
                              'python312.dll': b'portable runtime'}.items():
            (runtime / name).write_bytes(content)
        (app / 'app.json').write_text(json.dumps({
            'name': 'NoNo', 'installed': True, 'current_profile': 'China', 'current_version': self.version,
            'auto_start': False, 'update_method': 'AUTO_UPDATE', 'update_state': 'idle',
            'profiles': [{'name': 'China', 'git_url': 'https://github.com/Canda15/nono-update.git',
                          'main_script': 'selfhost/NoNo.exe', 'requires_python': '3.12',
                          'requirements': 'requirements.txt', 'pip_args': '--no-deps'}],
        }), encoding='utf-8')
        self.launcher_proof = {
            'schema_version': 1, 'app_name': 'NoNo', 'version': '1.2.7',
            'source_sha': '31cd261eb8360ba2cf9ac63ec59fa5ef253cca2a',
            'action_sha': '6dcb02954bb5fab5b83e95f1af243078213ec1ab',
            'adaptation_sha256': 'a' * 64, 'source_tree_sha256': 'b' * 64,
            'exe_sha256': self.portable.sha256(installation / 'NoNo.exe'),
        }
        (installation / 'launcher-build.json').write_text(json.dumps(self.launcher_proof), encoding='utf-8')
        info = {'schema_version': 1, 'build_kind': 'commit_snapshot',
                'source_sha': self.portable.git(self.source, 'rev-parse', 'HEAD'),
                'base_update_version': self.version,
                'base_update_commit': self.portable.git(app / 'repo', 'rev-parse', 'HEAD'),
                'deployment_files': digests, 'app_metadata_file': 'app-seed.json'}
        return installation, info

    def test_zip_fallback_restores_empty_directories_and_private_state_is_rejected(self):
        bundle, manifest = self.prepare_bundle()
        from selfhost import portable_protocol
        # 单独屏蔽 7-Zip 探测，其余真实文件检查保持有效。
        original_is_file = Path.is_file
        with patch.object(Path, 'is_file', autospec=True,
                          side_effect=lambda path: False if path.name == '7z.exe' else original_is_file(path)):
            archive = self.portable.archive_bundle(bundle, self.root / 'fallback', manifest, self.build_root)
        extracted = self.root / 'extracted'
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        payload = self.portable.incoming_payload(bundle, manifest)
        restored = self.portable.incoming_payload(extracted / 'NoNo', manifest)
        self.assertEqual(portable_protocol.collect_tree(payload), portable_protocol.collect_tree(restored))
        source_head = self.root / 'installation/NoNo/data/apps/NoNo/repo/HEAD'
        self.assertFalse(source_head.stat().st_mode & stat.S_IWUSR)
        self.assertTrue((payload / 'repo/HEAD').stat().st_mode & stat.S_IWUSR)
        # 同包再次实际解压覆盖，原裸库仍只读，交付副本目录和文件身份保持一致。
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        self.assertEqual(portable_protocol.collect_tree(payload), portable_protocol.collect_tree(restored))
        for relative in ('data/config', 'root/private', 'working/configs', 'working/Configs',
                         'working/LOGS/empty', 'working/.ok_cache', 'python/.PIP_UPDATE_NEEDED.tmp'):
            with self.subTest(directory=relative):
                changed = {**manifest, 'payload_directories': sorted({*manifest['payload_directories'], relative})}
                changed = portable_protocol.seal_manifest(changed)
                with self.assertRaises(RuntimeError):
                    portable_protocol.validate_manifest(changed)
        for relative in ('working/Configs/private.json', 'working/.ok_cache/schedule_tasks_cache.json',
                         'working/.PIP_UPDATE_NEEDED.tmp'):
            with self.subTest(file=relative):
                changed = {**manifest, 'payload_files': {**manifest['payload_files'], relative: {
                    'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}}}
                changed = portable_protocol.seal_manifest(changed)
                with self.assertRaises(RuntimeError):
                    portable_protocol.validate_manifest(changed)
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(nonfinite=value), self.assertRaises(ValueError):
                portable_protocol.json_bytes({'unknown': {'nested': value}})
        for relative in ('data/apps/NoNo/app.json', 'data/config/app_config.json', 'data/user/NoNo/configs/account.json',
                         'data/state/NoNo/receive.json', 'data/backups/NoNo/original.json', 'pyappify.yml'):
            with self.subTest(relative=relative):
                unsafe = self.root / 'unsafe.zip'
                shutil.copy2(archive, unsafe)
                with zipfile.ZipFile(unsafe, 'a') as package:
                    package.writestr('NoNo/' + relative, b'private user original')
                with self.assertRaises(RuntimeError):
                    self.portable.verify_archive(unsafe, 'NoNo', manifest)


@unittest.skipUnless(sys.platform == 'win32' and
                     (Path(__file__).resolve().parents[1] / 'selfhost' / 'NoNo.exe').is_file(),
                     '需先运行 scripts/build_runtime.ps1 编译原生宿主')
class TestNativeRuntime(unittest.TestCase):
    """真实启动编译后的 EXE，覆盖进程身份及既有启动参数的边界。"""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        build_root = cls.repo_root / 'build'
        build_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='原生宿主 验证-', dir=build_root)
        cls.root = Path(temporary.name).resolve()
        if cls.root.parent != build_root.resolve():
            raise ValueError('宿主验证目录必须位于项目 build 目录内')
        cls.addClassCleanup(temporary.cleanup)
        cls.working = cls.root / 'app' / 'working'
        cls.runtime = cls.root / 'app' / 'python'
        (cls.working / 'selfhost').mkdir(parents=True)
        cls.runtime.mkdir(parents=True)
        cls.executable = cls.working / 'selfhost' / 'NoNo.exe'
        shutil.copy2(cls.repo_root / 'selfhost' / 'NoNo.exe', cls.executable)
        shutil.copy2(cls.repo_root / 'selfhost/user_data.py', cls.working / 'selfhost/user_data.py')
        (cls.working / 'selfhost/__init__.py').write_bytes(b'')

        # 复制与 pyappify 相同的运行时目录布局，不能用 _pth 指回本机标准库，
        # 否则会掩盖宿主的 PYTHONHOME 和模块搜索路径错误。测试树不含目录联接。
        base = Path(sys.base_prefix)
        for pattern in ('python3*.dll', 'vcruntime*.dll'):
            for library in base.glob(pattern):
                shutil.copy2(library, cls.runtime / library.name)
        shutil.copy2(base / 'python.exe', cls.runtime / 'python.exe')
        for folder in ('Lib', 'DLLs'):
            shutil.copytree(base / folder, cls.runtime / folder,
                            ignore=shutil.ignore_patterns('__pycache__', 'site-packages', 'test'))
        (cls.working / 'native_fixture.py').write_text(
            'TOKEN = "中文模块加载成功"\n'
            'if __name__ == "__main__":\n'
            '    import json, sys\n'
            '    print(json.dumps({"executable": sys.executable}))\n', encoding='utf-8')
        (cls.working / 'main.py').write_text(
            'import json, os, sys\n'
            'from native_fixture import TOKEN\n'
            'print(json.dumps({"argv": sys.argv, "executable": sys.executable, '
            '"pid": os.getpid(), "cwd": os.getcwd(), "module": TOKEN}, ensure_ascii=False))\n',
            encoding='utf-8')

    def run_native(self, *arguments, cwd=None):
        return subprocess.run([str(self.executable), *arguments],
                              cwd=cwd or self.working, capture_output=True,
                              text=True, encoding='utf-8', timeout=15,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})

    def read_native_manifest(self, executable, *, remove=False):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
        kernel.LoadLibraryExW.restype = wintypes.HMODULE
        kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
        kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
        kernel.FindResourceW.restype = ctypes.c_void_p
        kernel.LoadResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
        kernel.LoadResource.restype = ctypes.c_void_p
        kernel.LockResource.argtypes = [ctypes.c_void_p]
        kernel.LockResource.restype = ctypes.c_void_p
        kernel.SizeofResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
        kernel.SizeofResource.restype = wintypes.DWORD
        module = kernel.LoadLibraryExW(str(executable), None, 0x2 | 0x20)
        self.assertTrue(module)
        languages = []
        try:
            resource = kernel.FindResourceW(module, 1, 24)
            if not resource:
                return b''
            content = ctypes.string_at(kernel.LockResource(kernel.LoadResource(module, resource)),
                                       kernel.SizeofResource(module, resource))
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                               ctypes.c_void_p, wintypes.WORD, ctypes.c_ssize_t)
            callback = callback_type(lambda module, kind, name, language, extra: languages.append(language) or True)
            kernel.EnumResourceLanguagesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
                                                      callback_type, ctypes.c_ssize_t]
            self.assertTrue(kernel.EnumResourceLanguagesW(module, 24, 1, callback, 0))
        finally:
            kernel.FreeLibrary(module)
        if remove:
            # 只移除隔离 EXE 的清单，保留代码和其它资源，证明核心不依赖进程长路径 opt-in。
            kernel.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
            kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
            kernel.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                               wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
            kernel.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
            update = kernel.BeginUpdateResourceW(str(executable), False)
            self.assertTrue(update)
            try:
                for language in languages:
                    self.assertTrue(kernel.UpdateResourceW(update, 24, 1, language, None, 0))
            except BaseException:
                kernel.EndUpdateResourceW(update, True)
                raise
            self.assertTrue(kernel.EndUpdateResourceW(update, False))
        return content

    def test_native_manifest_enables_long_paths_without_changing_elevation(self):
        manifest = self.read_native_manifest(self.executable)
        self.assertTrue(manifest, '原生宿主必须实际内嵌应用清单')
        root = ET.fromstring(manifest)
        self.assertEqual(root.find('.//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}longPathAware').text, 'true')
        self.assertEqual(root.find('.//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel').get('level'), 'asInvoker')

    def test_deep_native_migration_preserves_real_assets_without_manifest_opt_in(self):
        from selfhost.portable import process_environment, hidden_process_options
        relative = 'assets/templates/cancel_button_highlight_hcenter_vcenter_3840x2160_914_1255.png'
        contents = (self.repo_root / relative).read_bytes()
        for without_manifest in (False, True):
            with self.subTest(without_manifest=without_manifest):
                base = self.root / 'deep-deployment-' / str(without_manifest) / 'NoNo'
                padding = 280 - len(str(base / 'data/apps/NoNo/working' / relative))
                self.assertGreater(padding, 0)
                portable = self.root / ('deep-deployment-' + 'x' * padding) / str(without_manifest) / 'NoNo'
                working = portable / 'data/apps/NoNo/working'
                shutil.copytree(self.working, Path('\\\\?\\' + str(working)))
                shutil.copytree(self.runtime, Path('\\\\?\\' + str(portable / 'data/apps/NoNo/python')))
                executable = working / 'selfhost/NoNo.exe'
                if without_manifest:
                    self.read_native_manifest(executable, remove=True)
                    self.assertEqual(self.read_native_manifest(executable), b'')
                original = working / relative
                extended_original = Path('\\\\?\\' + str(original))
                extended_original.parent.mkdir(parents=True)
                extended_original.write_bytes(contents)
                (working / 'configs').mkdir()
                (working / 'configs/account.json').write_bytes(b'{"enabled":false}')
                self.assertGreater(len(str(original)), 260)
                code = ('import json,os; from pathlib import Path; '
                        'print((Path(os.environ["NONO_USER_DATA_ROOT"])/"configs/account.json").read_text())')
                result = subprocess.run([str(executable), '-c', code], cwd=working, env=process_environment(),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                        timeout=45, **hidden_process_options())
                error_path = portable / 'data/state/NoNo/user-data-error.json'
                error = error_path.read_text(encoding='utf-8') if error_path.exists() else ''
                self.assertEqual(result.returncode, 0, result.stderr + error)
                self.assertEqual(json.loads(result.stdout), {'enabled': False})
                receipt = json.loads((portable / 'data/state/NoNo/user-data.json').read_bytes())
                protected = portable / receipt['sources'][0]['path'] / relative
                self.assertGreater(len(str(protected)), 260)
                self.assertEqual(Path('\\\\?\\' + str(protected)).read_bytes(), contents)
                self.assertEqual(extended_original.read_bytes(), contents)

    def test_launcher_uses_native_main_entry(self):
        """宿主编出来却没被启动器使用，仍然会出现原来的 Python 进程。"""
        config = (self.repo_root / 'pyappify.yml').read_text(encoding='utf-8')
        self.assertRegex(config, r'main_script:\s*"selfhost/NoNo\.exe"')

    def test_business_runs_in_native_process(self):
        code = (
            'import ctypes, json, os, sys; '
            'path = ctypes.create_unicode_buffer(32768); '
            'ctypes.windll.kernel32.GetModuleFileNameW(None, path, len(path)); '
            'print(json.dumps({"pid": os.getpid(), "executable": sys.executable, '
            '"image": path.value, "prefix": sys.prefix}))'
        )
        with subprocess.Popen([str(self.executable), '-c', code], cwd=self.working,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding='utf-8') as process:
            try:
                output, error = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            self.assertEqual(process.returncode, 0, error)
            identity = json.loads(output)
            self.assertEqual(identity['pid'], process.pid, '业务不能另起 Python 子进程')
        for key in ('executable', 'image'):
            self.assertEqual(Path(identity[key]).resolve(), self.executable.resolve())
        self.assertEqual(Path(identity['prefix']).resolve(), self.runtime)

        # 同一真实运行时也覆盖打包探针的等待与参数传递，路径含中文和空格。
        from selfhost.portable import probe_executable
        identity = probe_executable(self.executable, self.working, self.runtime, check_deployment=False)
        self.assertEqual(Path(identity['image']).resolve(), self.executable.resolve())
        self.assertEqual(Path(identity['prefix']).resolve(), self.runtime)
        self.assertEqual(identity['sources'], {})

    def test_task_arguments_and_resources_survive_different_working_directory(self):
        result = self.run_native('-t', '角色 名称', '-e', '-h', cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state['argv'][1:], ['-t', '角色 名称', '-e', '-h'])
        self.assertEqual(Path(state['argv'][0]).resolve(), self.working / 'main.py')
        self.assertEqual(Path(state['cwd']).resolve(), self.working)
        self.assertEqual(state['module'], '中文模块加载成功')

    def test_scheduled_invocation_keeps_explicit_main_script(self):
        result = self.run_native('main.py', '-t', '1', '-e')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['argv'], ['main.py', '-t', '1', '-e'])

    def test_interpreter_flags_and_module_subprocess_remain_compatible(self):
        code = (
            'import subprocess, sys; '
            'result = subprocess.run([sys.executable, "-m", "native_fixture"], '
            'capture_output=True, text=True, check=True); print(result.stdout)'
        )
        result = self.run_native('-u', '-c', code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)['executable']).resolve(), self.executable)
        result = self.run_native('-c', 'raise SystemExit(7)')
        self.assertEqual(result.returncode, 7, '启动器必须收到业务的真实退出码')

    def test_missing_runtime_reports_failure_without_starting_system_python(self):
        library = self.runtime / 'python3.dll'
        backup = self.runtime / 'python3.dll.disabled'
        library.rename(backup)
        try:
            result = self.run_native()
            self.assertEqual(result.returncode, 1)
            self.assertIn('无法加载程序运行时', result.stderr)
        finally:
            backup.rename(library)

    def test_portable_direct_entry_derives_user_root_and_blocks_pending_or_busy_installation(self):
        import ctypes
        from ctypes import wintypes
        from selfhost.portable import process_environment, hidden_process_options
        portable = self.root / 'portable-direct/NoNo'
        working = portable / 'data/apps/NoNo/working'
        runtime = portable / 'data/apps/NoNo/python'
        shutil.copytree(self.working, working)
        shutil.copytree(self.runtime, runtime)
        executable = working / 'selfhost/NoNo.exe'
        code = ('import json,os,subprocess,sys; '
                'child=subprocess.run([sys.executable,"-c","import os; print(os.getenv(\\\"NONO_USER_DATA_ROOT\\\"))"],'
                'capture_output=True,text=True,check=True); '
                'print(json.dumps({"root":os.getenv("NONO_USER_DATA_ROOT"),"child":child.stdout.strip()}))')
        environment = process_environment()
        self.assertNotIn('NONO_USER_DATA_ROOT', environment)
        def invoke(script=code):
            return subprocess.run([str(executable), '-c', script], cwd=self.root, env=environment,
                                  capture_output=True, text=True, encoding='utf-8', timeout=30,
                                  **hidden_process_options())
        result = invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = json.loads(result.stdout)
        expected = (portable / 'data/user/NoNo').resolve()
        self.assertEqual(Path(identity['root']).resolve(), expected)
        self.assertEqual(Path(identity['child']).resolve(), expected)
        pending = portable / 'data/state/NoNo/receive.pending'
        pending.write_bytes(b'pending receive')
        refused = invoke('print("不能执行此业务")')
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn('不能执行此业务', refused.stdout)
        pending.unlink()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        lease = kernel.CreateFileW(str(portable / 'data/state/NoNo/runtime.lock'), 0x80000000 | 0x40000000,
                                   0, None, 3, 0x80, None)
        self.assertNotEqual(lease, wintypes.HANDLE(-1).value)
        try:
            refused = invoke('print("不能在交换期间执行")')
            self.assertNotEqual(refused.returncode, 0)
            self.assertNotIn('不能在交换期间执行', refused.stdout)
        finally:
            kernel.CloseHandle(lease)

    def test_legacy_native_migrates_before_any_business_code(self):
        from selfhost.portable import process_environment, hidden_process_options

        portable = self.root / 'legacy-upgrade/NoNo'
        working = portable / 'data/apps/NoNo/working'
        runtime = portable / 'data/apps/NoNo/python'
        shutil.copytree(self.working, working)
        shutil.copytree(self.runtime, runtime)
        core = self.repo_root / 'selfhost/user_data.py'
        if core.exists():
            shutil.copy2(core, working / 'selfhost/user_data.py')
        (working / 'selfhost/__init__.py').write_bytes(b'')
        source = working / 'configs/custom_teams/old-team'
        source.mkdir(parents=True)
        (source / 'team.json').write_bytes(b'{"name":"old-team","mode":"custom"}')
        (source / 'SyntheticChar.py').write_bytes(b'class SyntheticChar: pass\n')
        (working / 'captures').mkdir()
        (working / 'captures/old.bin').write_bytes(b'old capture')
        executable = working / 'selfhost/NoNo.exe'
        code = (
            'import json,os; from pathlib import Path; '
            'root=Path(os.environ["NONO_USER_DATA_ROOT"]); '
            'print(json.dumps({"team":json.loads((root/"configs/custom_teams/old-team/team.json").read_bytes()),'
            '"code":(root/"configs/custom_teams/old-team/SyntheticChar.py").read_text()}))'
        )
        result = subprocess.run([str(executable), '-c', code], cwd=self.root,
                                env=process_environment(), capture_output=True, text=True,
                                encoding='utf-8', timeout=45, **hidden_process_options())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['team']['mode'], 'custom')
        self.assertEqual(json.loads(result.stdout)['code'], 'class SyntheticChar: pass\n')

    def test_concurrent_native_handoff_keeps_named_helpers_and_runtime_lease(self):
        import time
        import psutil
        from selfhost import user_data
        from selfhost.portable import process_environment, hidden_process_options

        portable = self.root / 'concurrent/NoNo'
        working = portable / 'data/apps/NoNo/working'
        shutil.copytree(self.working, working)
        shutil.copytree(self.runtime, portable / 'data/apps/NoNo/python')
        (working / 'captures').mkdir()
        (working / 'captures/old.bin').write_bytes(b'original capture')
        executable = working / 'selfhost/NoNo.exe'
        processes = []
        receiver = user_data.FileLease(portable, 'receive.lock')
        try:
            for index in range(2):
                ready = self.root / f'concurrent-ready-{index}'
                code = (
                    'import json,runpy,sys; from pathlib import Path; '
                    f'api=runpy.run_path({str(self.repo_root / "ok/util/file.py")!r}); '
                    'capture=Path(api["get_user_data_path"]("captures"))/"old.bin"; '
                    'print(json.dumps({"capture":capture.read_text()}),flush=True); '
                    f'Path({str(ready)!r}).write_bytes(b"ready"); sys.stdin.read(1)'
                )
                processes.append(subprocess.Popen([str(executable), '-c', code], cwd=self.root,
                                                   env=process_environment(), stdin=subprocess.PIPE,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   text=True, encoding='utf-8', **hidden_process_options()))
            deadline = time.monotonic() + 10
            children = []
            while time.monotonic() < deadline:
                children = [child for process in processes for child in psutil.Process(process.pid).children()]
                if len(children) == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(len(children), 2)
            self.assertTrue(all(Path(child.exe()).resolve() == executable.resolve() for child in children))
            receiver.close()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if all((self.root / f'concurrent-ready-{index}').exists() for index in range(2)):
                    break
                if any(process.poll() is not None for process in processes):
                    break
                time.sleep(0.02)
            self.assertTrue(all((self.root / f'concurrent-ready-{index}').exists() for index in range(2)))
            with self.assertRaisesRegex(RuntimeError, 'USER_LEASE_BUSY'):
                user_data.FileLease(portable, 'runtime.lock')
            receipt = json.loads((portable / 'data/state/NoNo/user-data.json').read_bytes())
            self.assertEqual(len(receipt['sources']), 1)
            self.assertIn('captures', receipt['roots'])
            for process in processes:
                output, error = process.communicate('A', timeout=15)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(json.loads(output)['capture'], 'original capture')
            with user_data.FileLease(portable, 'runtime.lock'):
                pass
        finally:
            receiver.close()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_internal_native_mode_cannot_execute_business_arguments(self):
        result = self.run_native('--nono-bootstrap-helper', '-c', 'print("不应执行")')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('不应执行', result.stdout)


class TestUserDataMigration(unittest.TestCase):
    """真实旧布局、稳定根和持久化边界，不创建游戏运行时。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='用户迁移-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'NoNo'
        self.working = self.root / 'data/apps/NoNo/working'
        self.user = self.root / 'data/user/NoNo'
        self.working.mkdir(parents=True)
        self.environment = patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(self.user)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, base, relative, data):
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def prepare(self):
        from selfhost.user_data import bootstrap_user_data
        context = bootstrap_user_data(self.working)
        self.addCleanup(context.close)
        return context

    def test_explicit_external_project_keeps_its_own_user_paths(self):
        from ok.util.file import get_user_data_path
        external = Path(self.temporary.name).resolve() / 'other-project'
        original = self.write(external, 'captures/old.bin', b'external capture')
        before = original.stat()
        self.assertEqual(Path(get_user_data_path('captures', base_path=external)), external / 'captures')
        self.assertEqual(original.read_bytes(), b'external capture')
        self.assertEqual(original.stat().st_mtime_ns, before.st_mtime_ns)

    def test_fixed_roots_and_requested_custom_root_survive_deleted_working(self):
        from ok.util.file import get_user_data_path, read_json_file
        for relative, data in {
            'configs/Basic Options.json': b'{"enabled":false,"unknown":{"count":0,"value":null}}',
            'ok_tasks/manifest.json': b'{"scripts":{}}',
            'ok_import/sample/manifest.json': b'{"id":"sample"}',
            'ok_templates/coco_annotations.json': b'{"images":[],"annotations":[],"categories":[]}',
            'screenshots/old.bin': b'old screenshot', 'logs/old.log': b'old log',
            '.ok_cache/schedule_tasks_cache.json': b'{"tasks":[]}',
            'captures/session/old.bin': b'old capture',
        }.items():
            self.write(self.working, relative, data)
        context = self.prepare()
        data = read_json_file(get_user_data_path('configs', 'Basic Options.json'), strict=True)
        self.assertEqual(data, {'enabled': False, 'unknown': {'count': 0, 'value': None}})
        self.assertEqual((self.user / '.ok_cache/schedule_tasks_cache.json').read_bytes(), b'{"tasks":[]}')
        shutil.rmtree(self.working / 'captures')
        path = Path(get_user_data_path('captures/session'))
        self.assertEqual((path / 'old.bin').read_bytes(), b'old capture')
        (path / 'old.bin').unlink()
        context.close()
        self.prepare()
        self.assertFalse((Path(get_user_data_path('captures/session')) / 'old.bin').exists())

    def test_conflicting_teams_and_plugin_transactions_are_complete_units(self):
        self.write(self.user, 'configs/custom_teams/same/A.py', b'stable A')
        self.write(self.working, 'configs/custom_teams/same/A.py', b'old A')
        self.write(self.working, 'configs/custom_teams/same/B.py', b'old B')
        self.write(self.working, 'configs/custom_teams/same/team.json', b'{"mode":"custom"}')
        self.write(self.user, 'ok_import/sample/stable.py', b'stable plugin')
        self.write(self.working, 'ok_import/sample/old.py', b'old plugin')
        self.write(self.working, 'ok_import/.plugin-staging/sample/operation.json', b'{"action":"install"}')
        self.write(self.working, 'ok_import/.plugin-staging/sample/previous/old.py', b'previous plugin')
        self.prepare()
        self.assertEqual((self.user / 'configs/custom_teams/same/A.py').read_bytes(), b'stable A')
        self.assertFalse((self.user / 'configs/custom_teams/same/B.py').exists())
        self.assertFalse((self.user / 'ok_import/sample/old.py').exists())
        self.assertFalse((self.user / 'ok_import/.plugin-staging/sample').exists())
        receipt = json.loads((self.root / 'data/state/NoNo/user-data.json').read_bytes())
        conflicts = [entry for entry in receipt['conflicts'] if entry['root'] in ('configs', 'ok_import')]
        self.assertEqual(len(conflicts), 2)
        for entry in conflicts:
            self.assertTrue((self.root / entry['source']).exists())

    def test_staging_only_source_cannot_join_a_stable_plugin(self):
        from ok.core.plugin_staging import apply_pending_plugin_changes
        self.write(self.user, 'ok_import/sample/current.py', b'stable selected plugin')
        operation = {'namespace': 'sample', 'action': 'delete', 'phase': 'applying', 'had_previous': True}
        self.write(self.working, 'ok_import/.plugin-staging/sample/operation.json', json.dumps(operation).encode())
        source = self.write(self.working, 'ok_import/.plugin-staging/sample/previous/current.py', b'old deleted plugin')
        # 不同 namespace 仍可迁入，缺少 staging 的来源成员不能阻止普通插件复制。
        self.write(self.working, 'ok_import/new/current.py', b'new independent plugin')
        self.prepare()
        staged = self.user / 'ok_import/.plugin-staging/sample'
        self.assertFalse(staged.exists())
        results = apply_pending_plugin_changes(import_base=self.user / 'ok_import')
        self.assertNotIn('sample', results)
        self.assertEqual((self.user / 'ok_import/sample/current.py').read_bytes(), b'stable selected plugin')
        self.assertEqual((self.user / 'ok_import/new/current.py').read_bytes(), b'new independent plugin')
        self.assertEqual(source.read_bytes(), b'old deleted plugin')
        receipt = json.loads((self.root / 'data/state/NoNo/user-data.json').read_bytes())
        self.assertEqual([entry['unit'] for entry in receipt['conflicts'] if entry['root'] == 'ok_import'],
                         [['sample', '.plugin-staging/sample']])

    def test_source_plugin_cannot_join_a_stable_staging_only_transaction(self):
        from ok.core.plugin_staging import apply_pending_plugin_changes
        operation = {'namespace': 'sample', 'action': 'delete', 'phase': 'applying', 'had_previous': True}
        self.write(self.user, 'ok_import/.plugin-staging/sample/operation.json', json.dumps(operation).encode())
        self.write(self.user, 'ok_import/.plugin-staging/sample/previous/current.py', b'stable deletion original')
        self.write(self.working, 'ok_import/sample/current.py', b'old unrelated plugin')
        self.prepare()
        self.assertFalse((self.user / 'ok_import/sample').exists())
        apply_pending_plugin_changes(import_base=self.user / 'ok_import')
        self.assertFalse((self.user / 'ok_import/sample').exists())
        self.assertFalse((self.user / 'ok_import/.plugin-staging/sample').exists())
        self.assertEqual((self.working / 'ok_import/sample/current.py').read_bytes(), b'old unrelated plugin')

    def receive_protected_sources(self):
        import _winapi
        import msvcrt
        from selfhost import user_data
        transaction = '1234567890abcdef-12345'
        app = self.root / user_data.APP_PATH
        original = self.root / f'data/backups/NoNo/{transaction}/original/{user_data.APP_PATH}'

        def borrowed_stream(lease):
            handle = _winapi.DuplicateHandle(_winapi.GetCurrentProcess(), lease.handle,
                                             _winapi.GetCurrentProcess(), 0, False, _winapi.DUPLICATE_SAME_ACCESS)
            return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY), 'r+b')

        with ExitStack() as stack:
            receiver = stack.enter_context(user_data.FileLease(self.root, 'receive.lock'))
            idle = stack.enter_context(user_data.FileLease(self.root, 'runtime.lock'))
            stamp = user_data._stamp(self.root, app)
            user_data._copy(self.root, app, original, stamp)
            user_data._write_json(self.root, self.root / 'data/state/NoNo/receive.json', {
                'schema_version': 1, 'transaction_id': transaction, 'stage': 'originals_protected',
                'originals': {user_data.APP_PATH: stamp},
            })
            core = Path(__file__).resolve().parents[1] / 'selfhost/user_data.py'
            return subprocess.run([sys.executable, '-I', '-S', '-B', '-X', 'utf8', str(core),
                                   '--receive', str(self.root), '--transaction', transaction],
                                  stdin=stack.enter_context(borrowed_stream(receiver)),
                                  stdout=stack.enter_context(borrowed_stream(idle)), stderr=subprocess.PIPE,
                                  cwd=self.root, text=True, encoding='utf-8', timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW)

    def test_interrupted_capture_then_receive_produces_a_loadable_receipt(self):
        from selfhost import user_data
        from ok.util.file import get_user_data_path, read_json_file
        account = self.write(self.working, 'configs/account.json', b'{"enabled":false}')
        self.write(self.working, 'z-later/old.bin', b'late requested original')
        copy = user_data._copy

        def interrupt(root, source, destination, expected):
            if source.name == 'old.bin':
                raise OSError('合成封存中断')
            return copy(root, source, destination, expected)

        with patch.object(user_data, '_copy', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, '合成封存中断'):
                self.prepare()
        receipt_path = self.root / 'data/state/NoNo/user-data.json'
        before = json.loads(receipt_path.read_bytes())
        self.assertFalse(before['initialized'])
        self.assertTrue(before['capture'])
        captured = self.root / before['capture'][0]['path']
        self.assertEqual((captured / 'configs/account.json').read_bytes(), account.read_bytes())
        result = self.receive_protected_sources()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.prepare()
        after = json.loads(receipt_path.read_bytes())
        self.assertIsNone(after['capture'])
        self.assertTrue(after['initialized'])
        self.assertEqual(read_json_file(get_user_data_path('configs/account.json'), strict=True), {'enabled': False})
        shutil.rmtree(self.working / 'z-later')
        self.assertEqual((Path(get_user_data_path('z-later')) / 'old.bin').read_bytes(), b'late requested original')

    def test_changed_source_keeps_interrupted_capture_on_receiver_failure(self):
        from selfhost import user_data
        source = self.write(self.working, 'configs/account.json', b'{"enabled":false}')
        with patch.object(user_data, '_copy', side_effect=OSError('合成封存中断')):
            with self.assertRaisesRegex(OSError, '合成封存中断'):
                self.prepare()
        receipt = self.root / 'data/state/NoNo/user-data.json'
        before = receipt.read_bytes()
        source.write_bytes(b'{"enabled":true}')
        result = self.receive_protected_sources()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse((self.user / 'configs/account.json').exists())
        self.assertEqual(source.read_bytes(), b'{"enabled":true}')

    def test_unicode_requested_root_keeps_its_name_and_completed_identity(self):
        from ok.util.file import get_user_data_path
        self.write(self.working, 'Straße/session/old.bin', b'custom captured bytes')
        context = self.prepare()
        requested = Path(get_user_data_path('Straße/session'))
        self.assertEqual((requested / 'old.bin').read_bytes(), b'custom captured bytes')
        self.assertFalse((self.user / 'strasse').exists())
        alternate = Path(get_user_data_path('STRAßE/session'))
        self.assertTrue(requested.samefile(alternate))
        (requested / 'old.bin').unlink()
        context.close()
        self.prepare()
        self.assertFalse((Path(get_user_data_path('straße/session')) / 'old.bin').exists())
        receipt = json.loads((self.root / 'data/state/NoNo/user-data.json').read_bytes())
        self.assertIn('Straße', receipt['roots'])
        self.assertNotIn('strasse', receipt['roots'])

    def test_distinct_unicode_roots_have_independent_sources_and_receipts(self):
        from ok.util.file import get_user_data_path
        names = ('Straße', 'strasse', 'İstanbul', 'i\u0307stanbul')
        for index, name in enumerate(names):
            self.write(self.working, f'{name}/old.bin', str(index).encode())
        self.prepare()
        for index, name in enumerate(names):
            self.assertEqual((Path(get_user_data_path(name)) / 'old.bin').read_bytes(), str(index).encode())
        receipt = json.loads((self.root / 'data/state/NoNo/user-data.json').read_bytes())
        self.assertTrue(set(names) <= set(receipt['roots']))

    def test_bad_json_preserves_source_and_prevents_any_publication(self):
        source = self.write(self.working, 'configs/custom_teams/old/team.json', b'{broken')
        self.write(self.working, 'configs/Basic Options.json', b'{"enabled":false}')
        with self.assertRaisesRegex(RuntimeError, 'JSON'):
            self.prepare()
        self.assertEqual(source.read_bytes(), b'{broken')
        self.assertFalse((self.user / 'configs/Basic Options.json').exists())
        snapshots = list((self.root / 'data/backups/NoNo').rglob('team.json'))
        self.assertTrue(snapshots)
        self.assertTrue(all(path.read_bytes() == b'{broken' for path in snapshots))

    def test_interrupted_publication_resumes_only_the_recorded_directory_identity(self):
        from selfhost import user_data
        old = self.write(self.working, 'configs/old.json', b'{"enabled":false}')
        stable = self.write(self.user, 'configs/stable.json', b'{"value":0}')
        move = user_data._move

        def interrupted(root, source, destination, **kwargs):
            move(root, source, destination, **kwargs)
            if destination == self.user / 'configs':
                raise OSError('合成目录发布后中断')

        with patch.object(user_data, '_move', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, '合成目录发布后中断'):
                self.prepare()
        receipt = json.loads((self.root / 'data/state/NoNo/user-data.json').read_bytes())
        self.assertIsNotNone(receipt['pending'])
        self.assertEqual(old.read_bytes(), b'{"enabled":false}')
        stable.write_bytes(b'{"value":99}')
        with self.assertRaisesRegex(RuntimeError, 'USER_RECOVERY_IDENTITY'):
            self.prepare()
        self.assertEqual(stable.read_bytes(), b'{"value":99}')
        stable.write_bytes(b'{"value":0}')
        self.prepare()
        self.assertEqual((self.user / 'configs/old.json').read_bytes(), old.read_bytes())
        receipt = json.loads((self.root / 'data/state/NoNo/user-data.json').read_bytes())
        self.assertIsNone(receipt['pending'])

    def test_locked_original_fails_without_defaults_and_can_retry(self):
        from selfhost import user_data
        source = self.write(self.working, 'configs/account.json', b'{"accounts":["synthetic"]}')
        handle = user_data._open_handle(source)
        try:
            with self.assertRaises((OSError, RuntimeError)):
                self.prepare()
            self.assertFalse((self.user / 'configs/account.json').exists())
        finally:
            user_data._kernel().CloseHandle(handle)
        self.prepare()
        self.assertEqual((self.user / 'configs/account.json').read_bytes(), source.read_bytes())

    def test_fixed_user_root_file_conflict_fails_before_config_can_replace_it(self):
        source = self.write(self.working, 'configs/account.json', b'{"enabled":false}')
        original = self.write(self.user, 'configs', b'original file at the configuration root')
        with self.assertRaisesRegex(RuntimeError, 'USER_DIRECTORY_REQUIRED'):
            self.prepare()
        self.assertEqual(original.read_bytes(), b'original file at the configuration root')
        self.assertEqual(source.read_bytes(), b'{"enabled":false}')

    def test_structurally_invalid_receipts_cannot_claim_completed_migration(self):
        self.write(self.working, 'configs/old.json', b'{"enabled":false}')
        base = {'schema_version': 1, 'initialized': True, 'sources': [],
                'roots': {}, 'conflicts': [], 'capture': None, 'pending': None}
        from selfhost.user_data import FIXED_USER_ROOTS
        invalid = [
            {**base, 'schema_version': True},
            {**base, 'roots': {name: {} for name in FIXED_USER_ROOTS}},
            {**base, 'capture': [{'source': '../../outside'}]},
            {**base, 'pending': {'roots': list(FIXED_USER_ROOTS)}},
            {**base, 'sources': [{'kind': 'working', 'path': '../../outside', 'tree': {}}]},
        ]
        for state in invalid:
            with self.subTest(state=state):
                original = json.dumps(state).encode()
                receipt = self.write(self.root, 'data/state/NoNo/user-data.json', original)
                with self.assertRaises(RuntimeError):
                    self.prepare()
                self.assertEqual(receipt.read_bytes(), original)
                self.assertFalse((self.user / 'configs/old.json').exists())

    def test_legacy_accepted_receipt_preserves_deletions_and_keeps_custom_sources(self):
        from selfhost.portable_protocol import collect_tree
        from ok.util.file import get_user_data_path
        transaction = '1234567890abcdef-12345'
        original_app = self.root / f'data/backups/NoNo/{transaction}/original/data/apps/NoNo'
        self.write(original_app, 'working/configs/deleted.json', b'{"old":true}')
        self.write(original_app, 'working/captures/old.bin', b'protected custom capture')
        files, directories = collect_tree(original_app)
        accepted = {'schema_version': 1, 'transaction_id': transaction, 'bundle_id': 'a' * 64}
        proof = {**accepted, 'complete': True,
                 'originals': {'data/apps/NoNo': {'kind': 'directory',
                                                'stamp': {'files': files, 'directories': directories}}}}
        self.write(self.root, 'data/state/NoNo/accepted.json', json.dumps(accepted).encode())
        self.write(self.root, f'data/backups/NoNo/{transaction}/protection.json', json.dumps(proof).encode())
        context = self.prepare()
        self.assertFalse((self.user / 'configs/deleted.json').exists())
        self.assertEqual((Path(get_user_data_path('captures')) / 'old.bin').read_bytes(), b'protected custom capture')
        context.close()
        (self.root / 'data/state/NoNo/user-data.json').unlink()
        self.write(self.root, 'data/state/NoNo/accepted.json', json.dumps({**accepted, 'schema_version': True}).encode())
        with self.assertRaisesRegex(RuntimeError, 'USER_PREVIOUS_RECEIPT_INVALID'):
            self.prepare()
        self.write(self.root, 'data/state/NoNo/accepted.json',
                   json.dumps({**accepted, 'user_data_schema_version': 1}).encode())
        with self.assertRaisesRegex(RuntimeError, 'USER_RECEIPT_MISSING'):
            self.prepare()

    def test_portable_directory_move_keeps_relative_receipts_and_late_migration(self):
        from ok.util.file import get_user_data_path
        self.write(self.working, 'configs/old.json', b'{"enabled":false}')
        self.write(self.working, 'captures/old.bin', b'move-safe original')
        context = self.prepare()
        context.close()
        previous = self.root
        destination = self.root.parent / 'moved NoNo'
        self.root.rename(destination)
        self.root = destination
        self.working = destination / 'data/apps/NoNo/working'
        self.user = destination / 'data/user/NoNo'
        os.environ['NONO_USER_DATA_ROOT'] = str(self.user)
        shutil.rmtree(self.working / 'captures')
        self.prepare()
        self.assertEqual((Path(get_user_data_path('captures')) / 'old.bin').read_bytes(), b'move-safe original')
        receipt = (self.root / 'data/state/NoNo/user-data.json').read_text(encoding='utf-8')
        self.assertNotIn(str(previous), receipt)

    def test_receiver_rejects_pipe_handles_as_borrowed_locks(self):
        core = Path(__file__).resolve().parents[1] / 'selfhost/user_data.py'
        result = subprocess.run([sys.executable, '-I', '-S', '-X', 'utf8', str(core),
                                 '--receive', str(self.root), '--transaction', 'fake'],
                                input='', capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertFalse(self.user.exists())

    def test_real_main_imports_existing_accounts_and_team_after_migration(self):
        repo = Path(__file__).resolve().parents[1]
        from selfhost.portable import diagnostic_tail, process_environment, hidden_process_options
        # 模拟 CI 先安装的普通 src 包，避免本机未安装项目时掩盖源码被遮蔽的问题。
        installed = self.root / 'installed-site-packages'
        self.write(installed, 'src/__init__.py', b'')
        for broken in (False, True):
            with self.subTest(broken=broken):
                portable = self.root / str(broken)
                working = portable / 'data/apps/NoNo/working'
                (working / 'selfhost').mkdir(parents=True)
                for relative in ('main.py', 'selfhost/__init__.py', 'selfhost/user_data.py'):
                    shutil.copy2(repo / relative, working / relative)
                account = self.write(working, 'configs/MultiAccountConfig.json',
                                     b'{broken' if broken else b'{"accounts":["synthetic"],"enabled":false}')
                team = 'Alpha__Beta__Gamma'
                for name in ('Alpha', 'Beta', 'Gamma'):
                    self.write(working, f'configs/custom_teams/{team}/{name}.py', f'class {name}: pass\n'.encode())
                    self.write(working, f'configs/custom_teams/{team}/{name}.import.py', f'class {name}: pass\n'.encode())
                self.write(working, f'configs/custom_teams/{team}/team.json', b'{"code_mode":"import"}')
                marker = portable / 'configuration-imported'
                sources = {
                    'ok.util.config': repo / 'ok/util/config.py',
                    'src': repo / 'src/__init__.py',
                    'src.char.CustomCharLoader': repo / 'src/char/CustomCharLoader.py',
                    'selfhost.user_data': working / 'selfhost/user_data.py',
                }
                (working / 'config.py').write_text(
                    'import json,sys\nfrom pathlib import Path\n'
                    f'Path({str(marker)!r}).write_bytes(b"imported")\n'
                    f'sys.path.insert(0,{str(installed)!r})\nsys.path.insert(0,{str(repo)!r})\n'
                    'from ok.util.config import Config\n'
                    'from src.char.CustomCharLoader import list_custom_teams,get_team_code_mode,get_team_import_file\n'
                    'accounts=Config("MultiAccountConfig", {"accounts":[],"enabled":True})\n'
                    'team=list_custom_teams()[0]\n'
                    f'sources={{name:str(Path(sys.modules[name].__file__).resolve()) for name in {tuple(sources)!r}}}\n'
                    'print(json.dumps({"accounts":dict(accounts),"team":team,"mode":get_team_code_mode(team),'
                    '"code":get_team_import_file(team,"Alpha").read_text(),"sources":sources}))\n'
                    'raise SystemExit(0)\n', encoding='utf-8')
                result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(working / 'main.py')],
                                        cwd=working, env=process_environment(), capture_output=True, text=True,
                                        encoding='utf-8', timeout=30, **hidden_process_options())
                if broken:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(marker.exists())
                    self.assertEqual(account.read_bytes(), b'{broken')
                    self.assertFalse((portable / 'data/user/NoNo/configs/MultiAccountConfig.json').exists())
                else:
                    # ok 的异常钩子写入 stdout，失败时必须保留两路有界、脱敏诊断。
                    self.assertEqual(result.returncode, 0,
                                     f'标准输出尾部：\n{diagnostic_tail(result.stdout)}\n'
                                     f'标准错误尾部：\n{diagnostic_tail(result.stderr)}')
                    observed = json.loads(result.stdout.strip().splitlines()[-1])
                    self.assertEqual(observed['sources'], {name: str(path.resolve()) for name, path in sources.items()})
                    self.assertEqual(observed['accounts'], {'accounts': ['synthetic'], 'enabled': False})
                    self.assertEqual(observed['team'], ['Alpha', 'Beta', 'Gamma'])
                    self.assertEqual(observed['mode'], 'import')
                    self.assertEqual(observed['code'], 'class Alpha: pass\n')

    def test_schedule_cache_and_pending_plugin_recovery_consume_migrated_data(self):
        from ok.core.plugin_staging import apply_pending_plugin_changes, pending_plugin_changes
        from ok.util.windows_schedule import WindowsScheduleCache, ScheduleTaskInfo
        task = ScheduleTaskInfo(name='旧计划', path='\\NoNo\\旧计划', enabled=True,
                                task_identifier='ok_tasks.SyntheticTask', last_result=7)
        self.write(self.working, '.ok_cache/schedule_tasks_cache.json',
                   json.dumps({'legacy-task': task.to_dict()}, ensure_ascii=False).encode())
        self.write(self.working, 'ok_import/sample/current.py', b'unconfirmed version')
        self.write(self.working, 'ok_import/.plugin-staging/sample/previous/current.py', b'previous version')
        operation = {'namespace': 'sample', 'action': 'update', 'phase': 'validating', 'had_previous': True}
        self.write(self.working, 'ok_import/.plugin-staging/sample/operation.json', json.dumps(operation).encode())
        self.prepare()
        cache = WindowsScheduleCache({'config_folder': str(self.user / 'configs')})
        self.assertEqual(cache.get_all(), [task])
        self.assertEqual(pending_plugin_changes()['sample']['phase'], 'validating')
        result = apply_pending_plugin_changes()
        self.assertEqual(result['sample']['phase'], 'failed')
        self.assertEqual((self.user / 'ok_import/sample/current.py').read_bytes(), b'previous version')
        self.assertEqual((self.user / 'ok_import/.plugin-staging/sample/incoming/current.py').read_bytes(),
                         b'unconfirmed version')



class TestUserTaskImportRecovery(unittest.TestCase):
    def test_failed_binding_restores_old_modules_and_paths(self):
        from ok import og
        from ok.core.task_manager import TaskManager

        for legacy_kind in ('missing', 'namespace', 'regular'):
            with self.subTest(legacy_kind=legacy_kind), \
                    tempfile.TemporaryDirectory(prefix='任务导入回滚-') as temporary:
                root = Path(temporary).resolve()
                user = root / 'user'
                tasks = user / 'ok_tasks'
                working = root / 'working'
                legacy = root / 'previous' / 'ok_tasks'
                working.mkdir()
                (working / 'ConsumerReviewAppModule.py').write_text("VALUE = '应用模块'\n", encoding='utf-8')
                self._write_helpers(tasks, '稳定值')
                (tasks / '__init__.py').write_text(self._initializer(fail=True), encoding='utf-8')
                paths = [str(working), *sys.path]
                if legacy_kind != 'missing':
                    self._write_helpers(legacy, '旧版值')
                    if legacy_kind == 'regular':
                        (legacy / '__init__.py').write_text("VALUE = '旧包'\n", encoding='utf-8')
                    paths[1:1] = [str(legacy.parent), str(legacy)]
                with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(user)}), \
                        patch.object(sys, 'path', paths), patch.dict(sys.modules), \
                        patch.object(og, 'config', {'custom_tasks': True}), \
                        patch.object(TaskManager, 'load_imported_tasks'):
                    self._clear_probe_modules()
                    if legacy_kind != 'missing':
                        importlib.import_module('ok_tasks.helpers.nested')
                        importlib.import_module('ConsumerReviewBareHelper')
                    previous_paths = list(sys.path)
                    previous_modules = {name: module for name, module in sys.modules.items()
                                        if self._is_task_probe(name)}
                    with self.assertRaisesRegex(RuntimeError, '合成包初始化失败'):
                        TaskManager(SimpleNamespace(), SimpleNamespace(debug=False))
                    self.assertEqual(sys.path, previous_paths)
                    current_modules = {name: module for name, module in sys.modules.items()
                                       if self._is_task_probe(name)}
                    self.assertEqual(current_modules, previous_modules)
                    if legacy_kind != 'missing':
                        self.assertIs(sys.modules['ok_tasks'].helpers, previous_modules['ok_tasks.helpers'])
                        self.assertEqual(list(sys.modules['ok_tasks.helpers'].__path__),
                                         [str(legacy / 'helpers')])
                    # 失败包顺带导入的普通应用模块不属于任务绑定回滚范围。
                    self.assertEqual(sys.modules['ConsumerReviewAppModule'].VALUE, '应用模块')

    def test_failed_binding_retries_namespace_and_bare_helpers(self):
        from ok import og
        from ok.core.task_manager import TaskManager

        with tempfile.TemporaryDirectory(prefix='任务导入回滚-') as temporary:
            root = Path(temporary).resolve()
            user = root / 'user'
            tasks = user / 'ok_tasks'
            working = root / 'working'
            working.mkdir()
            (working / 'ConsumerReviewAppModule.py').write_text("VALUE = '应用模块'\n", encoding='utf-8')
            self._write_helpers(tasks, '失败尝试的旧值')
            (tasks / '__init__.py').write_text(self._initializer(fail=True), encoding='utf-8')
            with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(user)}), \
                    patch.object(sys, 'path', [str(working), *sys.path]), patch.dict(sys.modules), \
                    patch.object(og, 'config', {'custom_tasks': True}), \
                    patch.object(TaskManager, 'load_imported_tasks'):
                self._clear_probe_modules()
                with self.assertRaisesRegex(RuntimeError, '合成包初始化失败'):
                    TaskManager(SimpleNamespace(), SimpleNamespace(debug=False))
                self._write_helpers(tasks, '更正后的稳定值')
                (tasks / '__init__.py').write_text(self._initializer(fail=False), encoding='utf-8')
                manager = TaskManager(SimpleNamespace(), SimpleNamespace(debug=False))
                self.assertEqual(manager.task_errors, {})
                self.assertEqual(sys.modules['ok_tasks'].OBSERVED, ('更正后的稳定值', '更正后的稳定值'))

    def test_failed_bare_package_restores_nested_namespace_before_retry(self):
        from ok import og
        from ok.core.task_manager import TaskManager

        for imported_leaf in (False, True):
            with self.subTest(imported_leaf=imported_leaf), \
                    tempfile.TemporaryDirectory(prefix='任务导入回滚-') as temporary:
                root = Path(temporary).resolve()
                user = root / 'user'
                tasks = user / 'ok_tasks'
                bare_package = tasks / 'ConsumerReviewBarePackage'
                (bare_package / 'helpers').mkdir(parents=True)
                nested = bare_package / 'helpers/nested.py'
                nested.write_text("VALUE = '失败前'\n", encoding='utf-8')
                (tasks / '__init__.py').write_text('import ConsumerReviewBarePackage\n', encoding='utf-8')
                import_statement = ('from .helpers.nested import VALUE\n' if imported_leaf
                                    else 'from . import helpers\n')
                (bare_package / '__init__.py').write_text(
                    import_statement + "raise RuntimeError('合成辅助包失败')\n", encoding='utf-8')
                with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(user)}), \
                        patch.object(sys, 'path', list(sys.path)), patch.dict(sys.modules), \
                        patch.object(og, 'config', {'custom_tasks': True}), \
                        patch.object(TaskManager, 'load_imported_tasks'):
                    self._clear_probe_modules()
                    previous_paths = list(sys.path)
                    with self.assertRaisesRegex(RuntimeError, '合成辅助包失败'):
                        TaskManager(SimpleNamespace(), SimpleNamespace(debug=False))
                    self.assertEqual(sys.path, previous_paths)
                    self.assertFalse(any(self._is_task_probe(name) for name in sys.modules))
                    nested.write_text("VALUE = '更正后的稳定值'\n", encoding='utf-8')
                    (bare_package / '__init__.py').write_text(
                        'from .helpers.nested import VALUE\n', encoding='utf-8')
                    TaskManager(SimpleNamespace(), SimpleNamespace(debug=False))
                    self.assertEqual(sys.modules['ConsumerReviewBarePackage'].VALUE, '更正后的稳定值')
                    self.assertIs(sys.modules['ConsumerReviewBarePackage'].helpers,
                                  sys.modules['ConsumerReviewBarePackage.helpers'])

    @staticmethod
    def _write_helpers(tasks, value):
        (tasks / 'helpers').mkdir(parents=True, exist_ok=True)
        (tasks / 'helpers/nested.py').write_text(f'VALUE = {value!r}\n', encoding='utf-8')
        (tasks / 'ConsumerReviewBareHelper.py').write_text(f'VALUE = {value!r}\n', encoding='utf-8')

    @staticmethod
    def _initializer(*, fail):
        source = ('import ConsumerReviewAppModule\n'
                  'from .helpers.nested import VALUE as nested_value\n'
                  'from ConsumerReviewBareHelper import VALUE as bare_value\n'
                  'OBSERVED = (nested_value, bare_value)\n')
        return source + ("raise RuntimeError('合成包初始化失败')\n" if fail else '')

    @staticmethod
    def _is_task_probe(name):
        return (name == 'ok_tasks' or name.startswith('ok_tasks.') or name == 'ConsumerReviewBareHelper'
                or name == 'ConsumerReviewBarePackage' or name.startswith('ConsumerReviewBarePackage.'))

    @classmethod
    def _clear_probe_modules(cls):
        for name in tuple(sys.modules):
            if cls._is_task_probe(name) or name == 'ConsumerReviewAppModule':
                sys.modules.pop(name)
        importlib.invalidate_caches()


class TestTaskExportFailure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_export_dialog_reports_unreadable_manifest_without_opening_dialog(self):
        from ok.ui.qt.tasks.EditTaskTab import EditTaskTab

        module = importlib.import_module('ok.ui.qt.tasks.EditTaskTab')
        with tempfile.TemporaryDirectory(prefix='界面读取恢复-') as temporary:
            root = Path(temporary).resolve()
            tasks = root / 'ok_tasks'
            tasks.mkdir()
            (tasks / 'probe.py').write_text('pass\n', encoding='utf-8')
            manifest = tasks / 'manifest.json'
            original = b'{"unfinished":'
            manifest.write_bytes(original)
            page = SimpleNamespace(tr=lambda value: value, _success=Mock())
            with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(root)}), \
                    patch('ok.ui.qt.util.Alert.alert_error') as alert, \
                    patch.object(module, 'Dialog') as dialog:
                EditTaskTab.show_export_dialog(page)
                alert.assert_called_once()
                self.assertIn('CONFIG_JSON_INVALID', alert.call_args.args[0])
                dialog.assert_not_called()
                page._success.assert_not_called()
            self.assertEqual(manifest.read_bytes(), original)

    def test_export_dialog_reports_commit_failure_without_success_or_output(self):
        from PySide6.QtWidgets import QWidget

        from ok.ui.qt.tasks.EditTaskTab import EditTaskTab

        module = importlib.import_module('ok.ui.qt.tasks.EditTaskTab')
        with tempfile.TemporaryDirectory(prefix='界面读取恢复-') as temporary:
            root = Path(temporary).resolve()
            tasks = root / 'ok_tasks'
            tasks.mkdir()
            (tasks / 'probe.py').write_text('pass\n', encoding='utf-8')
            manifest = tasks / 'manifest.json'
            original = b'{"file_name":"probe","script_name":"probe","version":"1.0","extension":null}'
            manifest.write_bytes(original)
            page = QWidget()
            page._success = Mock()
            try:
                with patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(root)}), \
                        patch('ok.ui.qt.util.Alert.alert_error') as alert, \
                        patch.object(module.Dialog, 'exec', return_value=True), \
                        patch.object(module, 'reveal_in_explorer') as reveal, \
                        patch('ok.core.script_packager.get_downloads_folder', return_value=str(root / 'exports')) as output, \
                        patch('os.replace', side_effect=PermissionError('合成提交拒绝')):
                    EditTaskTab.show_export_dialog(page)
                    alert.assert_called_once()
                    self.assertIn('合成提交拒绝', alert.call_args.args[0])
                    page._success.assert_not_called()
                    reveal.assert_not_called()
                    output.assert_not_called()
                self.assertEqual(manifest.read_bytes(), original)
                self.assertFalse((root / 'exports').exists())
            finally:
                page.close()
                page.deleteLater()
                self.app.processEvents()


class TestTemplateReadFailure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import cv2
        import numpy as np
        from ok import Config

        self.temporary = tempfile.TemporaryDirectory(prefix='界面读取恢复-')
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(patch.dict(os.environ, {'NONO_USER_DATA_ROOT': str(self.root)}))
        self.enterContext(patch.object(Config, 'config_folder', str(self.root / 'configs')))
        self.module = importlib.import_module('ok.ui.qt.tasks.TemplateTab')
        self.pool = Mock()
        self.enterContext(patch.object(self.module.QThreadPool, 'globalInstance', return_value=self.pool))
        thumbnails = self.enterContext(patch.object(self.module, 'WindowsThumbnailReader'))
        thumbnails.return_value.open.return_value = False
        self.alert = self.enterContext(patch('ok.ui.qt.util.Alert.alert_error'))
        folder = self.root / 'ok_templates'
        folder.mkdir()
        self.image_path = folder / '0.png'
        self.coco_path = folder / 'coco_annotations.json'
        encoded, pixels = cv2.imencode('.png', np.zeros((8, 8, 3), dtype=np.uint8))
        self.assertTrue(encoded)
        pixels.tofile(self.image_path)
        self.write_data('初始标注')
        self.tab = self.module.TemplateTab(None)
        self.tab.load_initial_grid()
        self.pool.start.call_args.args[0].run()
        self.tab.on_card_clicked(str(self.image_path))
        self.previous = (self.tab.coco_data, tuple(self.tab.image_cards),
                         tuple(item['path'] for item in self.tab._image_items), self.tab.selected_image)

    def tearDown(self):
        self.tab.close()
        self.tab.deleteLater()
        self.app.processEvents()

    def write_data(self, marker):
        self.coco_path.write_text(json.dumps({
            'images': [{'id': 1, 'file_name': '0.png', 'width': 8, 'height': 8}],
            'annotations': [], 'categories': [], 'info': {'marker': marker},
        }), encoding='utf-8')

    def assert_previous_display(self):
        self.assertIs(self.tab.coco_data, self.previous[0])
        self.assertEqual(tuple(self.tab.image_cards), self.previous[1])
        self.assertEqual(tuple(item['path'] for item in self.tab._image_items), self.previous[2])
        self.assertEqual(self.tab.selected_image, self.previous[3])

    def test_template_read_failures_keep_display_and_allow_retry(self):
        original = self.coco_path.read_bytes()
        self.coco_path.write_bytes(b'{"unfinished":')
        calls_before = self.pool.start.call_count
        self.tab.load_initial_grid()
        self.alert.assert_called_once()
        self.assertEqual(self.pool.start.call_count, calls_before)
        self.assert_previous_display()
        self.assertFalse(self.tab._loaded)
        self.assertEqual(self.coco_path.read_bytes(), b'{"unfinished":')

        for failure in ('invalid', 'permission'):
            with self.subTest(failure=failure):
                self.coco_path.write_bytes(original)
                self.alert.reset_mock()
                self.tab.load_initial_grid()
                runnable = self.pool.start.call_args.args[0]
                self.assert_previous_display()
                if failure == 'invalid':
                    self.coco_path.write_bytes(b'{"unfinished":')
                    runnable.run()
                else:
                    original_open = open

                    def open_file(path, *args, **kwargs):
                        if isinstance(path, (str, os.PathLike)) and Path(path).resolve() == self.coco_path:
                            raise PermissionError('合成模板读取拒绝')
                        return original_open(path, *args, **kwargs)

                    with patch('builtins.open', open_file):
                        runnable.run()
                self.alert.assert_called_once()
                self.assert_previous_display()
                self.assertFalse(self.tab._loaded)
                self.assertFalse(self.tab._grid_loading)
                self.assertTrue(self.tab.progress_container.isHidden())
                self.assertEqual(self.coco_path.read_bytes(),
                                 b'{"unfinished":' if failure == 'invalid' else original)

        self.alert.reset_mock()
        self.write_data('重试成功')
        self.tab.load_initial_grid()
        self.pool.start.call_args.args[0].run()
        self.alert.assert_not_called()
        self.assertTrue(self.tab._loaded)
        self.assertEqual(self.tab.coco_data['info']['marker'], '重试成功')
        self.assertEqual(len(self.tab.image_cards), 1)

    def test_outdated_template_read_failure_does_not_replace_current_load(self):
        self.write_data('第一次加载')
        self.tab.load_initial_grid()
        previous_load = self.pool.start.call_args.args[0]
        self.write_data('第二次加载')
        self.tab.load_initial_grid()
        current_load = self.pool.start.call_args.args[0]
        self.coco_path.write_bytes(b'{"unfinished":')
        previous_load.run()
        self.alert.assert_not_called()
        self.assert_previous_display()
        self.assertTrue(self.tab._grid_loading)
        self.write_data('第二次加载')
        current_load.run()
        self.assertEqual(self.tab.coco_data['info']['marker'], '第二次加载')
        self.assertEqual(len(self.tab.image_cards), 1)
        self.assertFalse(self.tab._grid_loading)
