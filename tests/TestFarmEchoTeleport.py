import unittest
from unittest.mock import Mock, call, patch

from ok import BaseTask, Box, ForegroundInputError, ForegroundInputInterrupted, TaskDisabledException
from src.task.BaseWWTask import BaseWWTask
from src.task.FarmEchoTask import FarmEchoTask
from src.task.WWOneTimeTask import WWOneTimeTask


def make_task(config, is_team):
    task = FarmEchoTask.__new__(FarmEchoTask)
    task.config = config
    task.info = {}
    task.total_weekly_number = 9
    task.total_boss_number = 20
    task.nightmare_structure = [5, 10]
    for name in ('ensure_main', 'info_set', 'openF2Book', 'open_boss_book', 'click', 'click_configured_boss_level',
                 'click_team_challenge', 'wait_click_travel', 'wait_in_team_and_world', 'sleep'):
        setattr(task, name, Mock())
    task.book_targets = []
    task.click_on_book_target = lambda serial, total, structure=None: task.book_targets.append(
        (serial, total, structure)) or is_team
    return task


class TestFarmEchoTeleport(unittest.TestCase):

    def test_special_nightmare_is_found_after_the_nightmare_nests(self):
        task = make_task({'Teleport to Boss': 'Special Nightmare', 'Which Special Nightmare to Teleport': 3},
                         is_team=True)

        self.assertTrue(task.teleport_to_configured_boss())

        task.open_boss_book.assert_called_once_with('mengyan')
        self.assertEqual([(8, 15, [5, 10])], task.book_targets)
        task.click_team_challenge.assert_called_once_with()
        task.click_configured_boss_level.assert_not_called()

    def test_boss_challenge_target_is_unchanged(self):
        task = make_task({'Teleport to Boss': 'Boss Challenge', 'Which Boss Challenge to Teleport': 3},
                         is_team=False)

        self.assertFalse(task.teleport_to_configured_boss())

        task.open_boss_book.assert_called_once_with('qiangdi')
        self.assertEqual([(3, 20, None)], task.book_targets)
        task.wait_click_travel.assert_called_once_with()

    # With too few waveplates (incl. reserve), a "no rewards, enter anyway?" popup shows after
    # solo challenge and before the team screen.
    def test_weekly_challenge_confirms_low_waveplate_popup_before_team_screen(self):
        task = make_task({'Teleport to Boss': 'Weekly Challenge', 'Which Weekly Boss to Teleport': 2},
                         is_team=True)
        task.wait_click_skip_dialog_confirm = Mock()
        steps = Mock()
        for name in ('click_configured_boss_level', 'click', 'wait_click_skip_dialog_confirm', 'click_team_challenge'):
            steps.attach_mock(getattr(task, name), name)

        self.assertTrue(task.teleport_to_configured_boss())

        self.assertEqual([call.click_configured_boss_level(), call.click(0.880, 0.911, after_sleep=2),
                          call.wait_click_skip_dialog_confirm(time_out=1), call.click_team_challenge()],
                         steps.mock_calls)

    def test_reentry_from_f_confirms_low_waveplate_popup_before_starting(self):
        task = make_task({}, is_team=True)
        task.wait_until = Mock(return_value=True)
        task.send_key = Mock()
        task.init_parameters = Mock()
        task.wait_click_skip_dialog_confirm = Mock()
        steps = Mock()
        for name in ('click', 'wait_click_skip_dialog_confirm', 'wait_in_team_and_world'):
            steps.attach_mock(getattr(task, name), name)

        task.enter_configured_boss_realm_from_f()

        self.assertEqual([call.click(0.880, 0.911, after_sleep=2), call.wait_click_skip_dialog_confirm(time_out=1),
                          call.click(0.908, 0.919, after_sleep=5), call.wait_in_team_and_world(time_out=120)],
                         steps.mock_calls)

    def test_popup_mistaken_for_claim_is_retried_at_most_three_times(self):
        task = make_task({}, is_team=True)
        task.do_run = Mock(side_effect=RuntimeError('Teleport to boss failed'))
        task.handle_claim_button = Mock(return_value=True)
        task.handle_monthly_card = Mock(return_value=False)

        with patch.object(WWOneTimeTask, 'run'), self.assertRaises(RuntimeError):
            task.run()

        self.assertEqual(4, task.do_run.call_count)

    def test_farm_continues_after_claim_popup_is_closed(self):
        task = make_task({}, is_team=True)
        task.do_run = Mock(side_effect=[RuntimeError('Teleport to boss failed'), None])
        task.handle_claim_button = Mock(return_value=True)
        task.handle_monthly_card = Mock(return_value=False)

        with patch.object(WWOneTimeTask, 'run'):
            task.run()

        self.assertEqual(2, task.do_run.call_count)


