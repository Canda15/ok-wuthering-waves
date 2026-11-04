"""守住 selfhost 补丁面的最小冒烟集。

上游的测试覆盖的是"没有我们补丁"的树,测不到两件事:随 pin 重新内联的 ok/
是否还认这些 API,以及被我们重新注册的两个页面是否还能实例化。三条断言、
几十秒,由 .github/workflows/sync-upstream.yml 在每次自动合并后执行——它是整套
自动同步唯一的闸门,红了就不许推送。
"""

import inspect
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ok import og
from ok.util.clazz import init_class_by_name
from ok.util.config import Config
from selfhost.overrides import CUSTOM_TABS, apply


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
