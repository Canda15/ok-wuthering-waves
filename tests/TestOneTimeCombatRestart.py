import unittest
from contextlib import nullcontext
from copy import deepcopy
from itertools import count, product
from pathlib import Path
from unittest.mock import Mock, call, patch

import cv2

from config import config
from ok import TaskDisabledException, ForegroundInputError, ForegroundInputInterrupted
from ok.feature.Box import Box
from ok.task.TaskExecutor import TaskExecutor
from ok.test.TaskTestCase import TaskTestCase
from src.char.BaseChar import BaseChar
from src.scene.WWScene import WWScene
from src.task.DailyTask import DailyTask
from src.task.FarmEchoTask import FarmEchoTask
from src.task.GardenTask import GardenTask
from src.task.WWOneTimeTask import WWOneTimeTask


class TestOneTimeCombatRestart(unittest.TestCase):
    def setUp(self):
        self.scene = WWScene()
        self.executor = Mock(scene=self.scene, debug=False, interaction=None)
        self.task = FarmEchoTask(self.executor, None)
        self.task.scene = self.scene
        self.task.config = dict(self.task.default_config)
        self.task._enabled = True
        self.task.next_frame = Mock()
        self.task.in_combat = Mock(return_value=False)
        self.task.wait_feature = Mock(return_value=None)
        # 使用真实启动战斗检查，捕获与输入全部隔离。
        self.task.sleep = Mock(side_effect=lambda seconds: self.task.sleep_check())

    def stop_during_combat(self, skip_combat_check=False):
        char = BaseChar(self.task, 0, char_name='previous_team')
        char.is_current_char = True

        def interrupt_rotation():
            self.task.chars = [char, None, None]
            self.task._in_combat = True
            self.task.skip_combat_check = skip_combat_check
            self.task.in_liberation = True
            self.task.cds = {'resonance': 12}
            self.task.freeze_durations = [(1, 2, 0.1)]
            self.scene.set_in_combat()
            self.task.disable()
            raise TaskDisabledException()

        self.task.do_run = Mock(side_effect=interrupt_rotation)
        with self.assertRaises(TaskDisabledException):
            self.task.run()
        self.assertFalse(self.task.enabled)
        self.assertIs(self.task.chars[0], char)

    def test_restart_after_stop_reaches_farming_outside_combat(self):
        self.stop_during_combat()
        self.task._enabled = True
        self.task.do_run = Mock(return_value=True)

        # 旧战斗状态会在启动时错误触发 NotInCombatException。
        self.assertTrue(self.task.run())

        self.task.do_run.assert_called_once()

    def test_restart_drops_previous_rotation_before_startup_sleep(self):
        self.stop_during_combat(skip_combat_check=True)
        self.task._enabled = True

        def check_startup_state(seconds):
            self.assertFalse(self.task._in_combat)
            self.assertFalse(self.task.in_liberation)
            self.assertFalse(self.task.skip_combat_check)
            self.assertIsNone(self.task.get_current_char())
            self.assertEqual(self.task.cds, {})
            self.assertEqual(self.task.freeze_durations, [])
            self.assertFalse(self.scene.in_combat())
            self.task.sleep_check()

        self.task.sleep.side_effect = check_startup_state
        self.task.do_run = Mock(return_value=True)

        self.assertTrue(self.task.run())

        self.task.sleep.assert_called_with(0.5)
        self.task.do_run.assert_called_once()

    def test_noncombat_task_keeps_normal_startup(self):
        task = GardenTask(self.executor, None)
        task.sleep = Mock()

        WWOneTimeTask.run(task)

        task.sleep.assert_called_once_with(0.5)


