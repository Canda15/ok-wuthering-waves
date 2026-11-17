from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import config
from ok.device.interaction_methods.foreground_input import ForegroundInputInterrupted
from ok.test.TaskTestCase import TaskTestCase
from src.task.FarmEchoTask import FarmEchoTask


class TestCloudDisconnect(TaskTestCase):
    task_class = FarmEchoTask
    config = deepcopy(config)

    def test_detects_idle_disconnect_dialog(self):
        with patch.object(self.task, 'is_browser', return_value=True):
            self.set_image('tests/images/cloud_disconnect_1020.png')
            exit_game = self.task.find_cloud_disconnect()
            self.assertTrue(exit_game)
            self.assertIn('退出游戏', exit_game[0].name)

    def test_ignores_launcher_home(self):
        with patch.object(self.task, 'is_browser', return_value=True):
            self.set_image('tests/images/cloud_launcher_home.png')
            self.assertIsNone(self.task.find_cloud_disconnect())

    def test_skipped_outside_cloud(self):
        with patch.object(self.task, 'is_browser', return_value=False):
            self.set_image('tests/images/cloud_disconnect_1020.png')
            self.assertIsNone(self.task.find_cloud_disconnect())

    def test_detects_idle_disconnect_dialog_with_confirm_button(self):
        with patch.object(self.task, 'is_browser', return_value=True):
            self.set_image('tests/images/cloud_disconnect_4012.png')
            confirm = self.task.find_cloud_disconnect()
            self.assertTrue(confirm)
            self.assertEqual('确定', confirm[0].name)

    def test_reconnect_requires_world_confirmation_across_multiple_frames(self):
        button = object()
        task = SimpleNamespace(
            find_cloud_disconnect=lambda: [button], tr=lambda text: text, log_info=Mock(), click=Mock(),
            logged_in=True, in_team_and_world=Mock(side_effect=[False, True, True]))

        def wait_login():
            self.assertFalse(task.logged_in)
            task.logged_in = True
            return True

        def wait_until(condition, **kwargs):
            # 首次登录返回后，世界状态短暂消失；恢复成功前仍需继续等待。
            self.assertTrue(condition())
            self.assertFalse(condition())
            self.assertTrue(condition())
            self.assertTrue(condition())
            self.assertGreater(kwargs['settle_time'], 0)
            return True

        task.wait_login = Mock(side_effect=wait_login)
        task.wait_until = wait_until
        self.assertTrue(FarmEchoTask.handle_cloud_disconnect(task))
        task.click.assert_called_once_with(button, after_sleep=3)
        task.wait_login.assert_called_once_with()

    def test_reconnect_does_not_click_stale_disconnect_dialog(self):
        task = SimpleNamespace(
            _foreground_epoch=0, tr=lambda text: text, log_info=Mock(), click=Mock())

        def find_after_resume():
            task._foreground_epoch += 1
            return [object()]

        task.find_cloud_disconnect = find_after_resume
        with self.assertRaises(ForegroundInputInterrupted):
            FarmEchoTask.handle_cloud_disconnect(task)
        task.click.assert_not_called()
