import unittest
from unittest.mock import Mock, call

from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from ok.task.exceptions import TaskDisabledException
from src.task.BaseWWTask import BaseWWTask
from src.task.SkipBaseTask import SkipBaseTask


class TestSkipDialogConfirm(unittest.TestCase):

    def setUp(self):
        self.task = BaseWWTask.__new__(BaseWWTask)
        self.task.find_one = Mock()
        self.task.click = Mock()
        self.task.sleep = Mock()
        self.task.wait_foreground_frame = Mock()

    def test_focus_after_checkbox_does_not_click_checkbox_again(self):
        confirm, checkbox, fresh_confirm = object(), object(), object()
        self.task.find_one.side_effect = [confirm, checkbox, fresh_confirm]
        self.task.click.side_effect = [ForegroundInputInterrupted(), None]
        self.assertTrue(self.task.click_skip_dialog_confirm())
        self.assertEqual(self.task.click.call_args_list, [call(checkbox), call(fresh_confirm, after_sleep=.2)])
        self.task.wait_foreground_frame.assert_called_once()

    def test_new_frame_during_settle_discards_old_checkbox_and_confirm_boxes(self):
        confirm, checkbox, fresh_confirm = object(), object(), object()
        self.task.find_one.side_effect = [confirm, checkbox, fresh_confirm]
        self.task._foreground_epoch = 0
        self.task.sleep.side_effect = lambda seconds: setattr(self.task, '_foreground_epoch', 1)
        self.assertTrue(self.task.click_skip_dialog_confirm())
        self.task.click.assert_called_once_with(fresh_confirm, after_sleep=.2)

    def test_system_input_failure_never_retries_checkbox_or_confirm(self):
        self.task.find_one.side_effect = [object(), object()]
        self.task.click.side_effect = ForegroundInputError()
        with self.assertRaises(ForegroundInputError):
            self.task.click_skip_dialog_confirm()
        self.task.wait_foreground_frame.assert_not_called()
        self.task.click.assert_called_once()

    def test_clicks_checkbox_then_confirm(self):
        confirm = object()
        checkbox = object()
        self.task.find_one.side_effect = [confirm, checkbox]

        clicked = self.task.click_skip_dialog_confirm()

        self.assertTrue(clicked)
        self.task.find_one.assert_has_calls([
            call(
                ['confirm_btn_hcenter_vcenter', 'confirm_btn_highlight_hcenter_vcenter'],
                horizontal_variance=0.1,
                vertical_variance=0.1,
            ),
            call(
                'skip_dialog_check',
                horizontal_variance=0.1,
                vertical_variance=0.1,
            ),
        ])
        self.assertEqual(self.task.click.call_args_list, [call(checkbox), call(confirm)])
        self.assertEqual(self.task.sleep.call_args_list, [call(0.5), call(0.5), call(0.2)])

    def test_does_nothing_when_dialog_is_absent(self):
        self.task.find_one.return_value = None

        clicked = self.task.click_skip_dialog_confirm()

        self.assertFalse(clicked)
        self.task.click.assert_not_called()
        self.task.sleep.assert_not_called()

    def test_waits_for_optional_dialog_without_raising(self):
        self.task.wait_until = Mock(return_value=True)

        clicked = self.task.wait_click_skip_dialog_confirm(time_out=2)

        self.assertTrue(clicked)
        self.task.wait_until.assert_called_once_with(
            self.task.click_skip_dialog_confirm,
            time_out=2,
            raise_if_not_found=False,
        )

    def test_teleport_reuses_optional_dialog_handler(self):
        teleport = Mock(name='teleport')
        teleport.name = 'gray_teleport'
        self.task.find_one.return_value = teleport
        self.task.wait_click_skip_dialog_confirm = Mock()

        clicked = self.task.click_traval_button()

        self.assertTrue(clicked)
        self.task.click.assert_called_once_with(teleport, after_sleep=1)
        self.task.wait_click_skip_dialog_confirm.assert_called_once_with()


class TestSkipConfirmForegroundRecovery(unittest.TestCase):
    def make_task(self):
        task = SkipBaseTask.__new__(SkipBaseTask)
        task._foreground_epoch = 0
        task.confirm_dialog_checked = False
        task.click_skip_dialog_confirm = Mock(return_value=False)
        task.find_one = Mock(return_value=None)
        task.click = Mock()
        task.sleep = Mock()
        task.wait_foreground_frame = Mock()
        task.in_team_and_world = Mock(return_value=False)
        return task

    def test_两种跳过确认在等待恢复后重新定位(self):
        for feature in ('skip_quest_confirm', 'skip_quest_confirm_new'):
            with self.subTest(feature=feature):
                task = self.make_task()
                stale, fresh = object(), object()
                replacement = 'skip_quest_confirm_new' if feature == 'skip_quest_confirm' else 'skip_quest_confirm'
                visible = {feature: stale}
                task.find_one.side_effect = lambda name, **kwargs: visible.get(name)

                def resume_once(seconds):
                    if task.sleep.call_count == 1:
                        visible.clear()
                        visible[replacement] = fresh
                        task.on_foreground_resume()

                task.sleep.side_effect = resume_once

                self.assertTrue(task.skip_confirm())

                task.click.assert_called_once_with(fresh)
                task.wait_foreground_frame.assert_called_once()

    def test_跳过点击已接受后失焦只确认新页面(self):
        task = self.make_task()
        task.find_one.return_value = object()

        def accept(*args, **kwargs):
            task.find_one.return_value = None
            task.in_team_and_world.return_value = True
            raise ForegroundInputInterrupted('跳过确认已发出后失焦')

        task.click.side_effect = accept

        self.assertTrue(task.skip_confirm())

        task.click.assert_called_once()
        task.wait_foreground_frame.assert_called_once()

    def test_跳过等待取消或系统失败不会重试(self):
        for error in (ForegroundInputError, TaskDisabledException):
            with self.subTest(error=error):
                task = self.make_task()
                task.find_one.return_value = object()
                task.sleep.side_effect = error('跳过已停止')

                with self.assertRaises(error):
                    task.skip_confirm()

                task.click.assert_not_called()
                task.wait_foreground_frame.assert_not_called()


if __name__ == '__main__':
    unittest.main()
