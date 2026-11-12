"""桌面通知所有权、取消、窗口坐标和队列的离线核心验证。"""

import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import numpy as np
import win32con

from ok.device.interaction_methods.foreground_input import (
    ForegroundInput, ForegroundInputError, ForegroundInputInterrupted, ForegroundTarget,
)
from ok.notification.manager import NotificationManager
from ok.notification.messenger_images import _paste_from_context_menu, _wait_popup_text
from ok.notification.pipeline import NotificationPipeline
from ok.notification.windows_messenger import MessengerAutomation
from ok.task.exceptions import ExecutorBusyException, TaskDisabledException
from ok.util.GlobalConfig import (
    DISCORD_NOTIFICATION_ENABLED, DISCORD_WEBHOOK, QQ_NICKNAME, QQ_NOTIFICATION_ENABLED,
)


class NotificationNative:
    """只记录系统事件，不连接实际窗口或系统键鼠。"""

    def __init__(self):
        self.foreground = 100
        self.cursor = (1, 1)
        self.windows = {
            100: (1, 100, 100, (0, 0)),
            200: (2, 200, 200, (500, 100)),
            210: (2, 200, 200, (520, 130)),
            220: (2, 220, 200, (-50, -100)),
            230: (2, 230, 230, (700, 500)),
            300: (1, 300, 300, (0, 0)),
            900: (3, 900, 900, (1000, 500)),
        }
        self.points = {}
        self.log = []
        self.on_send = None
        self.partial_unicode = False
        self.reject_release = False
        self.last_error = 0
        self.now = 0.0

    def is_window(self, hwnd):
        return hwnd in self.windows

    def process_id(self, hwnd):
        return self.windows[hwnd][0]

    def root_window(self, hwnd, owned=False):
        return self.windows[hwnd][2 if owned else 1]

    def owner_window(self, hwnd):
        root, owner = self.windows[hwnd][1:3]
        return owner if owner != root else 0

    def is_child(self, hwnd, child):
        return child != hwnd and self.root_window(child) == hwnd

    def foreground_window(self):
        return self.foreground

    def cursor_pos(self):
        return self.cursor

    def window_at(self, point):
        return self.points.get(tuple(point), self.foreground)

    def set_cursor_pos(self, x, y):
        self.cursor = (x, y)
        self.log.append(('move', self.foreground, x, y))

    def screen_point(self, hwnd, point):
        left, top = self.windows[hwnd][3]
        screen = (left + point[0], top + point[1])
        self.points[screen] = hwnd
        return screen

    def switch(self, hwnd):
        self.foreground = hwnd
        self.log.append(('foreground', hwnd))

    @staticmethod
    def scan_code(vk):
        return vk

    @staticmethod
    def vk_key_scan(char):
        return ord(char.upper()) if char.isascii() else -1

    def send(self, events):
        inserted = len(events)
        if self.reject_release and all(event.up for event in events):
            inserted = 0
        elif self.partial_unicode and any(event.flags & 4 for event in events):
            self.partial_unicode = False
            inserted = 1
        self.log.extend(('input', self.foreground, event) for event in events[:inserted])
        if self.on_send is not None:
            self.on_send(events[:inserted])
        return inserted

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(.001, seconds)


class DeferredJob:
    def __init__(self, executor, fn, options):
        self.executor = executor
        self.fn = fn
        self.options = options
        self.future = Future()
        self.cancel_event = threading.Event()

    @property
    def cancel_requested(self):
        return self.cancel_event.is_set()

    def cancel(self):
        self.cancel_event.set()
        return self.future.cancel()