class TestNavigationForegroundRecovery(unittest.TestCase):
    def make_task(self):
        task = BaseWWTask.__new__(BaseWWTask)
        task._foreground_epoch = 0
        task.key_config = {}
        task.tr = str
        for name in ('log_info', 'log_error', 'ensure_main', 'send_key', 'send_key_down', 'send_key_up',
                     'sleep', 'click_relative', 'click_box', 'wait_foreground_frame'):
            setattr(task, name, Mock())
        task.find_one = Mock(return_value=None)
        task.in_team_and_world = Mock(return_value=True)
        task.wait_book = Mock(return_value=Box(10, 8, 4, 4, name='gray_book_all_monsters'))
        return task

    def make_foreground_task(self):
        # 组合复用前台生命周期夹具，保留真实任务、执行器、新帧通知和输入适配链。
        from tests import TestForegroundInput as foreground_tests

        fixture = foreground_tests.TestForegroundLifecycle()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        task = self.make_task()
        BaseTask.__init__(task, fixture.executor, fixture.task._app)
        task._enabled = task.running = True
        task.config = {}
        task.check_for_monthly_card = Mock(return_value=0)
        del task.sleep, task.click_box, task.wait_foreground_frame
        fixture.executor.current_task = task
        fixture.executor.onetime_tasks = [task]
        fixture.executor.next_frame()
        return task, fixture

    def test_图鉴真实等待恢复后只点击新帧中的框(self):
        task, fixture = self.make_foreground_task()
        original_epoch = task._foreground_epoch
        original_frame_count = fixture.capture.count
        stale = Box(2, 2, 4, 4, name='gray_book_all_monsters')
        fresh = Box(10, 8, 4, 4, name='gray_book_all_monsters')
        task.find_one.return_value = stale

        def locate_book(feature):
            if task.wait_book.call_count == 1:
                fixture.native.foreground = 20
                resume_at = fixture.clock() + .15
                fixture.clock.after_wait = lambda: setattr(fixture.native, 'foreground', 10) \
                    if fixture.clock() >= resume_at else None
                return stale
            return fresh

        task.wait_book.side_effect = locate_book

        self.assertIs(task.openF2Book(), fresh)

        self.assertGreater(task._foreground_epoch, original_epoch)
        self.assertGreater(fixture.capture.count, original_frame_count)
        self.assertEqual(task.wait_book.call_count, 2)
        self.assertEqual(fixture.native.moves, [(-1788, 70)])
        self.assertFalse(fixture.core._held)
        fixture.window.bring_to_front.assert_not_called()

    def test_图鉴识别空结果跨过恢复时重新识别(self):
        task = self.make_task()
        fresh = task.wait_book.return_value
        task.find_one.return_value = fresh

        def lose_result_once(feature):
            if task.wait_book.call_count == 1:
                task.on_foreground_resume()
                return None
            return fresh

        task.wait_book.side_effect = lose_result_once

        self.assertIs(task.openF2Book(), fresh)

        task.wait_foreground_frame.assert_called_once()
        task.click_box.assert_called_once_with(fresh, after_sleep=1.5)
        task.log_error.assert_not_called()

    def test_图鉴未恢复且识别失败仍报错(self):
        task = self.make_task()
        task.find_one.return_value = object()
        task.wait_book.return_value = None

        with self.assertRaisesRegex(Exception, "can't find gray_book_boss"):
            task.openF2Book()

        task.wait_foreground_frame.assert_not_called()
        task.click_box.assert_not_called()
        task.log_error.assert_called_once()

    def test_图鉴识别取消和系统失败直接上抛(self):
        for error in (ForegroundInputError, TaskDisabledException):
            with self.subTest(error=error):
                task = self.make_task()
                task.find_one.return_value = object()
                task.wait_book.side_effect = error('图鉴识别已停止')

                with self.assertRaises(error):
                    task.openF2Book()

                task.wait_foreground_frame.assert_not_called()
                task.click_box.assert_not_called()
                task.log_error.assert_not_called()

    def test_图鉴点击后真实等待恢复会根据当前页面重新定位(self):
        for resumed_page in ('book', 'world'):
            with self.subTest(resumed_page=resumed_page):
                task, fixture = self.make_foreground_task()
                original_epoch = task._foreground_epoch
                stale = Box(2, 2, 4, 4, name='gray_book_all_monsters')
                fresh = Box(10, 8, 4, 4, name='gray_book_all_monsters')
                state = {'page': 'book', 'resumed': False}
                task.find_one.side_effect = lambda feature: fresh if state['page'] == 'book' else None
                task.wait_book.side_effect = lambda feature: fresh if state['resumed'] else stale
                task.in_team_and_world.side_effect = lambda: state['page'] == 'world'
                task.ensure_main.side_effect = lambda: state.update(page='world')
                task.send_key.side_effect = lambda *args, **kwargs: state.update(page='book')
                original_sleep = task.sleep

                def resume_after_click(seconds):
                    if seconds == 1.5 and not state['resumed']:
                        state.update(page=resumed_page, resumed=True)
                        fixture.native.foreground = 20
                        resume_at = fixture.clock() + .15
                        fixture.clock.after_wait = lambda: setattr(fixture.native, 'foreground', 10) \
                            if fixture.clock() >= resume_at else None
                    return original_sleep(seconds)

                task.sleep = resume_after_click
                try:
                    result = task.openF2Book()

                    self.assertIs(result, fresh)
                    self.assertEqual(state['page'], 'book')
                    self.assertGreater(task._foreground_epoch, original_epoch)
                    self.assertEqual(task.wait_book.call_count, 2)
                    # 首次点击在失焦前完成，恢复后的幂等选择只能使用重新识别的新框。
                    self.assertEqual(fixture.native.moves, [(-1796, 64), (-1788, 70)])
                    self.assertFalse(fixture.core._held)
                    if resumed_page == 'world':
                        task.ensure_main.assert_called_once()
                        task.send_key.assert_called_once_with('f2', after_sleep=4)
                    else:
                        task.ensure_main.assert_not_called()
                        task.send_key.assert_not_called()
                    fixture.window.bring_to_front.assert_not_called()
                finally:
                    fixture.doCleanups()

    def test_图鉴正常真实点击等待不重复选择(self):
        task, fixture = self.make_foreground_task()
        selected = task.wait_book.return_value
        task.find_one.return_value = selected
        original_epoch = task._foreground_epoch

        self.assertIs(task.openF2Book(), selected)

        self.assertEqual(task._foreground_epoch, original_epoch)
        self.assertEqual(fixture.native.moves, [(-1788, 70)])
        task.wait_book.assert_called_once()
        task.ensure_main.assert_not_called()
        task.send_key.assert_not_called()

    def configure_alt_menu(self, task, page='world'):
        state = {'held': False, 'page': page}
        task.send_key_down.side_effect = lambda key: state.update(held=True)
        task.send_key_up.side_effect = lambda key: state.update(held=False)
        task.find_one.side_effect = lambda feature: feature == 'esc_setting' and state['page'] == 'menu'
        task.ensure_main.side_effect = lambda: state.update(page='world')

        def click_menu(*args, **kwargs):
            self.assertTrue(state['held'], 'Alt 已被释放时不能续接原点击')
            state['page'] = 'menu'

        task.click_relative.side_effect = click_menu
        return state

    def test_菜单等待恢复后重新按住Alt才点击(self):
        task = self.make_task()
        state = self.configure_alt_menu(task)

        def resume_once(seconds):
            if seconds == .05 and task.send_key_down.call_count == 1:
                state['held'] = False
                task.on_foreground_resume()

        task.sleep.side_effect = resume_once

        task.open_esc_menu()

        task.click_relative.assert_called_once_with(.95, .04)
        self.assertEqual(task.send_key_down.call_count, 2)
        self.assertEqual(task.send_key_up.call_count, 2)
        self.assertEqual(state['page'], 'menu')
        self.assertFalse(state['held'])
        task.wait_foreground_frame.assert_called_once()

    def test_菜单点击已接受后失焦不会再次点击(self):
        task = self.make_task()
        state = self.configure_alt_menu(task)

        def accepted_then_interrupted(*args, **kwargs):
            state['page'] = 'menu'
            raise ForegroundInputInterrupted('菜单点击已发出后失焦')

        task.click_relative.side_effect = accepted_then_interrupted

        task.open_esc_menu()

        task.click_relative.assert_called_once()
        task.ensure_main.assert_not_called()
        task.wait_foreground_frame.assert_called_once()
        self.assertFalse(state['held'])

    def test_菜单点击后等待恢复会重新判断当前页面(self):
        task = self.make_task()
        state = self.configure_alt_menu(task)

        def close_menu_during_wait(seconds):
            if seconds == .5 and task.click_relative.call_count == 1:
                state['page'] = 'world'
                task.on_foreground_resume()

        task.sleep.side_effect = close_menu_during_wait

        task.open_esc_menu()

        self.assertEqual(state['page'], 'menu')
        self.assertEqual(task.click_relative.call_count, 2)
        self.assertEqual(task.send_key_down.call_count, 2)
        task.ensure_main.assert_called_once()

    def test_图鉴Alt备用路径恢复后不续接已释放的手势(self):
        task = self.make_task()
        state = self.configure_alt_menu(task)
        task.find_one.side_effect = lambda feature: state['page'] == 'book'

        def click_book(*args, **kwargs):
            self.assertTrue(state['held'], '图鉴备用点击必须使用新的 Alt 手势')
            state['page'] = 'book'

        def resume_once(seconds):
            if seconds == .05 and task.send_key_down.call_count == 1:
                state['held'] = False
                task.on_foreground_resume()

        task.click_relative.side_effect = click_book
        task.sleep.side_effect = resume_once

        task.openF2Book()

        task.click_relative.assert_called_once_with(.77, .05)
        self.assertEqual(task.send_key_down.call_count, 2)
        self.assertEqual(task.send_key_up.call_count, 2)
        task.click_box.assert_called_once_with(task.wait_book.return_value, after_sleep=1.5)

    def test_图鉴恢复时已打开不会重复切换快捷键(self):
        task = self.make_task()
        state = {'page': 'world'}
        task.find_one.side_effect = lambda feature: state['page'] == 'book'
        task.in_team_and_world.side_effect = lambda: state['page'] == 'world'

        def open_then_resume(*args, **kwargs):
            state['page'] = 'book'
            task.on_foreground_resume()

        task.send_key.side_effect = open_then_resume

        task.openF2Book()

        task.send_key.assert_called_once_with('f2', after_sleep=4)
        task.send_key_down.assert_not_called()
        task.click_box.assert_called_once()

    def test_导航取消和系统输入失败直接上抛(self):
        for method in ('open_esc_menu', 'openF2Book'):
            for error in (ForegroundInputError, TaskDisabledException):
                with self.subTest(method=method, error=error):
                    task = self.make_task()
                    task.find_one.return_value = task.wait_book.return_value
                    task.sleep.side_effect = error('导航已停止')

                    with self.assertRaises(error):
                        getattr(task, method)()

                    task.wait_foreground_frame.assert_not_called()
                    task.click_relative.assert_not_called()
                    task.click_box.assert_not_called()
                    if method == 'open_esc_menu':
                        task.send_key_up.assert_called_once_with('alt')


if __name__ == '__main__':
    unittest.main()
