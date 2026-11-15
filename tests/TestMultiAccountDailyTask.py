import ctypes
import unittest
import time
from contextlib import nullcontext
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock, patch

import src.task.MultiAccountDailyTask as multi_account_module
from ok import og
from ok.task.exceptions import TaskDisabledException, WaitFailedException
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.task.BaseWWTask import BaseWWTask, LOGIN_CLICK_SETTLE_TIME, LOGIN_TEXTS
from src.task.MultiAccountConfigTask import MultiAccountConfigTask
from src.task.DailyTask import (
    ADDITIONAL_TASKS, CHECK_WEEKLY_GARDEN, MERGE_ECHO_IF_DISCARDED_OVER_1000,
    TELEPORT_AND_FARM_4C_ECHO, DailyTask,
)
from src.task.MultiAccountDailyTask import (
    MultiAccountDailyTask,
    account_pattern,
    normalize_account_name,
    select_combo_item,
)


class FakeComboTask:
    """隔离账号遍历决策；原生前台选号在下面的输入场景中单独验证。"""

    class FakeExecutor:
        def get_task_by_class(self, task_class):
            return None

    executor = FakeExecutor()
    _is_done = MultiAccountDailyTask._is_done
    _select_account_by_combo = MultiAccountDailyTask._select_account_by_combo

    def __init__(self, done=()):
        self.done_set = {normalize_account_name(name) for name in done}
        self.all_accounts = set()
        self.next_frame = Mock()
        self.sleep = Mock()

    def _login_combo(self):
        return 1

    def ocr(self, *args, **kwargs):
        raise AssertionError('原生账号选择不应回退到 OCR')

    def click(self, *args, **kwargs):
        raise AssertionError('账号遍历应委托统一的前台选号方法')

    def info_set(self, *args):
        pass

    def log_info(self, *args):
        pass

    def tr(self, message):
        return message


