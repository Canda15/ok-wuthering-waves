import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ok import TaskDisabledException
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.char.BaseChar import BaseChar, CharType
from src.char.Lily import Lily
from src.scene.WWScene import WWScene
from src.task.BaseCombatTask import BaseCombatTask
from src.task.BaseWWTask import BaseWWTask


class TestBaseCombatTask(unittest.TestCase):

    def make_combat(self):
        task = BaseCombatTask.__new__(BaseCombatTask)
        task.scene = WWScene()
        task.info = {}
        task.wait_combat = Mock(return_value=True)
        task.switch_healer_enabled = Mock(return_value=False)
        task.in_combat = Mock(side_effect=[True, True, False])
        old_char = SimpleNamespace(perform=Mock(side_effect=ForegroundInputInterrupted()))
        fresh_char = SimpleNamespace(perform=Mock())
        task.chars = [old_char, None, None]
        task.get_current_char = lambda *args, **kwargs: task.chars[0]
        task.reset_combat_state = Mock(side_effect=lambda reason: setattr(task, 'chars', [None, None, None]))
        task.load_chars = Mock(side_effect=lambda: setattr(task, 'chars', [fresh_char, None, None]))
        task.wait_foreground_frame = Mock()
        task.combat_end = Mock()
        task.wait_in_team_and_world = Mock()
        return task, old_char, fresh_char

    def test_combat_focus_resume_loads_new_character_and_preserves_combat_count(self):
        task, old_char, fresh_char = self.make_combat()
        self.assertTrue(task.combat_once())
        old_char.perform.assert_called_once()
        fresh_char.perform.assert_called_once()
        task.load_chars.assert_called_once()
        task.wait_foreground_frame.assert_called_once()
        self.assertEqual(task.info['Combat Count'], 1)

    def test_combat_system_input_failure_is_not_retried(self):
        task, old_char, fresh_char = self.make_combat()
        old_char.perform.side_effect = ForegroundInputError()
        with self.assertRaises(ForegroundInputError):
            task.combat_once()
        task.wait_foreground_frame.assert_not_called()
        fresh_char.perform.assert_not_called()

    def test_cancellation_during_combat_focus_wait_propagates(self):
        task, _, _ = self.make_combat()
        task.wait_foreground_frame.side_effect = TaskDisabledException()
        with self.assertRaises(TaskDisabledException):
            task.combat_once()

    def test_character_switch_restores_outer_rotation_marker_on_interruption(self):
        outer = object()
        char = BaseChar.__new__(BaseChar)
        char.task = SimpleNamespace(_active_char_rotation=outer)
        char._switch_other_char = Mock(side_effect=ForegroundInputInterrupted())
        with self.assertRaises(ForegroundInputInterrupted):
            char.switch_other_char()
        self.assertIs(char.task._active_char_rotation, outer)

    def test_character_break_fallback_does_not_swallow_input_errors(self):
        from src.char.Lucy import Lucy
        for error in (ForegroundInputInterrupted(), ForegroundInputError(), TaskDisabledException()):
            with self.subTest(error=type(error).__name__):
                char = Lucy.__new__(Lucy)
                char.task = SimpleNamespace(check_f_break=Mock(return_value=True), f_break=Mock(side_effect=error))
                char.perform_standard = Mock()
                with self.assertRaises(type(error)):
                    char.do_perform()
                char.perform_standard.assert_not_called()

    def test_character_recognition_fallback_propagates_foreground_interruption(self):
        from src.char.Linnai import Linnai
        char = Linnai.__new__(Linnai)
        char.task = SimpleNamespace(get_box_by_name=Mock(side_effect=ForegroundInputInterrupted()))
        char.logger = Mock()
        with self.assertRaises(ForegroundInputInterrupted):
            char.find_target_status_in_box('box_target_enemy_long')
    def test_revive_search_boss_name_matches_game_language(self):
        cases = {
            "zh_CN": "无冠者",
            "zh_TW": "無冠者",
            "en_US": "Crownless",
            "unknown_lang": "无冠者",
        }

        for lang, expected in cases.items():
            with self.subTest(lang=lang):
                self.assertEqual(BaseCombatTask.get_revive_search_boss_name(SimpleNamespace(game_lang=lang)), expected)

    def test_revive_flow_uses_language_aware_search_name(self):
        task = SimpleNamespace(game_lang='en_US', ensure_main=Mock(), search_monsters=Mock(),
            click=Mock(), wait_until=lambda condition, **kwargs: True,
            _travel_to_nearest_waypoint=Mock())
        task.get_revive_search_boss_name = lambda: BaseCombatTask.get_revive_search_boss_name(task)
        BaseCombatTask.revive_at_tower_and_heal(task)
        task.search_monsters.assert_called_once_with('Crownless')
        task._travel_to_nearest_waypoint.assert_called_once()

    def test_interrupted_search_relocates_and_clears_partial_text_before_retry(self):
        task = BaseWWTask.__new__(BaseWWTask)
        task.ensure_main = Mock()
        task.openF2Book = Mock()
        task.click = Mock()
        task.wait_foreground_frame = Mock()
        task.send_key_down = Mock()
        task.send_key_up = Mock()
        content = ['旧内容']

        def key(key, **kwargs):
            if key == 'backspace':
                content[0] = ''

        def text(value):
            if task.input_text.call_count == 1:
                content[0] += value[:1]
                raise ForegroundInputInterrupted()
            content[0] += value

        task.send_key = Mock(side_effect=key)
        task.input_text = Mock(side_effect=text)
        task.search_monsters('无冠者')
        self.assertEqual(content[0], '无冠者')
        self.assertEqual(task.openF2Book.call_count, 2)
        self.assertEqual(task.send_key_up.call_count, 2)
        task.wait_foreground_frame.assert_called_once()

    def test_foreground_resume_clears_rotation_and_preserves_live_abyss_check(self):
        task = BaseCombatTask.__new__(BaseCombatTask)
        task.scene = WWScene()
        task.scene.set_in_combat()
        task.chars = [object(), None, None]
        task._active_char_rotation = task.chars[0]
        task._abyss_combat_active = True
        task.cds = {'resonance': 12}
        task.freeze_durations = [(1, 2, .1)]
        with self.assertRaises(ForegroundInputInterrupted):
            task.on_foreground_resume()
        self.assertEqual(task.chars, [None, None, None])
        self.assertEqual(task.cds, {})
        self.assertEqual(task.freeze_durations, [])
        self.assertTrue(task._abyss_combat_active)
        self.assertFalse(task.scene.in_combat())

    def test_character_rotation_marker_is_restored_on_input_interruption(self):
        char = BaseChar.__new__(BaseChar)
        char.task = SimpleNamespace()
        char.index = 0
        char.logger = Mock()
        char.do_perform = Mock(side_effect=ForegroundInputInterrupted())
        with self.assertRaises(ForegroundInputInterrupted):
            char.perform()
        self.assertIsNone(char.task._active_char_rotation)

    def test_Lily补能结果通过真实公共切换完成或阻止延奏交接(self):
        for mode, con, expected_fills, expected_keys, expected_intro in (
            ('解放不可用且补能超时', 0.7, [(1, False)], [], None),
            ('解放失败且二次补能超时', 0.9, [(0.8, True), (1, False)], ['r'], None),
            ('新梦乡协奏未满', 0.9, [(0.8, True)], ['r'], None),
            ('新梦乡协奏已满', 1.0, [(0.8, True)], ['r', 2], True),
            ('满协奏正常交接', 1.0, [(1, True)], [2], True),
            ('原梦乡入口快速交接', 0.7, [], [2], False),
        ):
            with self.subTest(mode=mode):
                clock = SimpleNamespace(now=100.0)
                state = SimpleNamespace(index=0)
                outer_rotation = object()
                task = BaseCombatTask.__new__(BaseCombatTask)
                task.freeze_durations = []
                task.skip_combat_check = False
                task.use_liberation = True
                task.in_liberation = False
                task._active_char_rotation = outer_rotation
                task.switch_char_time_out = 5
                task.check_combat = Mock()
                task.check_f_break = Mock(return_value=False)
                task.update_lib_portrait_icon = Mock()
                task.log_debug = Mock()
                task.has_cd = Mock(return_value=True)
                task.get_current_con = Mock(return_value=con)
                task.get_liberation_key = lambda: 'r'
                task.in_team = lambda: (True, state.index, 3)
                task.wait_until = lambda condition, **kwargs: condition()
                task.screenshot = Mock()
                keys, fills = [], []

                def sleep(seconds):
                    clock.now += max(0.05, seconds)

                def click(*args, **kwargs):
                    clock.now += 0.1

                def key(value, **kwargs):
                    keys.append(value)
                    if isinstance(value, int):
                        state.index = value - 1

                task.sleep = sleep
                task.next_frame = lambda: sleep(0.05)
                task.click = click
                task.send_key = key
                current = Lily(task, 0, char_name='char_lily', char_type=CharType.HEALER, buff_time=30)
                target = BaseChar(task, 1, char_name='main', char_type=CharType.MAIN_DPS)
                support = BaseChar(task, 2, char_name='support', char_type=CharType.SUB_DPS)
                support.last_buff_time = clock.now
                support.set_buff_time(30)
                current.is_current_char = True
                task.chars = [current, target, support]
                current.in_slumberland = Mock(return_value=mode == '原梦乡入口快速交接')
                if mode.startswith('新梦乡'):
                    current.in_slumberland.side_effect = [False, True]
                current.click_echo = Mock()
                current.build_forte = Mock(return_value=True)
                current.is_mouse_forte_full = Mock(return_value=True)
                current.perform_heavy = Mock(return_value=True)
                current.is_forte_full = Mock(return_value=False)
                if mode == '解放失败且二次补能超时' or mode.startswith('新梦乡'):
                    current.liberation_available = Mock(side_effect=[True, True, False, False])
                else:
                    current.liberation_available = Mock(return_value=False)

                def fill(target_con=1):
                    result = Lily.fill_concerto(current, target_con)
                    fills.append((target_con, result))
                    return result

                # 保留补能、解放和两层公共切换；仅 HUD、时间推进及实际输入使用替身。
                current.fill_concerto = fill
                with patch('src.char.Lily.time.time', side_effect=lambda: clock.now):
                    if expected_intro is None:
                        with self.assertRaisesRegex(TimeoutError, '协奏'):
                            current.perform()
                        self.assertEqual(state.index, 0)
                        task.screenshot.assert_called_once()
                        self.assertEqual(current.last_outro_time, -1)
                        self.assertEqual(current.last_full_con_switch_time, -1)
                        self.assertEqual(current.last_buff_time, -1)
                    else:
                        current.perform()
                        self.assertEqual(state.index, 1)
                        self.assertEqual(target.has_intro, expected_intro)
                        self.assertTrue(target.is_current_char)
                        task.screenshot.assert_not_called()
                        if expected_intro:
                            self.assertGreater(current.last_outro_time, 0)
                            self.assertGreater(current.last_full_con_switch_time, 0)
                            self.assertGreater(current.last_buff_time, 0)
                        else:
                            self.assertEqual(current.last_outro_time, -1)
                self.assertEqual(fills, expected_fills)
                self.assertEqual(keys, expected_keys)
                self.assertIs(task._active_char_rotation, outer_rotation)

    def test_lily_heavy_releases_mouse_on_start_and_finish_timeouts(self):
        for observations in ([False], [True, False]):
            with self.subTest(observations=observations):
                task = SimpleNamespace(check_combat=Mock(), mouse_down=Mock(), mouse_up=Mock(),
                                       wait_until=Mock(side_effect=observations))
                char = Lily(task, 0)
                char.sleep = Mock()
                self.assertFalse(char.perform_heavy())
                task.mouse_down.assert_called_once()
                task.mouse_up.assert_called_once()
                char.sleep.assert_not_called()

    def test_lily_heavy_propagates_cancellation_and_input_errors_after_release(self):
        for error in (TaskDisabledException(), ForegroundInputError(), ForegroundInputInterrupted()):
            boundaries = [('开始等待', [error]), ('结束等待', [True, error])]
            if isinstance(error, (TaskDisabledException, ForegroundInputError)):
                boundaries.append(('诊断截图', [False]))
            for boundary, observations in boundaries:
                with self.subTest(error=type(error).__name__, boundary=boundary):
                    outer_rotation = object()
                    task = SimpleNamespace(check_combat=Mock(), mouse_down=Mock(), mouse_up=Mock(),
                                           wait_until=Mock(side_effect=observations), screenshot=Mock(),
                                           use_liberation=True, _active_char_rotation=outer_rotation)
                    if boundary == '诊断截图':
                        task.screenshot.side_effect = error
                    char = Lily(task, 0)
                    char.in_slumberland = Mock(return_value=False)
                    char.click_echo = Mock()
                    char.build_forte = Mock(return_value=True)
                    char.is_mouse_forte_full = Mock(return_value=True)
                    char.fill_concerto = Mock()
                    char.click_liberation = Mock()
                    char.switch_next_char = Mock()
                    char.sleep = Mock()
                    with self.assertRaises(type(error)):
                        char.perform()
                    task.mouse_up.assert_called_once()
                    if boundary == '诊断截图':
                        task.screenshot.assert_called_once_with('莉莉重击结果无法确认')
                    else:
                        task.screenshot.assert_not_called()
                    char.fill_concerto.assert_not_called()
                    char.click_liberation.assert_not_called()
                    char.switch_next_char.assert_not_called()
                    char.sleep.assert_not_called()
                    self.assertIs(task._active_char_rotation, outer_rotation)


if __name__ == "__main__":
    unittest.main()
