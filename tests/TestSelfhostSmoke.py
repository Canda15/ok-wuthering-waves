"""守住 selfhost 补丁面的最小冒烟集。

上游的测试覆盖的是"没有我们补丁"的树,测不到四件事:随 pin 重新内联的 ok/
是否还认这些 API、被我们重新注册的两个页面是否还能实例化、改名相关的 override
是否还压得住上游的默认值、以及施加在 ok/ 源码里的品牌补丁是否还在。
六条断言、几十秒,由 .github/workflows/sync-upstream.yml 在每次自动合并后执行
——它是整套自动同步唯一的闸门,红了就不许推送。
"""

import inspect
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ok import og
from ok.util.clazz import init_class_by_name
from ok.util.config import Config
from selfhost.overrides import APP_NAME, CUSTOM_TABS, apply


class TestSelfhostSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 两个页面在构造时会读 og.app 的翻译和配置目录,这里给最小替身,
        # 避免冒烟测试碰到用户真实的 configs/。
        self.app_patch = patch.object(og, 'app', SimpleNamespace(tr=lambda text: text,
                                                               start_controller=Mock()))
        self.app_patch.start()
        self.old_config_folder = Config.config_folder
        self.temp_dir = tempfile.TemporaryDirectory()
        Config.config_folder = self.temp_dir.name

    def tearDown(self):
        Config.config_folder = self.old_config_folder
        self.temp_dir.cleanup()
        self.app.processEvents()
        self.app_patch.stop()

    def test_apply_registers_both_tabs_and_is_idempotent(self):
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
        config = {
            'gui_title': 'OK-WW',
            'links': {'default': {'github': 'https://github.com/ok-oldking/ok-wuthering-waves'}},
            # 上游重新塞回这个键,首次显示窗口时会把启动器换成上游版本。
            'update_pyappify': {'to_version': '1.2.3', 'zip_url': 'https://example.invalid/ok-ww-win32.zip'},
        }
        apply(config)
        self.assertEqual(config['gui_title'], APP_NAME)
        self.assertEqual(config['log_file'], f'logs/{APP_NAME}.log')
        self.assertEqual(config['error_log_file'], f'logs/{APP_NAME}_error.log')
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

    def test_sync_script_keeps_pyproject_parseable(self):
        """上游的 dependencies 数组结尾不带逗号,把生成块插进去会让 pyproject 变成非法 TOML。

        这个坑踩过一次:构建跑到"装依赖"才炸,而测试全绿——因为它们只需要 requirements.txt。
        所以这里按上游的真实形状造一份文件,要求改写之后仍然可解析,且该删的都删了。
        """
        import tomllib
        from importlib import util as importlib_util

        script = Path(__file__).resolve().parents[1] / 'scripts' / 'sync_ok.py'
        spec = importlib_util.spec_from_file_location('selfhost_sync_ok', script)
        sync_ok = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(sync_ok)

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
        for module_name, class_name in apply({}):
            tab = init_class_by_name(module_name, class_name)
            try:
                self.assertTrue(tab.name, f'{class_name} 没有可用的 name')
                self.assertIsNotNone(tab.icon, f'{class_name} 没有可用的 icon')
                self.assertIsInstance(tab.add_after_default_tabs, bool)
            finally:
                tab.close()
                tab.deleteLater()

