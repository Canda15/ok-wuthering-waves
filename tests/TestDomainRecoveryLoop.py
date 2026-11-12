import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ok.feature.Box import Box
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from ok.task.exceptions import TaskDisabledException
from src.task.BaseWWTask import BaseWWTask
from src.task.DomainTask import DomainTask
from src.task.ForgeryTask import ForgeryTask
from src.task.SimulationTask import SimulationTask
from src.task.BaseWWTask import TEAM_SCREEN_FEATURES


class TestDomainRecoveryLoop(unittest.TestCase):
    def make_domain(self):
        task = DomainTask.__new__(DomainTask)
        task.tr = str
        task.stamina_once = 40
        task.open_F2_book_and_get_stamina = Mock(return_value=(200, 0, 200))
        task.farm_in_domain = Mock()
        task.log_info = Mock()
        task.sleep = Mock()
        task.back = Mock()
        task.make_sure_in_world = Mock()
        task.in_realm = Mock(return_value=False)
        task.in_team_and_world = Mock(return_value=True)
        task.wait_foreground_frame = Mock()
        return task

    def test_death_reentry_preserves_already_used_daily_allowance(self):
        task = self.make_domain()
        task.farm_in_domain.side_effect = [(False, 140), (True, 60)]
        teleport = Mock()
        task.farm_domain_with_recovery_loop(180, teleport)
        self.assertEqual([call.kwargs['must_use'] for call in task.farm_in_domain.call_args_list], [180, 140])
        self.assertEqual(teleport.call_count, 2)

    def test_death_retry_budget_stops_and_leaves_domain(self):
        task = self.make_domain()
        task.farm_in_domain.return_value = (False, 0)
        teleport = Mock()
        task.farm_domain_with_recovery_loop(0, teleport)
        self.assertEqual(teleport.call_count, 3)
        task.make_sure_in_world.assert_called_once()

    def test_insufficient_stamina_never_enters_domain(self):
        task = self.make_domain()
        task.open_F2_book_and_get_stamina.return_value = (20, 0, 20)
        teleport = Mock()
        task.farm_domain_with_recovery_loop(0, teleport)
        teleport.assert_not_called()
        task.farm_in_domain.assert_not_called()

    def test_focus_wait_does_not_exhaust_domain_death_retry_budget(self):
        task = self.make_domain()
        task.farm_in_domain.return_value = (True, 0)
        teleport = Mock(side_effect=[ForegroundInputInterrupted() for _ in range(9)] + [None])
        task.farm_domain_with_recovery_loop(0, teleport)
        task.open_F2_book_and_get_stamina.assert_called_once()
        task.farm_in_domain.assert_called_once_with(must_use=0)
        self.assertEqual(task.wait_foreground_frame.call_count, 9)

    def test_result_interruption_does_not_replay_confirmed_resource_claim(self):
        task = self.make_domain()
        task.teleport_timeout = 100
        task.info_incr = Mock()
        task.in_combat = Mock(return_value=True)
        task.has_claim_stamina = Mock(return_value=True)
        task.walk_until_f = task.pick_f = task.combat_once = task.walk_to_treasure = Mock()
        task.use_stamina = Mock(side_effect=[(True, 80), (False, 40)])
        task.finish_challenge_result = Mock(side_effect=[ForegroundInputInterrupted(), None, None])
        finished, remaining = DomainTask.farm_in_domain(task, must_use=180)
        self.assertTrue(finished)
        self.assertEqual(remaining, 60)
        self.assertEqual([call.kwargs['must_use'] for call in task.use_stamina.call_args_list], [180, 100])
        self.assertEqual([call.args for call in task.info_incr.call_args_list],
                         [('used stamina', 0), ('used stamina', 80), ('used stamina', 40)])
        task.wait_foreground_frame.assert_called_once()


class TestDomainExitConfirmation(unittest.TestCase):
    def make_task(self):
        task = DomainTask.__new__(DomainTask)
        task._executor = SimpleNamespace(interaction=Mock(), reset_scene=Mock())
        task.logger = Mock()
        task.teleport_timeout = 100
        task.sleep = Mock()
        task.find_one = Mock(return_value=Box(200, 100, 40, 20, name='退出确认'))
        task.wait_in_team_and_world = Mock()
        task.wait_foreground_frame = Mock()
        task.in_realm = Mock(return_value=False)
        task.ensure_main = Mock()
        return task

    def test_已打开的退出确认使用真实框点击接口和相对位置(self):
        task = self.make_task()
        # 保留 click_box → BaseWWTask.click → BaseTask.click 的真实签名及坐标换算。
        with patch('ok.task.task.communicate'):
            task.make_sure_in_world()

        task.executor.interaction.click.assert_called_once_with(
            160, 110, move_back=False, name='退出确认', move=True, down_time=.2, key='left')
        task.sleep.assert_called_once_with(1)
        task.wait_in_team_and_world.assert_called_once_with(time_out=100)
        task.ensure_main.assert_not_called()

    def test_退出确认已接受后失焦不会再次点击(self):
        task = self.make_task()

        def accepted_then_interrupted(*args, **kwargs):
            task.find_one.return_value = None
            raise ForegroundInputInterrupted('退出确认已发出后失焦')

        task.executor.interaction.click.side_effect = accepted_then_interrupted
        with patch('ok.task.task.communicate'):
            task.make_sure_in_world()

        task.executor.interaction.click.assert_called_once()
        task.wait_foreground_frame.assert_called_once()
        task.ensure_main.assert_called_once()

    def test_退出确认取消或系统失败直接上抛(self):
        for error in (ForegroundInputError, TaskDisabledException):
            with self.subTest(error=error):
                task = self.make_task()
                task.executor.interaction.click.side_effect = error('退出已停止')

                with patch('ok.task.task.communicate'), self.assertRaises(error):
                    task.make_sure_in_world()

                task.executor.interaction.click.assert_called_once()
                task.wait_foreground_frame.assert_not_called()
                task.wait_in_team_and_world.assert_not_called()


