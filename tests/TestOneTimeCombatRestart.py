import unittest
from contextlib import nullcontext
from copy import deepcopy
from itertools import count
from pathlib import Path
from unittest.mock import Mock, patch

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

    def prepare_weekly_rewards(self, task, available=(1000, 6000), points=6000, batch=False):
        centers = (948, 1232, 1516, 1800, 2084, 2369)
        page = {'available': set(available), 'popup': False, 'submitted': []}
        reward = Box(800, 300, 200, 60, name='获得物品')
        numbers = [Box(496, 1210, 180, 55, name=str(points))]
        numbers.extend(Box(x - 145, 1288, 100, 44, name=str((index + 1) * 1000))
                       for index, x in enumerate(centers))

        def read_page(*args, **kwargs):
            match = kwargs.get('match')
            if match is task.REWARD_PATTERN:
                return [reward] if page['popup'] else []
            if page['popup'] or match in (task.WEEKLY_BUTTON_PATTERN, task.CLAIMED_PATTERN):
                return []
            if args == (.09, .08, .41, .17):
                return [Box(776, 161, 149, 48, name='周度游历')]
            if args == (.11, .36, .38, .44):
                return [Box(339, 556, 192, 45, name='幻梦游园')]
            return numbers

        def find_hint(feature_name=None, *, box=None, **kwargs):
            if feature_name != 'weekly_garden_reward_hint' or page['popup']:
                return None
            for threshold in sorted(page['available'], reverse=True):
                hint = Box(centers[threshold // 1000 - 1] + 30, 1214, 27, 29,
                           name='weekly_garden_reward_hint')
                if box is None or (box.x <= hint.x and box.y <= hint.y
                                   and hint.x + hint.width <= box.x + box.width
                                   and hint.y + hint.height <= box.y + box.height):
                    return hint
            return None

        def click_page(target, *args, **kwargs):
            if not isinstance(target, Box):
                return
            center = target.x + target.width / 2
            index = min(range(6), key=lambda value: abs(centers[value] - center))
            self.assertLess(abs(centers[index] - center), 25, '应点击宝箱中央，而不是提示菱形')
            threshold = (index + 1) * 1000
            self.assertIn(threshold, page['available'])
            page['submitted'].append(threshold)
            page['available'].difference_update(page['available'] if batch else {threshold})
            page['popup'] = True

        task.ocr.side_effect = read_page
        task.find_one.side_effect = find_hint
        task.next_frame = Mock(return_value=task.frame)
        task.wait_ocr.side_effect = lambda *args, **kwargs: read_page(*args, **kwargs)
        task.click.side_effect = click_page
        task.back.side_effect = lambda *args, **kwargs: page.update(popup=False)
        return page

    def test_daily_full_points_claims_icon_rewards_before_skipping_play(self):
        task = self.make_task()
        page = self.prepare_weekly_rewards(task)
        daily = DailyTask.__new__(DailyTask)
        daily.tr = str
        daily.info_set = Mock()
        daily.log_info = Mock()
        daily.get_task_by_class = Mock(return_value=task)
        daily.run_task_by_class = Mock()

        daily.check_weekly_garden()

        self.assertEqual([6000, 1000], page['submitted'])
        self.assertFalse(page['available'])
        self.assertFalse(page['popup'])
        daily.run_task_by_class.assert_not_called()

    def test_weekly_return_rescans_after_single_or_batch_claim(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                task = self.make_task()
                task._garden_phase = 'weekly'
                page = self.prepare_weekly_rewards(task, available=(1000, 3000, 6000), batch=batch)

                task.resume_after_foreground()

                self.assertEqual([6000] if batch else [6000, 3000, 1000], page['submitted'])
                self.assertFalse(page['available'])
                self.assertFalse(page['popup'])
                self.assertEqual('completed', task._garden_phase)

    def test_weekly_claim_state_does_not_leak_to_later_checks(self):
        task = self.make_task()
        page = self.prepare_weekly_rewards(task, available=(6000,))

        task.open_garden_weekly_page()
        task.open_garden_weekly_page()
        self.assertEqual([6000], page['submitted'])
        self.assertFalse(task._garden_weekly_claim_pending)
        self.assertFalse(task._garden_weekly_claim_confirmed)
        page['available'].add(5000)
        task.open_garden_weekly_page()

        self.assertEqual([6000, 5000], page['submitted'])

    def test_reward_popup_resets_empty_page_stability(self):
        task = self.make_task()
        page = self.prepare_weekly_rewards(task, available=())
        page['time'] = 0.0
        task.business_time = lambda: page['time']
        frames = iter((
            (0.0, False, set()),
            (1.0, True, set()),
            (1.1, False, set()),
            (1.3, False, {6000}),
            (1.6, False, set()),
            (2.2, False, set()),
        ))

        def next_page():
            instant, popup, available = next(frames)
            page.update(time=instant, popup=popup, available=available)
            return task.frame

        task.next_frame.side_effect = next_page
        task.open_garden_weekly_page()

        # 弹窗已打断空页观察，关闭后的暂空帧不能沿用弹窗前的稳定计时。
        self.assertEqual([6000], page['submitted'])
        self.assertFalse(page['popup'])

    def test_pending_icon_without_reward_result_never_replays(self):
        task = self.make_task()
        page = self.prepare_weekly_rewards(task, available=(6000,))
        submit = task.click.side_effect

        def missing_result(target, *args, **kwargs):
            submit(target, *args, **kwargs)
            page['popup'] = False

        task.click.side_effect = missing_result
        for _ in range(2):
            with self.assertRaisesRegex(ForegroundInputError, '避免重复领取'):
                task.open_garden_weekly_page()
        self.assertEqual([6000], page['submitted'])
        self.assertTrue(task._garden_weekly_claim_pending)
        task.wait_foreground_frame.assert_not_called()

    def test_weekly_input_failure_and_cancellation_are_not_retried(self):
        for error in (ForegroundInputError('输入失败'), TaskDisabledException()):
            with self.subTest(error=type(error).__name__):
                task = self.make_task()
                self.prepare_weekly_rewards(task, available=(1000,))

                def stop_on_reward(target, *args, **kwargs):
                    if isinstance(target, Box):
                        raise error

                task.click.side_effect = stop_on_reward
                with self.assertRaises(type(error)):
                    task.open_garden_weekly_page()
                self.assertEqual(1, sum(isinstance(call.args[0], Box) for call in task.click.call_args_list))
                task.wait_foreground_frame.assert_not_called()

    def bind_weekly_foreground_context(self, task):
        executor = task.executor
        outer = Mock()
        executor.current_task = outer
        executor._task_context_stack = []
        executor.task_context.side_effect = lambda inner: TaskExecutor.task_context(executor, inner)
        return executor, outer

    def test_icon_claim_and_close_resume_without_duplicate_submission(self):
        for interrupt_at in ('submitted', 'before_close', 'after_close'):
            with self.subTest(interrupt_at=interrupt_at):
                task = self.make_task()
                page = self.prepare_weekly_rewards(task)
                executor, outer = self.bind_weekly_foreground_context(task)
                submit, close = task.click.side_effect, task.back.side_effect
                interrupted = False

                def submit_then_interrupt(target, *args, **kwargs):
                    nonlocal interrupted
                    submit(target, *args, **kwargs)
                    if isinstance(target, Box) and interrupt_at == 'submitted' and not interrupted:
                        interrupted = True
                        TaskExecutor._notify_tasks_resumed(executor)

                def close_then_interrupt(*args, **kwargs):
                    nonlocal interrupted
                    if interrupt_at == 'before_close' and not interrupted:
                        interrupted = True
                        TaskExecutor._notify_tasks_resumed(executor)
                    close(*args, **kwargs)
                    if interrupt_at == 'after_close' and not interrupted:
                        interrupted = True
                        TaskExecutor._notify_tasks_resumed(executor)

                task.click.side_effect = submit_then_interrupt
                task.back.side_effect = close_then_interrupt
                # 等待阶段再次失焦也应仍由同一个导航恢复边界处理。
                task.wait_foreground_frame.side_effect = [ForegroundInputInterrupted('等待时再次失焦'), None]
                task.open_garden_weekly_page()

                self.assertEqual([6000, 1000], page['submitted'])
                self.assertFalse(page['popup'])
                self.assertFalse(task._garden_weekly_claim_pending)
                self.assertFalse(task._garden_weekly_claim_confirmed)
                self.assertEqual([], executor._task_context_stack)
                self.assertIs(outer, executor.current_task)
                self.assertEqual(2, task.wait_foreground_frame.call_count)
                outer.on_foreground_resume.assert_called_once()

    def test_scan_resume_discards_old_hint_and_restarts_empty_stability(self):
        for start_empty in (False, True):
            with self.subTest(start_empty=start_empty):
                task = self.make_task()
                page = self.prepare_weekly_rewards(task, available=() if start_empty else (6000,))
                executor, outer = self.bind_weekly_foreground_context(task)
                find_hint = task.find_one.side_effect
                interrupted = False

                def interrupt_hint(*args, **kwargs):
                    nonlocal interrupted
                    hint = find_hint(*args, **kwargs)
                    if hint is not None and not interrupted:
                        interrupted = True
                        page['available'] = {1000}
                        TaskExecutor._notify_tasks_resumed(executor)
                    return hint

                def interrupt_empty_wait(seconds):
                    nonlocal interrupted
                    if seconds == .2 and not interrupted:
                        interrupted = True
                        page['available'] = {1000}
                        TaskExecutor._notify_tasks_resumed(executor)

                if start_empty:
                    task.sleep.side_effect = interrupt_empty_wait
                else:
                    task.find_one.side_effect = interrupt_hint
                task.open_garden_weekly_page()

                self.assertEqual([1000], page['submitted'])
                self.assertEqual([], executor._task_context_stack)
                self.assertIs(outer, executor.current_task)

    def test_incomplete_or_locked_reward_row_never_clicks(self):
        for locked, resumed in ((True, False), (False, False), (False, True)):
            with self.subTest(locked=locked, resumed=resumed):
                task = self.make_task()
                self.prepare_weekly_rewards(task, available=(6000,) if locked else (), points=0)
                if not locked:
                    def incomplete_numbers(*args, **kwargs):
                        if resumed:
                            task._foreground_epoch += 1
                        return []
                    task.ocr.side_effect = incomplete_numbers
                error = ForegroundInputInterrupted if resumed else ForegroundInputError
                with self.assertRaises(error):
                    task._find_weekly_reward(task.frame)
                task.click.assert_not_called()
                if resumed:
                    task.screenshot.assert_not_called()
                else:
                    task.screenshot.assert_called_once_with('garden_result_unknown')

    def test_confirmed_reward_text_from_old_frame_is_not_accepted(self):
        task = self.make_task()
        task._garden_weekly_claim_target = 6000

        def stale_reward(*args, **kwargs):
            task._foreground_epoch += 1
            return [Box(800, 300, 200, 60, name='获得物品')]

        task.wait_ocr.side_effect = stale_reward
        with self.assertRaises(ForegroundInputInterrupted):
            task._confirm_weekly_claim()
        self.assertFalse(getattr(task, '_garden_weekly_claim_confirmed', False))
        task.back.assert_not_called()

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
            ('6,000', True), ('3500/6000', False), ('6000/6000', True),
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

    def test_missing_or_ambiguous_current_points_stops(self):
        for current in (
            [Box(2219, 1286, 104, 45, name='6000')],
            [Box(496, 1210, 40, 55, name='O')],
            [Box(496, 1210, 40, 55, name='0'), Box(550, 1210, 160, 55, name='6000')],
        ):
            with self.subTest(current=current):
                task = self.make_task()
                task.ocr.return_value = current
                with self.assertRaisesRegex(ForegroundInputError, '当前游历值'):
                    task.is_weekly_garden_completed()
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
        for points, resumed in (('0', False), ('6000', False), ('0', True), ('6000', True)):
            with self.subTest(points=points, resumed=resumed):
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

    def test_weekly_claim_focus_loss_during_close_never_claims_twice(self):
        for popup_closed_before_loss in (False, True):
            with self.subTest(popup_closed_before_loss=popup_closed_before_loss):
                task = self.make_task()
                reward = Box(1, 1, 1, 1, name='获得物品')
                claim = [Box(1, 1, 1, 1, name='领取')]
                popup = {'open': False, 'claimed': False}
                page = self.prepare_weekly_rewards(task, available=())
                read_weekly_page = task.ocr.side_effect

                def read_page(*args, **kwargs):
                    if kwargs.get('match') is task.REWARD_PATTERN:
                        return [reward] if popup['open'] else []
                    if kwargs.get('match') is task.WEEKLY_BUTTON_PATTERN:
                        return [] if popup['claimed'] else claim
                    return read_weekly_page(*args, **kwargs)

                def click_page(target, *args, **kwargs):
                    if target is claim[0]:
                        popup['open'] = True
                        popup['claimed'] = True

                def close_result(*args, **kwargs):
                    if task.back.call_count == 1:
                        if popup_closed_before_loss:
                            popup['open'] = False
                        raise ForegroundInputInterrupted('领奖结果关闭时失焦')
                    popup['open'] = False

                task.ocr.side_effect = read_page
                task.wait_ocr.side_effect = read_page
                task.click.side_effect = click_page
                task.back.side_effect = close_result

                task.open_garden_weekly_page()

                task.executor.task_context.assert_called_once_with(task)
                self.assertEqual(1, sum(args.args[0] is claim[0] for args in task.click.call_args_list))
                task.wait_foreground_frame.assert_called_once()
                self.assertFalse(popup['open'])
                self.assertFalse(task._garden_weekly_claim_pending)
                self.assertFalse(page['submitted'])

    def test_unknown_weekly_claim_saves_evidence_and_does_not_retry(self):
        task = self.make_task()
        claim = [Box(1, 1, 1, 1, name='领取')]
        task.ocr.side_effect = lambda *args, **kwargs: claim if kwargs.get('match') is task.WEEKLY_BUTTON_PATTERN else []

        with self.assertRaisesRegex(ForegroundInputError, '避免重复领取'):
            task.open_garden_weekly_page()

        self.assertEqual(1, sum(args.args[0] is claim[0] for args in task.click.call_args_list))
        task.screenshot.assert_called_once_with('garden_result_unknown')
        task.wait_foreground_frame.assert_not_called()

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
        self.task._garden_weekly_claim_pending = False
        self.task._garden_weekly_claim_confirmed = False

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
        click.assert_called_once_with(.343, .129, after_sleep=1)
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

    def test_reward_hint_template_does_not_join_play_features(self):
        self.assertNotIn('weekly_garden_reward_hint', self.task.garden_features)
        feature = self.task.find_one('weekly_garden_reward_hint', threshold=.99)
        self.assertIsNotNone(feature)
        self.assertLessEqual(abs(feature.x - 144), 1)
        self.assertLessEqual(abs(feature.y - 226), 1)

    def test_original_zero_page_and_unrelated_hints_have_no_reward_candidate(self):
        original = self.task.frame.copy()
        for scale in (.5, .75, 1):
            with self.subTest(scale=scale):
                frame = cv2.resize(original, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                self.task.executor.feature_set.check_size(frame)
                self.assertIsNone(self.task._find_weekly_reward(frame))

    def test_synthetic_reward_hints_keep_slot_and_scale_geometry(self):
        original = self.task.frame.copy()
        cases = [(source_y, scale, index) for source_y in (766, 947)
                 for scale, index in ((.5, 0), (.75, 2), (1, 5))]
        for source_y, scale, index in cases:
            with self.subTest(source_y=source_y, scale=scale, slot=index):
                frame = original.copy()
                # 合成状态仅验证图标几何和真实 OCR，不代表已取得实机可领取截图。
                # 提示取自同一原图另外两个位置，避免复制模板自身造成自匹配假象。
                hint = original[source_y:source_y + 29, 144:171].copy()
                frame[1200:1265, 490:650] = original[960:1025, 1545:1705]
                x = round(self.task.WEEKLY_REWARD_CENTERS[index]) + 30
                frame[1214:1243, x:x + 27] = hint
                frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                self.task.executor.feature_set.check_size(frame)

                candidate = self.task._find_weekly_reward(frame)

                self.assertIsNotNone(candidate)
                threshold, box = candidate
                self.assertEqual((index + 1) * 1000, threshold)
                self.assertLess(abs(box.x + box.width / 2 - self.task.WEEKLY_REWARD_CENTERS[index] * scale), 2)
                self.assertLess(abs(box.y + box.height / 2 - 1278.5 * scale), 2)

    def test_unknown_page_stops_without_clicking(self):
        blank = self.task.frame.copy()
        blank[:] = 0
        with patch.object(self.task, 'next_frame', return_value=blank), \
                patch.object(self.task, 'click') as click, patch.object(self.task, 'screenshot') as screenshot:
            with self.assertRaises(ForegroundInputError):
                self.task._advance_weekly_page()

        click.assert_not_called()
        screenshot.assert_called_once_with('garden_result_unknown')


if __name__ == '__main__':
    unittest.main()
