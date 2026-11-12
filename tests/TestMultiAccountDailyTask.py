import unittest
import time
from contextlib import nullcontext
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

    def native_combo_task(self, extended=False):
        items = ['第一个账号', '第二个账号', '目标账号']
        state = SimpleNamespace(index=0, dropped=False, focused=False, combo=42)
        task = SimpleNamespace(
            width=1600, height=900, next_frame=Mock(), _login_combo=lambda: state.combo,
            executor=SimpleNamespace(interaction=SimpleNamespace(
                capture=SimpleNamespace(get_abs_cords=lambda x, y: (x - 1800, y + 100)))),
            wait_until=lambda condition, **kwargs: condition(),
        )

        def click(x, y, **kwargs):
            # 负屏幕坐标和登录子窗口位置必须反算为同一捕获坐标，才会点中控件。
            state.focused = (x, y) == (200, 215)

        def send_key(key, **kwargs):
            if not state.focused:
                return
            if key == ('down' if extended else 'f4') and not state.dropped:
                state.dropped = True
            elif state.dropped:
                if key == 'home':
                    state.index = 0
                elif key == 'down':
                    state.index = min(len(items) - 1, state.index + 1)
                elif key == 'enter':
                    state.dropped = False

        def read_message(combo, message, wparam, lparam):
            values = {
                multi_account_module.CB_GETCURSEL: state.index,
                multi_account_module.CB_GETDROPPEDSTATE: int(state.dropped),
                multi_account_module.CB_GETITEMHEIGHT: 30,
                multi_account_module.CB_GETEXTENDEDUI: int(extended),
            }
            if message not in values:
                raise AssertionError('只能查询原生控件，不能发送写消息')
            return values[message]

        task.click = Mock(side_effect=click)
        task.send_key = Mock(side_effect=send_key)
        self.enterContext(patch.object(multi_account_module, '_SendMessageW', side_effect=read_message))
        self.enterContext(patch.object(multi_account_module, 'combo_items', return_value=items))
        self.enterContext(patch.object(multi_account_module, 'combo_selected_item',
                                      side_effect=lambda combo: items[state.index]))
        self.enterContext(patch('win32gui.GetClientRect', return_value=(0, 0, 200, 240)))
        self.enterContext(patch('win32gui.ClientToScreen',
                                side_effect=lambda combo, point: (point[0] - 1700, point[1] + 300)))
        return task, state

    def test_native_selection_uses_capture_coordinates_and_confirms_keyboard_result(self):
        task, state = self.native_combo_task()
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        task.click.assert_called_once_with(200, 215, after_sleep=.2)
        self.assertEqual(state.index, 2)
        self.assertFalse(state.dropped)
        self.assertEqual([call.args[0] for call in task.send_key.call_args_list],
                         ['f4', 'home', 'down', 'down', 'enter'])

    def test_native_extended_ui_opens_with_down_before_selecting(self):
        task, state = self.native_combo_task(extended=True)
        self.assertTrue(select_combo_item(task, 42, 2, '目标账号'))
        self.assertEqual(state.index, 2)
        self.assertEqual(task.send_key.call_args_list[0].args, ('down',))

    def test_native_selection_does_not_report_success_when_key_has_no_effect(self):
        task, _ = self.native_combo_task()
        task.send_key.side_effect = None
        self.assertFalse(select_combo_item(task, 42, 2, '目标账号'))

    def test_native_selection_stops_before_input_after_dialog_or_items_change(self):
        task, state = self.native_combo_task()
        for combo, item in [(43, '目标账号'), (42, '过期账号')]:
            with self.subTest(combo=combo, item=item):
                state.combo = combo
                self.assertFalse(select_combo_item(task, 42, 2, item))
        task.click.assert_not_called()
        task.send_key.assert_not_called()

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