class TestStaminaForegroundRecovery(unittest.TestCase):
    def make_task(self, initial=(60, 0, 60)):
        task = BaseWWTask.__new__(BaseWWTask)
        state = SimpleNamespace(page='claim')
        task.sleep = Mock()
        task.screenshot = Mock()
        task.get_stamina = Mock(return_value=initial)
        task.has_claim_stamina = lambda: state.page == 'claim'
        task.find_one = lambda feature, **kwargs: state.page == 'backup' if feature == 'gem_add_stamina' else None
        task.ocr = lambda *args, **kwargs: [SimpleNamespace(name='获得奖励')] if state.page == 'completed' else []
        task.wait_feature = lambda feature, **kwargs: state.page == 'backup' if feature == 'gem_add_stamina' else None
        task.wait_foreground_frame = Mock()
        task.back = Mock()

        def wait(condition, **kwargs):
            for _ in range(3):
                value = condition()
                if value:
                    return value
            return None

        task.wait_until = wait
        task.click_dialog_left_button = Mock(side_effect=lambda: setattr(state, 'page', 'completed'))
        task.click_dialog_right_button = task.click_dialog_left_button
        task.click_relative = Mock()
        return task, state

    def test_accepted_cost_interrupted_by_focus_is_confirmed_without_second_claim(self):
        task, state = self.make_task()

        def claim():
            state.page = 'completed'
            raise ForegroundInputInterrupted()

        task.click_dialog_left_button.side_effect = claim
        self.assertEqual(task.use_stamina(), (False, 60))
        task.click_dialog_left_button.assert_called_once()
        task.wait_foreground_frame.assert_called_once()



    def test_uncharged_new_claim_menu_allows_fresh_claim_decision(self):
        task, state = self.make_task()

        def claim():
            if task.click_dialog_left_button.call_count == 1:
                raise ForegroundInputInterrupted()
            state.page = 'completed'

        task.click_dialog_left_button.side_effect = claim
        self.assertEqual(task.use_stamina(), (False, 60))
        self.assertEqual(task.click_dialog_left_button.call_count, 2)

    def test_changed_stamina_with_unconfirmed_result_is_not_claimed_twice(self):
        task, _ = self.make_task()
        task.get_stamina.side_effect = [(60, 0, 60), (0, 0, 0)]
        task.click_dialog_left_button.side_effect = ForegroundInputInterrupted()
        with self.assertRaises(ForegroundInputError):
            task.use_stamina()
        task.click_dialog_left_button.assert_called_once()
        task.screenshot.assert_called_once_with('stamina_result_unknown')

    def test_ambiguous_resource_result_stops_without_recording_success(self):
        task, state = self.make_task()

        def claim():
            state.page = 'unknown'
            raise ForegroundInputInterrupted()

        task.click_dialog_left_button.side_effect = claim
        with self.assertRaises(ForegroundInputError):
            task.use_stamina()
        task.click_dialog_left_button.assert_called_once()
        task.screenshot.assert_called_once()

    def test_sent_reserve_confirmation_with_unknown_result_is_never_replayed(self):
        task, state = self.make_task((0, 60, 60))
        task.click_dialog_left_button.side_effect = lambda: setattr(state, 'page', 'backup')
        task.click_relative.side_effect = ForegroundInputInterrupted()
        with self.assertRaises(ForegroundInputError):
            task.use_stamina()
        task.click_relative.assert_called_once()
        task.click_dialog_left_button.assert_called_once()

    def test_system_injection_failure_is_not_converted_into_resource_retry(self):
        task, _ = self.make_task()
        task.click_dialog_left_button.side_effect = ForegroundInputError()
        with self.assertRaises(ForegroundInputError):
            task.use_stamina()
        task.wait_foreground_frame.assert_not_called()
        task.click_dialog_left_button.assert_called_once()

    def test_uncertain_initial_claim_ui_prevents_any_resource_input(self):
        task, state = self.make_task()
        state.page = 'unknown'
        with self.assertRaises(ForegroundInputError):
            task.use_stamina()
        task.click_dialog_left_button.assert_not_called()

    def test_completed_reserve_confirmation_is_observed_without_second_confirmation(self):
        task, state = self.make_task((0, 60, 60))
        task.click_dialog_left_button.side_effect = lambda: setattr(state, 'page', 'backup')

        def reserve(*args, **kwargs):
            state.page = 'completed'
            raise ForegroundInputInterrupted()

        task.click_relative.side_effect = reserve
        self.assertEqual(task.use_stamina(), (False, 60))
        task.click_relative.assert_called_once()
        task.click_dialog_left_button.assert_called_once()