class NativeComboScenario:
    """模拟原生列表的几何和真实点击效果，导航键默认无效，复现登录 SDK 的现场。"""

    def __init__(self, case, *, items=None, top_index=0, visible_rows=3, upward=False, scale=1):
        self.items = list(items or ['第一个账号', '第二个账号', '目标账号'])
        self.index = 0
        self.dropped = False
        self.combo = 42
        self.list_hwnd = 0x100000084
        self.identity = (10, 20)
        self.top_index = top_index
        self.row_height = 40 * scale
        self.width = 200 * scale
        self.field_height = 30 * scale
        self.viewport_height = visible_rows * self.row_height
        self.origin = (-1800 * scale, 100 * scale)
        self.combo_origin = (self.origin[0] + 100 * scale, self.origin[1] + 200 * scale)
        self.upward = upward
        self.scroll_step = 1
        self.scroll_stalled = False
        self.ignore_row_click = False
        self.info_failed = False
        self.rect_failed = False
        self.info_combo = self.combo
        self.task = SimpleNamespace(
            width=1600 * scale, height=900 * scale, _foreground_epoch=0,
            next_frame=Mock(), sleep=Mock(), _login_combo=lambda: self.combo,
            executor=SimpleNamespace(interaction=SimpleNamespace(
                target_generation=1,
                capture=SimpleNamespace(get_abs_cords=lambda x, y: (x + self.origin[0], y + self.origin[1])))),
            wait_until=lambda condition, **kwargs: condition(),
        )
        self.task.click = Mock(side_effect=self.click)
        self.task.scroll = Mock(side_effect=self.scroll)
        self.task.send_key = Mock()
        case.enterContext(patch.object(multi_account_module, '_SendMessageW', side_effect=self.read_message))
        case.enterContext(patch.object(multi_account_module, '_GetComboBoxInfo',
                                       side_effect=self.combo_info, create=True))
        case.enterContext(patch.object(multi_account_module, 'combo_items', side_effect=lambda combo: self.items.copy()))
        case.enterContext(patch.object(multi_account_module, 'combo_selected_item',
                                       side_effect=lambda combo: self.items[self.index]))
        case.enterContext(patch('win32gui.GetClientRect', side_effect=self.client_rect))
        case.enterContext(patch('win32gui.ClientToScreen', side_effect=self.client_to_screen))
        case.enterContext(patch('win32gui.IsWindow', side_effect=lambda hwnd: hwnd in (self.combo, self.list_hwnd)))
        case.enterContext(patch('win32gui.IsWindowVisible',
                               side_effect=lambda hwnd: hwnd == self.combo or (hwnd == self.list_hwnd and self.dropped)))
        case.enterContext(patch('win32process.GetWindowThreadProcessId', side_effect=lambda hwnd: self.identity))

    @property
    def list_origin(self):
        x, y = self.combo_origin
        return x, y - self.viewport_height if self.upward else y + self.field_height

    def client_rect(self, hwnd):
        return (0, 0, self.width, self.field_height if hwnd == self.combo else self.viewport_height)

    def client_to_screen(self, hwnd, point):
        origin = self.combo_origin if hwnd == self.combo else self.list_origin
        return origin[0] + point[0], origin[1] + point[1]

    def combo_info(self, hwnd, pointer):
        if self.info_failed:
            return 0
        info = pointer._obj
        if info.cbSize != ctypes.sizeof(info):
            raise AssertionError('必须按真实结构大小初始化 ComboBox 查询')
        info.hwndCombo = self.info_combo
        info.hwndList = self.list_hwnd
        return 1

    def read_message(self, hwnd, message, wparam, lparam):
        if message == 0x198 and hwnd == self.list_hwnd:  # LB_GETITEMRECT
            if self.rect_failed:
                return -1
            rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.right = 0, self.width
            rect.top = (wparam - self.top_index) * self.row_height
            rect.bottom = rect.top + self.row_height
            return 0
        if message == 0x18E and hwnd == self.list_hwnd:  # LB_GETTOPINDEX
            return self.top_index
        values = {0x147: self.index, 0x157: int(self.dropped), 0x154: self.field_height, 0x156: 0}
        if hwnd != self.combo or message not in values:
            raise AssertionError('只能查询本次原生控件，不能发送写消息')
        return values[message]

    def click(self, x, y, **kwargs):
        screen = (x + self.origin[0], y + self.origin[1])
        combo_x, combo_y = self.combo_origin
        if combo_x <= screen[0] < combo_x + self.width and combo_y <= screen[1] < combo_y + self.field_height:
            self.dropped = not self.dropped
            return
        list_x, list_y = self.list_origin
        if (self.dropped and list_x <= screen[0] < list_x + self.width
                and list_y <= screen[1] < list_y + self.viewport_height):
            if screen[0] >= list_x + self.width * .75:
                raise AssertionError('选号不得点击账号行右侧的删除区域')
            if not self.ignore_row_click:
                self.index = self.top_index + (screen[1] - list_y) // self.row_height
                self.dropped = False

    def scroll(self, x, y, count):
        screen = (x + self.origin[0], y + self.origin[1])
        list_x, list_y = self.list_origin
        if not (self.dropped and list_x <= screen[0] < list_x + self.width
                and list_y <= screen[1] < list_y + self.viewport_height):
            raise AssertionError('滚动必须指向当前展开的账号列表')
        if not self.scroll_stalled:
            max_top = max(0, len(self.items) - max(1, self.viewport_height // self.row_height))
            self.top_index = max(0, min(max_top, self.top_index - count * self.scroll_step))


class TestMultiAccountDailyTask(unittest.TestCase):

    def make_daily(self, phase='stamina'):
        task = DailyTask.__new__(DailyTask)
        task._daily_phase = phase
        task._daily_additional_index = 0
        task.support_tasks = ['Tacet Suppression', 'Forgery Challenge', 'Simulation Challenge']
        task.config = {'Which to Farm': 'Tacet Suppression'}
        task._executor = SimpleNamespace(task_context=lambda child: nullcontext())
        task.open_daily = Mock(return_value=(60, False))
        task.ensure_main = Mock()
        task.sleep = Mock()
        task.claim_daily = Mock()
        task.claim_mail = Mock()
        task.claim_battle_pass = Mock()
        task.run_additional_tasks = Mock()
        task.wait_foreground_frame = Mock()
        task.log_info = Mock()
        task.tr = str
        return task

    def test_daily_reward_focus_interruption_does_not_repeat_completed_stamina_stage(self):
        task = self.make_daily()
        farm_task = SimpleNamespace(farm_tacet=Mock())
        task.get_task_by_class = Mock(return_value=farm_task)
        task.claim_daily.side_effect = [ForegroundInputInterrupted(), None]
        task._continue_daily()
        farm_task.farm_tacet.assert_called_once_with(daily=True, used_stamina=60, config=task.config)
        task.claim_mail.assert_called_once()
        task.claim_battle_pass.assert_called_once()
        task.wait_foreground_frame.assert_called_once()
        self.assertEqual(task._daily_phase, 'completed')

    def test_daily_additional_focus_interruption_keeps_completed_task_index(self):
        task = self.make_daily('additional')
        task.config[ADDITIONAL_TASKS] = [CHECK_WEEKLY_GARDEN, MERGE_ECHO_IF_DISCARDED_OVER_1000,
                                       TELEPORT_AND_FARM_4C_ECHO]
        task.check_weekly_garden = Mock()
        task.check_discarded_echo = Mock(side_effect=[ForegroundInputInterrupted(), None])
        task._farm_additional_echo = Mock()
        task.run_additional_tasks = lambda: DailyTask.run_additional_tasks(task)
        task._continue_daily()
        task.check_weekly_garden.assert_called_once()
        self.assertEqual(task.check_discarded_echo.call_count, 2)
        task._farm_additional_echo.assert_called_once()

    def test_missing_daily_stamina_progress_is_never_treated_as_zero(self):
        task = self.make_daily()
        task.openF2Book = Mock()
        task.click = Mock()
        task.ocr = Mock(return_value=[])
        task.get_total_daily_points = Mock(return_value=10)
        task.screenshot = Mock()
        with self.assertRaises(ForegroundInputError):
            DailyTask.open_daily(task)
        task.screenshot.assert_called_once_with('daily_stamina_progress_unknown')

    def test_completed_daily_ui_skips_resource_farming_even_when_progress_row_is_hidden(self):
        task = self.make_daily()
        task.openF2Book = Mock()
        task.click = Mock()
        task.ocr = Mock(return_value=[])
        task.info_set = Mock()
        task.get_total_daily_points = Mock(return_value=100)
        self.assertEqual(DailyTask.open_daily(task), (180, True))
        task.open_daily.return_value = (180, True)
        task.get_task_by_class = Mock()
        task._continue_daily()
        task.get_task_by_class.assert_not_called()

    def test_account_dropdown_accepts_multiple_login_text_matches(self):
        account_box = object()

        class FakeTask:
            def ocr(self):
                return []

            def find_boxes(self, texts, match):
                if match == account_pattern:
                    return [account_box]
                if match == LOGIN_TEXTS:
                    return [object(), object()]
                return []

        self.assertIs(MultiAccountDailyTask.do_find_account_drop_down(FakeTask()), account_box)

    def test_account_name_normalization_groups_common_ocr_variants(self):
        self.assertEqual(
            normalize_account_name("cc****33@demo.com.hk"),
            normalize_account_name("cc****33@dem0.com.hk"),
        )
        self.assertEqual(
            normalize_account_name("bb****02@example.com"),
            normalize_account_name("bb****02@example.con"),
        )

    def test_click_account_list_selects_visible_third_account_after_first_two_are_done(self):
        class AccountBox:
            def __init__(self, name):
                self.name = name

        class FakeTask:
            def __init__(self):
                self.done_set = {
                    normalize_account_name("aa****01@example.com"),
                    normalize_account_name("bb****02@example.com"),
                }
                self.all_accounts = set()
                self.clicked = []

            _is_done = MultiAccountDailyTask._is_done

            def ocr(self, match=None):
                return [
                    AccountBox("aa****01@example.com"),
                    AccountBox("aa****01@example.com"),
                    AccountBox("bb****02@example.com"),
                    AccountBox("cc****03@example.com.hk"),
                ]

            def info_set(self, *args):
                pass

            def click(self, account, after_sleep=0):
                self.clicked.append(account.name)

            def log_info(self, *args):
                pass

            def tr(self, message):
                return message

        task = FakeTask()

        selected = MultiAccountDailyTask._click_account_in_list(task)

        self.assertEqual(selected, "cc****03@example.com.hk")
        self.assertEqual(task.clicked, ["cc****03@example.com.hk"])

    def test_combo_selection_picks_first_undone_account_including_unmasked_names(self):
        selected = []
        items = ["aa****01@example.com", "Display Name", "bb****02@example.com"]
        with patch.object(multi_account_module, 'combo_items', return_value=items), \
                patch.object(multi_account_module, 'select_combo_item',
                             side_effect=lambda task, combo, index, name: selected.append(index) or True):
            task = FakeComboTask(done=["aa****01@example.com"])
            account = MultiAccountDailyTask._select_account_by_combo(task, 1)

        self.assertEqual(account, "Display Name")
        self.assertEqual(selected, [1])
        self.assertEqual(task.all_accounts, {normalize_account_name(name) for name in items})

    def test_combo_selection_returns_none_when_every_account_is_done(self):
        items = ["aa****01@example.com", "Display Name"]
        with patch.object(multi_account_module, 'combo_items', return_value=items), \
                patch.object(multi_account_module, 'select_combo_item') as select:
            account = MultiAccountDailyTask._select_account_by_combo(FakeComboTask(done=items), 1)

        self.assertIsNone(account)
        select.assert_not_called()

    def test_combo_selection_raises_when_selection_does_not_stick(self):
        with patch.object(multi_account_module, 'combo_items', return_value=["aa****01@example.com"]), \
                patch.object(multi_account_module, 'select_combo_item', return_value=False):
            with self.assertRaises(Exception):
                MultiAccountDailyTask._select_account_by_combo(FakeComboTask(), 1)

    def test_select_and_login_ends_sweep_through_combo_without_ocr(self):
        with patch.object(multi_account_module, 'combo_items', return_value=["aa****01@example.com"]):
            task = FakeComboTask(done=["aa****01@example.com"])
            self.assertIsNone(MultiAccountDailyTask._select_and_login_account(task))

    def test_detect_current_account_prefers_combo_selection(self):
        with patch.object(multi_account_module, 'combo_selected_item', return_value="Display Name"):
            account = MultiAccountDailyTask._detect_current_account_from_login(FakeComboTask())

        self.assertEqual(account, "Display Name")


    def test_selection_ends_the_sweep_once_every_detected_account_is_done(self):
        class AccountBox:
            def __init__(self, name):
                self.name = name

        class FakeExecutor:
            def get_task_by_class(self, task_class):
                return None

        class FakeTask:
            executor = FakeExecutor()

            def __init__(self):
                self.done_set = {normalize_account_name("aa****01@example.com")}
                self.all_accounts = set()
                self.clicked = []
                self.logs = []
                self.next_frame = Mock()

            _is_done = MultiAccountDailyTask._is_done
            _click_account_in_list = MultiAccountDailyTask._click_account_in_list

            def _login_combo(self):
                return None

            def ocr(self, match=None):
                return [AccountBox("aa****01@example.com")]

            def info_set(self, *args):
                pass

            def click(self, account, after_sleep=0):
                # 下拉入口没有账号名称，仅记录被点击的账号行。
                if hasattr(account, 'name'):
                    self.clicked.append(account.name)

            def log_info(self, *args):
                self.logs.append(args)

            def tr(self, message):
                return message

            def sleep(self, *args):
                pass

            def find_account_drop_down(self):
                return object()

            def do_find_account_drop_down(self):
                return None

            def wait_until(self, condition, time_out=0, raise_if_not_found=False, **_kwargs):
                value = condition()
                if not value and raise_if_not_found:
                    raise WaitFailedException()
                return value

        task = FakeTask()

        self.assertIsNone(MultiAccountDailyTask._select_and_login_account(task))
        self.assertEqual(task.clicked, [])

    def test_selection_still_fails_when_no_account_is_readable(self):
        class FakeExecutor:
            def get_task_by_class(self, task_class):
                return None

        class FakeTask:
            executor = FakeExecutor()

            def __init__(self):
                self.done_set = set()
                self.all_accounts = set()
                self.clicked = []
                self.next_frame = Mock()

            _is_done = MultiAccountDailyTask._is_done
            _click_account_in_list = MultiAccountDailyTask._click_account_in_list

            def _login_combo(self):
                return None

            def ocr(self, match=None):
                return []

            def info_set(self, *args):
                pass

            def click(self, account, after_sleep=0):
                if hasattr(account, 'name'):
                    self.clicked.append(account.name)

            def log_info(self, *args):
                pass

            def tr(self, message):
                return message

            def sleep(self, *args):
                pass

            def find_account_drop_down(self):
                return object()

            def do_find_account_drop_down(self):
                return None

            def wait_until(self, condition, time_out=0, raise_if_not_found=False, **_kwargs):
                value = condition()
                if not value and raise_if_not_found:
                    raise WaitFailedException()
                return value

        with self.assertRaises(WaitFailedException):
            MultiAccountDailyTask._select_and_login_account(FakeTask())

    def native_combo_task(self, **kwargs):
        scenario = NativeComboScenario(self, **kwargs)
        return scenario.task, scenario

    def test_native_selection_clicks_visible_row_when_navigation_keys_have_no_effect(self):
        task, state = self.native_combo_task()
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        self.assertEqual(task.click.call_args_list[0].args, (200, 215))
        self.assertEqual(state.index, 2)
        self.assertFalse(state.dropped)
        self.assertEqual(task.click.call_count, 2)
        task.send_key.assert_not_called()

    def test_native_selection_keeps_an_already_open_list(self):
        task, state = self.native_combo_task()
        state.dropped = True
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        self.assertEqual(state.index, 2)
        self.assertEqual(task.click.call_count, 1)
        task.send_key.assert_not_called()

    def test_native_editable_combo_uses_shortcut_only_to_open_then_clicks_the_row(self):
        task, state = self.native_combo_task()

        def click_without_opening(*args, **kwargs):
            if state.dropped:
                state.click(*args, **kwargs)

        def open_with_shortcut(key, **kwargs):
            if key == 'f4':
                state.dropped = True

        task.click.side_effect = click_without_opening
        task.send_key.side_effect = open_with_shortcut
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        self.assertEqual(state.index, 2)
        self.assertEqual(task.send_key.call_args_list[0].args, ('f4',))
        self.assertEqual(task.send_key.call_count, 1)

    def test_native_selection_does_not_report_success_when_row_click_has_no_effect(self):
        task, state = self.native_combo_task()
        state.ignore_row_click = True
        self.assertFalse(select_combo_item(task, 42, 2, '目标账号'))
        self.assertEqual(state.index, 0)
        self.assertTrue(state.dropped)

    def test_native_selection_stops_before_input_after_dialog_or_items_change(self):
        task, state = self.native_combo_task()
        for combo, item in [(43, '目标账号'), (42, '过期账号')]:
            with self.subTest(combo=combo, item=item):
                state.combo = combo
                self.assertFalse(select_combo_item(task, 42, 2, item))
        task.click.assert_not_called()
        task.send_key.assert_not_called()

    def test_native_selection_does_not_reopen_an_already_confirmed_account(self):
        task, state = self.native_combo_task()
        state.index = 2
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        task.click.assert_not_called()
        task.scroll.assert_not_called()
        task.send_key.assert_not_called()

    def test_native_selection_handles_upward_lists_and_scaled_screen_pixels(self):
        for upward in (False, True):
            for scale in (1, 2):
                with self.subTest(upward=upward, scale=scale):
                    task, state = self.native_combo_task(upward=upward, scale=scale)
                    self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
                    self.assertEqual(state.index, 2)
                    self.assertEqual(task.click.call_args_list[0].args, (200 * scale, 215 * scale))
                    task.send_key.assert_not_called()

    def test_native_selection_scrolls_in_both_directions_before_clicking(self):
        items = [f'测试账号{i}' for i in range(8)]
        for top_index, target in ((0, 7), (5, 0)):
            with self.subTest(top_index=top_index, target=target):
                task, state = self.native_combo_task(items=items, top_index=top_index, visible_rows=2)
                state.index = (target + 1) % len(items)
                self.assertTrue(select_combo_item(task, 42, target, items[target]))
                self.assertEqual(state.index, target)
                self.assertFalse(state.dropped)
                self.assertGreater(task.scroll.call_count, 0)
                task.send_key.assert_not_called()

    def test_native_selection_scrolls_a_partially_visible_row_before_clicking(self):
        task, state = self.native_combo_task()
        state.viewport_height = 100
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        self.assertEqual(state.index, 2)
        task.scroll.assert_called_once()

    def test_native_selection_bounds_stalled_or_oscillating_scrolls(self):
        items = [f'测试账号{i}' for i in range(8)]
        for stalled, step, target in ((True, 1, 7), (False, 3, 2)):
            with self.subTest(stalled=stalled, step=step):
                task, state = self.native_combo_task(items=items, visible_rows=2)
                state.scroll_stalled, state.scroll_step = stalled, step
                self.assertFalse(select_combo_item(task, 42, target, items[target]))
                self.assertEqual(state.index, 0)
                self.assertTrue(state.dropped)
                self.assertLessEqual(task.scroll.call_count, 2)
                self.assertEqual(task.click.call_count, 1)

    def test_native_selection_rejects_unusable_or_foreign_list_geometry(self):
        for attribute, value in (('info_failed', True), ('rect_failed', True), ('info_combo', 99),
                                 ('viewport_height', 0), ('viewport_height', 20)):
            with self.subTest(attribute=attribute):
                task, state = self.native_combo_task()
                state.dropped = True
                setattr(state, attribute, value)
                self.assertFalse(select_combo_item(task, 42, 2, '目标账号'))
                task.click.assert_not_called()
                task.scroll.assert_not_called()

    def test_native_selection_does_not_click_a_row_outside_the_capture(self):
        task, state = self.native_combo_task(upward=True)
        state.viewport_height = 500
        state.dropped = True
        self.assertFalse(select_combo_item(task, 42, 1, '第二个账号'))
        task.click.assert_not_called()
        task.scroll.assert_not_called()

    def test_native_selection_rechecks_dialog_identity_and_account_order_after_opening(self):
        for changed in ('dialog', 'identity', 'items'):
            with self.subTest(changed=changed):
                task, state = self.native_combo_task()

                def click_and_change(*args, **kwargs):
                    state.click(*args, **kwargs)
                    if changed == 'dialog':
                        state.combo = 43
                    elif changed == 'identity':
                        state.identity = (11, 21)
                    else:
                        state.items.reverse()

                task.click.side_effect = click_and_change
                self.assertFalse(select_combo_item(task, 42, 2, '目标账号'))
                self.assertEqual(task.click.call_count, 1)
                self.assertEqual(state.index, 0)

    def test_native_selection_discards_old_points_after_silent_foreground_changes(self):
        for changed in ('epoch', 'generation', 'origin', 'capture'):
            with self.subTest(changed=changed):
                task, state = self.native_combo_task()

                def click_and_change(*args, **kwargs):
                    state.click(*args, **kwargs)
                    if changed == 'epoch':
                        task._foreground_epoch += 1
                    elif changed == 'generation':
                        task.executor.interaction.target_generation += 1
                    elif changed == 'origin':
                        state.origin = (-1700, 100)
                    else:
                        task.executor.interaction.capture = SimpleNamespace(get_abs_cords=lambda x, y: (x, y))

                task.click.side_effect = click_and_change
                with self.assertRaises(ForegroundInputInterrupted):
                    select_combo_item(task, 42, 2, '目标账号')
                self.assertEqual(task.click.call_count, 1)
                self.assertEqual(state.index, 0)

    def test_native_selection_confirms_a_completed_click_after_foreground_recovery(self):
        task, state = self.native_combo_task()

        def click_and_resume(*args, **kwargs):
            state.click(*args, **kwargs)
            if not state.dropped:
                task._foreground_epoch += 1

        task.click.side_effect = click_and_resume
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        self.assertEqual(state.index, 2)
        self.assertEqual(task.click.call_count, 2)

    def test_native_selection_requeries_row_geometry_immediately_before_input(self):
        for changed in ('scroll', 'list'):
            with self.subTest(changed=changed):
                task, state = self.native_combo_task(items=['账号一', '账号二', '目标账号', '账号四'])
                state.dropped = True
                moved = False

                def query_then_change(hwnd, message, wparam, lparam):
                    nonlocal moved
                    result = state.read_message(hwnd, message, wparam, lparam)
                    if message == 0x198 and not moved:
                        moved = True
                        if changed == 'scroll':
                            state.top_index = 1
                        else:
                            state.list_hwnd += 1
                    return result

                with patch.object(multi_account_module, '_SendMessageW', side_effect=query_then_change):
                    with self.assertRaises(ForegroundInputInterrupted):
                        select_combo_item(task, 42, 2, '目标账号')
                task.click.assert_not_called()
                task.scroll.assert_not_called()

    def test_native_selection_discards_points_when_only_the_list_window_moves(self):
        task, state = self.native_combo_task()
        state.dropped = True
        moved = False

        def get_position_then_move(hwnd, point):
            nonlocal moved
            position = state.client_to_screen(hwnd, point)
            if hwnd == state.list_hwnd and point == (0, 0) and not moved:
                moved = True
                state.combo_origin = (state.combo_origin[0] + 30, state.combo_origin[1])
            return position

        with patch('win32gui.ClientToScreen', side_effect=get_position_then_move):
            with self.assertRaises(ForegroundInputInterrupted):
                select_combo_item(task, 42, 2, '目标账号')
        task.click.assert_not_called()
        task.scroll.assert_not_called()

    def test_native_selection_propagates_cancellation_and_input_failures(self):
        for error_type in (TaskDisabledException, ForegroundInputError, ForegroundInputInterrupted):
            for operation in ('click', 'scroll'):
                with self.subTest(error=error_type.__name__, operation=operation):
                    task, state = self.native_combo_task(visible_rows=2)
                    state.dropped = True
                    if operation == 'click':
                        state.viewport_height = 120
                    getattr(task, operation).side_effect = error_type()
                    with self.assertRaises(error_type):
                        select_combo_item(task, 42, 2, '目标账号')
                    self.assertEqual(state.index, 0)

    def test_native_confirmation_failure_prevents_daily_login(self):
        task = FakeComboTask()
        with patch.object(multi_account_module, 'combo_items', return_value=['目标账号']), \
                patch.object(multi_account_module, 'select_combo_item', return_value=False):
            with self.assertRaises(Exception):
                MultiAccountDailyTask._select_and_login_account(task)
        task.sleep.assert_not_called()

    def test_unconfirmed_account_is_not_reused_after_later_dropdown_failures(self):
        task = SimpleNamespace(
            tr=str, _login_combo=lambda: None, sleep=Mock(),
            next_frame=Mock(),
            find_account_drop_down=Mock(return_value=object()), click=Mock(),
            do_find_account_drop_down=Mock(side_effect=[False, True, True, True, True]),
            _click_account_in_list=lambda: '目标账号',
            wait_until=lambda condition, **kwargs: condition(),
            _detect_current_account_from_login=lambda: '其它账号',
            _same_account=lambda left, right: left == right,
            log_info=Mock(), log_error=Mock(), screenshot=Mock(),
            find_boxes=Mock(), ensure_main=Mock(),
        )
        with self.assertRaises(WaitFailedException):
            MultiAccountDailyTask._select_and_login_account(task)
        task.find_boxes.assert_not_called()
        task.ensure_main.assert_not_called()

    def login_task(self, displayed):
        return SimpleNamespace(
            sleep=Mock(), next_frame=Mock(), wait_foreground_frame=Mock(),
            _detect_current_account_from_login=Mock(side_effect=displayed),
            ocr=Mock(return_value=[]), box_of_screen=Mock(),
            find_boxes=Mock(return_value=['登录按钮']), click=Mock(),
            click_relative=Mock(), ensure_main=Mock(),
        )

    def real_login_chain(self, task_class, resumed_account='账号B', silent_resume=False, start_game=False,
                         start_game_account=None):
        """真实公共调用链，仅替换画面、前台等待和系统输入，不初始化应用。"""
        state = SimpleNamespace(screen='login', account='账号A', logged_in=False, settle_count=0)
        task = task_class.__new__(task_class)
        task._executor = SimpleNamespace(debug=False, business_time=time.monotonic)
        task._app = None
        task.tr = str
        task.info_set = Mock()
        task.log_info = Mock()
        task.log_debug = Mock()
        task.next_frame = Mock()
        task.in_team_and_world = lambda: state.screen == 'world'
        task.in_realm = lambda: False
        task.handle_monthly_card = lambda: False
        task.find_one = Mock(return_value=None)
        task.box_of_screen = Mock()
        task.ocr = lambda **kwargs: ['登录'] if state.screen == 'login' else ['开始游戏']
        task._detect_current_account_from_login = Mock(side_effect=lambda: state.account)
        task.back = Mock()

        def find_boxes(texts, match=None, **kwargs):
            if match == LOGIN_TEXTS:
                return ['登录'] if state.screen == 'login' else None
            if isinstance(match, list) and match[0] == '开始游戏':
                return ['开始游戏'] if state.screen == 'start' else None
            return None

        def wait_until(condition, **kwargs):
            for _ in range(6):
                if result := condition():
                    return result
            return None

        def resume():
            state.account = resumed_account

        def sleep(seconds):
            if seconds == LOGIN_CLICK_SETTLE_TIME:
                state.settle_count += 1
                # 首次等待属于外层选号；第二次才是 wait_login 的实际提交等待。
                if state.settle_count == 2:
                    if silent_resume:
                        task.wait_foreground_frame()
                    else:
                        raise ForegroundInputInterrupted()

        def click(*args, **kwargs):
            if task.click.call_count == 1 and start_game:
                state.screen = 'start'
                state.account = start_game_account
            elif task.click.call_count > 1:
                state.screen = 'world'

        task.find_boxes = find_boxes
        task.wait_until = wait_until
        task.wait_foreground_frame = Mock(side_effect=resume)
        task.sleep = Mock(side_effect=sleep)
        task.click = Mock(side_effect=click)
        task.click_relative = Mock()
        self.enterContext(patch.object(og, 'my_app', state, create=True))
        return task, state

    def run_guarded_login(self, task):
        if isinstance(task, MultiAccountConfigTask):
            with patch.object(MultiAccountConfigTask, '_select_target_account'):
                task._login_target_account('账号A')
        else:
            task._login_selected_account('账号A', lambda expected, displayed: expected == displayed)

    def test_real_login_chain_blocks_changed_account_after_inner_focus_recovery(self):
        for task_class in (MultiAccountDailyTask, MultiAccountConfigTask):
            for silent_resume in (False, True):
                with self.subTest(task_class=task_class.__name__, silent_resume=silent_resume):
                    task, state = self.real_login_chain(task_class, silent_resume=silent_resume)
                    with self.assertRaisesRegex(WaitFailedException, '账号确认失败'):
                        self.run_guarded_login(task)
                    self.assertEqual(state.account, '账号B')
                    self.assertEqual(task.click.call_count, 1)
                    task.click_relative.assert_not_called()
                    task.wait_foreground_frame.assert_called_once()
                    self.assertNotIn('_login_account_guard', task.__dict__)

    def test_real_login_chain_resumes_when_target_account_is_unchanged(self):
        for task_class in (MultiAccountDailyTask, MultiAccountConfigTask):
            with self.subTest(task_class=task_class.__name__):
                task, state = self.real_login_chain(task_class, resumed_account='账号A')
                self.run_guarded_login(task)
                self.assertEqual(state.screen, 'world')
                self.assertEqual(task.click.call_count, 2)
                task.wait_foreground_frame.assert_called_once()
                self.assertNotIn('_login_account_guard', task.__dict__)

    def test_real_login_chain_cancellation_restores_outer_confirmation_context(self):
        for task_class in (MultiAccountDailyTask, MultiAccountConfigTask):
            with self.subTest(task_class=task_class.__name__):
                task, _ = self.real_login_chain(task_class)
                previous = Mock()
                task._login_account_guard = previous
                task.wait_foreground_frame.side_effect = TaskDisabledException()
                with self.assertRaises(TaskDisabledException):
                    self.run_guarded_login(task)
                self.assertIs(task._login_account_guard, previous)
                previous.assert_not_called()
                self.assertEqual(task.click.call_count, 1)
                BaseWWTask._confirm_login_account(task, require_visible=False)
                previous.assert_called_once_with(require_visible=False)

    def test_authenticated_start_game_can_continue_without_a_visible_account_field(self):
        for task_class in (MultiAccountDailyTask, MultiAccountConfigTask):
            with self.subTest(task_class=task_class.__name__):
                task, state = self.real_login_chain(task_class, start_game=True)
                self.run_guarded_login(task)
                self.assertEqual(state.screen, 'world')
                self.assertEqual(task.click.call_count, 2)
                self.assertIsNone(state.account)
                self.assertNotIn('_login_account_guard', task.__dict__)

    def test_start_game_blocks_a_different_visible_account_before_entering_world(self):
        for task_class in (MultiAccountDailyTask, MultiAccountConfigTask):
            with self.subTest(task_class=task_class.__name__):
                task, state = self.real_login_chain(task_class, start_game=True, start_game_account='账号B')
                with self.assertRaisesRegex(WaitFailedException, '账号确认失败'):
                    self.run_guarded_login(task)
                self.assertEqual(state.screen, 'start')
                self.assertEqual(task.click.call_count, 1)
                self.assertNotIn('_login_account_guard', task.__dict__)

    def test_coordinate_login_fallback_checks_account_after_ocr_before_submission(self):
        task = self.login_task(['账号B'])
        task.find_boxes.return_value = None
        with self.assertRaisesRegex(WaitFailedException, '账号确认失败'):
            MultiAccountDailyTask._login_selected_account(task, '账号A', lambda expected, displayed: expected == displayed)
        task.click.assert_not_called()
        task.click_relative.assert_not_called()
        task.ensure_main.assert_not_called()
        self.assertNotIn('_login_account_guard', task.__dict__)

    def test_interrupted_login_observes_completion_without_resending_click(self):
        task = self.login_task(['目标账号', None])
        task.click.side_effect = ForegroundInputInterrupted()
        MultiAccountDailyTask._login_selected_account(task, '目标账号', lambda a, b: a == b)
        task.click.assert_called_once()
        task.wait_foreground_frame.assert_called_once()
        task.ensure_main.assert_called_once_with(time_out=180)

    def test_interrupted_login_rejects_a_changed_displayed_account(self):
        task = self.login_task(['目标账号', '其它账号'])
        task.click.side_effect = ForegroundInputInterrupted()
        with self.assertRaisesRegex(WaitFailedException, '恢复后账号确认失败'):
            MultiAccountDailyTask._login_selected_account(task, '目标账号', lambda a, b: a == b)
        task.click.assert_called_once()
        task.ensure_main.assert_not_called()

    def test_focus_wait_during_login_completion_does_not_restart_submission(self):
        task = self.login_task(['目标账号', None])
        task.ensure_main.side_effect = [ForegroundInputInterrupted(), None]
        MultiAccountDailyTask._login_selected_account(task, '目标账号', lambda a, b: a == b)
        task.click.assert_called_once()
        self.assertEqual(task.ensure_main.call_count, 2)
        task.wait_foreground_frame.assert_called_once()

    def test_cancellation_during_login_focus_wait_is_propagated(self):
        task = self.login_task(['目标账号'])
        task.click.side_effect = ForegroundInputInterrupted()
        task.wait_foreground_frame.side_effect = TaskDisabledException()
        with self.assertRaises(TaskDisabledException):
            MultiAccountDailyTask._login_selected_account(task, '目标账号', lambda a, b: a == b)
        task.ensure_main.assert_not_called()

    def test_system_input_failure_during_login_is_not_retried(self):
        task = self.login_task(['目标账号'])
        task.click.side_effect = ForegroundInputError()
        with self.assertRaises(ForegroundInputError):
            MultiAccountDailyTask._login_selected_account(task, '目标账号', lambda a, b: a == b)
        task.click.assert_called_once()
        task.wait_foreground_frame.assert_not_called()
        task.ensure_main.assert_not_called()
        self.assertNotIn('_login_account_guard', task.__dict__)

    def test_logout_resumes_an_already_open_menu_instead_of_toggling_escape(self):
        state = SimpleNamespace(page='主界面')
        task = SimpleNamespace(
            tr=str, log_info=Mock(), next_frame=Mock(), wait_foreground_frame=Mock(),
            _login_combo=lambda: 42 if state.page == '登录' else None,
            do_find_account_drop_down=lambda: None, wait_feature=Mock(),
            find_one=lambda feature: state.page == '设置' if feature == 'esc_setting' else None,
            click_confirm=Mock(), wait_until=lambda condition, **kwargs: condition(),
        )

        def open_menu(*args, **kwargs):
            state.page = '设置'
            raise ForegroundInputInterrupted()

        task.send_key = Mock(side_effect=open_menu)
        task.click_relative = Mock(side_effect=lambda *args, **kwargs: setattr(state, 'page', '登录'))
        MultiAccountDailyTask._switch_to_login(task)
        task.send_key.assert_called_once()
        task.click_relative.assert_called_once()
        task.wait_foreground_frame.assert_called_once()


if __name__ == "__main__":
    unittest.main()
