import unittest
from contextlib import nullcontext
from itertools import count
from unittest.mock import Mock

from ok import TaskDisabledException, ForegroundInputError, ForegroundInputInterrupted
from ok.feature.Box import Box
from src.char.BaseChar import BaseChar
from src.scene.WWScene import WWScene
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
        task._executor = Mock()
        task.executor.task_context.side_effect = lambda inner: nullcontext()
        task._garden_phase = 'play'
        task._garden_needs_refresh = False
        task._garden_pending_source = None
        task.sleep = Mock()
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

    def test_weekly_limit_preserves_noop_without_starting_garden(self):
        task = self.make_task()
        task.open_garden_weekly_page = Mock()
        task.ocr.return_value = [Box(1, 1, 1, 1, name='6000')]

        task.run()

        task.open_garden_weekly_page.assert_called_once()
        task.sleep.assert_called_once_with(.5)
        task.click.assert_not_called()
        self.assertEqual('completed', task._garden_phase)

    def test_goal_number_alone_does_not_mark_partial_weekly_progress_completed(self):
        task = self.make_task()
        task.ocr.return_value = [Box(1, 1, 1, 1, name='3500/6000')]

        self.assertFalse(task.is_weekly_garden_completed())
        self.assertFalse(task.is_garden_done([]))
        self.assertFalse(task.is_garden_done(['3500/6000']))
        self.assertTrue(task.is_garden_done(['6000/6000']))
        self.assertTrue(task.is_garden_done(['已达到上限']))

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
                popup = {'open': False}

                def read_page(*args, **kwargs):
                    if kwargs.get('match') is task.REWARD_PATTERN:
                        return [reward] if popup['open'] else []
                    if kwargs.get('match') is task.WEEKLY_BUTTON_PATTERN:
                        return claim
                    return []

                def click_page(target, *args, **kwargs):
                    if target is claim:
                        popup['open'] = True

                def close_result(*args, **kwargs):
                    if task.back.call_count == 1:
                        if popup_closed_before_loss:
                            popup['open'] = False
                        raise ForegroundInputInterrupted('领奖结果关闭时失焦')
                    popup['open'] = False

                task.ocr.side_effect = read_page
                task.wait_ocr.return_value = [reward]
                task.click.side_effect = click_page
                task.back.side_effect = close_result

                task.open_garden_weekly_page()

                task.executor.task_context.assert_called_once_with(task)
                self.assertEqual(1, sum(args.args[0] is claim for args in task.click.call_args_list))
                task.wait_foreground_frame.assert_called_once()
                self.assertFalse(popup['open'])
                self.assertFalse(task._garden_weekly_claim_pending)

    def test_unknown_weekly_claim_saves_evidence_and_does_not_retry(self):
        task = self.make_task()
        claim = [Box(1, 1, 1, 1, name='领取')]
        task.ocr.side_effect = lambda *args, **kwargs: claim if kwargs.get('match') is task.WEEKLY_BUTTON_PATTERN else []

        with self.assertRaisesRegex(ForegroundInputError, '避免重复领取'):
            task.open_garden_weekly_page()

        self.assertEqual(1, sum(args.args[0] is claim for args in task.click.call_args_list))
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


if __name__ == '__main__':
    unittest.main()
