"""后台消息的请求额度、目标身份和持有清理；所有原生调用均使用替身。"""

import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace

from pynput.keyboard import Key

from ok.core.start_request import StartRequest
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from ok.device.interaction_methods.post_message_test import (
    PostMessageTestInteraction, WindowIdentity, WM_ACTIVATE, WM_CHAR, WM_KEYDOWN, WM_KEYUP,
    WM_MOUSEMOVE, WM_MOUSEWHEEL,
)
from ok.task.exceptions import TaskDisabledException


class FakeClock:
    def __init__(self):
        self.value = 1000.0
        self.after_wait = None

    def __call__(self):
        return self.value

    def wait(self, seconds):
        self.value += seconds
        if self.after_wait:
            self.after_wait()


class FakeMessages:
    def __init__(self):
        self.identities = {10: WindowIdentity(10, 1, 100, 101, 'Game')}
        self.bounds = {10: (-300, 50, 700, 650)}
        self.hidden, self.minimized, self.disabled = set(), set(), set()
        self.parents, self.owners = {}, {}
        self.attempts, self.messages, self.layouts = [], [], []
        self.last_error = 5
        self.failure = None
        self.after_post = None

    def identity(self, hwnd):
        return self.identities.get(hwnd)

    def post_message(self, hwnd, message, wparam, lparam):
        event = hwnd, message, wparam, lparam
        self.attempts.append(event)
        if self.failure and self.failure(event):
            return False
        self.messages.append(event)
        if self.after_post:
            self.after_post(event)
        return True

    def is_visible(self, hwnd):
        return hwnd not in self.hidden

    def is_minimized(self, hwnd):
        return hwnd in self.minimized

    def is_enabled(self, hwnd):
        return hwnd not in self.disabled

    def parent_window(self, hwnd):
        return self.parents.get(hwnd, 0)

    def owner_window(self, hwnd):
        return self.owners.get(hwnd, 0)

    def client_bounds(self, hwnd):
        return self.bounds[hwnd]

    def client_to_screen(self, hwnd, point):
        return self.bounds[hwnd][0] + point[0], self.bounds[hwnd][1] + point[1]

    def screen_to_client(self, hwnd, point):
        return point[0] - self.bounds[hwnd][0], point[1] - self.bounds[hwnd][1]

    def scan_code(self, vk, thread_id):
        self.layouts.append(thread_id)
        return {0x1B: 1, 0x57: 0x11, 0xA0: 0x2A, 0xA1: 0x36,
                0xA2: 0x1D, 0xA3: 0xE01D, 0xA4: 0x38, 0xA5: 0xE038}.get(vk, vk)

    def vk_key_scan(self, char, thread_id):
        self.layouts.append(thread_id)
        if char.isascii() and char.isalpha():
            return ord(char.upper()) | (0x100 if char.isupper() else 0)
        return -1


class FakeWindow:
    def __init__(self, native):
        self.native = native
        self.hwnd = self.top_hwnd = 10
        self.order = [10]
        self.top_hwnd_class = ['Login']
        self.capture_origin = (-280, 70)
        self.app_exit_event = threading.Event()
        self.stop_event = threading.Event()
        self.device_manager = None

    @property
    def hwnds(self):
        return [(hwnd, '游戏.exe', right - left, bottom - top, left, top, '',
                 self.native.identities[hwnd].class_name, 1)
                for hwnd in self.order if hwnd in self.native.identities
                for left, top, right, bottom in (self.native.bounds[hwnd],)]

    @property
    def capture_target_signature(self):
        return self.hwnd, self.top_hwnd, self.capture_origin, tuple(self.hwnds)

    def do_update_window_size(self):
        pass


def unpack_point(value):
    return tuple(part if part < 0x8000 else part - 0x10000 for part in (value & 0xFFFF, value >> 16 & 0xFFFF))