class DeferredExecutor:
    """测试明确推进作业，通知线程不能自行发送。"""

    def __init__(self):
        self.thread = None
        self.current_job = None
        self.jobs = []
        self.submitted = threading.Event()
        self.device_manager = SimpleNamespace(
            hwnd_window=SimpleNamespace(hwnd=100), interaction=object())
        self.ocr_lib = Mock()
        self.prepare_foreground = Mock(return_value=False)
        self.next_frame = Mock()

    def is_executor_thread(self):
        return threading.current_thread() is self.thread

    def check_input_cancelled(self, allow_pause=False):
        if self.current_job is not None and self.current_job.cancel_requested:
            raise TaskDisabledException()

    def submit(self, fn, **options):
        job = DeferredJob(self, fn, options)
        self.jobs.append(job)
        self.submitted.set()
        return job

    def run_next(self):
        job = self.jobs.pop(0)
        if not job.future.set_running_or_notify_cancel():
            return
        previous_thread = self.thread
        self.thread = threading.current_thread()
        self.current_job = job
        try:
            job.future.set_result(job.fn())
        except BaseException as error:
            job.future.set_exception(error)
        finally:
            self.current_job = None
            self.thread = previous_thread


class ForegroundNotificationCase(unittest.TestCase):
    def setUp(self):
        self.native = NotificationNative()
        self.core = ForegroundInput(
            self.native, clock=self.native.clock, sleep=self.native.sleep, start_monitor=False)
        self.addCleanup(self.core.stop)
        self.exit_event = threading.Event()
        self.gui = Mock()
        self.gui.IsWindow.side_effect = self.native.is_window
        self.gui.IsChild.side_effect = self.native.is_child
        self.gui.GetAncestor.side_effect = lambda hwnd, flag: self.native.root_window(
            hwnd, owned=flag == win32con.GA_ROOTOWNER)
        self.gui.GetForegroundWindow.side_effect = self.native.foreground_window
        self.gui.SetForegroundWindow.side_effect = self.native.switch
        self.gui.IsIconic.return_value = False
        self.gui.ClientToScreen.side_effect = self.native.screen_point
        self.gui.GetClientRect.return_value = (0, 0, 1000, 800)
        self.gui.SendMessage.side_effect = AssertionError('禁止消息式键鼠输入')
        self.gui.PostMessage.side_effect = AssertionError('禁止消息式键鼠输入')
        process = SimpleNamespace(
            GetWindowThreadProcessId=lambda hwnd: (1, self.native.process_id(hwnd)))
        replacements = {
            'ok.notification.windows_messenger.win32gui': self.gui,
            'ok.notification.manager.win32gui': self.gui,
            'ok.notification.messenger_images.win32gui': self.gui,
            'ok.notification.windows_messenger.win32process': process,
            'ok.notification.messenger_images.win32process': process,
            'ok.notification.windows_messenger.get_shared_foreground_input': lambda: self.core,
        }
        for name, replacement in replacements.items():
            context = patch(name, replacement)
            context.start()
            self.addCleanup(context.stop)

    def make_automation(self):
        automation = MessengerAutomation(
            ('TemporaryMessenger.exe',), None, exit_event=self.exit_event, switch_from_hwnd=100)
        automation._find_hwnd = Mock(return_value=200)
        automation._save_error_screenshot = Mock()
        automation._wait = lambda _: automation._check()
        return automation

    def make_manager(self):
        executor = DeferredExecutor()
        config = {QQ_NOTIFICATION_ENABLED: True, QQ_NICKNAME: '临时受控联系人'}
        manager = NotificationManager(
            SimpleNamespace(get_config=lambda _: config), executor,
            app_name='测试应用', system_notifier=None)
        manager.pipeline.interval = 0
        manager.desktop_pipeline.interval = 0
        self.addCleanup(manager.stop)
        return manager, executor

    @staticmethod
    def desktop_enqueue_event(manager, message):
        enqueued = threading.Event()
        original_submit = manager.desktop_pipeline.submit

        def submit(title, content, images):
            accepted = original_submit(title, content, images)
            if accepted and content == message:
                enqueued.set()
            return accepted

        manager.desktop_pipeline.submit = Mock(side_effect=submit)
        return enqueued