class TestDomainEntryForegroundRecovery(unittest.TestCase):
    def make_entry(self, cls, page='team'):
        task = cls.__new__(cls)
        task.total_number = 20
        task.structure = [5, 5, 5, 5]
        task.teleport_timeout = 100
        task.tr = str
        for name in ('openF2Book', 'open_boss_book', 'info_set', 'click', 'click_relative',
                     'click_team_challenge', 'screenshot'):
            setattr(task, name, Mock())
        task.click_on_book_target = Mock(return_value=True)
        task.wait_foreground_frame = Mock()
        task.in_team_and_world = Mock(return_value=False)
        task.find_one = lambda features, **kwargs: (SimpleNamespace(name='team_start_challenge')
            if features == TEAM_SCREEN_FEATURES and page == 'team' else None)
        task._confirm_resumed_skip_dialog = Mock(return_value=False)
        task.box_of_screen = Mock()
        task.wait_in_team_and_world = Mock(return_value=page != 'unknown')
        return task

    def test_accepted_forgery_entry_reuses_new_team_screen_without_second_proceed(self):
        task = self.make_entry(ForgeryTask)
        task.click.side_effect = ForegroundInputInterrupted()
        task.teleport_into_domain(1)
        task.click.assert_called_once()
        task.click_on_book_target.assert_called_once()
        task.click_team_challenge.assert_called_once()
        task.wait_foreground_frame.assert_called_once()

    def test_accepted_simulation_entry_reuses_new_team_screen_without_second_proceed(self):
        task = self.make_entry(SimulationTask)
        task.click_relative.side_effect = ForegroundInputInterrupted()
        task.teleport_into_domain('Shell Credit')
        task.click_relative.assert_called_once()
        self.assertEqual(task.click.call_count, 2)
        task.click_team_challenge.assert_called_once()

    def test_unconfirmed_entry_stops_without_clicking_old_coordinate_again(self):
        task = self.make_entry(ForgeryTask, 'unknown')
        task.click.side_effect = ForegroundInputInterrupted()
        with self.assertRaises(ForegroundInputError):
            task.teleport_into_domain(1)
        task.click.assert_called_once()
        task.click_team_challenge.assert_not_called()
        task.screenshot.assert_called_once_with('domain_entry_unknown')

    def test_entry_system_error_does_not_start_focus_recovery(self):
        task = self.make_entry(ForgeryTask)
        task.click.side_effect = ForegroundInputError()
        with self.assertRaises(ForegroundInputError):
            task.teleport_into_domain(1)
        task.wait_foreground_frame.assert_not_called()

    def test_accepted_result_exit_observes_world_without_second_click(self):
        task = BaseWWTask.__new__(BaseWWTask)
        world = [False]
        task.in_team_and_world = lambda: world[0]
        task.in_realm = lambda: False
        task.wait_foreground_frame = Mock()
        task.wait_ocr = Mock()

        def accept(*args, **kwargs):
            world[0] = True
            raise ForegroundInputInterrupted()

        task.click_relative = Mock(side_effect=accept)
        task.finish_challenge_result(False)
        task.click_relative.assert_called_once()
        task.wait_ocr.assert_not_called()

    def test_unaccepted_result_click_uses_new_button_box_after_focus_resume(self):
        task = BaseWWTask.__new__(BaseWWTask)
        task.in_team_and_world = Mock(return_value=False)
        task.in_realm = Mock(return_value=False)
        task.wait_foreground_frame = Mock()
        task.click_relative = Mock(side_effect=ForegroundInputInterrupted())
        fresh = SimpleNamespace(x=630, y=830, width=20, height=20)
        task.box_of_screen = Mock(return_value=SimpleNamespace(x=640, y=840))
        task.wait_ocr = Mock(return_value=[fresh])
        task.click = Mock()
        task.wait_click_skip_dialog_confirm = Mock()
        task.wait_in_team_and_world = Mock(return_value=True)
        task.finish_challenge_result(True, hcenter=True)
        task.click_relative.assert_called_once()
        task.click.assert_called_once_with(fresh, after_sleep=1)
        self.assertTrue(all(call.kwargs.get('hcenter') for call in task.box_of_screen.call_args_list))
        task.wait_foreground_frame.assert_called_once()


if __name__ == "__main__":
    unittest.main()