class TestGardenForegroundRecovery(unittest.TestCase):
    def make_task(self):
        task = GardenTask.__new__(GardenTask)
        task.tr = str
        task._executor = Mock(frame=Mock(shape=(1440, 2560, 3)))
        task._foreground_epoch = 0
        task.executor.task_context.side_effect = lambda inner: nullcontext()
        task._garden_phase = 'play'
        task._garden_needs_refresh = False
        task._garden_pending_source = None
        task.sleep = Mock()
        task.next_frame = Mock(return_value=task.frame)
        task.wait_foreground_frame = Mock()
        task.find_best_garden_feature = Mock(return_value=None)
        task.find_one = Mock(return_value=None)
        task.ocr = Mock(return_value=[])
        task.wait_ocr = Mock(return_value=[])
        task.wait_feature = Mock(return_value=Box(1, 1, 10, 10, name='确认'))
        task.get_box_by_name = lambda name: Box(1, 1, 10, 10, name=name)
        task.ensure_main = Mock()
        task.openF2Book = Mock()
        task.info_set = Mock()
        task.log_info = Mock()
        task.screenshot = Mock()
        task.click = Mock()
        task.back = Mock()
        clock = count(0, .1)
        task.business_time = lambda: next(clock)
        return task

    def prepare_weekly_pair(self, task, *, points=6000, claimed=False, effective=True):
        page = {'points': points, 'claimed': claimed, 'popup': False, 'submitted': 0, 'pair': []}

        def read_page(*args, **kwargs):
            if kwargs.get('match') is task.WEEKLY_REWARD_TITLE_PATTERN:
                return [Box(1220, 340, 120, 55, name='获得')] if page['popup'] else []
            if page['popup']:
                return []
            if args == (.09, .08, .41, .17):
                return [Box(776, 161, 149, 48, name='周度游历')]
            if args == (.11, .36, .38, .44):
                return [Box(339, 556, 192, 45, name='幻梦游园')]
            return [Box(496, 1210, 180, 55, name=str(page['points']))]

        def click_page(*args, **kwargs):
            if args != task.WEEKLY_REWARD_POINT:
                return
            page['pair'].append('close' if page['popup'] else 'first')
            if page['popup']:
                page['popup'] = False
            elif not page['claimed'] and page['points'] >= 6000 and effective:
                page['submitted'] += 1
                page['claimed'] = True
                page['popup'] = True

        task.ocr.side_effect = read_page
        task.click.side_effect = click_page
        task.find_one.side_effect = lambda name, **kwargs: (
            Box(2332, 1244, 76, 68, name=name)
            if name == 'weekly_garden_reward_claimed' and page['claimed'] and not page['popup'] else None)
        return page

    def bind_weekly_foreground_context(self, task):
        executor = task.executor
        outer = Mock()
        executor.current_task = outer
        executor._task_context_stack = []
        executor.task_context.side_effect = lambda inner: TaskExecutor.task_context(executor, inner)
        return executor, outer

    def test_weekly_pair_preserves_original_order_and_waits(self):
        task = self.make_task()
        page = self.prepare_weekly_pair(task)
        calls = Mock()
        calls.attach_mock(task.openF2Book, 'book')
        calls.attach_mock(task.sleep, 'sleep')
        calls.attach_mock(task.click, 'click')

        task.open_garden_weekly_page()

        self.assertEqual([
            call.book('gray_book_quest'), call.sleep(1), call.click(.343, .129, after_sleep=1),
            call.click(.927, .893, after_sleep=3), call.click(.927, .893, after_sleep=2),
        ], calls.mock_calls)
        self.assertEqual(1, page['submitted'])
        self.assertFalse(page['popup'])
        self.assertIsNone(task._garden_weekly_step)
        task.back.assert_not_called()

    def test_daily_full_points_uses_original_pair_before_skipping_play(self):
        task = self.make_task()
        page = self.prepare_weekly_pair(task)
        daily = DailyTask.__new__(DailyTask)
        daily.tr = str
        daily.info_set = Mock()
        daily.log_info = Mock()
        daily.get_task_by_class = Mock(return_value=task)
        daily.run_task_by_class = Mock()

        daily.check_weekly_garden()

        self.assertEqual(['first', 'close'], page['pair'])
        self.assertEqual(1, page['submitted'])
        daily.run_task_by_class.assert_not_called()

    def test_return_from_garden_uses_original_pair_without_replaying_play(self):
        task = self.make_task()
        page = self.prepare_weekly_pair(task)
        task._garden_phase = 'result_return'
        task._leave_garden_result = Mock()

        task.resume_after_foreground()

        task._leave_garden_result.assert_called_once()
        self.assertEqual(['first', 'close'], page['pair'])
        self.assertEqual('completed', task._garden_phase)
        task.ensure_main.assert_not_called()

    def test_claimed_or_locked_last_tier_preserves_original_noop_pair(self):
        for points in (0, 3500, 6000):
            with self.subTest(points=points):
                task = self.make_task()
                page = self.prepare_weekly_pair(task, points=points, claimed=points == 6000)
                task.open_garden_weekly_page()
                task.open_garden_weekly_page()
                self.assertEqual(['first'] * 4, page['pair'])
                self.assertEqual(0, page['submitted'])
                self.assertIsNone(task._garden_weekly_step)
                self.assertFalse(page['popup'])

    def test_unknown_first_click_saves_evidence_and_never_replays(self):
        task = self.make_task()
        page = self.prepare_weekly_pair(task, effective=False)
        for _ in range(2):
            with self.assertRaisesRegex(ForegroundInputError, '首次领奖结果'):
                task.open_garden_weekly_page()
        self.assertEqual(['first'], page['pair'])
        self.assertEqual('first_pending', task._garden_weekly_step)
        task.wait_foreground_frame.assert_not_called()
        task.screenshot.assert_called_with('garden_result_unknown')

    def test_weekly_input_failure_and_cancellation_are_not_retried(self):
        for error in (ForegroundInputError, TaskDisabledException):
            with self.subTest(error=error):
                task = self.make_task()
                self.prepare_weekly_pair(task)

                def fail_first(*args, **kwargs):
                    if args == task.WEEKLY_REWARD_POINT:
                        raise error('受控输入失败')

                task.click.side_effect = fail_first
                with self.assertRaises(error):
                    task.open_garden_weekly_page()
                self.assertEqual(1, sum(item.args == task.WEEKLY_REWARD_POINT for item in task.click.call_args_list))
                task.wait_foreground_frame.assert_not_called()
                self.assertEqual('first_pending', task._garden_weekly_step)

    def test_pair_focus_resume_checks_result_and_never_repeats_first(self):
        for interrupt_at, attempts in (('after_first', 2), ('before_close', 3), ('after_close', 2)):
            with self.subTest(interrupt_at=interrupt_at):
                task = self.make_task()
                page = self.prepare_weekly_pair(task)
                executor, outer = self.bind_weekly_foreground_context(task)
                click_page = task.click.side_effect
                interrupted = False

                def interrupt_once(*args, **kwargs):
                    nonlocal interrupted
                    closing = args == task.WEEKLY_REWARD_POINT and page['popup']
                    if closing and interrupt_at == 'before_close' and not interrupted:
                        interrupted = True
                        TaskExecutor._notify_tasks_resumed(executor)
                    click_page(*args, **kwargs)
                    if args == task.WEEKLY_REWARD_POINT and not interrupted and (
                            interrupt_at == 'after_first' and not closing
                            or interrupt_at == 'after_close' and closing):
                        interrupted = True
                        TaskExecutor._notify_tasks_resumed(executor)

                task.click.side_effect = interrupt_once
                task.wait_foreground_frame.side_effect = [ForegroundInputInterrupted('等待期间再次失焦'), None]
                task.open_garden_weekly_page()

                self.assertEqual(1, page['submitted'])
                self.assertFalse(page['popup'])
                self.assertEqual(attempts, sum(item.args == task.WEEKLY_REWARD_POINT for item in task.click.call_args_list))
                self.assertIsNone(task._garden_weekly_step)
                self.assertEqual([], executor._task_context_stack)
                self.assertIs(outer, executor.current_task)
                outer.on_foreground_resume.assert_called_once()

    def test_stale_first_result_is_not_accepted(self):
        for changed_at in ('title', 'claimed'):
            with self.subTest(changed_at=changed_at):
                task = self.make_task()
                page = self.prepare_weekly_pair(task, claimed=changed_at == 'claimed')
                page['popup'] = changed_at == 'title'
                task._garden_weekly_step = 'first_pending'
                if changed_at == 'title':
                    read = task.ocr.side_effect
                    def stale_title(*args, **kwargs):
                        result = read(*args, **kwargs)
                        if kwargs.get('match') is task.WEEKLY_REWARD_TITLE_PATTERN:
                            task._foreground_epoch += 1
                        return result
                    task.ocr.side_effect = stale_title
                else:
                    def stale_claimed(*args, **kwargs):
                        task._foreground_epoch += 1
                        return Box(2332, 1244, 76, 68, name='weekly_garden_reward_claimed')
                    task.find_one.side_effect = stale_claimed
                with self.assertRaises(ForegroundInputInterrupted):
                    task._confirm_weekly_first_click(task.frame)
                task.click.assert_not_called()
                self.assertEqual('first_pending', task._garden_weekly_step)

    def test_weekly_limit_preserves_noop_without_starting_garden(self):
        task = self.make_task()
        task.open_garden_weekly_page = Mock()
        task.ocr.return_value = [Box(496, 1210, 160, 55, name='6000')]

        task.run()

        task.open_garden_weekly_page.assert_called_once()
        task.sleep.assert_called_once_with(.5)
        task.click.assert_not_called()
        self.assertEqual('completed', task._garden_phase)

    def test_current_weekly_points_ignore_reward_scale_and_progress_target(self):
        task = self.make_task()
        for text, completed in (
            ('0', False), ('3500', False), ('5999', False), ('6000', True),
            ('6,000', True), ('3500/6000', False), ('6000/6000', True), ('6001', True),
        ):
            with self.subTest(text=text):
                task.ocr.return_value = [
                    Box(496, 1210, 180, 55, name=text),
                    Box(1557, 969, 120, 45, name='6000'),
                    Box(2219, 1286, 104, 45, name='6000'),
                ]
                self.assertEqual(completed, task.is_weekly_garden_completed())

        self.assertFalse(task.is_garden_done([]))
        self.assertFalse(task.is_garden_done(['3500/6000']))
        self.assertTrue(task.is_garden_done(['6000/6000']))
        self.assertTrue(task.is_garden_done(['已达到上限']))

    def test_missing_current_points_reads_the_same_frame_region_once(self):
        for points in ('0', '8', '3500', '6000', '6001'):
            with self.subTest(points=points):
                task = self.make_task()
                frame = task.frame
                task.ocr.side_effect = [
                    [Box(2219, 1286, 104, 45, name='6000')],
                    [Box(496, 1210, 180, 55, name=points)],
                ]

                self.assertEqual(int(points) >= 6000, task.is_weekly_garden_completed())

                self.assertEqual(2, task.ocr.call_count)
                first, local = task.ocr.call_args_list
                self.assertIs(first.kwargs['frame'], frame)
                self.assertIs(local.kwargs['frame'], frame)
                self.assertEqual(local.args, (.102, .793, .284, .956))
                task.click.assert_not_called()

    def test_missing_or_ambiguous_current_points_stops(self):
        for current, expected_reads in (
            ([Box(2219, 1286, 104, 45, name='6000')], 2),
            ([Box(496, 1210, 40, 55, name='O')], 1),
            ([Box(496, 1210, 40, 55, name='0'), Box(550, 1210, 160, 55, name='6000')], 1),
        ):
            with self.subTest(current=current):
                task = self.make_task()
                task.ocr.return_value = current
                with self.assertRaisesRegex(ForegroundInputError, '当前游历值'):
                    task.is_weekly_garden_completed()
                self.assertEqual(expected_reads, task.ocr.call_count)
                task.click.assert_not_called()
                task.screenshot.assert_called_once_with('garden_result_unknown')

    def test_local_points_still_require_a_unique_numeric_value_inside_the_region(self):
        for local in (
            [], [Box(496, 1210, 40, 55, name='O')],
            [Box(496, 1210, 40, 55, name='0'), Box(550, 1210, 160, 55, name='6000')],
            [Box(2219, 1286, 104, 45, name='6000')],
        ):
            with self.subTest(local=local):
                task = self.make_task()
                task.ocr.side_effect = [[], local]

                with self.assertRaisesRegex(ForegroundInputError, '当前游历值'):
                    task.is_weekly_garden_completed()

                self.assertEqual(2, task.ocr.call_count)
                task.click.assert_not_called()
                task.screenshot.assert_called_once_with('garden_result_unknown')

    def test_weekly_page_requires_both_localized_anchors(self):
        for title, entry, ready in (
            (['周度游历'], ['幻梦游园', '·狂想'], True),
            (['周度遊歷'], ['幻夢遊園'], True),
            (['Weekly', 'Activity'], ['Phantasma', 'Dreamland: Rhapsody'], True),
            (['周度游历'], ['千道门扉的异想'], False),
            (['活跃度'], ['幻梦游园'], False),
            (['6000'], ['0'], False),
        ):
            with self.subTest(title=title, entry=entry):
                task = self.make_task()
                task.ocr.side_effect = [
                    [Box(1, 1, 1, 1, name=text) for text in title],
                    [Box(1, 1, 1, 1, name=text) for text in entry],
                ]
                self.assertEqual(ready, task._is_weekly_garden_page())

    def test_foreground_change_discards_weekly_page_and_points(self):
        for method in ('_is_weekly_garden_page', 'is_weekly_garden_completed'):
            with self.subTest(method=method):
                task = self.make_task()

                def changed_frame(*args, **kwargs):
                    task._foreground_epoch += 1
                    return [Box(496, 1210, 160, 55, name='6000')]

                task.ocr.side_effect = changed_frame
                with self.assertRaises(ForegroundInputInterrupted):
                    getattr(task, method)()
                task.click.assert_not_called()
                task.screenshot.assert_not_called()

    def test_daily_points_read_receives_nested_foreground_resume(self):
        for points, resumed, full_frame_missing in product(('0', '6000'), (False, True), (False, True)):
            with self.subTest(points=points, resumed=resumed, full_frame_missing=full_frame_missing):
                task = self.make_task()
                executor = task.executor
                daily = DailyTask.__new__(DailyTask)
                daily.tr = str
                daily.info_set = Mock()
                daily.log_info = Mock()
                daily.on_foreground_resume = Mock()
                daily.get_task_by_class = Mock(return_value=task)
                daily.run_task_by_class = Mock()
                task.open_garden_weekly_page = Mock()
                executor.current_task = daily
                executor._task_context_stack = []
                executor.task_context.side_effect = lambda inner: TaskExecutor.task_context(executor, inner)

                def read_points(*args, **kwargs):
                    if full_frame_missing and not args:
                        return []
                    if resumed:
                        # 导航上下文已退出，积分读取仍需注册内层实例，才能丢弃恢复前的 OCR 结果。
                        executor.frame = Mock(shape=(1440, 2560, 3))
                        TaskExecutor._notify_tasks_resumed(executor)
                    return [Box(496, 1210, 160, 55, name=points)]

                task.ocr.side_effect = read_points
                if resumed:
                    with self.assertRaises(ForegroundInputInterrupted):
                        daily.check_weekly_garden()
                    daily.on_foreground_resume.assert_called_once()
                    self.assertEqual(1, task._foreground_epoch)
                else:
                    daily.check_weekly_garden()
                    daily.on_foreground_resume.assert_not_called()
                    self.assertEqual(0, task._foreground_epoch)

                if not resumed and points == '0':
                    daily.run_task_by_class.assert_called_once_with(GardenTask)
                else:
                    daily.run_task_by_class.assert_not_called()
                self.assertEqual(2 if full_frame_missing else 1, task.ocr.call_count)
                self.assertEqual([], executor._task_context_stack)
                self.assertIs(daily, executor.current_task)
                task.screenshot.assert_not_called()

    def test_action_is_identified_after_wait_before_click(self):
        task = self.make_task()
        old = Box(1, 1, 1, 1, name='garden_next_day')
        fresh = Box(2, 2, 1, 1, name='garden_shop_close')
        task.find_best_garden_feature.return_value = old
        task.sleep.side_effect = lambda seconds: task.find_best_garden_feature.configure_mock(return_value=fresh)

        task._play_garden_step()

        task.click.assert_called_once_with(fresh, after_sleep=1)

    def test_accepted_purchase_focus_loss_waits_for_transition_without_buying_again(self):
        task = self.make_task()
        button = Box(1, 1, 1, 1, name='garden_shop_buy')
        task.find_one.return_value = button

        def accepted_then_interrupted(*args, **kwargs):
            task.find_one.return_value = None
            task.find_best_garden_feature.return_value = Box(1, 1, 1, 1, name='garden_next_day')
            raise ForegroundInputInterrupted('购买已发出后失焦')

        task.click.side_effect = accepted_then_interrupted

        def advance():
            if task._garden_pending_source is None and task.click.call_count:
                task._garden_phase = 'completed'
            else:
                task._submit_garden_transaction('garden_shop_buy', button)

        task._play_garden_step = Mock(side_effect=advance)
        task.resume_after_foreground()

        task.click.assert_called_once_with(button, after_sleep=1)
        task.wait_foreground_frame.assert_called_once()
        self.assertEqual('completed', task._garden_phase)

    def test_purchase_button_disappearance_alone_does_not_prove_purchase_completed(self):
        task = self.make_task()
        task._garden_phase = 'pending'
        task._garden_pending_source = 'garden_shop_buy'
        close = Box(1, 1, 1, 1, name='garden_shop_close')
        task.find_one.side_effect = lambda name: close if name == 'garden_shop_close' else None
        task.find_best_garden_feature.return_value = close

        with self.assertRaisesRegex(ForegroundInputError, '避免重复提交'):
            task.resume_after_foreground()

        task.click.assert_not_called()

    def test_unchanged_pending_selection_fails_without_repeating_confirmation(self):
        task = self.make_task()
        task._garden_phase = 'pending'
        task._garden_pending_source = 'garden_get_skip'
        task._garden_needs_refresh = True
        task.find_one.return_value = Box(1, 1, 1, 1, name='garden_get_skip')

        with self.assertRaisesRegex(ForegroundInputError, '避免重复提交'):
            task.resume_after_foreground()

        task.click.assert_not_called()
        task.wait_foreground_frame.assert_called_once()
        task.screenshot.assert_called_once_with('garden_result_unknown')

    def test_empty_settlement_text_is_not_treated_as_completed(self):
        task = self.make_task()
        task.find_one.side_effect = lambda name: Box(1, 1, 1, 1, name=name)

        with self.assertRaisesRegex(ForegroundInputError, '积分识别不完整'):
            task._check_garden_result()

        task.click.assert_not_called()
        task.back.assert_not_called()

    def test_partial_blessing_focus_loss_returns_to_preparation_without_old_coordinates(self):
        task = self.make_task()
        task._garden_phase = 'blessing'
        task.open_garden_weekly_page = Mock()
        task.is_weekly_garden_completed = Mock(return_value=True)
        task.click.side_effect = [None, ForegroundInputInterrupted('选择祝福时失焦')]

        task.resume_after_foreground()

        self.assertEqual(2, task.click.call_count)
        task.wait_foreground_frame.assert_called_once()
        task.ensure_main.assert_called_once()
        task.open_garden_weekly_page.assert_called_once()
        self.assertEqual('completed', task._garden_phase)