class TestMessengerForeground(ForegroundNotificationCase):
    def test_handoff_releases_game_before_focus_and_uses_actual_menu_origin(self):
        game = self.core.session('game', ForegroundTarget(100, process_id=1))
        game.key_down('a')
        automation = self.make_automation()

        def send_content(*_):
            automation._click(210, (2, 3))
            automation._hotkey(210, win32con.VK_CONTROL, ord('V'))
            automation._type_text(210, '你好🙂')
            automation._click(220, (4, 5))
            return True

        automation._send = Mock(side_effect=send_content)
        self.assertTrue(automation.send('受控联系人', '', '测试', []))
        self.assertTrue(self.native.log[1][2].up)
        self.assertEqual(self.native.log[2], ('foreground', 200))
        moves = [item[2:] for item in self.native.log if item[0] == 'move']
        self.assertEqual(moves, [(522, 133), (-46, -95)])
        self.assertTrue(automation.restore_allowed)
        self.assertEqual(self.core._held, {})
        self.gui.SendMessage.assert_not_called()
        self.gui.PostMessage.assert_not_called()

    def test_focus_loss_during_unicode_text_never_clicks_send_or_retries(self):
        automation = self.make_automation()

        def switch_during_text(events):
            if any(event.flags & 4 for event in events):
                self.native.on_send = None
                self.native.switch(900)

        self.native.on_send = switch_during_text
        automation._send = Mock(side_effect=lambda *_: automation._send_content(
            200, (900, 750), (820, 705), '', '第一段第二段', []))
        with self.assertRaises(ForegroundInputInterrupted):
            automation.send('受控联系人', '', '测试', [])
        automation._send.assert_called_once()
        moves = [item[2:] for item in self.native.log if item[0] == 'move']
        self.assertEqual(moves, [(1320, 805)])
        self.assertFalse(automation.restore_allowed)
        self.assertEqual(self.core._held, {})
        self.assertFalse(any(item[0] == 'input' and item[1] == 900 and not item[2].up
                             for item in self.native.log))

    def test_partial_text_injection_is_failure_and_cleanup_releases_prefix(self):
        automation = self.make_automation()
        self.native.partial_unicode = True

        def send_content(*_):
            automation._type_text(200, '中文')
            automation._click(200, (900, 750))

        automation._send = Mock(side_effect=send_content)
        with self.assertRaises(ForegroundInputError):
            automation.send('受控联系人', '', '测试', [])
        self.assertEqual(self.core._held, {})
        self.assertFalse(any(item[0] == 'move' for item in self.native.log))
        automation._send.assert_called_once()

    def test_cancel_during_wait_releases_modifier_and_prevents_restore(self):
        automation = self.make_automation()
        automation._wait_cancel = lambda _: self.exit_event.set()

        def send_content(*_):
            automation._input.key_down('ctrl')
            MessengerAutomation._wait(automation, 1)

        automation._send = Mock(side_effect=send_content)
        with self.assertRaises(ForegroundInputError):
            automation.send('受控联系人', '', '测试', [])
        self.assertEqual(self.core._held, {})
        self.assertTrue(self.native.log[-1][2].up)
        self.assertFalse(automation.restore_allowed)

    def test_observed_user_focus_loss_prevents_restore_even_after_return(self):
        for held in (False, True):
            with self.subTest(held=held):
                self.native.foreground = 100
                automation = self.make_automation()

                def send_content(*_):
                    if held:
                        automation._input.key_down('ctrl')
                    self.native.foreground = 900
                    self.core.poll_foreground()
                    self.native.foreground = 200
                    return True

                automation._send = Mock(side_effect=send_content)
                self.assertTrue(automation.send('受控联系人', '', '', []))
                self.assertEqual(self.core._held, {})
                self.assertFalse(automation.restore_allowed)

    def test_denied_foreground_request_does_not_emit_notification_input(self):
        automation = self.make_automation()
        self.gui.SetForegroundWindow.side_effect = None
        original_request = automation._request_foreground
        automation._request_foreground = lambda hwnd: original_request(hwnd, timeout=0)
        automation._send = Mock()
        with self.assertRaises(ForegroundInputError):
            automation.send('受控联系人', '', '测试', [])
        self.gui.SetForegroundWindow.assert_called_once_with(200)
        automation._send.assert_not_called()
        self.assertEqual(self.native.log, [])

    def test_user_switch_before_handoff_prevents_foreground_request(self):
        automation = self.make_automation()
        self.native.foreground = 900
        automation._send = Mock()
        with self.assertRaises(ForegroundInputInterrupted):
            automation.send('受控联系人', '', '测试', [])
        self.gui.SetForegroundWindow.assert_not_called()
        automation._send.assert_not_called()

    def test_reused_hwnd_process_is_rejected_before_requesting_foreground(self):
        automation = self.make_automation()
        automation._send = Mock()
        original_session = self.core.session

        def changed_process(owner, target, cancel_check=None):
            self.native.windows[200] = (4, 200, 200, (500, 100))
            return original_session(owner, target, cancel_check)

        with patch.object(self.core, 'session', side_effect=changed_process):
            with self.assertRaises(ForegroundInputInterrupted):
                automation.send('受控联系人', '', '测试', [])
        self.gui.SetForegroundWindow.assert_not_called()
        automation._send.assert_not_called()

    def test_unrelated_window_in_same_process_is_not_an_input_target(self):
        automation = self.make_automation()
        automation._send = Mock(side_effect=lambda *_: automation._click(230, (5, 5)))
        with self.assertRaises(ForegroundInputInterrupted):
            automation.send('受控联系人', '', '测试', [])
        self.assertFalse(any(item[0] in ('move', 'input') for item in self.native.log))

    def test_image_menu_uses_owned_popup_coordinates_and_shared_right_click(self):
        automation = self.make_automation()
        automation._set_clipboard_image = Mock()
        automation._send = Mock(side_effect=lambda *_: _paste_from_context_menu(
            automation, 200, (820, 705), object()))
        with patch('ok.notification.messenger_images._wait_popup_text', return_value=(220, (3, 7))):
            self.assertTrue(automation.send('受控联系人', '', '', []))
        buttons = [item[2].code for item in self.native.log
                   if item[0] == 'input' and item[2].kind == 'mouse' and not item[2].up]
        self.assertEqual(buttons, [1, 2, 1])
        self.assertEqual([item[2:] for item in self.native.log if item[0] == 'move'][-1], (-47, -93))
        self.gui.SendMessage.assert_not_called()
        self.gui.PostMessage.assert_not_called()

    def test_popup_ocr_rejects_unrelated_same_process_candidate(self):
        automation = self.make_automation()
        automation._capture = Mock(side_effect=lambda hwnd: np.full((10, 10, 3), hwnd, dtype=np.uint16))
        automation._ocr = Mock(side_effect=lambda frame: [] if frame[0, 0, 0] == 210
                               else [('粘贴', 1, 2, 4, 6)])
        automation._send = Mock(side_effect=lambda *_: _wait_popup_text(
            automation, 200, {'粘贴'}, timeout=1))
        with patch('ok.notification.messenger_images._tool_popups', return_value=[230, 220]), \
                patch('ok.notification.messenger_images._render_surface', return_value=210):
            self.assertEqual(automation.send('受控联系人', '', '', []), (220, (3, 5)))
        self.assertNotIn(call(230), automation._capture.call_args_list)

    def test_image_failure_restores_clipboard_and_releases_held_modifier(self):
        automation = self.make_automation()
        automation._get_clipboard_text = Mock(return_value='原剪贴板')
        automation._restore_clipboard_text = Mock()
        automation._send = Mock(side_effect=lambda *_: automation._send_content(
            200, (900, 750), (820, 705), '', '', [object()]))

        def fail_image(*_):
            automation._input.key_down('ctrl')
            raise RuntimeError('受控图片插入失败')

        with patch('ok.notification.messenger_images.insert_messenger_image', side_effect=fail_image):
            with self.assertRaisesRegex(RuntimeError, '受控图片插入失败'):
                automation.send('受控联系人', '', '', [])
        automation._restore_clipboard_text.assert_called_once_with('原剪贴板')
        self.assertEqual(self.core._held, {})


