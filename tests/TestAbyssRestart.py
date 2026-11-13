import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from ok import TaskDisabledException
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.combat.CombatCheck import CombatCheck
from src.scene.WWScene import WWScene
from src.task.BaseCombatTask import BaseCombatTask


class TestAbyssRestart(unittest.TestCase):
    def make_task(self, page='arena'):
        task = BaseCombatTask.__new__(BaseCombatTask)
        task.scene = WWScene()
        task.page = page
        task.now = 0
        task._foreground_epoch = 0
        task.skip_combat_check = False
        task._in_combat = False
        task._abyss_force_combat_until = 0
        task._abyss_combat_active = False
        task.reset_to_false = Mock()
        task.business_time = lambda: task.now
        task.texts = {
            'character_info': '战斗特色', 'details': '环境特性', 'overwrite': '是否覆盖',
            'results': '确认成绩', 'confirm': '确认', 'rechallenge': '重新挑战',
            'start': '挑战开始', 'success': '挑战成功',
        }
        task.controls = {state: SimpleNamespace(name=text, state=state) for state, text in task.texts.items()}
        task.after_pages = {
            'character_info': 'arena', 'details': 'arena', 'overwrite': 'start',
            'results': 'rechallenge', 'confirm': 'arena', 'rechallenge': 'arena', 'start': 'arena',
        }

        def ocr(*args, match, **kwargs):
            text = task.texts.get(task.page, '')
            return [task.controls[task.page]] if text and match.search(text) else []

        def sleep(seconds):
            task.now += max(0, seconds)

        def wait_until(condition, time_out=0, pre_action=None, post_action=None,
                       settle_time=-1, raise_if_not_found=False):
            start = task.now
            while task.now - start < time_out:
                task.next_frame()
                if pre_action:
                    pre_action()
                if result := condition():
                    return result
                if post_action:
                    post_action()
                task.sleep(min(0.1, max(0, time_out - (task.now - start))))
            return None

        def click(x, y=-1, after_sleep=0):
            task.page = task.after_pages['character_info' if x == 0.942 else x.state]

        def send_key(key, after_sleep=0):
            if key == 'f':
                task.page = 'running'
            elif task.page in ('details', 'overwrite'):
                task.page = task.after_pages[task.page]
            elif task.page in ('combat', 'dead', 'unknown'):
                task.page = 'rechallenge'

        def resume():
            task._foreground_epoch += 1

        task.ocr = Mock(side_effect=ocr)
        task.sleep = Mock(side_effect=sleep)
        task.wait_until = Mock(side_effect=wait_until)
        task.find_abyss_start_marker = Mock(side_effect=lambda: object() if task.page == 'arena' else None)
        task.walk_to_box = Mock(side_effect=lambda find_function, time_out=30, end_condition=None: bool(end_condition()))
        task.find_f_with_text = Mock(side_effect=lambda: object() if task.page == 'arena' else None)
        task.box_of_screen = Mock()
        task.load_chars = Mock(return_value=True)
        task.in_team = Mock(side_effect=lambda: (task.page in ('arena', 'running'), 0, 3))
        task.get_abyss_remaining_time = Mock(side_effect=lambda force=False: 180 if task.page == 'running' else None)
        task.send_key = Mock(side_effect=send_key)
        task.click = Mock(side_effect=click)
        task.next_frame = Mock()
        task.wait_foreground_frame = Mock(side_effect=resume)
        task.screenshot = Mock()
        return task

    def test_idle_arena_starts_without_opening_menu(self):
        task = self.make_task()
        with patch('src.task.BaseCombatTask.time.monotonic', return_value=100):
            self.assertTrue(task.restart_combat_in_abyss())
        task.send_key.assert_called_once_with('f')
        task.click.assert_not_called()
        self.assertTrue(task._in_combat)
        self.assertEqual(task._abyss_force_combat_until, 105)
        self.assertFalse(task.skip_combat_check)

    def test_success_and_failure_results_rechallenge_directly(self):
        for result in ('success', 'failure'):
            with self.subTest(result=result):
                task = self.make_task('rechallenge')
                button = task.controls['rechallenge']
                task.restart_combat_in_abyss()
                task.click.assert_called_once_with(button, after_sleep=0)
                task.send_key.assert_called_once_with('f')
                task.wait_until.assert_called_once()

    def test_combat_and_death_open_rechallenge_menu(self):
        for state in ('combat', 'dead'):
            with self.subTest(state=state):
                task = self.make_task(state)
                task.restart_combat_in_abyss()
                self.assertEqual(task.send_key.call_args_list[0].args, ('esc',))
                self.assertEqual(task.send_key.call_args_list[-1].args, ('f',))
                task.click.assert_called_once_with(task.controls['rechallenge'], after_sleep=0)

    def test_failed_navigation_does_not_force_combat(self):
        task = self.make_task()
        task.walk_to_box.side_effect = None
        task.walk_to_box.return_value = False
        with self.assertRaisesRegex(RuntimeError, '无法到达'):
            task.restart_combat_in_abyss()
        task.send_key.assert_not_called()
        self.assertFalse(task.skip_combat_check)
        self.assertFalse(task._in_combat)
        self.assertEqual(task._abyss_force_combat_until, 0)

    def test_rechallenge_ocr_excludes_pause_dialog_explanation(self):
        task = self.make_task()
        task.restart_combat_in_abyss()
        pattern = next(call.kwargs['match'] for call in task.ocr.call_args_list
                       if call.kwargs['match'].search('重新挑战'))
        self.assertIsNone(pattern.search(
            '【返回】将清空当前进度并结束挑战，【重新挑战】将重开当前队伍的挑战'))
        for label in ('重新挑战', '重新挑戰', 'Rechallenge'):
            self.assertIsNotNone(pattern.search(label))

    def test_result_confirmation_is_accepted_before_navigation(self):
        task = self.make_task('rechallenge')
        retry, confirm = task.controls['rechallenge'], task.controls['confirm']
        task.after_pages['rechallenge'] = 'confirm'
        task.restart_combat_in_abyss()
        self.assertEqual([call.args[0] for call in task.click.call_args_list], [retry, confirm])
        task.send_key.assert_called_once_with('f')

    def test_character_intro_is_optional(self):
        task = self.make_task()
        self.assertFalse(task.dismiss_abyss_character_info())
        task.click.assert_not_called()
        task.next_frame.assert_not_called()

    def test_character_intro_is_closed_before_restart_navigation(self):
        task = self.make_task('character_info')
        task.restart_combat_in_abyss()
        task.click.assert_called_once_with(0.942, 0.058, after_sleep=0)
        task.send_key.assert_called_once_with('f')

    def test_character_intro_appearing_after_rechallenge_is_closed(self):
        task = self.make_task('rechallenge')
        retry = task.controls['rechallenge']
        task.after_pages['rechallenge'] = 'character_info'
        task.restart_combat_in_abyss()
        self.assertEqual(len(task.click.call_args_list), 2)
        self.assertEqual(task.click.call_args_list[0].args, (retry,))
        self.assertEqual(task.click.call_args_list[1].args, (0.942, 0.058))
        task.send_key.assert_called_once_with('f')

    def test_intro_close_requires_a_known_followup_page_and_never_repeats(self):
        for followup in ('character_info', 'unknown'):
            with self.subTest(followup=followup):
                task = self.make_task('character_info')
                task.after_pages['character_info'] = followup
                with self.assertRaisesRegex(RuntimeError, '关闭结果无法确认'):
                    task.dismiss_abyss_character_info()
                task.click.assert_called_once_with(0.942, 0.058, after_sleep=0)
                task.send_key.assert_not_called()
                task.screenshot.assert_called_once()

    def test_intro_header_is_anchored_and_accepts_supported_languages(self):
        task = self.make_task('character_info')
        for text, expected in (
            ('战斗特色', True), ('戰鬥特色', True), ('Combat Features', True),
            ('Combat Characteristics', True), ('Combat Style', True),
            ('介绍中的战斗特色说明', False),
        ):
            with self.subTest(text=text):
                task.texts['character_info'] = text
                self.assertEqual(bool(task.find_abyss_character_info()), expected)

    def test_empty_intro_detection_across_resume_is_discarded(self):
        task = self.make_task('character_info')
        original_ocr = task.ocr.side_effect
        first = True

        def ocr(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                task._foreground_epoch += 1
                return []
            return original_ocr(*args, **kwargs)

        task.ocr.side_effect = ocr
        self.assertTrue(task.dismiss_abyss_character_info())
        task.wait_foreground_frame.assert_called_once()
        task.click.assert_called_once_with(0.942, 0.058, after_sleep=0)

    def test_intro_close_resume_observes_the_new_page(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                task = self.make_task('character_info')
                original_sleep = task.sleep.side_effect
                first = True

                def sleep(seconds):
                    nonlocal first
                    original_sleep(seconds)
                    if first:
                        first = False
                        task._foreground_epoch += 1
                        if interrupted:
                            raise ForegroundInputInterrupted()

                task.sleep.side_effect = sleep
                self.assertTrue(task.dismiss_abyss_character_info())
                task.click.assert_called_once_with(0.942, 0.058, after_sleep=0)
                task.send_key.assert_not_called()

    def test_intro_close_does_not_swallow_cancellation_or_input_failure(self):
        for error in (TaskDisabledException(), ForegroundInputError()):
            for boundary in ('click', 'sleep', 'next_frame'):
                with self.subTest(error=type(error).__name__, boundary=boundary):
                    task = self.make_task('character_info')
                    getattr(task, boundary).side_effect = error
                    with self.assertRaises(type(error)):
                        task.dismiss_abyss_character_info()
                    task.click.assert_called_once_with(0.942, 0.058, after_sleep=0)
                    task.wait_foreground_frame.assert_not_called()
                    task.send_key.assert_not_called()

    def test_stale_navigation_button_after_resume_is_never_clicked(self):
        task = self.make_task('results')
        original_ocr = task.ocr.side_effect
        stale = True

        def ocr(*args, **kwargs):
            nonlocal stale
            result = original_ocr(*args, **kwargs)
            if stale and result and task.page == 'results':
                stale = False
                task.page = 'rechallenge'
                task._foreground_epoch += 1
            return result

        task.ocr.side_effect = ocr
        self.assertTrue(task.restart_combat_in_abyss())
        task.click.assert_called_once_with(task.controls['rechallenge'], after_sleep=0)
        task.wait_foreground_frame.assert_called_once()

    def test_accepted_navigation_actions_survive_sleep_resume_without_replay(self):
        for state in ('results', 'rechallenge', 'confirm', 'start'):
            for interrupted in (False, True):
                with self.subTest(state=state, interrupted=interrupted):
                    task = self.make_task(state)
                    original_sleep = task.sleep.side_effect
                    first = True

                    def sleep(seconds):
                        nonlocal first
                        original_sleep(seconds)
                        if first:
                            first = False
                            task._foreground_epoch += 1
                            if interrupted:
                                raise ForegroundInputInterrupted()

                    task.sleep.side_effect = sleep
                    self.assertTrue(task.restart_combat_in_abyss())
                    self.assertEqual(sum(call.args[0] is task.controls[state]
                                         for call in task.click.call_args_list), 1)
                    task.send_key.assert_called_once_with('f')
                    task.reset_to_false.assert_called_once()
                    self.assertFalse(task.skip_combat_check)

    def test_same_accepted_confirmation_is_only_observed_until_transition(self):
        task = self.make_task('confirm')
        task.after_pages['confirm'] = 'confirm'
        original_sleep = task.sleep.side_effect

        def sleep(seconds):
            original_sleep(seconds)
            if task.now >= 2:
                task.page = 'arena'

        task.sleep.side_effect = sleep
        self.assertTrue(task.restart_combat_in_abyss())
        task.click.assert_called_once_with(task.controls['confirm'], after_sleep=0)
        task.send_key.assert_called_once_with('f')

    def test_interruption_inside_an_accepted_click_never_replays_that_stage(self):
        for state in ('results', 'rechallenge', 'confirm', 'start'):
            with self.subTest(state=state):
                task = self.make_task(state)
                original_click = task.click.side_effect
                first = True

                def click(*args, **kwargs):
                    nonlocal first
                    original_click(*args, **kwargs)
                    if first:
                        first = False
                        raise ForegroundInputInterrupted()

                task.click.side_effect = click
                self.assertTrue(task.restart_combat_in_abyss())
                self.assertEqual(sum(call.args[0] is task.controls[state]
                                     for call in task.click.call_args_list), 1)
                task.wait_foreground_frame.assert_called_once()
                task.reset_to_false.assert_called_once()
                task.send_key.assert_called_once_with('f')

    def test_accepted_confirmation_with_unknown_result_fails_without_extra_input(self):
        task = self.make_task('confirm')
        task.after_pages['confirm'] = 'unknown'
        with self.assertRaisesRegex(RuntimeError, '确认'):
            task.restart_combat_in_abyss()
        task.click.assert_called_once_with(task.controls['confirm'], after_sleep=0)
        task.send_key.assert_not_called()
        task.screenshot.assert_called_once()
        self.assertFalse(task.skip_combat_check)
        self.assertFalse(task._in_combat)

    def test_resume_after_reaching_arena_does_not_open_an_unknown_menu(self):
        task = self.make_task()

        def load_chars():
            task.page = 'unknown'
            task._foreground_epoch += 1
            return True

        task.load_chars.side_effect = load_chars
        with self.assertRaisesRegex(RuntimeError, '确认'):
            task.restart_combat_in_abyss()
        task.wait_foreground_frame.assert_called_once()
        task.reset_to_false.assert_called_once()
        task.click.assert_not_called()
        task.send_key.assert_not_called()
        task.screenshot.assert_called_once()

    def test_closed_intro_followed_by_unknown_page_does_not_open_pause(self):
        task = self.make_task('character_info')

        def next_frame():
            if task.page == 'arena' and task.now > 0.5:
                task.page = 'unknown'

        task.next_frame.side_effect = next_frame
        with self.assertRaisesRegex(RuntimeError, '确认'):
            task.restart_combat_in_abyss()
        task.click.assert_called_once_with(0.942, 0.058, after_sleep=0)
        task.send_key.assert_not_called()
        task.reset_to_false.assert_called_once()

    def test_results_overwrite_cancellation_advances_without_reconfirming_results(self):
        task = self.make_task('results')
        task.after_pages['results'] = 'overwrite'
        self.assertTrue(task.restart_combat_in_abyss())
        self.assertEqual([call.args[0] for call in task.click.call_args_list],
                         [task.controls['results'], task.controls['start']])
        self.assertEqual([call.args[0] for call in task.send_key.call_args_list], ['esc', 'f'])

    def test_interrupted_f_is_not_repeated_and_requires_real_countdown(self):
        for accepted in (False, True):
            for interrupted in (False, True):
                with self.subTest(accepted=accepted, interrupted=interrupted):
                    task = self.make_task()
                    original_key = task.send_key.side_effect

                    def send_key(key, after_sleep=0):
                        if accepted:
                            original_key(key, after_sleep)
                        task._foreground_epoch += 1
                        if interrupted:
                            raise ForegroundInputInterrupted()

                    task.send_key.side_effect = send_key
                    if accepted:
                        self.assertTrue(task.restart_combat_in_abyss())
                        self.assertTrue(task._abyss_combat_active)
                    else:
                        with self.assertRaisesRegex(RuntimeError, '无法.*确认'):
                            task.restart_combat_in_abyss()
                        self.assertFalse(task._abyss_combat_active)
                    task.send_key.assert_called_once_with('f')
                    task.get_abyss_remaining_time.assert_called()
                    task.reset_to_false.assert_called_once()

    def test_restart_propagates_system_input_failure_and_cancellation(self):
        for error in (ForegroundInputError(), TaskDisabledException()):
            for boundary in ('click', 'sleep'):
                with self.subTest(error=type(error).__name__, boundary=boundary):
                    task = self.make_task('rechallenge')
                    getattr(task, boundary).side_effect = error
                    with self.assertRaises(type(error)):
                        task.restart_combat_in_abyss()
                    task.wait_foreground_frame.assert_not_called()
                    task.send_key.assert_not_called()
                    self.assertFalse(task.skip_combat_check)

    def test_cancellation_while_waiting_for_foreground_propagates(self):
        task = self.make_task('confirm')
        task.click.side_effect = ForegroundInputInterrupted()
        task.wait_foreground_frame.side_effect = TaskDisabledException()
        with self.assertRaises(TaskDisabledException):
            task.restart_combat_in_abyss()
        task.click.assert_called_once()
        task.send_key.assert_not_called()
        self.assertFalse(task.skip_combat_check)

    def test_repeated_resume_does_not_reset_navigation_budget(self):
        task = self.make_task()

        def next_frame():
            task.now += 4
            raise ForegroundInputInterrupted()

        task.next_frame.side_effect = next_frame
        with self.assertRaisesRegex(RuntimeError, '规定时间'):
            task.restart_combat_in_abyss()
        self.assertEqual(task.now, 32)
        task.reset_to_false.assert_called_once()
        task.click.assert_not_called()
        task.send_key.assert_not_called()
        task.screenshot.assert_called_once()

    def test_recognition_exhausting_budget_prevents_late_input(self):
        for page, timeout in (('character_info', 5), ('results', 30)):
            with self.subTest(page=page):
                task = self.make_task(page)
                original_ocr = task.ocr.side_effect

                def ocr(*args, **kwargs):
                    result = original_ocr(*args, **kwargs)
                    if result:
                        task.now = timeout
                    return result

                task.ocr.side_effect = ocr
                with self.assertRaises(RuntimeError):
                    if page == 'character_info':
                        task.dismiss_abyss_character_info()
                    else:
                        task.restart_combat_in_abyss()
                task.click.assert_not_called()
                task.send_key.assert_not_called()
                task.screenshot.assert_called_once()

    def test_forced_combat_expires_after_five_seconds(self):
        task = self.make_task()
        task._abyss_force_combat_until = 105
        with patch.object(CombatCheck, 'do_check_in_combat', return_value=False) as normal_check:
            with patch('src.task.BaseCombatTask.time.monotonic', return_value=104.99):
                self.assertTrue(task.do_check_in_combat(False))
                normal_check.assert_not_called()
            with patch('src.task.BaseCombatTask.time.monotonic', return_value=105):
                self.assertFalse(task.do_check_in_combat(False))
                normal_check.assert_called_once_with(False)

    def test_reset_clears_forced_combat(self):
        task = self.make_task()
        task._abyss_force_combat_until = 105
        with patch.object(CombatCheck, 'do_reset_to_false', return_value=False):
            self.assertFalse(task.do_reset_to_false())
        self.assertEqual(task._abyss_force_combat_until, 0)

    def test_active_countdown_prevents_false_combat_end_after_target_loss(self):
        task = self.make_task()
        task._abyss_combat_active = True
        task.in_team = Mock(return_value=(True, 1, 3))
        task.get_abyss_remaining_time = Mock(return_value=173.42)
        task.scene = Mock()
        task.scene.set_in_combat.return_value = True
        with patch.object(CombatCheck, 'do_check_in_combat', return_value=False) as normal:
            self.assertTrue(task.do_check_in_combat(False))
            normal.assert_not_called()
            task.get_abyss_remaining_time.return_value = None
            self.assertFalse(task.do_check_in_combat(False))
            normal.assert_called_once_with(False)

    def test_tower_success_confirms_results_before_rechallenging(self):
        task = self.make_task('results')
        confirm, retry = task.controls['results'], task.controls['rechallenge']
        task.restart_combat_in_abyss()
        self.assertEqual([call.args[0] for call in task.click.call_args_list], [confirm, retry])

    def test_selected_tower_floor_starts_without_escaping_to_another_page(self):
        task = self.make_task('start')
        start = task.controls['start']
        task.restart_combat_in_abyss()
        task.click.assert_called_once_with(start, after_sleep=0)
        task.send_key.assert_called_once_with('f')

    def test_tower_team_confirmation_uses_open_challenge_label(self):
        task = self.make_task('start')
        task.texts['start'] = '开启挑战'
        start = task.controls['start']
        task.restart_combat_in_abyss()
        task.click.assert_called_once_with(start, after_sleep=0)
        task.send_key.assert_called_once_with('f')

    def test_timer_reader_does_not_accept_hp_or_hud_decorations(self):
        task = self.make_task()
        del task.get_abyss_remaining_time
        task.box_of_screen_scaled = Mock()
        task.ocr.side_effect = None
        for text, expected in [('04:53:49', 293.49), ('03:12', 192), ('18476/18476', None), ('12:99', None)]:
            task.ocr.return_value = [Mock(name='unused')]
            task.ocr.return_value[0].name = text
            self.assertEqual(task.get_abyss_remaining_time(force=True), expected)

    def switch_task(self):
        task = self.make_task()
        task.click = Mock()
        current, target = Mock(), Mock()
        current.index, current.char_type, current.is_sub_dps = 0, 'Healer', False
        current.last_switch_time = 100
        target.index, target.char_type, target.intro_motion_freeze_duration = 1, 'MainDps', .9
        target.wait_switch.return_value = False
        task._choose_switch_target = Mock(return_value=target)
        task.update_lib_portrait_icon = Mock()
        task.check_combat = Mock()
        task.next_frame = Mock()
        task.sleep = Mock()
        task.log_debug = Mock()
        task.add_freeze_duration = Mock()
        task.switch_char_time_out = 5
        task.raise_not_in_combat = Mock(side_effect=RuntimeError('switch timed out'))
        return task, current, target

    def test_switch_waits_for_party_hud_to_return_during_intro(self):
        task, current, target = self.switch_task()
        task.in_team = Mock(side_effect=[(True, 0, 3), (False, -1, 0), (False, -1, 0), (True, 1, 3)])
        with patch('src.task.BaseCombatTask.time', SimpleNamespace(time=lambda: 100)):
            task.switch_next_char(current, free_intro=True)
        task.raise_not_in_combat.assert_not_called()
        current.switch_out.assert_called_once_with(con_full=True)
        self.assertTrue(target.is_current_char)
        task.next_frame.assert_called_once()

    def test_switch_still_times_out_when_party_hud_never_returns(self):
        task, current, target = self.switch_task()
        task.in_team = Mock(return_value=(False, -1, 0))
        with patch('src.task.BaseCombatTask.time', SimpleNamespace(time=Mock(side_effect=[100, 106]))):
            with self.assertRaisesRegex(RuntimeError, 'switch timed out'):
                task.switch_next_char(current, free_intro=True)
        current.switch_out.assert_not_called()


if __name__ == '__main__':
    unittest.main()