class TestBackgroundInput(unittest.TestCase):
    def setUp(self):
        self.native = FakeMessages()
        self.window = FakeWindow(self.native)
        self.capture = SimpleNamespace(get_abs_cords=lambda x, y: (
            self.window.capture_origin[0] + x, self.window.capture_origin[1] + y))
        self.clock = FakeClock()
        self.backend = self.new_backend()

    def tearDown(self):
        self.native.failure = None
        self.backend.release_all()

    def new_backend(self):
        return PostMessageTestInteraction(self.capture, self.window, native=self.native,
                                          clock=self.clock, wait=self.clock.wait)

    def prepare(self, source='鼠标开始'):
        request = StartRequest(source)
        self.backend.prepare_start(request)
        return request

    def activations(self):
        return [event for event in self.native.attempts if event[1] == WM_ACTIVATE]

    def add_window(self, hwnd=11, *, parent=10, process_id=1, class_name='Button'):
        self.native.identities[hwnd] = WindowIdentity(hwnd, process_id, 100, 111, class_name)
        self.native.bounds[hwnd] = (-250, 80, -50, 280)
        self.native.parents[hwnd] = parent
        self.window.order.insert(0, hwnd)

    def test_begin_pause_resume_and_all_inputs_have_exact_activation_counts(self):
        request = self.prepare()
        self.backend.prepare_start(request)
        self.backend.send_key('w')
        self.backend.click(50, 50)
        self.backend.right_click(50, 50)
        self.backend.send_key('mouse4')
        self.backend.scroll(50, 50, -1)
        self.backend.input_text('示例😀')
        self.backend.swipe(10, 10, 80, 30, 15)
        self.backend.on_run()
        self.backend.refresh_target(force=True)
        self.backend.send_key_down('w')
        self.backend.release_all()
        self.backend.on_destroy()
        self.assertEqual(self.activations(), [(10, WM_ACTIVATE, 1, 0)])
        self.assertFalse(self.backend._held)
        self.prepare('F9恢复')
        self.assertEqual(self.activations(), [(10, WM_ACTIVATE, 1, 0)] * 2)
        self.assertFalse(any(event[1] == WM_ACTIVATE and event[2] == 0 for event in self.native.attempts))

    def test_preview_without_start_cannot_inject_and_control_messages_are_blocked(self):
        self.assertTrue(self.backend.should_capture())
        self.assertTrue(self.backend.refresh_target(force=True))
        with self.assertRaises(ForegroundInputError):
            self.backend.send_key('esc')
        self.assertFalse(self.native.attempts)
        self.prepare()
        before = len(self.native.attempts)
        for message in (WM_ACTIVATE, 0x0007, 0x0008, 0x001C, 0x0086, 0x0021):
            with self.assertRaises(ForegroundInputError):
                self.backend._post(self.native.identity(10), message, 1)
        self.assertEqual(len(self.native.attempts), before)

    def test_activation_failure_consumes_request_even_after_backend_recreation(self):
        request = StartRequest('鼠标开始')
        self.native.failure = lambda event: event[1] == WM_ACTIVATE
        with self.assertRaisesRegex(ForegroundInputError, '系统错误码：5'):
            self.backend.prepare_start(request)
        self.assertTrue(request.activation_attempted)
        self.assertFalse(request.activation_accepted)
        self.native.failure = None
        for backend in (self.backend, self.new_backend()):
            with self.assertRaises(ForegroundInputError):
                backend.prepare_start(request)
        self.assertEqual(len(self.activations()), 1)
        with self.assertRaises(ForegroundInputError):
            self.backend.send_key('esc')
        self.prepare('重新开始')
        self.assertEqual(len(self.activations()), 2)

    def test_cancelled_or_missing_target_never_activates(self):
        request = StartRequest('已取消开始')
        request.cancel()
        with self.assertRaises(TaskDisabledException):
            self.backend.prepare_start(request)
        with self.assertRaises(TaskDisabledException):
            self.backend.prepare_start(StartRequest(), cancel_check=lambda: True)
        self.window.hwnd = 0
        with self.assertRaisesRegex(ForegroundInputError, '请先手动打开游戏'):
            self.backend.prepare_start(StartRequest())
        self.assertFalse(self.native.attempts)

    def test_single_activation_always_targets_main_even_when_login_is_selected(self):
        self.add_window(parent=0, class_name='Login')
        self.window.top_hwnd = 11
        self.native.disabled.add(10)
        request = self.prepare()
        self.backend.send_key('esc')
        self.assertEqual(self.activations(), [(10, WM_ACTIVATE, 1, 0)])
        self.assertEqual([event[0] for event in self.native.messages if event[1] == WM_KEYDOWN], [11])
        new_backend = self.new_backend()
        new_backend.prepare_start(request)
        self.assertEqual(len(self.activations()), 1)

    def test_minimize_releases_and_restore_refreshes_without_activation(self):
        self.prepare()
        self.backend.send_key_down('w')
        self.native.minimized.add(10)
        self.assertFalse(self.backend.should_capture())
        self.assertEqual(self.native.messages[-1][1:3], (WM_KEYUP, 0x57))
        with self.assertRaises(ForegroundInputInterrupted):
            self.backend.click(50, 50)
        self.native.minimized.clear()
        with self.assertRaises(ForegroundInputInterrupted):
            self.backend.send_key('esc')
        self.assertTrue(self.backend.refresh_target(force=True))
        self.backend.send_key('esc')
        self.assertEqual(len(self.activations()), 1)
        self.assertFalse(self.backend._held)

    def test_process_or_main_identity_change_invalidates_old_request_and_old_up(self):
        for attribute, value in (('process_id', 2), ('created_at', 200), ('thread_id', 102), ('class_name', 'Other')):
            with self.subTest(attribute=attribute):
                self.setUp()
                request = self.prepare()
                self.backend.send_key_down('w')
                before = len(self.native.attempts)
                self.native.identities[10] = replace(self.native.identities[10], **{attribute: value})
                with self.assertRaises(ForegroundInputError):
                    self.backend.send_key('esc')
                self.assertEqual(len(self.native.attempts), before)
                self.assertFalse(self.backend._held)
                with self.assertRaises(TaskDisabledException):
                    self.new_backend().prepare_start(request)
                self.prepare('新窗口显式开始')
                self.assertEqual(len(self.activations()), 2)

    def test_main_hwnd_replacement_releases_original_target_but_never_inputs_new_target(self):
        self.prepare()
        self.backend.mouse_down(10, 10)
        self.add_window(20, parent=0, process_id=2, class_name='Game')
        self.window.hwnd = self.window.top_hwnd = 20
        with self.assertRaises(ForegroundInputError):
            self.backend.should_capture()
        self.assertEqual(self.native.messages[-1][0:2], (10, 0x0202))
        self.assertFalse(any(event[0] == 20 for event in self.native.attempts))

    def test_foreign_same_process_and_common_owner_cannot_receive_pointer(self):
        self.add_window(parent=0)
        self.native.owners.update({10: 99, 11: 99})
        self.native.identities[99] = WindowIdentity(99, 9, 900, 999, 'Launcher')
        self.prepare()
        self.backend.click(50, 50)
        self.assertFalse(any(event[0] == 11 for event in self.native.attempts))
        self.native.owners[11] = 10
        self.backend.refresh_target(force=True)
        self.backend.click(50, 50)
        self.assertEqual(self.native.messages[-1][0], 11)

    def test_keyboard_modifiers_are_shared_and_extended_bits_are_correct(self):
        self.prepare()
        self.backend.send_key_down('shift')
        self.backend.send_key('W')
        self.assertEqual([(event[1], event[2]) for event in self.native.messages[1:]],
                         [(WM_KEYDOWN, 0x10), (WM_KEYDOWN, 0x57), (WM_KEYUP, 0x57)])
        self.backend.send_key_up('shift')
        self.backend.send_key(Key.ctrl_r)
        down, up = self.native.messages[-2:]
        self.assertEqual(down[2:], (0x11, 0x011D0001))
        self.assertEqual(up[2:], (0x11, 0xC11D0001))
        self.backend.send_key('esc')
        self.assertEqual(self.native.messages[-2][2:], (0x1B, 0x00010001))
        self.assertEqual(self.native.messages[-1][2:], (0x1B, 0xC0010001))
        self.assertEqual(set(self.native.layouts), {101})
        self.assertFalse(self.backend._held)

    def test_release_uses_original_mapping_without_current_layout_or_pause_checks(self):
        self.prepare()
        self.backend.send_key_down('W')
        self.native.vk_key_scan = lambda *args: self.fail('松键不得重新读取键盘布局')
        self.native.scan_code = lambda *args: self.fail('松键不得重新读取扫描码')
        self.window.stop_event.set()
        self.backend.send_key_up('W')
        self.assertEqual([(event[1], event[2]) for event in self.native.messages[-2:]],
                         [(WM_KEYUP, 0x57), (WM_KEYUP, 0x10)])
        self.assertFalse(self.backend._held)

    def test_child_coordinates_wheel_and_side_button_use_correct_spaces(self):
        self.add_window()
        self.prepare()
        self.backend.click(50, 50)
        self.assertEqual(self.native.messages[-1][0], 11)
        self.assertEqual(unpack_point(self.native.messages[-1][3]), (20, 40))
        self.backend.scroll(50, 50, -1)
        wheel = self.native.messages[-1]
        self.assertEqual(wheel[0:2], (11, WM_MOUSEWHEEL))
        self.assertEqual(unpack_point(wheel[3]), (-230, 120))
        self.assertEqual(wheel[2] >> 16, 0xFF88)
        self.backend.send_key('mouse5')
        self.assertEqual(self.native.messages[-2][1:3], (0x020B, 0x00020040))
        self.assertEqual(self.native.messages[-1][1:3], (0x020C, 0x00020000))

    def test_geometry_change_releases_old_gesture_then_requires_new_frame(self):
        self.add_window()
        self.prepare()
        self.backend.mouse_down(50, 50)
        self.native.bounds[11] = (-240, 80, -40, 280)
        with self.assertRaises(ForegroundInputInterrupted):
            self.backend.move(60, 50)
        self.assertEqual(self.native.messages[-1][0:2], (11, 0x0202))
        self.assertEqual(unpack_point(self.native.messages[-1][3]), (20, 40))
        self.backend.refresh_target()
        self.backend.click(50, 50)
        self.assertEqual(unpack_point(self.native.messages[-1][3]), (10, 40))
        self.assertEqual(len(self.activations()), 1)

    def test_child_handle_reuse_requires_new_frame_and_drops_original_release(self):
        self.add_window()
        self.prepare()
        self.backend.mouse_down(50, 50)
        self.native.identities[11] = replace(self.native.identities[11], thread_id=211)
        before = len(self.native.attempts)
        with self.assertRaises(ForegroundInputInterrupted):
            self.backend.click(50, 50)
        self.assertEqual(len(self.native.attempts), before)
        self.assertFalse(self.backend._held)
        self.backend.refresh_target(force=True)
        self.backend.click(50, 50)
        self.assertEqual(self.native.messages[-1][0], 11)
        self.assertEqual(len(self.activations()), 1)

    def test_text_sends_utf16_units_and_failure_does_not_replay(self):
        self.prepare()
        self.backend.input_text('中😀')
        self.assertEqual([event[2] for event in self.native.messages if event[1] == WM_CHAR],
                         [0x4E2D, 0xD83D, 0xDE00])
        self.native.failure = lambda event: event[1] == WM_CHAR and event[2] == ord('密')
        with self.assertRaises(ForegroundInputError) as raised:
            self.backend.input_text('账密码')
        self.assertNotIn('账密码', str(raised.exception))
        self.assertEqual([event[2] for event in self.native.attempts if event[1] == WM_CHAR][-2:], [ord('账'), ord('密')])
        self.assertEqual(len(self.activations()), 1)

    def test_short_and_zero_swipes_reach_endpoint_in_milliseconds(self):
        self.prepare()
        for duration in (0, 5, 25):
            with self.subTest(duration=duration):
                start = self.clock()
                self.backend.swipe(10, 10, 80, 40, duration, settle_time=0.02)
                moves = [event for event in self.native.messages if event[1] == WM_MOUSEMOVE]
                self.assertEqual(unpack_point(moves[-1][3]), (100, 60))
                self.assertEqual(unpack_point(self.native.messages[-1][3]), (100, 60))
                self.assertAlmostEqual(self.clock() - start, duration / 1000 + 0.02)
                self.assertFalse(self.backend._held)

    def test_cancel_during_keyboard_and_drag_releases_even_while_hidden(self):
        self.prepare()

        def cancel():
            self.native.hidden.add(10)
            self.window.stop_event.set()

        for operation in (lambda: self.backend.send_key('W', 1),
                          lambda: self.backend.swipe(10, 10, 80, 40, 500)):
            self.window.stop_event.clear()
            self.native.hidden.clear()
            self.backend.refresh_target(force=True)
            self.clock.after_wait = cancel
            with self.assertRaises(TaskDisabledException):
                operation()
            self.assertFalse(self.backend._held)
            self.assertIn(self.native.messages[-1][1], (WM_KEYUP, 0x0202))
        self.assertEqual(len(self.activations()), 1)

    def test_failed_release_keeps_ledger_and_blocks_new_down_until_cleaned(self):
        self.prepare()
        self.backend.send_key_down('w')
        self.native.failure = lambda event: event[1] == WM_KEYUP
        with self.assertRaises(ForegroundInputError):
            self.backend.release_all()
        down_count = len([event for event in self.native.attempts if event[1] == WM_KEYDOWN])
        with self.assertRaises(ForegroundInputError):
            self.backend.send_key('esc')
        self.assertEqual(len([event for event in self.native.attempts if event[1] == WM_KEYDOWN]), down_count)
        self.assertTrue(self.backend._held)
        self.native.failure = None
        self.backend.release_all()
        self.backend.send_key('esc')
        self.assertFalse(self.backend._held)
        self.assertEqual(len(self.activations()), 1)

    def test_cancel_after_accepted_activation_does_not_authorize_inputs(self):
        request = StartRequest()
        self.native.after_post = lambda event: request.cancel() if event[1] == WM_ACTIVATE else None
        with self.assertRaises(TaskDisabledException):
            self.backend.prepare_start(request)
        self.assertTrue(request.activation_accepted)
        with self.assertRaises(ForegroundInputError):
            self.backend.send_key('esc')
        self.assertEqual(len(self.native.attempts), 1)

    def test_target_changed_after_activation_revokes_request_without_second_message(self):
        request = StartRequest()

        def replace_main(event):
            if event[1] == WM_ACTIVATE:
                self.native.identities[10] = replace(self.native.identities[10], thread_id=201)

        self.native.after_post = replace_main
        with self.assertRaises(ForegroundInputError):
            self.backend.prepare_start(request)
        self.assertTrue(request.activation_accepted)
        self.assertTrue(request.cancelled)
        with self.assertRaises(TaskDisabledException):
            self.new_backend().prepare_start(request)
        self.assertEqual(self.activations(), [(10, WM_ACTIVATE, 1, 0)])

    def test_native_conversion_failure_is_visible_and_releases_held_input(self):
        self.prepare()
        self.backend.mouse_down(10, 10)

        def fail_conversion(*args):
            raise OSError('受控坐标转换失败')

        self.native.screen_to_client = fail_conversion
        with self.assertRaises(ForegroundInputError):
            self.backend.move(20, 20)
        self.assertEqual(self.native.messages[-1][1], 0x0202)
        self.assertFalse(self.backend._held)

    def test_preparation_cannot_insert_activation_inside_held_gesture(self):
        self.prepare()
        entered, finish, preparation_attempted = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def wait(seconds):
            entered.set()
            if not finish.wait(2):
                raise AssertionError('受控手势未及时解除')
            self.clock.value += seconds

        def run(operation):
            try:
                operation()
            except BaseException as error:
                errors.append(error)

        self.backend.wait = wait
        gesture = threading.Thread(target=run, args=(lambda: self.backend.send_key('w', 0.02),))

        def prepare_other():
            preparation_attempted.set()
            self.prepare('排队任务开始')

        preparation = threading.Thread(target=run, args=(prepare_other,))
        gesture.start()
        try:
            self.assertTrue(entered.wait(2))
            preparation.start()
            self.assertTrue(preparation_attempted.wait(2))
            self.assertEqual(len(self.activations()), 1)
        finally:
            finish.set()
            gesture.join(2)
            if preparation.ident is not None:
                preparation.join(2)
        self.assertFalse(errors)
        self.assertFalse(gesture.is_alive())
        self.assertFalse(preparation.is_alive())
        self.assertEqual([event[1] for event in self.native.messages], [WM_ACTIVATE, WM_KEYDOWN, WM_KEYUP, WM_ACTIVATE])


if __name__ == '__main__':
    unittest.main()