class TestNotificationQueue(unittest.TestCase):
    def test_fifo_provider_failure_is_not_retried(self):
        processed = []
        completed = threading.Event()

        def handler(title, message, images):
            processed.append(message)
            if message == '第一条':
                raise RuntimeError('受控发送失败')
            completed.set()

        pipeline = NotificationPipeline(handler, interval=0)
        self.addCleanup(lambda: pipeline.stop(wait=True))
        pipeline.submit('', '第一条', [])
        pipeline.submit('', '第二条', [])
        self.assertTrue(completed.wait(1))
        self.assertTrue(pipeline.stop(wait=True))
        self.assertEqual(processed, ['第一条', '第二条'])
        self.assertEqual(pipeline.queue.unfinished_tasks, 0)

    def test_plain_exit_event_stops_interval_wait_and_discards_pending_requests(self):
        exit_event = threading.Event()
        started = threading.Event()
        processed = []

        def handler(title, message, images):
            processed.append(message)
            started.set()

        pipeline = NotificationPipeline(handler, exit_event=exit_event, interval=60)
        self.addCleanup(lambda: pipeline.stop(wait=True))
        pipeline.submit('', '第一条', [])
        pipeline.submit('', '第二条', [])
        self.assertTrue(started.wait(1))
        exit_event.set()
        pipeline.thread.join(timeout=1)
        self.assertFalse(pipeline.thread.is_alive())
        self.assertEqual(processed, ['第一条'])
        self.assertEqual(pipeline.queue.unfinished_tasks, 0)
        self.assertFalse(pipeline.submit('', '退出后的请求', []))


