import unittest
from unittest.mock import Mock, call

from ok import Box, TaskDisabledException
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


def make_rechallenge_task(rounds=2, page='arena'):
    """用可观察的菜单动作驱动真实刷取循环，区分重挑战和退出。"""
    task = make_task({'Repeat Farm Count': rounds, 'Echo Pickup Method': 'Walk'})
    task.page = page
    task.now = 0
    task._foreground_epoch = 0
    task.actions = []
    task.after_restart = 'arena'
    task._in_realm = True
    task.has_new_battle = False
    task.controls = {
        'restart': Box(1010, 868, 250, 72, name='重新挑战'),
        'exit': Box(1760, 868, 262, 72, name='确定退出'),
        'cancel': Box(680, 868, 220, 72, name='取消'),
        'confirm': Box(1580, 868, 240, 72, name='确定'),
        'prompt': Box(860, 560, 700, 80, name='是否重新挑战？'),
        'explanation': Box(600, 540, 1300, 80, name='选择重新挑战将重开当前队伍的挑战'),
    }
    task.visible_controls = {
        'menu': ('restart', 'exit', 'cancel'),
        'confirm': ('prompt', 'confirm', 'cancel'),
        'exit_only': ('exit', 'cancel'),
        'explanation': ('explanation', 'exit', 'cancel'),
        'unrelated_confirm': ('confirm', 'cancel'),
    }

    def ocr(*args, **kwargs):
        return [task.controls[key] for key in task.visible_controls.get(task.page, ())]

    def find_one(names, **kwargs):
        if isinstance(names, str):
            names = [names]
        if task.page in task.visible_controls:
            if any(name.startswith('confirm_btn') for name in names):
                return task.controls['confirm' if task.page == 'confirm' else 'exit']
            if any('cancel_button' in name for name in names):
                return task.controls['cancel']
        return None

    def send_key(key, **kwargs):
        task.actions.append(('key', key))
        if key == 'esc':
            task.page = 'arena' if task.page == 'menu' else 'menu'
        return True

    def click(control, **kwargs):
        key = next(key for key, value in task.controls.items() if value is control)
        task.actions.append(('click', key))
        if key == 'restart':
            task.page = task.after_restart
            task.has_new_battle = task.page == 'arena'
        elif key == 'confirm':
            task.page = 'arena'
            task.has_new_battle = True
        elif key == 'exit':
            task.page = 'world'
        else:
            raise AssertionError('重挑战流程不应点击其它控件')
        return True

    def combat_once(**kwargs):
        task.actions.append(('fight', task.page))
        if task.page != 'world':
            task.page = 'won'
            task.has_new_battle = False

    def sleep(seconds):
        task.now += max(0, seconds)

    def resume():
        task._foreground_epoch += 1

    task.ocr = Mock(side_effect=ocr)
    task.find_one = Mock(side_effect=find_one)
    task.send_key = Mock(side_effect=send_key)
    task.click = Mock(side_effect=click)
    task.combat_once.side_effect = combat_once
    task.in_realm.side_effect = lambda: task.page in ('arena', 'won')
    task.in_world = Mock(side_effect=lambda: task.page == 'world')
    task.in_team_and_world = Mock(side_effect=lambda: task.page in ('arena', 'won', 'world'))
    task.in_combat.return_value = False
    task.check_health_bar = Mock(side_effect=lambda: task.page == 'arena' and task.has_new_battle)
    task.pick_echo.return_value = True
    task.wait_until = Mock(side_effect=lambda condition, **kwargs: condition())
    task.sleep.side_effect = sleep
    task.business_time = lambda: task.now
    task.next_frame = Mock()
    task.screenshot = Mock()
    task.wait_foreground_frame.side_effect = resume
    return task