class TestGardenWeeklyPage(TaskTestCase):
    task_class = GardenTask
    config = deepcopy(config)

    def setUp(self):
        self.set_image(str(Path(__file__).resolve().parents[1] / 'ok_templates' / 'weekly_garden_zero.webp'))
        self.task._garden_phase = 'prepare'
        self.task._garden_needs_refresh = False
        self.task._garden_pending_source = None
        self.task._garden_weekly_step = None

    def test_daily_zero_progress_reaches_garden_dispatch(self):
        daily = DailyTask.__new__(DailyTask)
        daily.tr = str
        daily.info_set = Mock()
        daily.log_info = Mock()
        daily.get_task_by_class = Mock(return_value=self.task)
        daily.run_task_by_class = Mock()

        # 回放真实零分页面；旧代码把 0 读成 O，在调度乐园前就会抛出异常。
        with patch.object(self.task, 'openF2Book'), patch.object(self.task, 'sleep'), \
                patch.object(self.task, 'click') as click, patch.object(self.task, 'back') as back, \
                patch.object(self.task, 'screenshot'):
            daily.check_weekly_garden()

        daily.run_task_by_class.assert_called_once_with(GardenTask)
        self.assertEqual([call(.343, .129, after_sleep=1), call(.927, .893, after_sleep=3),
                          call(.927, .893, after_sleep=2)], click.call_args_list)
        back.assert_not_called()

    def test_zero_progress_allows_garden_entry(self):
        self.task._garden_phase = 'enter'
        with patch.object(self.task, 'find_best_garden_feature', return_value=None), \
                patch.object(self.task, 'wait_until', return_value=True), \
                patch.object(self.task, '_play_garden_step', side_effect=TaskDisabledException), \
                patch.object(self.task, 'click') as click, patch.object(self.task, 'screenshot'):
            # 只验证入口决策，到达游玩阶段即取消，不能把替身点击当作实机通关。
            with self.assertRaises(TaskDisabledException):
                self.task.resume_after_foreground()

        click.assert_called_once_with(.246, .486, after_sleep=1)
        self.assertEqual('play', self.task._garden_phase)

    def test_real_reward_popup_and_claimed_postcondition(self):
        root = Path(__file__).resolve().parents[1] / 'ok_templates'
        for name, obtained, claimed in (
            ('weekly_garden_obtained.png', True, False),
            ('weekly_garden_claimed.png', False, True),
            ('weekly_garden_claimed_hover.png', False, True),
            ('weekly_garden_unclaimed.png', False, False),
            ('weekly_garden_zero.webp', False, False),
        ):
            self.set_image(str(root / name))
            original = self.task.frame.copy()
            for scale in (.5, .75, 1):
                with self.subTest(source=name, scale=scale):
                    frame = cv2.resize(original, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                    self.task.executor.feature_set.check_size(frame)
                    self.assertEqual(obtained, self.task._weekly_reward_visible(frame))
                    if not obtained:
                        self.assertTrue(self.task._is_weekly_garden_page(frame=frame))
                    # 已领悬停说明可以存在；只有中央获得标题或末档灰勾用于首次结果确认。
                    if obtained or claimed or name == 'weekly_garden_zero.webp':
                        self.task._confirm_weekly_first_click(frame)
                    else:
                        with patch.object(self.task, 'screenshot') as screenshot:
                            with self.assertRaisesRegex(ForegroundInputError, '首次领奖结果'):
                                self.task._confirm_weekly_first_click(frame)
                        screenshot.assert_called_once_with('garden_result_unknown')

    def test_missing_current_digits_stops_despite_visible_page_and_reward_scale(self):
        original = self.task.frame.copy()
        missing = original.copy()
        # 仅用邻近背景遮住当前数字，页面锚点、游历值标签和右侧六档刻度保持原样。
        missing[1200:1276, 480:680] = original[1120:1196, 600:800]
        for scale in (.5, .75, 1):
            with self.subTest(scale=scale):
                frame = cv2.resize(missing, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                self.assertTrue(self.task._is_weekly_garden_page(frame=frame))
                with patch.object(self.task, 'screenshot') as screenshot:
                    with self.assertRaisesRegex(ForegroundInputError, '当前游历值'):
                        self.task._confirm_weekly_first_click(frame)
                screenshot.assert_called_once_with('garden_result_unknown')

    def test_unknown_page_stops_without_clicking(self):
        blank = self.task.frame.copy()
        blank[:] = 0
        with patch.object(self.task, 'openF2Book'), patch.object(self.task, 'sleep'), \
                patch.object(self.task, 'next_frame', return_value=blank), \
                patch.object(self.task, 'click') as click, patch.object(self.task, 'screenshot') as screenshot:
            with self.assertRaises(ForegroundInputError):
                self.task.open_garden_weekly_page()

        click.assert_called_once_with(.343, .129, after_sleep=1)
        screenshot.assert_called_once_with('garden_result_unknown')


if __name__ == '__main__':
    unittest.main()