class TestDesktopExecutorLifecycle(ForegroundNotificationCase):
    def test_desktop_consumer_waits_for_deferred_job_without_sending_on_network_worker(self):
        manager, executor = self.make_manager()
        manager._send_desktop = Mock(return_value=True)
        manager.submit('', '受控消息')
        self.assertTrue(executor.submitted.wait(1))
        manager._send_desktop.assert_not_called()
        self.assertTrue(executor.jobs[0].options['defer_while_unfocused'])
        executor.run_next()
        manager._send_desktop.assert_called_once()

    def test_stop_cancels_queued_job_and_leaves_no_late_desktop_send(self):
        manager, executor = self.make_manager()
        manager._send_desktop = Mock(return_value=True)
        manager.submit('', '受控消息')
        self.assertTrue(executor.submitted.wait(1))
        job = executor.jobs[0]
        self.assertTrue(manager.stop())
        self.assertTrue(job.future.cancelled())
        executor.run_next()
        manager._send_desktop.assert_not_called()
        self.assertEqual(manager._desktop_jobs, set())

    def test_executor_thread_never_waits_for_its_own_submitted_future(self):
        manager, executor = self.make_manager()
        executor.thread = threading.current_thread()
        job = manager._submit_desktop('受控消息', [])
        self.assertIs(job, executor.jobs[0])
        self.assertFalse(executor.jobs[0].future.done())
        manager.stop()
        for pipeline in (manager.pipeline, manager.desktop_pipeline):
            pipeline.thread.join(timeout=1)
            self.assertFalse(pipeline.thread.is_alive())

    def test_desktop_consumer_rejects_executor_thread_before_submitting_a_job(self):
        manager, executor = self.make_manager()
        executor.thread = threading.current_thread()
        with patch('ok.notification.manager.logger'):
            self.assertFalse(manager._deliver_desktop('', '受控消息', []))
        self.assertEqual(executor.jobs, [])
        executor.thread = None
        self.assertTrue(manager.stop())

    def test_completed_job_timeout_is_failure_instead_of_endless_future_wait(self):
        manager, executor = self.make_manager()
        job = executor.submit(lambda: None)
        job.future.set_exception(TimeoutError('受控作业超时'))
        job.future.result = Mock(side_effect=[
            TimeoutError('受控作业超时'), TimeoutError('受控作业超时'),
            AssertionError('已完成的失败作业仍被反复等待'),
        ])
        executor.submit = Mock(return_value=job)
        with patch('ok.notification.manager.logger') as logger:
            self.assertFalse(manager._deliver_desktop('', '受控消息', []))
            logger.error.assert_called_once()
        self.assertEqual(job.future.result.call_count, 2)
        self.assertEqual(manager._desktop_jobs, set())

    def test_exclusive_executor_delays_desktop_until_submission_is_available(self):
        manager, executor = self.make_manager()
        manager._send_desktop = Mock(return_value=True)
        exclusive = threading.Event()
        exclusive.set()
        attempted = threading.Event()
        original_submit = executor.submit

        def submit(fn, **options):
            if exclusive.is_set():
                attempted.set()
                raise ExecutorBusyException('受控独占')
            return original_submit(fn, **options)

        executor.submit = Mock(side_effect=submit)
        manager.submit('', '受控消息')
        self.assertTrue(attempted.wait(1))
        manager._send_desktop.assert_not_called()
        self.assertEqual(executor.jobs, [])
        exclusive.clear()
        self.assertTrue(executor.submitted.wait(1))
        executor.run_next()
        manager._send_desktop.assert_called_once()

    def test_stop_interrupts_exclusive_wait_without_creating_a_late_job(self):
        manager, executor = self.make_manager()
        manager._send_desktop = Mock(return_value=True)
        attempted = threading.Event()

        def submit(fn, **options):
            attempted.set()
            raise ExecutorBusyException('受控独占')

        executor.submit = Mock(side_effect=submit)
        manager.submit('', '受控消息')
        self.assertTrue(attempted.wait(1))
        self.assertTrue(manager.stop())
        self.assertEqual(executor.jobs, [])
        manager._send_desktop.assert_not_called()

    def test_pending_desktop_and_unfocused_game_do_not_block_network_or_tray(self):
        manager, executor = self.make_manager()
        self.native.foreground = 900
        manager.config[DISCORD_NOTIFICATION_ENABLED] = True
        manager.config[DISCORD_WEBHOOK] = 'https://controlled.invalid/webhook'
        manager.system_notifier = Mock(show=Mock(return_value=True))
        manager._send_desktop = Mock(return_value=True)
        network_messages = []
        second_network = threading.Event()

        def send(webhook, title, message, images, *args):
            network_messages.append(message)
            if message == '第二条':
                second_network.set()
            return True

        with patch('ok.notification.manager.DiscordProvider', return_value=SimpleNamespace(send=send)), \
                patch('ok.notification.manager.logger') as logger:
            self.assertTrue(manager.submit('', '第一条'))
            self.assertTrue(executor.submitted.wait(1))
            first_job = executor.jobs[0]
            self.assertFalse(first_job.future.done())
            self.assertTrue(first_job.options['defer_while_unfocused'])
            self.assertTrue(manager.submit('', '第二条'))
            self.assertTrue(second_network.wait(1))
            self.assertEqual(network_messages, ['第一条', '第二条'])
            self.assertFalse(first_job.future.done())
            manager._send_desktop.assert_not_called()
            logger.debug.assert_not_called()
            self.assertTrue(manager.notify_system('托盘', '受控系统通知'))
            manager.system_notifier.show.assert_called_once_with('托盘', '受控系统通知', False)
            self.assertTrue(manager.stop())
            self.assertTrue(first_job.future.cancelled())

    def test_exclusive_executor_wait_does_not_block_later_network_notifications(self):
        manager, executor = self.make_manager()
        manager.config[DISCORD_NOTIFICATION_ENABLED] = True
        manager.config[DISCORD_WEBHOOK] = 'https://controlled.invalid/webhook'
        manager._send_desktop = Mock(return_value=True)
        attempted = threading.Event()
        second_network = threading.Event()
        network_messages = []

        def submit(fn, **options):
            attempted.set()
            raise ExecutorBusyException('受控 MCP 独占')

        def send(webhook, title, message, images, *args):
            network_messages.append(message)
            if message == '第二条':
                second_network.set()
            return True

        executor.submit = Mock(side_effect=submit)
        with patch('ok.notification.manager.DiscordProvider', return_value=SimpleNamespace(send=send)):
            self.assertTrue(manager.submit('', '第一条'))
            self.assertTrue(attempted.wait(1))
            self.assertTrue(manager.submit('', '第二条'))
            self.assertTrue(second_network.wait(1))
            self.assertEqual(network_messages, ['第一条', '第二条'])
            self.assertEqual(executor.jobs, [])
            manager._send_desktop.assert_not_called()
            self.assertTrue(manager.stop())

    def test_desktop_messages_use_one_consumer_and_execute_in_fifo_order(self):
        manager, executor = self.make_manager()
        second_enqueued = self.desktop_enqueue_event(manager, '第二条')
        consumer_threads = []
        delivered = []
        original_submit = executor.submit

        def submit(fn, **options):
            consumer_threads.append(threading.current_thread())
            return original_submit(fn, **options)

        executor.submit = Mock(side_effect=submit)
        manager._send_desktop = Mock(side_effect=lambda message, images: delivered.append(message) or True)
        manager.submit('', '第一条')
        self.assertTrue(executor.submitted.wait(1))
        first = executor.jobs[0]
        manager.submit('', '第二条')
        self.assertTrue(second_enqueued.wait(1))
        self.assertEqual(executor.jobs, [first])
        self.assertEqual(delivered, [])
        executor.submitted.clear()
        executor.run_next()
        self.assertTrue(executor.submitted.wait(1))
        executor.run_next()
        self.assertEqual(delivered, ['测试应用:\n第一条', '测试应用:\n第二条'])
        self.assertEqual(consumer_threads, [manager.desktop_pipeline.thread] * 2)
        self.assertTrue(manager.stop())

    def test_exit_cancels_active_future_and_discards_unstarted_desktop_messages(self):
        manager, executor = self.make_manager()
        manager._send_desktop = Mock(return_value=True)
        second_enqueued = self.desktop_enqueue_event(manager, '第二条')
        manager.submit('', '第一条')
        self.assertTrue(executor.submitted.wait(1))
        first = executor.jobs[0]
        manager.submit('', '第二条')
        self.assertTrue(second_enqueued.wait(1))
        self.assertTrue(manager.stop())
        self.assertTrue(first.future.cancelled())
        self.assertEqual(executor.jobs, [first])
        manager._send_desktop.assert_not_called()
        for pipeline in (manager.pipeline, manager.desktop_pipeline):
            self.assertFalse(pipeline.thread.is_alive())
            self.assertEqual(pipeline.queue.unfinished_tasks, 0)
        self.assertFalse(manager.submit('', '退出后的请求'))

    def test_final_failure_is_reported_once_and_next_desktop_message_still_runs(self):
        manager, executor = self.make_manager()
        second_enqueued = self.desktop_enqueue_event(manager, '第二条')
        error = RuntimeError('受控实际发送失败')
        manager._send_desktop = Mock(side_effect=[error, True])
        with patch('ok.notification.manager.logger') as logger:
            manager.submit('', '第一条')
            self.assertTrue(executor.submitted.wait(1))
            manager.submit('', '第二条')
            self.assertTrue(second_enqueued.wait(1))
            logger.error.assert_not_called()
            executor.submitted.clear()
            executor.run_next()
            self.assertTrue(executor.submitted.wait(1))
            executor.run_next()
            logger.error.assert_called_once_with('桌面通知执行失败，本条不自动重发', error)
            self.assertEqual(manager._send_desktop.call_args_list, [
                call('测试应用:\n第一条', []), call('测试应用:\n第二条', []),
            ])
            self.assertTrue(manager.stop())

    def test_accepted_request_is_not_reported_as_sent_before_false_final_result(self):
        manager, executor = self.make_manager()
        manager._send_desktop = Mock(return_value=False)
        with patch('ok.notification.manager.logger') as logger:
            self.assertTrue(manager.submit('', '受控消息'))
            self.assertTrue(executor.submitted.wait(1))
            self.assertFalse(executor.jobs[0].future.done())
            logger.warning.assert_not_called()
            logger.debug.assert_not_called()
            executor.run_next()
            logger.warning.assert_called_once_with('桌面通知发送未完成，本条不自动重发')
            logger.debug.assert_not_called()
            manager._send_desktop.assert_called_once_with('测试应用:\n受控消息', [])
            self.assertTrue(manager.stop())

    def test_executor_thread_never_waits_for_its_exclusive_owner_to_release(self):
        manager, executor = self.make_manager()
        executor.thread = threading.current_thread()
        executor.submit = Mock(side_effect=ExecutorBusyException('受控独占'))
        with self.assertRaises(ExecutorBusyException):
            manager._submit_desktop('受控消息', [])
        executor.thread = None
        self.assertTrue(manager.stop())

    def test_cancel_check_on_observer_thread_detects_running_job_cancel(self):
        manager, executor = self.make_manager()
        job = DeferredJob(executor, lambda: None, {})
        executor.current_job = job
        job.cancel_event.set()
        errors = []

        def observe():
            try:
                manager._desktop_cancelled()
            except TaskDisabledException:
                errors.append('已取消')

        observer = threading.Thread(target=observe)
        observer.start()
        observer.join(timeout=1)
        self.assertEqual(errors, ['已取消'])
        executor.current_job = None

    def run_desktop_lifecycle(self, mode):
        manager, executor = self.make_manager()
        executor.thread = threading.current_thread()
        attempts = []

        def prepare(**options):
            self.assertEqual(self.core._held, {})
            self.assertFalse(options['cancel_check']())
            self.native.switch(100)
            self.assertFalse(options['cancel_check']())
            return True

        executor.prepare_foreground.side_effect = prepare

        def send(automation, hwnd, nickname, title, message, images):
            attempts.append(mode)
            automation._input.key_down('ctrl')
            if mode == '失焦':
                self.native.switch(900)
                automation._check()
            if mode == '换设备':
                executor.device_manager.hwnd_window = SimpleNamespace(hwnd=300)
            if mode == '失败':
                raise RuntimeError('受控发送状态不明')
            if mode == '释放失败':
                self.native.reject_release = True
            return True

        with patch.object(MessengerAutomation, '_find_hwnd', return_value=200), \
                patch.object(MessengerAutomation, '_send', new=send), \
                patch.object(MessengerAutomation, '_save_error_screenshot'):
            result = manager._send_desktop('受控消息', [])
        self.assertEqual(attempts, [mode])
        if mode == '释放失败':
            self.assertTrue(self.core._held)
            self.native.reject_release = False
            self.core.release_all()
        self.assertEqual(self.core._held, {})
        return result, executor

    def test_success_restores_game_after_release_then_requests_fresh_frame(self):
        result, executor = self.run_desktop_lifecycle('成功')
        self.assertTrue(result)
        executor.prepare_foreground.assert_called_once()
        executor.next_frame.assert_called_once_with(time_out=5)
        self.assertEqual(self.native.foreground, 100)

    def test_uncertain_send_failure_is_not_retried_and_still_cleans_up_and_restores(self):
        result, executor = self.run_desktop_lifecycle('失败')
        self.assertFalse(result)
        executor.prepare_foreground.assert_called_once()
        executor.next_frame.assert_called_once_with(time_out=5)

    def test_user_focus_loss_during_send_prevents_game_restore(self):
        result, executor = self.run_desktop_lifecycle('失焦')
        self.assertFalse(result)
        executor.prepare_foreground.assert_not_called()
        executor.next_frame.assert_not_called()
        self.assertEqual(self.native.foreground, 900)

    def test_release_failure_prevents_game_focus_restore_until_cleanup_succeeds(self):
        result, executor = self.run_desktop_lifecycle('释放失败')
        self.assertFalse(result)
        executor.prepare_foreground.assert_not_called()
        executor.next_frame.assert_not_called()
        self.assertEqual(self.native.foreground, 200)

    def test_device_replacement_during_send_prevents_restore_of_old_game(self):
        result, executor = self.run_desktop_lifecycle('换设备')
        self.assertTrue(result)
        executor.prepare_foreground.assert_not_called()
        executor.next_frame.assert_not_called()
        self.assertEqual(self.native.foreground, 200)

    def test_user_switch_between_job_selection_and_execution_never_takes_foreground(self):
        manager, executor = self.make_manager()
        executor.thread = threading.current_thread()
        self.native.foreground = 900
        with patch.object(MessengerAutomation, 'send') as send:
            self.assertFalse(manager._send_desktop('受控消息', []))
        send.assert_not_called()
        self.gui.SetForegroundWindow.assert_not_called()


if __name__ == '__main__':
    unittest.main()