class TestFarmEchoPickup(unittest.TestCase):

    def test_realm_next_round_rechallenges_instead_of_confirming_exit(self):
        task = make_rechallenge_task()
        task.do_run()
        self.assertEqual(task.actions, [('fight', 'arena'), ('key', 'esc'),
                                        ('click', 'restart'), ('fight', 'arena')])
        self.assertEqual(task.pick_echo.call_count, 2)
        self.assertEqual(task._farm_state['count'], 2)

    def test_first_entry_and_exhausted_rounds_do_not_open_rechallenge(self):
        task = make_rechallenge_task(rounds=1)
        task.do_run()
        self.assertEqual(task.actions, [('fight', 'arena')])
        task.send_key.assert_not_called()
        task.click.assert_not_called()

    def test_world_round_keeps_existing_treasure_restart_path(self):
        task = make_task()
        task._just_entered_boss_realm = False
        task.in_combat.return_value = False
        task.walk_to_treasure_and_restart = Mock(return_value=True)
        task.handle_boss_restart_after_treasure = Mock()
        task._restart_echo_realm = Mock()
        task._prepare_echo_round(1)
        task.walk_to_treasure_and_restart.assert_called_once()
        task.handle_boss_restart_after_treasure.assert_called_once()
        task._restart_echo_realm.assert_not_called()

    def test_rechallenge_accepts_only_its_own_confirmation(self):
        task = make_rechallenge_task(page='menu')
        task.after_restart = 'confirm'
        self.assertTrue(task._restart_echo_realm())
        self.assertEqual(task.actions, [('click', 'restart'), ('click', 'confirm')])
        task.send_key.assert_not_called()

    def test_weekly_rechallenge_does_not_require_illusive_realm_marker(self):
        task = make_rechallenge_task(page='menu')
        task.in_realm.side_effect = None
        task.in_realm.return_value = False
        self.assertTrue(task._restart_echo_realm())
        self.assertEqual(task.actions, [('click', 'restart')])
        task.in_realm.assert_not_called()

    def test_unknown_exit_and_explanation_pages_never_click(self):
        for page in ('unknown', 'exit_only', 'explanation', 'unrelated_confirm', 'confirm'):
            with self.subTest(page=page):
                task = make_rechallenge_task(page=page)
                with self.assertRaises(ForegroundInputError):
                    task._restart_echo_realm(time_out=0.3)
                task.click.assert_not_called()
                task.send_key.assert_not_called()
                task.screenshot.assert_called_once()

    def test_restart_label_is_exact_and_duplicate_targets_are_rejected(self):
        for text in ('重新挑战', '重新挑戰', '再次挑战', 'Rechallenge', 'Restart Challenge'):
            with self.subTest(text=text):
                task = make_rechallenge_task(page='menu')
                task.controls['restart'].name = text
                self.assertTrue(task._restart_echo_realm())
        task = make_rechallenge_task(page='menu')
        task.visible_controls['menu'] = ('restart', 'restart', 'exit', 'cancel')
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.3)
        task.click.assert_not_called()

    def test_foreground_change_during_recognition_discards_old_button(self):
        task = make_rechallenge_task(page='menu')
        old = task.controls['restart']
        recognize = task.ocr.side_effect

        def interrupted_recognition(*args, **kwargs):
            result = recognize(*args, **kwargs)
            task.controls['restart'] = Box(1110, 900, 250, 72, name='重新挑战')
            task._foreground_epoch += 1
            task.ocr.side_effect = recognize
            return result

        task.ocr.side_effect = interrupted_recognition
        self.assertTrue(task._restart_echo_realm())
        task.wait_foreground_frame.assert_called_once()
        self.assertIsNot(task.click.call_args.args[0], old)

    def test_foreground_after_restart_click_observes_result_without_replaying(self):
        for following in ('arena', 'confirm'):
            with self.subTest(following=following):
                # 直接重开需有旧通关画面无血条、新战斗出现血条的正向对照。
                task = make_rechallenge_task(page='won' if following == 'arena' else 'menu')
                task.after_restart = following
                click = task.click.side_effect

                def interrupted_click(control, **kwargs):
                    click(control, **kwargs)
                    task.click.side_effect = click
                    raise ForegroundInputInterrupted()

                task.click.side_effect = interrupted_click
                self.assertTrue(task._restart_echo_realm())
                self.assertEqual(task.actions.count(('click', 'restart')), 1)
                task.wait_foreground_frame.assert_called_once()

    def test_escape_interrupted_before_input_reopens_menu_from_current_realm(self):
        task = make_rechallenge_task(page='won')
        send = task.send_key.side_effect

        def interrupted_before_send(key, **kwargs):
            task.send_key.side_effect = send
            raise ForegroundInputInterrupted('前台守卫在发送按键前中断')

        task.send_key.side_effect = interrupted_before_send
        self.assertTrue(task._restart_echo_realm(time_out=0.3))
        self.assertEqual(task.actions, [('key', 'esc'), ('click', 'restart')])
        self.assertEqual(task.send_key.call_count, 2)

    def test_escape_delivered_before_interruption_does_not_close_open_menu(self):
        task = make_rechallenge_task(page='won')
        send = task.send_key.side_effect

        def interrupted_after_send(key, **kwargs):
            send(key, **kwargs)
            task.send_key.side_effect = send
            raise ForegroundInputInterrupted()

        task.send_key.side_effect = interrupted_after_send
        self.assertTrue(task._restart_echo_realm(time_out=0.3))
        self.assertEqual(task.actions, [('key', 'esc'), ('click', 'restart')])
        task.send_key.assert_called_once()

    def test_unsubmitted_restart_and_closed_menu_are_not_new_realm_evidence(self):
        task = make_rechallenge_task(page='menu')
        task.click.side_effect = ForegroundInputInterrupted('点击发出前中断')

        def resume_to_old_cleared_realm():
            task._foreground_epoch += 1
            task.page = 'won'

        task.wait_foreground_frame.side_effect = resume_to_old_cleared_realm
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.3)
        self.assertEqual(task.actions, [])

    def test_old_enemy_health_does_not_prove_an_interrupted_restart(self):
        task = make_rechallenge_task(page='arena')
        task.has_new_battle = True
        task.click.side_effect = ForegroundInputInterrupted('点击发出前中断')

        def resume_to_old_battle():
            task._foreground_epoch += 1
            task.page = 'arena'

        task.wait_foreground_frame.side_effect = resume_to_old_battle
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.3)
        self.assertEqual(task.actions, [('key', 'esc')])

    def test_recognition_resume_does_not_reuse_new_health_from_old_frame(self):
        task = make_rechallenge_task(page='won')
        click = task.click.side_effect
        health = task.check_health_bar.side_effect

        def interrupted_click(control, **kwargs):
            click(control, **kwargs)
            raise ForegroundInputInterrupted()

        def stale_health():
            found = health()
            if found:
                task._foreground_epoch += 1
                task.page = 'won'
                task.has_new_battle = False
            return found

        task.click.side_effect = interrupted_click
        task.check_health_bar.side_effect = stale_health
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.4)
        self.assertEqual(task.actions, [('key', 'esc'), ('click', 'restart')])

    def test_unconfirmed_restart_is_not_replayed_or_reported_successful(self):
        task = make_rechallenge_task(page='menu')
        task.click.side_effect = ForegroundInputInterrupted()
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.3)
        task.click.assert_called_once()
        task.send_key.assert_not_called()

    def test_background_hud_does_not_confirm_an_unrelated_dialog(self):
        task = make_rechallenge_task(page='menu')
        task.after_restart = 'unrelated_confirm'
        task.in_team_and_world.side_effect = None
        task.in_team_and_world.return_value = True
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.3)
        self.assertEqual(task.actions, [('click', 'restart')])

    def test_focus_after_confirmation_does_not_submit_twice(self):
        task = make_rechallenge_task(page='won')
        task.after_restart = 'confirm'
        click = task.click.side_effect

        def interrupted_confirmation(control, **kwargs):
            result = click(control, **kwargs)
            if control is task.controls['confirm']:
                raise ForegroundInputInterrupted()
            return result

        task.click.side_effect = interrupted_confirmation
        self.assertTrue(task._restart_echo_realm())
        self.assertEqual(task.actions, [('key', 'esc'), ('click', 'restart'), ('click', 'confirm')])
        task.wait_foreground_frame.assert_called_once()

    def test_unsubmitted_confirmation_cannot_reuse_restart_acknowledgement(self):
        task = make_rechallenge_task(page='won')
        task.after_restart = 'confirm'
        click = task.click.side_effect

        def interrupted_confirmation(control, **kwargs):
            if control is task.controls['confirm']:
                raise ForegroundInputInterrupted('确认点击前中断')
            return click(control, **kwargs)

        def resume_to_old_cleared_realm():
            task._foreground_epoch += 1
            task.page = 'won'

        task.click.side_effect = interrupted_confirmation
        task.wait_foreground_frame.side_effect = resume_to_old_cleared_realm
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.4)
        self.assertEqual(task.actions, [('key', 'esc'), ('click', 'restart')])

    def test_implicit_resume_after_click_preserves_submitted_stage(self):
        task = make_rechallenge_task(page='won')
        sleep = task.sleep.side_effect

        def resumed_sleep(seconds):
            sleep(seconds)
            task._foreground_epoch += 1

        task.sleep.side_effect = resumed_sleep
        self.assertTrue(task._restart_echo_realm())
        self.assertEqual(task.actions, [('key', 'esc'), ('click', 'restart')])

    def test_implicit_resume_without_restart_evidence_cannot_accept_old_hud(self):
        task = make_rechallenge_task(page='menu')
        task.after_restart = 'won'
        sleep = task.sleep.side_effect

        def resumed_sleep(seconds):
            sleep(seconds)
            task._foreground_epoch += 1

        task.sleep.side_effect = resumed_sleep
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.3)
        self.assertEqual(task.actions, [('click', 'restart')])

    def test_skipped_click_does_not_acknowledge_a_restart(self):
        task = make_rechallenge_task(page='menu')

        def skipped_click(control, **kwargs):
            task.page = 'won'
            return False

        task.click.side_effect = skipped_click
        with self.assertRaises(ForegroundInputError):
            task._restart_echo_realm(time_out=0.3)
        task.click.assert_called_once()

    def test_world_hud_after_restart_is_not_dungeon_success(self):
        task = make_rechallenge_task(page='menu')
        task.after_restart = 'world'
        with self.assertRaisesRegex(ForegroundInputError, '大世界'):
            task._restart_echo_realm()
        self.assertEqual(task.actions, [('click', 'restart')])

    def test_rechallenge_propagates_cancellation_and_system_input_failure(self):
        for error in (TaskDisabledException(), ForegroundInputError('输入失败')):
            with self.subTest(error=type(error).__name__):
                task = make_rechallenge_task(page='menu')
                task.click.side_effect = error
                with self.assertRaises(type(error)):
                    task._restart_echo_realm()
                task.click.assert_called_once()
                task.wait_foreground_frame.assert_not_called()

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
