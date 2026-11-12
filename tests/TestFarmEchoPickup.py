import unittest
from unittest.mock import Mock, call

from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.task.FarmEchoTask import FarmEchoTask


def make_task(config=None):
    task = FarmEchoTask.__new__(FarmEchoTask)
    task.tr = str
    task.config = config or {}
    task.info = {}
    task.combat_wait_time = 0
    task.bypass_end_wait = True
    task._in_realm = False
    task._has_treasure = False
    task._just_entered_boss_realm = True
    task.is_revived = False
    task.yolo_time_out = 8
    task.yolo_threshold = 0.5
    task.logger = Mock()
    for name in (
        'in_realm', 'manage_boss_parameters', 'init_parameters', 'teleport_to_boss_enabled',
        'teleport_to_configured_boss_and_prepare', 'in_realm_check', 'manage_boss_interactions',
        'in_combat', 'check_boss_name', 'combat_once', 'pick_echo', 'middle_click',
        'yolo_find_echo', 'run_in_circle_to_find_echo', 'walk_find_echo', 'incr_drop',
        'sleep', 'log_info', 'log_debug'
    ):
        setattr(task, name, Mock())
    task.in_realm.return_value = False
    task.teleport_to_boss_enabled.return_value = False
    task.in_combat.return_value = True
    task.wait_foreground_frame = Mock()
    return task


class TestFarmEchoPickup(unittest.TestCase):

    def test_focus_after_drop_count_continues_wait_without_replaying_combat_or_pickup(self):
        task = make_task({'Repeat Farm Count': 1})
        task.bypass_end_wait = False
        task.pick_echo.return_value = True
        task.wait_until = Mock(side_effect=[ForegroundInputInterrupted(), None])
        task.do_run()
        task.combat_once.assert_called_once()
        task.pick_echo.assert_called_once()
        task.incr_drop.assert_called_once_with(True)
        task.wait_foreground_frame.assert_called_once()
        self.assertEqual(task._farm_state['count'], 1)

    def test_focus_during_pickup_requeries_pickup_without_another_combat(self):
        task = make_task({'Repeat Farm Count': 1})
        task.pick_echo.side_effect = [ForegroundInputInterrupted(), True]
        task.do_run()
        task.combat_once.assert_called_once()
        task.incr_drop.assert_called_once_with(True)
        self.assertEqual(task.pick_echo.call_count, 2)

    def test_system_input_failure_does_not_reteleport_or_retry_round(self):
        task = make_task({'Repeat Farm Count': 1})
        task.pick_echo.side_effect = ForegroundInputError()
        task.should_reteleport_after_farm_exception = Mock(return_value=True)
        with self.assertRaises(ForegroundInputError):
            task.do_run()
        task.should_reteleport_after_farm_exception.assert_not_called()
        task.wait_foreground_frame.assert_not_called()
        task.incr_drop.assert_not_called()

    def test_pick_echo_on_face_skips_middle_click(self):
        task = make_task({"Repeat Farm Count": 1})
        task.pick_echo.return_value = True

        task.do_run()

        task.pick_echo.assert_called_once()
        task.middle_click.assert_not_called()
        task.incr_drop.assert_called_once_with(True)

    def test_pick_echo_failure_triggers_middle_click_before_movement(self):
        task = make_task({"Repeat Farm Count": 1, "Echo Pickup Method": "Run in Circle"})
        task.pick_echo.return_value = False
        task.run_in_circle_to_find_echo.return_value = True
        steps = Mock()
        steps.attach_mock(task.middle_click, 'middle_click')
        steps.attach_mock(task.run_in_circle_to_find_echo, 'run_in_circle_to_find_echo')

        task.do_run()

        self.assertEqual(
            [call.middle_click(after_sleep=0.2), call.run_in_circle_to_find_echo(circle_count=2)],
            steps.mock_calls
        )
        task.incr_drop.assert_called_once_with(True)

    def test_pick_echo_failure_triggers_middle_click_before_yolo(self):
        task = make_task({"Repeat Farm Count": 1, "Echo Pickup Method": "Yolo"})
        task.pick_echo.return_value = False
        task.yolo_find_echo.return_value = (True, False)
        steps = Mock()
        steps.attach_mock(task.middle_click, 'middle_click')
        steps.attach_mock(task.yolo_find_echo, 'yolo_find_echo')

        task.do_run()

        self.assertEqual(
            [
                call.middle_click(after_sleep=0.2),
                call.yolo_find_echo(turn=False, use_color=False, time_out=8, threshold=0.5)
            ],
            steps.mock_calls
        )
        task.incr_drop.assert_called_once_with(True)


if __name__ == '__main__':
    unittest.main()
