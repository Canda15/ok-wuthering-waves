import unittest
from unittest.mock import Mock
import re

from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from ok.task.exceptions import WaitFailedException
from src.Labels import Labels
from src.task.BaseWWTask import BaseWWTask, LOGIN_CLICK_SETTLE_TIME, LOGIN_TEXTS


class TextBox:
    def __init__(self, name):
        self.name = name


class FakeLoginTask:
    """Drives BaseWWTask.wait_login with scripted OCR frames.

    Each entry in frames is the list of TextBox returned by one ocr() call,
    so a frame sequence like [[login], []] simulates a login button that
    disappears while the game's own auto login is running (#1356).
    """

    def __init__(self, frames):
        self.frames = list(frames)
        self.logged_in = False
        self.debug = False
        self.clicked = []
        self.slept = []

    def in_team_and_world(self):
        return False

    def handle_monthly_card(self):
        return False

    def find_one(self, *args, **kwargs):
        return None

    def box_of_screen(self, *args, **kwargs):
        return args

    def ocr(self, log=False):
        return self.frames.pop(0) if self.frames else []

    def find_boxes(self, texts, boundary=None, match=None):
        if not texts:
            return None
        if match == LOGIN_TEXTS:
            return [b for b in texts
                    if 'log' in b.name.lower() or '登录' in b.name or '登入' in b.name] or None
        if match == "+86":
            return [b for b in texts if '+86' in b.name] or None
        return None

    def click(self, target, after_sleep=0):
        self.clicked.append([b.name for b in target])

    def sleep(self, timeout):
        self.slept.append(timeout)

    def log_info(self, *args, **kwargs):
        pass

    def tr(self, message):
        return message

    def log_debug(self, *args, **kwargs):
        pass


class TestWaitLogin(unittest.TestCase):

    def test_authentication_progress_checks_account_before_agree_or_enter_fallback(self):
        for step in ('agree', 'enter_fallback'):
            with self.subTest(step=step):
                texts = [TextBox('同意'), TextBox('隐私政策')] if step == 'agree' else [TextBox('账号B')]
                task = FakeLoginTask(frames=[texts])
                task._login_account_guard = Mock(side_effect=WaitFailedException('账号确认失败'))
                task.click_relative = Mock()
                task.find_one = lambda name, **kwargs: object() if (
                    step == 'enter_fallback' and name == Labels.switch_account) else None

                def find_boxes(values, match=None, **kwargs):
                    if match is None:
                        return values
                    if isinstance(match, str):
                        return [box for box in values if match in box.name]
                    if isinstance(match, re.Pattern):
                        return [box for box in values if match.search(box.name)]
                    return None

                task.find_boxes = find_boxes
                with self.assertRaises(WaitFailedException):
                    BaseWWTask.wait_login(task)
                task._login_account_guard.assert_called_once_with(require_visible=False)
                self.assertEqual(task.clicked, [])
                task.click_relative.assert_not_called()

    def test_monthly_card_focus_after_accepted_click_checks_world_without_replay(self):
        task = BaseWWTask.__new__(BaseWWTask)
        world = [False]
        task.find_monthly_card = Mock(return_value=object())
        task.in_team_and_world = lambda: world[0]
        task.sleep = Mock()
        task.wait_foreground_frame = Mock()
        task.set_check_monthly_card = Mock()

        def accept(*args, **kwargs):
            world[0] = True
            raise ForegroundInputInterrupted()

        task.click_relative = Mock(side_effect=accept)
        self.assertTrue(task.handle_monthly_card())
        task.click_relative.assert_called_once()
        task.set_check_monthly_card.assert_called_once_with(next_day=True)

    def test_monthly_card_system_input_failure_is_not_retried(self):
        task = BaseWWTask.__new__(BaseWWTask)
        task.find_monthly_card = Mock(return_value=object())
        task.click_relative = Mock(side_effect=ForegroundInputError())
        task.wait_foreground_frame = Mock()
        with self.assertRaises(ForegroundInputError):
            task.handle_monthly_card()
        task.click_relative.assert_called_once()
        task.wait_foreground_frame.assert_not_called()

    def test_transient_login_button_is_not_clicked(self):
        # Global server auto login: the login button disappears by itself,
        # clicking it would pop up the account/password dialog (#1356).
        task = FakeLoginTask(frames=[[TextBox('Log In')], []])

        result = BaseWWTask.wait_login(task)

        self.assertFalse(result)
        self.assertEqual(task.clicked, [])
        self.assertEqual(task.slept, [LOGIN_CLICK_SETTLE_TIME])

    def test_persistent_login_button_is_clicked(self):
        # No saved credentials: the login button stays, so it must be clicked.
        task = FakeLoginTask(frames=[[TextBox('Log In')], [TextBox('Log In')]])

        result = BaseWWTask.wait_login(task)

        self.assertFalse(result)
        self.assertEqual(task.clicked, [['Log In']])

    def test_no_click_and_no_settle_when_phone_login_is_already_shown(self):
        # +86 phone login present on the first frame: the pre-existing guard
        # must skip both the click and the settle wait entirely.
        task = FakeLoginTask(frames=[[TextBox('登录'), TextBox('+86 手机号')]])

        result = BaseWWTask.wait_login(task)

        self.assertFalse(result)
        self.assertEqual(task.clicked, [])
        self.assertEqual(task.slept, [])

    def test_no_click_when_phone_login_appears_after_settle(self):
        # CN phone-number login screen (+86) must never be auto clicked,
        # even when it only shows up on the confirmation frame.
        task = FakeLoginTask(frames=[[TextBox('登录')],
                                     [TextBox('登录'), TextBox('+86 手机号')]])

        result = BaseWWTask.wait_login(task)

        self.assertFalse(result)
        self.assertEqual(task.clicked, [])


if __name__ == "__main__":
    unittest.main()
