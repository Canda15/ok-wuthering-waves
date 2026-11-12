import unittest
import re
from unittest.mock import Mock

from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.task.BaseCombatTask import CharRevivedException
from src.task.NightmareNestTask import NestTarget, NightmareNestTask


class FakeBox:

    def __init__(self, name, x=0, y=0, width=20, height=10):
        self.name = name
        self.x = x
        self.y = y
        self.width = width
        self.height = height

    def copy(self):
        return FakeBox(self.name, self.x, self.y, self.width, self.height)


class TestNightmareNestTask(unittest.TestCase):

    def make_nest_combat(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task._capture_mode = False
        task._capture_success = False
        task._unreachable_nests = set()
        task._nest_deaths = {}
        task.click = Mock()
        task.wait_feature = Mock(return_value=FakeBox('team_start_challenge'))
        task.click_team_challenge = Mock()
        task.wait_in_team_and_world = Mock(return_value=True)
        task.combat_once = Mock(return_value=False)
        task.sleep = Mock()
        task._should_continue_combat_after_pickup = Mock(return_value=False)
        task.log_info = Mock()
        task.esc_world_confirm = Mock()
        task.wait_foreground_frame = Mock()
        task.in_team_and_world = Mock(return_value=False)
        task.in_realm = Mock(return_value=True)
        task.find_one = Mock(return_value=None)
        return task

    def test_focus_before_nest_selection_requeries_current_row_coordinates(self):
        task = self.make_nest_combat()
        old = NestTarget(FakeBox('0/24', y=100), 'go_nest:24:5')
        fresh = NestTarget(FakeBox('0/24', y=160), old.cache_key)
        task.get_nest_to_go = Mock(return_value=fresh)
        task.click.side_effect = [ForegroundInputInterrupted(), None]
        task.combat_nest(old)
        self.assertEqual([call.args[0] for call in task.click.call_args_list], [old.box, fresh.box])
        self.assertEqual(task._nest_deaths, {})
        self.assertEqual(task._unreachable_nests, set())

    def test_focus_during_exit_keeps_finished_combat_and_pickup(self):
        task = self.make_nest_combat()
        task.esc_world_confirm.side_effect = [ForegroundInputInterrupted(), None]
        task._pick_nest_echo = Mock(return_value=True)
        task.combat_nest(NestTarget(FakeBox('0/24'), 'go_nest:24:5'))
        task.combat_once.assert_called_once()
        task._pick_nest_echo.assert_called_once()
        task.wait_foreground_frame.assert_called_once()

    def test_accepted_travel_focus_interrupt_does_not_mark_nest_unreachable(self):
        task = self.make_nest_combat()
        world = [False]
        task.in_team_and_world = lambda: world[0]
        task.wait_until = lambda condition, **kwargs: FakeBox('gray_teleport')
        task.back = Mock()

        def travel(*args, **kwargs):
            world[0] = True
            raise ForegroundInputInterrupted()

        task.click.side_effect = travel
        target = NestTarget(FakeBox('0/24'), 'go_nest:24:5')
        self.assertTrue(task._travel_to_nest_or_skip(target))
        self.assertEqual(task._unreachable_nests, set())
        task.back.assert_not_called()

    def test_nest_system_input_failure_is_not_retried(self):
        task = self.make_nest_combat()
        task.click.side_effect = ForegroundInputError()
        with self.assertRaises(ForegroundInputError):
            task.combat_nest(NestTarget(FakeBox('0/24'), 'go_nest:24:5'))
        task.wait_foreground_frame.assert_not_called()
        task.combat_once.assert_not_called()

    def test_fallback_refreshes_completed_nest_row_without_reusing_old_coordinates(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.queues = [task.go_nest]
        task.count_re = re.compile(r'(\d{1,2})/(\d{1,2})')
        task.height_of_screen = lambda fraction: fraction * 1000
        task.width_of_screen = lambda fraction: fraction * 1000
        completed = FakeBox('24/24', x=400, y=100)
        key = task._make_nest_cache_key(completed, '24')
        task.ocr = Mock(return_value=[completed])
        refreshed = task._refresh_nest_target(NestTarget(object(), key))
        self.assertEqual(refreshed.cache_key, key)
        self.assertIsNot(refreshed.box, completed)
        self.assertEqual(refreshed.box.x, 900)

    def test_nest_is_checked_before_nightmare_changes_book_scroll(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task.config = {'Which to Farm': ['Nightmare Purification', 'Tacet Discord Nest']}
        task._init_queue()
        self.assertEqual(['go_nest', 'go_nest_scroll', 'go_nightmare', 'go_nightmare_scroll'],
                         [action.__name__ for action in task.queues])

    def test_capture_success_clears_combat_before_post_combat_waits(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task._capture_mode = True
        task._in_combat = True
        picked = []

        task.pick_f = lambda handle_claim=True: picked.append(handle_claim)
        task.has_echo_notification = lambda: True

        def reset_to_false(reason=''):
            task._in_combat = False
            task.out_of_combat_reason = reason
            return False

        task.reset_to_false = reset_to_false

        self.assertFalse(task.on_combat_check())
        self.assertEqual([False], picked)
        self.assertFalse(task._in_combat)
        self.assertEqual('echo captured', task.out_of_combat_reason)

    def test_combat_nest_rechecks_after_pickup_in_team_and_open_world(self):
        for feature_name in ('team_close', 'team_start_challenge', 'fast_travel_custom'):
            with self.subTest(feature_name=feature_name):
                task = NightmareNestTask.__new__(NightmareNestTask)
                task.tr = str
                task._capture_mode = False
                task._capture_success = False
                combat_calls = []
                pickup_calls = []
                combat_results = iter([True, False])

                task.click = lambda *args, **kwargs: None
                task.wait_feature = lambda *args, **kwargs: FakeBox(feature_name)
                task.click_team_challenge = lambda: None
                task.wait_in_team_and_world = lambda *args, **kwargs: True
                task._travel_to_nest_or_skip = lambda nest: True
                task.sleep = lambda *args, **kwargs: None
                task.find_f_with_text = lambda: False
                task.run_until = lambda *args, **kwargs: None
                task.combat_once = lambda **kwargs: combat_calls.append(kwargs) or True
                task.walk_find_echo = lambda **kwargs: pickup_calls.append(kwargs) or True
                task.wait_combat = lambda **kwargs: next(combat_results)
                task.log_info = lambda *args, **kwargs: None
                task.send_key = lambda *args, **kwargs: None
                task.esc_world_confirm = lambda *args, **kwargs: None

                task.combat_nest(FakeBox('nest'))

                self.assertEqual([10, 1], [call['wait_combat_time'] for call in combat_calls])
                self.assertEqual(2, len(pickup_calls))

    def test_team_screen_is_detected_when_close_button_is_hovered(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task._capture_mode = False
        task._capture_success = False
        challenge_clicks = []
        probes = []

        def wait_feature(features, **kwargs):
            probes.append(list(features))
            return FakeBox('team_start_challenge')

        task.click = lambda *args, **kwargs: None
        task.wait_feature = wait_feature
        task.click_team_challenge = lambda: challenge_clicks.append(True)
        task.wait_in_team_and_world = lambda *args, **kwargs: True
        task.combat_once = lambda **kwargs: False
        task.sleep = lambda *args, **kwargs: None
        task.walk_find_echo = lambda **kwargs: True
        task.wait_combat = lambda **kwargs: False
        task._should_continue_combat_after_pickup = lambda: False
        task.log_info = lambda *args, **kwargs: None
        task.esc_world_confirm = lambda *args, **kwargs: None

        task.combat_nest(FakeBox('nest'))

        self.assertIn('team_close', probes[0])
        self.assertIn('team_start_challenge', probes[0])
        self.assertEqual([True], challenge_clicks)

    def test_capture_mode_does_not_check_combat_after_pickup(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task._capture_mode = True
        task.wait_combat = lambda **kwargs: self.fail('capture mode should leave after obtaining an echo')

        self.assertFalse(task._should_continue_combat_after_pickup())

    def test_unreachable_nest_is_cached_when_travel_does_not_enter_world(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task._unreachable_nests = set()
        backs = []
        clicks = []
        wait_timeouts = []
        world_waits = []
        travel = FakeBox('fast_travel_custom')

        task.wait_until = lambda *args, **kwargs: wait_timeouts.append(kwargs['time_out']) or travel
        task.find_one = lambda name, **kwargs: travel if name == travel.name else None
        task.click = lambda box, **kwargs: clicks.append((box, kwargs))
        task.wait_in_team_and_world = lambda *args, **kwargs: world_waits.append(kwargs) or False
        task.back = lambda *args, **kwargs: backs.append(kwargs)
        task.log_info = lambda *args, **kwargs: None

        target = NestTarget(object(), 'go_nightmare:36:0.205')

        self.assertFalse(task._travel_to_nest_or_skip(target))
        self.assertIn(target.cache_key, task._unreachable_nests)
        self.assertEqual([1], wait_timeouts)
        self.assertEqual([(travel, {'after_sleep': 1})], clicks)
        self.assertEqual([], world_waits)
        self.assertEqual([{'after_sleep': 1}], backs)

    def test_nest_is_skipped_after_max_deaths(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task._capture_mode = False
        task._capture_success = False
        task._unreachable_nests = set()
        task._nest_deaths = {}
        logs = []

        def combat_once(**kwargs):
            raise CharRevivedException('char dead')

        task.click = lambda *args, **kwargs: None
        task.wait_feature = lambda *args, **kwargs: FakeBox('team_close')
        task.click_team_challenge = lambda: None
        task.wait_in_team_and_world = lambda *args, **kwargs: True
        task.combat_once = combat_once
        task.log_info = lambda message, **kwargs: logs.append(message)
        nest = NestTarget(object(), 'go_nest:36:10')

        for _ in range(NightmareNestTask.max_nest_deaths - 1):
            task.combat_nest(nest)
            self.assertNotIn(nest.cache_key, task._unreachable_nests)
        task.combat_nest(nest)

        self.assertIn(nest.cache_key, task._unreachable_nests)
        self.assertIn('nightmare nest: died 3 times, skip this run: go_nest:36:10', logs)

    def test_travel_waits_up_to_120_seconds_for_loading(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task._unreachable_nests = set()
        travel = FakeBox('fast_travel_custom')
        world_waits = []

        task.wait_until = lambda *args, **kwargs: travel
        task.find_one = lambda *args, **kwargs: None
        task.click = lambda *args, **kwargs: None
        task.wait_in_team_and_world = lambda *args, **kwargs: world_waits.append(kwargs) or True

        self.assertTrue(task._travel_to_nest_or_skip(NestTarget(object(), 'go_nest:36:10')))
        self.assertEqual([{'time_out': 120, 'raise_if_not_found': False}], world_waits)

    def test_find_nest_skips_cached_unreachable_row(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task.count_re = re.compile(r"(\d{1,2})/(\d{1,2})")
        task.queues = [lambda: None]
        task._unreachable_nests = {'<lambda>:36:10'}
        task.log_info = lambda *args, **kwargs: None
        task.height_of_screen = lambda value: 1000 * value
        task.width_of_screen = lambda value: 2000 * value
        ocr_calls = []

        count_boxes = [
            FakeBox('0/36', y=200),
            FakeBox('0/36', y=300),
        ]

        def ocr(*args, **kwargs):
            ocr_calls.append((args, kwargs))
            return count_boxes

        task.ocr = ocr

        target = task.find_nest()

        self.assertIsInstance(target, NestTarget)
        self.assertIs(target.box, count_boxes[1])
        self.assertEqual('<lambda>:36:15', target.cache_key)
        self.assertEqual(1800, target.box.x)
        self.assertEqual(1, len(ocr_calls))

    def test_cache_key_ignores_small_ocr_position_jitter(self):
        task = NightmareNestTask.__new__(NightmareNestTask)
        task.tr = str
        task.queues = [lambda: None]
        task.height_of_screen = lambda value: 1000 * value

        first = task._make_nest_cache_key(FakeBox('0/36', y=200), '36')
        shifted = task._make_nest_cache_key(FakeBox('0/36', y=202), '36')

        self.assertEqual(first, shifted)


if __name__ == '__main__':
    unittest.main()
