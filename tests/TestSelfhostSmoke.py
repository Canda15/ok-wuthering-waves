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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def load_sync_script():
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'sync_ok.py'
    spec = importlib_util.spec_from_file_location('selfhost_sync_ok', script)
    sync_ok = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(sync_ok)
    return sync_ok


class TestSelfhostSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 原生宿主验证只依赖标准库，不应提前加载 Qt 和游戏框架。
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.user_root = Path(self.temp_dir.name) / 'user'
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

    def test_default_input_policy_and_mouse_reset_removal(self):
        """上游配置重新带回后台默认值或光标补救任务时必须在自维护门禁发现。"""
        from config import config
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(config['windows']['interaction'], 'Pynput')
        self.assertNotIn(('src.task.MouseResetTask', 'MouseResetTask'),
                         [tuple(task) for task in config['trigger_tasks']])
        self.assertFalse((repo_root / 'src' / 'task' / 'MouseResetTask.py').exists())

    def test_upstream_input_overwrite_preserves_foreground_policy_and_other_settings(self):
        """覆盖入口同时守住前台默认值、旧注册和用户捕获参数，且可重复执行。"""
        from selfhost.overrides import apply
        kept_task = ['src.task.AutoPickTask', 'AutoPickTask']
        for old_name in ('PostMessage', 'Genshin', 'ForegroundPostMessage', 'PyDirect'):
            with self.subTest(old_name=old_name):
                config = {
                    'windows': {'interaction': old_name, 'exe': 'fixture.exe', 'capture_method': ['WGC']},
                    'trigger_tasks': [kept_task, ['src.task.MouseResetTask', 'MouseResetTask']],
                }
                apply(config)
                apply(config)
                self.assertEqual(config['windows'], {
                    'interaction': 'Pynput', 'exe': 'fixture.exe', 'capture_method': ['WGC'],
                })
                self.assertEqual(config['trigger_tasks'], [kept_task])

    def test_foreground_sources_remain_inline_and_are_deployed(self):
        """部署白名单或导入来源漂移会让已通过的前台实现在用户包中消失。"""
        import ok
        from ok.device import interaction_methods
        from ok.device.interaction_methods import foreground_input
        repo_root = Path(__file__).resolve().parents[1]
        self.assertTrue(Path(ok.__file__).resolve().is_relative_to(repo_root / 'ok'))
        self.assertTrue(Path(foreground_input.__file__).resolve().is_relative_to(repo_root / 'ok'))
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
        self.assertEqual(CocoTemplateStore().folder, self.user_root / 'ok_templates')
        self.assertEqual(Path(get_coco_path()), self.user_root / 'ok_templates/coco_annotations.json')
        generate_label_enum('ok_tasks.SyntheticLabels', ['synthetic_feature'])
        self.assertTrue((self.user_root / 'ok_tasks/SyntheticLabels.py').is_file())
        external = Path(self.temp_dir.name) / 'external'
        self.assertEqual(CocoTemplateStore(external).folder, external)
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

    def test_b7_resources_keep_foreground_messages_and_removed_update_source_keys(self):
        from PySide6 import QtCore
        repo = Path(__file__).resolve().parents[1]
        qt_folder = repo / 'ok/ui/qt'
        current = {
            ('SelectInteractionListView', '前台系统键鼠'),
            ('SelectInteractionListView', '使用真实键盘和鼠标；失焦释放并等待，回到游戏后自动继续'),
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
        self.assertEqual(manifest['framework_version'], '3.0.5b7')
        paths = {entry['path'] for entry in manifest['entries']}
        self.assertNotIn('ui/qt/MainWindow.py', paths)
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
                if not auto_start and not requested and not starting and paused:
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
        (self.source / 'main.py').write_text('TOKEN = "本次构建"\n', encoding='utf-8')
        snapshot, digests = self.prepare_snapshot()
        self.assertEqual((snapshot / 'main.py').read_bytes(), (self.source / 'main.py').read_bytes())
        self.assertEqual((snapshot / 'selfhost' / 'NoNo.exe').read_bytes(), b'MZ-current-native-host')
        self.assertEqual((snapshot / 'config.py').read_text(encoding='utf-8'), f'version = "{self.version}"\n')
        self.assertEqual((self.source / 'config.py').read_bytes(), self.files['config.py'])
        self.assertEqual((self.source / 'requirements.txt').read_bytes(), self.files['requirements.txt'])
        self.assertEqual((snapshot / '.gitignore').read_bytes(), self.files['.update_repo_gitignore'])
        self.assertNotIn('pyappify', (snapshot / 'requirements.txt').read_text(encoding='utf-8'))
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
        for release_tag in (None, self.version, 'v3.8.1-beta.1'):
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
                         'working/LOGS/empty', 'python/.PIP_UPDATE_NEEDED.tmp'):
            with self.subTest(directory=relative):
                changed = {**manifest, 'payload_directories': sorted({*manifest['payload_directories'], relative})}
                changed = portable_protocol.seal_manifest(changed)
                with self.assertRaises(RuntimeError):
                    portable_protocol.validate_manifest(changed)
        for relative in ('working/Configs/private.json', 'working/.PIP_UPDATE_NEEDED.tmp'):
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

        # 复制与 pyappify 相同的运行时目录布局，不能用 _pth 指回本机标准库，
        # 否则会掩盖宿主的 PYTHONHOME 和模块搜索路径错误。测试树不含目录联接。
        base = Path(sys.base_prefix)
        for pattern in ('python3*.dll', 'vcruntime*.dll'):
            for library in base.glob(pattern):
                shutil.copy2(library, cls.runtime / library.name)
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

