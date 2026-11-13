"""前台输入状态机与跨层生命周期；全部原生输入及用户配置均使用替身。"""

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

from ok.device.DeviceManager import DeviceManager, normalize_pc_interaction
from ok.device.capture_methods.base import BaseWindowsCaptureMethod
from ok.device.capture_methods.hwnd_window import HwndWindow
from ok.device.interaction_methods.base import BaseInteraction
from ok.device.interaction_methods import foreground_input as foreground_module
from ok.device.interaction_methods.foreground_input import (
    ForegroundInput, ForegroundInputError, ForegroundInputInterrupted, ForegroundTarget,
    InputEvent, Win32InputBackend, _INPUT,
)
from ok.device.interaction_methods.pynput import PynputInteraction
from ok.feature.Box import Box
from ok.task.TaskExecutor import ExecutorJob, TaskExecutor
from ok.core.start_controller import StartController
from ok.core.mcp_server import LocalMcpServer
from ok.task.exceptions import TaskDisabledException, WaitFailedException
from ok.task.task import BaseTask
from ok.util import window as window_module


class FakeClock:
    def __init__(self):
        self.value = 1000.0
        self.after_wait = None

    def __call__(self):
        return self.value

    def wait(self, seconds):
        self.value += max(0, seconds)
        if self.after_wait is not None:
            self.after_wait()


class FakeNative:
    def __init__(self):
        self.foreground = 10
        self.position = (100, 100)
        self.point_window = 10
        self.windows = {
            10: (1, 10, 10), 11: (1, 10, 10), 12: (1, 12, 10),
            20: (2, 20, 20), 30: (1, 30, 30), 40: (3, 40, 40), 41: (3, 40, 40),
        }
        self.owners = {12: 10}
        self.calls = []
        self.inserted = []
        self.moves = []
        self.results = deque()
        self.last_error = 5
        self.cursor_error = False

    def send(self, events):
        self.calls.append(tuple(events))
        count = self.results.popleft() if self.results else len(events)
        if isinstance(count, Exception):
            raise count
        self.inserted.extend(events[:count])
        return count

    def set_cursor_pos(self, x, y):
        if self.cursor_error:
            raise ForegroundInputError('受控替身模拟移动失败')
        self.position = (x, y)
        self.moves.append(self.position)

    def cursor_pos(self):
        return self.position

    def foreground_window(self):
        return self.foreground

    def window_at(self, point):
        return self.point_window

    def is_window(self, hwnd):
        return hwnd in self.windows

    def process_id(self, hwnd):
        return self.windows[hwnd][0]

    def root_window(self, hwnd, owned=False):
        return self.windows[hwnd][2 if owned else 1]

    def owner_window(self, hwnd):
        return self.owners.get(hwnd, 0)

    def is_child(self, parent, child):
        return parent != child and self.root_window(parent) == self.root_window(child)

    def scan_code(self, vk):
        mapping = {0x41: 0x1E, 0x57: 0x11, 0xA0: 0x2A, 0xA1: 0x36,
                   0xA2: 0x1D, 0xA3: 0xE01D, 0xA4: 0x38, 0xA5: 0xE038}
        return mapping.get(vk, vk)

    def vk_key_scan(self, char):
        if len(char) != 1 or ord(char) > 127:
            return -1
        if char.isalpha():
            return ord(char.upper()) | (0x100 if char.isupper() else 0)
        if char == '!':
            return 0x131
        return ord(char)


class FakeWindow:
    def __init__(self):
        self.foreground_target = ForegroundTarget(10, 1, 1)
        self.app_exit_event = threading.Event()
        self.stop_event = threading.Event()
        self.device_manager = None
        self.origin = (-1800, 60)
        self.do_update_window_size = Mock()
        self.bring_to_front = Mock(return_value=True)

    def get_abs_cords(self, x, y):
        return self.origin[0] + x, self.origin[1] + y


class FakeCapture:
    width, height = 30, 20

    def __init__(self, window, clock):
        self.window = window
        self.clock = clock
        self.count = 0

    def get_abs_cords(self, x, y):
        return self.window.get_abs_cords(x, y)

    def connected(self):
        return True

    def get_frame(self, busy_wait=0):
        self.count += 1
        self.clock.wait(min(0.02, busy_wait))
        return np.full((20, 30, 3), self.count, dtype=np.uint8)


class InputFixture(unittest.TestCase):
    def setUp(self):
        self.native, self.clock = FakeNative(), FakeClock()
        self.core = ForegroundInput(self.native, clock=self.clock, sleep=self.clock.wait, start_monitor=False)
        self.target = ForegroundTarget(10, 1, 1)
        self.session = self.core.session('游戏', lambda: self.target)
        self.addCleanup(self.core.stop)


class TestForegroundInput(InputFixture):

    def test_共享核心重建前先释放旧核心的失败持有(self):
        self.session.key_down('ctrl')
        self.native.results.append(0)
        with self.assertRaises(ForegroundInputError):
            self.core.stop()

        replacement = ForegroundInput(self.native, clock=self.clock, sleep=self.clock.wait, start_monitor=False)
        self.addCleanup(replacement.stop)
        self.addCleanup(self.native.results.clear)
        factory = Mock(return_value=replacement)
        self.native.results.append(0)
        before = list(self.native.inserted)
        with patch.object(foreground_module, '_shared_input', self.core), \
                patch.object(foreground_module, 'ForegroundInput', factory):
            with self.assertRaises(ForegroundInputError):
                foreground_module.get_shared_foreground_input()
            factory.assert_not_called()
            self.assertIs(foreground_module._shared_input, self.core)
            self.assertEqual(self.native.inserted, before)

            # 旧 Ctrl 的松键真正送达后才允许重建；新 W 不能混入释放失败的旧持有。
            fresh = foreground_module.get_shared_foreground_input()
            fresh.session('新游戏', self.target).send_key('w', down_time=0)
            factory.assert_called_once_with()
            self.assertEqual([(event.code, event.up) for event in self.native.inserted],
                             [(0x1D, False), (0x1D, True), (0x11, False), (0x11, True)])

    def test_公开键值_整数与显式虚拟键保持不同语义(self):
        for key in (1, Key.enter, KeyCode.from_vk(0x61), 'control', 'num1', Key.alt_r):
            self.session.send_key(key, down_time=0)
        down = [event for event in self.native.inserted if not event.up]
        self.assertEqual([event.code for event in down], [0x31, 0x0D, 0x61, 0x1D, 0x61, 0x38])
        self.assertTrue(down[-1].flags & 1)
        self.assertFalse(self.core._held)

    def test_修饰键别名与侧键使用标准事件(self):
        for key in ('lcontrol', 'rctrl', 'pageup', 'printscreen', Button.x1, 'mouse5'):
            self.session.send_key(key, down_time=0)
        down = [event for event in self.native.inserted if not event.up]
        self.assertEqual(down[-2:], [InputEvent('mouse', 4), InputEvent('mouse', 5)])
        self.assertTrue(down[1].flags & 1)
        self.assertTrue(down[2].flags & 1)
        self.assertFalse(down[0].flags & 1)

    def test_重复按下与内层短按不会释放外层持有(self):
        self.session.key_down('control')
        self.session.key_down('lctrl')
        self.session.hotkey(Key.ctrl_l, 'a', down_time=0)
        self.assertEqual(len(self.core._held), 1)
        self.assertEqual([event.code for event in self.native.inserted if event.up], [0x1E])
        self.session.key_up('ctrl')
        self.session.key_up('ctrl')
        self.assertEqual([event.code for event in self.native.inserted if event.up], [0x1E, 0x1D])

    def test_重叠显式按键共享修饰键到最后一次释放(self):
        self.session.key_down('shift')
        self.session.key_down('A')
        self.session.key_up('shift')
        self.assertEqual(len(self.core._held), 2)
        self.session.key_up('A')
        self.assertFalse(self.core._held)

    def test_非法参数在任何输入之前拒绝(self):
        actions = [lambda: self.session.send_key('不存在的按键'),
                   lambda: self.session.click(1, 1, key='未知按钮'),
                   lambda: self.session.swipe(1, 1, 2, 2, -0.1),
                   lambda: self.session.click(1, 1, down_time=-1),
                   lambda: self.session.send_key(KeyCode.from_vk(300)),
                   lambda: self.session.input_text('\ud800')]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(ValueError):
                action()
        self.assertFalse(self.native.calls)
        self.assertFalse(self.native.moves)

    def test_失焦阻止新输入但仍释放本程序长按(self):
        self.session.key_down('w')
        self.native.foreground = 20
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.send_key('a')
        self.assertEqual(self.native.inserted, [InputEvent('keyboard', 0x11, 8), InputEvent('keyboard', 0x11, 8, up=True)])
        self.assertFalse(self.core._held)
        self.native.foreground = 10
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.send_key('a')
        self.session.acknowledge()
        self.session.send_key('a', down_time=0)

    def test_取消之后松键不调用普通门禁(self):
        check = Mock(return_value=None)
        self.session.cancel_check = check
        self.session.key_down('w')
        before = check.call_count
        check.side_effect = TaskDisabledException()
        self.session.key_up('w')
        self.assertEqual(check.call_count, before)
        self.assertFalse(self.core._held)

    def test_没有新动作时也观察失焦和取消(self):
        self.session.key_down('w')
        self.native.foreground = 20
        self.core.poll_foreground()
        self.assertFalse(self.core._held)
        self.native.foreground = 10
        self.session.acknowledge()
        self.session.key_down(Button.left)
        self.session.cancel_check = Mock(side_effect=TaskDisabledException())
        self.core.poll_foreground()
        self.assertFalse(self.core._held)

    def test_目标代次与进程身份变化释放旧持有(self):
        self.session.key_down('w')
        self.target = replace(self.target, generation=2)
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.move(50, 50)
        self.assertFalse(self.native.moves)
        self.session.acknowledge()
        self.native.windows[10] = (2, 10, 10)
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.send_key('a')

    def test_已开始但暂未持有的会话仍锁存失焦(self):
        self.session.send_key('w', down_time=0)
        self.assertFalse(self.core._held)
        self.native.foreground = 20
        self.core.poll_foreground()
        self.native.foreground = 10
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.click(100, 100)
        self.assertFalse(self.native.moves)

    def test_根子拥有与登录允许窗口不放行无关同进程窗口(self):
        for hwnd in (10, 11, 12):
            self.native.foreground = hwnd
            self.assertTrue(self.session.is_foreground())
        for hwnd in (20, 30, 40):
            self.native.foreground = hwnd
            self.assertFalse(self.session.is_foreground())
        self.target = replace(self.target, allowed_hwnds=(40,))
        self.native.foreground = 41
        self.assertTrue(self.session.is_foreground())
        self.target = replace(self.target, allowed_hwnds=())
        self.native.windows.update({
            10: (1, 10, 99), 12: (1, 12, 99), 13: (1, 13, 99),
            30: (1, 30, 99), 99: (1, 99, 99),
        })
        self.native.owners.update({10: 99, 12: 10, 13: 12, 30: 99})
        # 主窗自身被启动器拥有时，真正向上到主窗的直接、嵌套拥有窗仍应可用。
        for hwnd in (12, 13):
            self.native.foreground = hwnd
            self.session.acknowledge()
            before = len(self.native.inserted)
            self.session.send_key('w', down_time=0)
            self.assertEqual(len(self.native.inserted) - before, 2)
        self.native.foreground = 30
        before = len(self.native.inserted)
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.send_key('w', down_time=0)
        self.assertEqual(len(self.native.inserted), before)

    def test_鼠标落点不属于目标时点击滚轮和移动均拒绝(self):
        self.native.point_window = 20
        for action in (lambda: self.session.click(10, 20), lambda: self.session.scroll(2),
                       lambda: self.session.move(-1900, 50)):
            with self.subTest(action=action), self.assertRaises(ForegroundInputInterrupted):
                self.session.acknowledge()
                action()
        self.assertFalse(self.native.calls)
        self.assertFalse(self.native.moves)

    def test_滚轮方向与侧键原生结构正确(self):
        self.session.scroll(-2, x=10, y=20)
        self.assertEqual([event.code for event in self.native.inserted], [-120, -120])
        wheel = Win32InputBackend.make_input(self.native.inserted[0])
        self.assertEqual(wheel.mi.dwFlags, 0x0800)
        self.assertEqual(wheel.mi.mouseData, (-120) & 0xFFFFFFFF)
        side = Win32InputBackend.make_input(InputEvent('mouse', 5, up=True))
        self.assertEqual((side.mi.dwFlags, side.mi.mouseData), (0x0100, 2))
        self.assertEqual(ctypes.sizeof(_INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_零时长拖拽仍到终点并释放(self):
        self.session.swipe(-1800, 50, -1500, 200, duration=0)
        self.assertEqual(self.native.moves, [(-1800, 50), (-1500, 200)])
        self.assertEqual(self.native.inserted, [InputEvent('mouse', 1), InputEvent('mouse', 1, up=True)])

    def test_拖拽秒时长稳定时间与终点正确(self):
        start = self.clock()
        self.session.swipe(10, 10, 120, 80, duration=0.6, settle_time=0.2)
        self.assertAlmostEqual(self.clock() - start, 0.8, places=5)
        self.assertEqual(self.native.moves[-1], (120, 80))
        self.assertFalse(self.core._held)

    def test_拖拽失焦不完成旧终点也不重放(self):
        self.clock.after_wait = lambda: setattr(self.native, 'foreground', 20)
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.swipe(10, 10, 200, 200, duration=1)
        self.assertNotEqual(self.native.moves[-1], (200, 200))
        self.assertTrue(self.native.inserted[-1].up)
        count = len(self.native.inserted)
        self.native.foreground = 10
        self.session.acknowledge()
        self.assertEqual(len(self.native.inserted), count)

    def test_生命周期清理使复合动作明确中断(self):
        self.clock.after_wait = lambda: self.core.release_all()
        with self.assertRaises(ForegroundInputInterrupted):
            self.session.send_key('w', down_time=1)
        self.assertFalse(self.core._held)

    def test_取消与光标失败都会清理复合动作(self):
        self.session.cancel_check = lambda: (_ for _ in ()).throw(TaskDisabledException()) if self.clock() > 1000 else None
        with self.assertRaises(TaskDisabledException):
            self.session.hotkey('ctrl', 'a', down_time=0.5)
        self.assertFalse(self.core._held)
        self.session.cancel_check = None
        self.session.acknowledge()
        self.native.cursor_error = True
        with self.assertRaises(ForegroundInputError):
            self.session.move(20, 20)
        self.assertFalse(self.core._held)

    def test_中文与补充平面文本按UTF16成对发射(self):
        self.session.input_text('你好😀')
        down = [event for event in self.native.inserted if not event.up]
        self.assertEqual([event.code for event in down], [0x4F60, 0x597D, 0xD83D, 0xDE00])
        self.assertTrue(all(event.flags == 4 for event in down))
        self.assertTrue(all(self.native.inserted[i + 1] == self.native.inserted[i].released()
                            for i in range(0, len(self.native.inserted), 2)))

    def test_零注入与部分Unicode注入均明确失败且不重放(self):
        self.native.results.append(0)
        with self.assertRaises(ForegroundInputError):
            self.session.send_key('w')
        self.assertFalse(self.core._held)
        self.session.acknowledge()
        self.native.results.extend((1, 1))
        with self.assertRaises(ForegroundInputError):
            self.session.input_text('不可重放')
        self.assertEqual([event.code for event in self.native.inserted], [ord('不'), ord('不')])
        self.assertFalse(self.core._held)

    def test_一个释放失败仍清理其它状态并保留待重试项(self):
        self.session.key_down('w')
        self.session.key_down('shift')
        self.native.results.extend((0, 1))
        with self.assertRaises(ForegroundInputError):
            self.session.release_all()
        self.assertEqual(len(self.core._held), 1)
        self.session.release_all()
        self.assertFalse(self.core._held)

    def test_释放失败后布局变化仍用原扫描码且阻止新按下(self):
        self.session.key_down('w')
        self.native.results.append(0)
        with self.assertRaises(ForegroundInputError):
            self.session.release_all()
        with self.assertRaises(ForegroundInputError):
            self.session.send_key('a')
        self.native.scan_code = lambda vk: 0x50
        self.session.key_up('w')
        self.assertEqual(self.native.inserted[-1].code, 0x11)
        self.assertFalse(self.core._held)
        self.assertFalse(self.core._pending_release)

    def test_失焦释放失败保持独立错误类型(self):
        self.session.key_down('w')
        self.native.foreground = 20
        self.native.results.extend((0, 0))
        with self.assertRaises(ForegroundInputError):
            self.session.send_key('a')
        self.assertTrue(self.core._held)
        self.native.results.clear()
        self.session.release_all()

    def test_会话交接在置前前释放且退出异常清理(self):
        self.session.key_down('w')
        notification = self.core.session('通知', ForegroundTarget(40, 1, 3))
        with self.assertRaisesRegex(RuntimeError, '受控失败'):
            with notification:
                self.assertFalse(self.core._held)
                self.native.foreground, self.native.point_window = 40, 40
                notification.key_down(Button.right)
                raise RuntimeError('受控失败')
        self.assertFalse(self.core._held)
        self.assertIs(self.core._active, self.session)

    def test_独占会话阻止其它线程混入新事件(self):
        started, done = threading.Event(), threading.Event()
        notification = self.core.session('通知', ForegroundTarget(40, 1, 3))
        other = self.core.session('下一通知', ForegroundTarget(40, 1, 3))
        errors = []

        def send_later():
            started.set()
            try:
                other.send_key('a', down_time=0)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        with notification:
            self.native.foreground, self.native.point_window = 40, 40
            notification.key_down(Button.left)
            worker = threading.Thread(target=send_later, daemon=True)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(done.wait(0.01))
            self.assertEqual(len(self.native.inserted), 1)
        self.assertTrue(done.wait(1))
        worker.join(timeout=1)
        self.assertFalse(errors)
        self.assertTrue(self.native.inserted[1].up)

    def test_退出释放失败时外来持有阻止游戏新事件(self):
        self.session.check()
        notification = self.core.session('通知', ForegroundTarget(40, 1, 3))
        with self.assertRaises(ForegroundInputError):
            with notification:
                self.native.foreground, self.native.point_window = 40, 40
                notification.key_down('control')
                self.native.results.append(0)
        self.native.foreground, self.native.point_window = 10, 10
        self.native.results.append(0)
        before = len(self.native.inserted)
        with self.assertRaises(ForegroundInputError):
            self.session.send_key('a')
        self.assertEqual(len(self.native.inserted), before)
        self.assertTrue(self.core._held)
        self.core.poll_foreground()
        self.assertFalse(self.core._held)
        self.session.acknowledge()
        self.session.send_key('a', down_time=0)


class TestForegroundLifecycle(InputFixture):
    def setUp(self):
        super().setUp()
        for scoped_patch in (
                patch('ok.device.interaction_methods.foreground_input._shared_input', self.core),
                patch('ok.device.interaction_methods.pynput.get_shared_foreground_input', return_value=self.core),
                patch('ok.task.TaskExecutor.time.monotonic', self.clock),
                patch('ok.task.TaskExecutor.time.time', self.clock),
                patch('ok.task.TaskExecutor.communicate'),
                patch('ok.task.task.communicate')):
            scoped_patch.start()
            self.addCleanup(scoped_patch.stop)
        self.window = FakeWindow()
        self.capture = FakeCapture(self.window, self.clock)
        self.adapter = PynputInteraction(self.capture, self.window)
        executor = self.executor = TaskExecutor.__new__(TaskExecutor)
        executor.__dict__.update(
            current_job=None, current_task=None, _exclusive_owner=None, thread=threading.current_thread(),
            lock=threading.Lock(), _frame=None, _last_frame_time=0, _snapshot_enabled=False,
            _frame_target_generation=None, _foreground_waiting=False, _focus_wait_started=None,
            _focus_wait_total=0.0, _needs_foreground_frame=False, _notify_foreground_resume=False,
            paused=False, debug_mode=False, debug=False, scene=None, exit_event=self.window.app_exit_event,
            blur_overlay_processor=None, _job_queue=[], trigger_tasks=[], onetime_tasks=[],
            onetime_task_queue=[], _task_context_stack=[], trigger_task_index=-1,
            wait_scene_timeout=0.1, wait_until_settle_time=0, _wake_condition=None,
        )
        manager = SimpleNamespace(interaction=self.adapter, capture_method=self.capture,
                                  get_preferred_device=lambda: {'device': 'windows'},
                                  wait_for_binding=Mock(return_value=self.adapter))
        manager.executor = executor
        self.window.device_manager = executor.device_manager = manager
        executor._wait_for_activity = self.clock.wait
        self.task = BaseTask(executor, SimpleNamespace(tr=lambda message: message))
        self.task._enabled = self.task.running = True
        self.task.config = {}
        self.task.on_foreground_resume = Mock()
        executor.current_task = self.task
        executor.onetime_tasks = [self.task]

    def _make_combat_task(self):
        from src.scene.WWScene import WWScene
        from src.task.BaseCombatTask import BaseCombatTask

        self.executor.device_manager.supported_ratio = None
        task = BaseCombatTask.__new__(BaseCombatTask)
        task.__dict__.update(self.task.__dict__)
        task.__dict__.pop('on_foreground_resume', None)
        task.scene = WWScene()
        task._foreground_epoch = 0
        task.next_monthly_card_start = 0
        task._abyss_combat_active = True
        task.chars = [None, None, None]
        task.freeze_durations = []
        task.cds = {}
        self.executor.scene = task.scene
        self.executor.current_task = task
        self.executor.onetime_tasks = [task]
        self.executor.next_frame()
        task.skip_combat_check = True
        task.use_liberation = True
        return task

    def _schedule_focus_loss(self):
        lost = False
        restore_at = self.clock() + 0.14

        def change_focus():
            nonlocal lost
            if not lost:
                self.native.foreground = 20
                lost = True
            elif self.clock() >= restore_at:
                self.native.foreground = 10
                self.clock.after_wait = None

        self.clock.after_wait = change_focus

    def _make_lily_rotation(self, interrupted_stage=None):
        from src.char.BaseChar import BaseChar
        from src.char.Lily import Lily

        task = self._make_combat_task()
        task.is_con_full = Mock(return_value=True)
        char = Lily(task, 0)
        task.chars = [char, None, None]
        char.in_slumberland = Mock(return_value=False)
        char.click_echo = Mock()
        char.build_forte = Mock(return_value=True)
        char.is_mouse_forte_full = Mock(return_value=True)
        char.liberation_available = Mock(return_value=True)
        char.fill_concerto = Mock(return_value=True)
        char.click_liberation = Mock(return_value=True)
        char.switch_next_char = Mock()
        observations = 0

        def heavy_state():
            nonlocal observations
            observations += 1
            if interrupted_stage == '结束等待' and observations == 1:
                self._schedule_focus_loss()
            return observations == 1

        char.heavy_in_progress = Mock(side_effect=heavy_state)
        if interrupted_stage == '开始等待':
            self._schedule_focus_loss()
        if interrupted_stage == '收尾休眠':
            def sleep(seconds, check_combat=True):
                self._schedule_focus_loss()
                return BaseChar.sleep(char, seconds, check_combat)

            char.sleep = sleep
        return task, char

    def _assert_lily_rotation_interrupted(self, stage):
        task, char = self._make_lily_rotation(stage)
        epoch = task._foreground_epoch
        # 保留真实 perform → wait/sleep → 执行器 → 恢复钩子，只有系统输入与抓帧使用替身。
        with patch.object(task, 'mouse_up', wraps=task.mouse_up) as release:
            with self.assertRaises(ForegroundInputInterrupted):
                char.perform()
            release.assert_called_once()
        char.fill_concerto.assert_not_called()
        char.click_liberation.assert_not_called()
        char.switch_next_char.assert_not_called()
        self.assertEqual(task.chars, [None, None, None])
        self.assertGreater(task._foreground_epoch, epoch)
        self.assertIsNone(task._active_char_rotation)
        self.assertFalse(self.core._held)
        self.assertEqual(self.native.inserted, [InputEvent('mouse', 1), InputEvent('mouse', 1, up=True)])
        self.window.bring_to_front.assert_not_called()

    def test_Lily开始等待内恢复释放并退出旧轮转(self):
        self._assert_lily_rotation_interrupted('开始等待')

    def test_Lily结束等待内恢复释放并退出旧轮转(self):
        self._assert_lily_rotation_interrupted('结束等待')

    def test_Lily收尾sleep内恢复释放并退出旧轮转(self):
        self._assert_lily_rotation_interrupted('收尾休眠')

    def test_Lily正常重击完成释放并进入后续阶段(self):
        for liberation_confirmed in (True, False):
            with self.subTest(liberation_confirmed=liberation_confirmed):
                task, char = self._make_lily_rotation()
                if not liberation_confirmed:
                    char.click_liberation.return_value = False
                    char.in_slumberland.side_effect = [False, True]
                before = len(self.native.inserted)
                with patch.object(task, 'mouse_up', wraps=task.mouse_up) as release:
                    char.perform()
                    release.assert_called_once()
                char.fill_concerto.assert_called_once_with(0.8)
                char.click_liberation.assert_called_once()
                task.is_con_full.assert_called_once()
                char.switch_next_char.assert_called_once()
                self.assertIsNone(task._active_char_rotation)
                self.assertFalse(self.core._held)
                self.assertEqual(self.native.inserted[before:],
                                 [InputEvent('mouse', 1), InputEvent('mouse', 1, up=True)])

    def test_Lily协奏未确认时停止轮转且不进入后续技能或切人(self):
        for stage, can_liberate, fills, liberated, entered_slumberland, screenshot_name in (
            ('首次满协奏', False, [False], False, False, '莉莉协奏补充结果无法确认'),
            ('首次解放补能', True, [False], True, False, '莉莉协奏补充结果无法确认'),
            ('解放失败后二次补能', True, [True, False], False, False, '莉莉协奏补充结果无法确认'),
            ('解放成功后仍未满', True, [True], True, False, '莉莉延奏交接无法确认'),
            ('解放未确认但已进梦乡', True, [True], False, True, '莉莉延奏交接无法确认'),
        ):
            with self.subTest(stage=stage):
                task, char = self._make_lily_rotation()
                task.screenshot = Mock(wraps=task.screenshot)
                task.is_con_full.return_value = False
                char.liberation_available.return_value = can_liberate
                char.fill_concerto.side_effect = fills
                char.click_liberation.return_value = liberated
                if entered_slumberland:
                    char.in_slumberland.side_effect = [False, True]
                if stage == '解放成功后仍未满':
                    # 解放后的新 HUD 必须覆盖旧角色的满协奏缓存。
                    char.current_con = 1
                before = len(self.native.inserted)
                with patch.object(task, 'mouse_up', wraps=task.mouse_up) as release:
                    with self.assertRaisesRegex(TimeoutError, '协奏'):
                        char.perform()
                    release.assert_called_once()
                task.screenshot.assert_called_once_with(screenshot_name)
                self.assertEqual(char.fill_concerto.call_count, len(fills))
                self.assertEqual(char.click_liberation.call_count, int(fills[0]))
                char.switch_next_char.assert_not_called()
                self.assertIsNone(task._active_char_rotation)
                self.assertFalse(self.core._held)
                self.assertEqual(self.native.inserted[before:],
                                 [InputEvent('mouse', 1), InputEvent('mouse', 1, up=True)])
                self.window.bring_to_front.assert_not_called()

    def test_Lily协奏阶段取消或输入失败保持原异常并清理轮转(self):
        for stage in ('首次补能', '二次补能', '交接复核'):
            for error in (TaskDisabledException(), ForegroundInputError(), ForegroundInputInterrupted()):
                with self.subTest(stage=stage, error=type(error).__name__):
                    task, char = self._make_lily_rotation()
                    task.screenshot = Mock()
                    if stage == '首次补能':
                        char.fill_concerto.side_effect = error
                    elif stage == '二次补能':
                        char.click_liberation.return_value = False
                        char.fill_concerto.side_effect = [True, error]
                    else:
                        task.is_con_full.side_effect = error
                    before = len(self.native.inserted)
                    with patch.object(task, 'mouse_up', wraps=task.mouse_up) as release:
                        with self.assertRaises(type(error)):
                            char.perform()
                        release.assert_called_once()
                    task.screenshot.assert_not_called()
                    char.switch_next_char.assert_not_called()
                    self.assertIsNone(task._active_char_rotation)
                    self.assertFalse(self.core._held)
                    self.assertEqual(self.native.inserted[before:],
                                     [InputEvent('mouse', 1), InputEvent('mouse', 1, up=True)])
                    self.window.bring_to_front.assert_not_called()

    def test_Lily重击开始或收尾超时停止轮转并释放鼠标(self):
        for started in (False, True):
            for lose_focus_during_screenshot in (False, True):
                with self.subTest(started=started, lose_focus_during_screenshot=lose_focus_during_screenshot):
                    task, char = self._make_lily_rotation()
                    original_screenshot = task.screenshot

                    def screenshot(name):
                        if lose_focus_during_screenshot:
                            self._schedule_focus_loss()
                            self.native.foreground = 20
                        return original_screenshot(name)

                    task.screenshot = Mock(side_effect=screenshot)
                    char.HEAVY_START_TIMEOUT = 0.2
                    char.HEAVY_TIMEOUT = 0.2
                    char.heavy_in_progress = Mock(return_value=started)
                    before = len(self.native.inserted)
                    with patch.object(task, 'mouse_up', wraps=task.mouse_up) as release, \
                            patch.object(task, 'wait_until', wraps=task.wait_until) as waits:
                        with self.assertRaisesRegex(TimeoutError, '重击开始或收尾'):
                            char.perform()
                        release.assert_called_once()
                        self.assertEqual(waits.call_count, 2 if started else 1)
                    task.screenshot.assert_called_once_with('莉莉重击结果无法确认')
                    char.fill_concerto.assert_not_called()
                    char.click_liberation.assert_not_called()
                    char.switch_next_char.assert_not_called()
                    self.assertIsNone(task._active_char_rotation)
                    self.assertFalse(self.core._held)
                    self.assertEqual(self.native.inserted[before:],
                                     [InputEvent('mouse', 1), InputEvent('mouse', 1, up=True)])
                    self.window.bring_to_front.assert_not_called()

    def test_深塔介绍关闭等待内恢复仍要求新帧结果(self):
        task = self._make_combat_task()
        task.ocr = Mock(return_value=[Box(10, 3, 3, 2, name='战斗特色')])
        task.screenshot = Mock()
        original_click = task.click

        def click(*args, **kwargs):
            result = original_click(*args, **kwargs)
            if args[0] == 0.942:
                self._schedule_focus_loss()
            return result

        with patch.object(task, 'click', side_effect=click) as close:
            with self.assertRaisesRegex(RuntimeError, '关闭结果无法确认'):
                task.dismiss_abyss_character_info(time_out=0.4)
            # 相对坐标会再次调用像素 click；外层意图和实际原生事件分别证明没有重复发送。
            self.assertEqual([call.args for call in close.call_args_list if call.args[0] == 0.942],
                             [(0.942, 0.058)])
        self.assertGreater(task._foreground_epoch, 0)
        self.assertFalse(self.core._held)
        self.assertEqual(self.native.inserted, [InputEvent('mouse', 1), InputEvent('mouse', 1, up=True)])
        self.window.bring_to_front.assert_not_called()

    def test_深塔已发送重挑战恢复后只点新编队确认(self):
        task = self._make_combat_task()
        retry = Box(10, 10, 3, 2, name='重新挑战')
        confirm = Box(20, 10, 3, 2, name='确认')
        page = 'rechallenge'

        def ocr(*args, match, **kwargs):
            control = retry if page == 'rechallenge' else confirm if page == 'confirm' else None
            return [control] if control and match.search(control.name) else []

        task.ocr = Mock(side_effect=ocr)
        task.find_abyss_start_marker = Mock(side_effect=lambda: retry if page == 'arena' else None)
        task.find_f_with_text = Mock(side_effect=lambda: confirm if page == 'arena' else None)
        task.load_chars = Mock(return_value=True)
        original_click = task.click

        def click(*args, **kwargs):
            nonlocal page
            result = original_click(*args, **kwargs)
            if args[0] is retry:
                page = 'confirm'
                self._schedule_focus_loss()
            elif args[0] is confirm:
                page = 'arena'
            return result

        with patch.object(task, 'click', side_effect=click) as clicks, \
                patch.object(task, 'send_key', wraps=task.send_key) as keys:
            self.assertTrue(task.restart_combat_in_abyss())
            self.assertEqual([call.args[0] for call in clicks.call_args_list
                              if call.args[0] is retry or call.args[0] is confirm], [retry, confirm])
            keys.assert_called_once_with('f')
        self.assertGreater(task._foreground_epoch, 0)
        self.assertFalse(self.core._held)
        self.assertEqual([event.kind for event in self.native.inserted if not event.up],
                         ['mouse', 'mouse', 'keyboard'])
        self.window.bring_to_front.assert_not_called()

    def test_薄适配强制移动并保留捕获裁剪负坐标(self):
        self.task.click(20, 30, move=False, after_sleep=0)
        self.assertEqual(self.native.moves[-1], (-1780, 90))
        self.assertFalse(self.core._held)
        with self.assertRaises(ValueError):
            self.adapter.check_clickable = False

    def test_公共拖拽只转换一次单位且拒绝微小负时长(self):
        self.task.sleep = Mock()
        start = self.clock()
        self.task.swipe(10, 10, 20, 20, duration=0.6)
        self.assertAlmostEqual(self.clock() - start, 0.6, places=5)
        self.assertEqual(self.native.moves[-1], (-1780, 80))
        before = len(self.native.inserted)
        with self.assertRaises(ValueError):
            self.task.swipe(10, 10, 20, 20, duration=-0.00001)
        self.assertEqual(len(self.native.inserted), before)

    def test_公共松键先清理而不执行取消检查或抓帧(self):
        self.adapter.send_key_down('w')
        self.adapter.mouse_down()
        self.task._enabled = False
        self.native.foreground = 20
        self.executor.reset_scene = Mock()
        self.task.send_key_up('w')
        self.task.mouse_up()
        self.assertFalse(self.core._held)
        self.assertEqual(self.capture.count, 0)
        self.assertTrue(all(call.kwargs == {'check_enabled': False}
                            for call in self.executor.reset_scene.call_args_list))

    def test_公共按键保留Key与KeyCode(self):
        self.task.send_key(Key.ctrl_l)
        self.task.send_key(KeyCode.from_vk(0x61))
        self.assertEqual([event.code for event in self.native.inserted if not event.up], [0x1D, 0x61])

    def test_文本绘制与异常不携带完整文本(self):
        self.task.box_of_screen = Mock(return_value=[])
        text = '演示文本😀'
        self.task.input_text(text)
        from ok.task import task as task_module
        draw = task_module.communicate.emit_draw_box.call_args
        self.assertEqual(draw.args[0], '文本输入')
        self.assertNotIn(text, str(draw))
        self.native.results.append(0)
        with self.assertRaises(ForegroundInputError) as failure:
            self.task.input_text(text)
        self.assertNotIn(text, str(failure.exception))
        with self.assertRaises(Exception) as invalid:
            self.task.send_key(text)
        self.assertNotIn(text, str(invalid.exception))

    def test_next_frame失焦仍受明确期限约束(self):
        self.adapter.send_key_down('w')
        self.native.foreground = 20
        start = self.clock()
        self.assertIsNone(self.executor.next_frame(time_out=0.2))
        self.assertAlmostEqual(self.clock() - start, 0.2, places=5)
        self.assertFalse(self.core._held)
        self.assertEqual(self.capture.count, 0)
        self.window.bring_to_front.assert_not_called()

    def test_等待前台取消不被内层等待挡住(self):
        self.native.foreground = 20
        self.clock.after_wait = lambda: setattr(self.task, '_enabled', False)
        with self.assertRaises(TaskDisabledException):
            self.executor.wait_foreground_frame()
        self.assertLess(self.clock() - 1000, 0.2)

    def test_安全等待回前台刷新代次和新帧但不置前(self):
        self.native.foreground = 20

        def resume():
            if self.clock() >= 1000.1:
                self.native.foreground = 10
                self.window.foreground_target = ForegroundTarget(10, 2, 1)

        self.clock.after_wait = resume
        frame = self.executor.wait_foreground_frame(time_out=1)
        self.assertEqual(int(frame[0, 0, 0]), 1)
        self.assertEqual(self.executor._frame_target_generation, self.executor._capture_generation())
        self.task.on_foreground_resume.assert_called_once()
        self.window.bring_to_front.assert_not_called()

    def test_业务识别等待不消耗普通失焦预算(self):
        self.native.foreground = 20
        self.clock.after_wait = lambda: setattr(self.native, 'foreground', 10) if self.clock() >= 1002 else None
        condition = Mock(side_effect=[False, True])
        start = self.executor.business_time()
        self.assertTrue(self.executor.wait_condition(condition, time_out=0.1))
        self.assertGreaterEqual(self.clock(), 1002)
        self.assertLess(self.executor.business_time() - start, 0.1)
        self.assertEqual(condition.call_count, 2)

    def test_条件持续成立的稳定期不执行补救动作(self):
        recognized = object()
        condition = Mock(return_value=recognized)
        post_action = Mock()
        self.executor.wait_until_settle_time = 0.05
        start = self.executor.business_time()

        result = self.task.wait_until(condition, time_out=0.2, post_action=post_action)

        self.assertIs(result, recognized)
        self.assertGreaterEqual(self.executor.business_time() - start, 0.05)
        self.assertGreater(condition.call_count, 1)
        post_action.assert_not_called()

    def test_稳定条件丢失后只在丢失时补救并重新计时(self):
        observations = []
        post_action = Mock()

        def condition():
            observations.append(self.executor.business_time())
            # 第三帧条件丢失，第四帧必须开始新的稳定期。
            return len(observations) != 3

        self.assertTrue(self.executor.wait_condition(
            condition, time_out=0.3, settle_time=0.05, post_action=post_action))

        post_action.assert_called_once()
        self.assertGreaterEqual(self.executor.business_time() - observations[3], 0.05)

    def test_稳定期未满仍保留业务超时和失败异常(self):
        for raise_if_not_found in (False, True):
            with self.subTest(raise_if_not_found=raise_if_not_found):
                condition = Mock(return_value=True)
                post_action = Mock()
                start = self.executor.business_time()
                kwargs = dict(time_out=0.1, settle_time=1, post_action=post_action,
                              raise_if_not_found=raise_if_not_found)
                if raise_if_not_found:
                    with self.assertRaises(WaitFailedException):
                        self.executor.wait_condition(condition, **kwargs)
                else:
                    self.assertIsNone(self.executor.wait_condition(condition, **kwargs))

                self.assertAlmostEqual(self.executor.business_time() - start, 0.1, places=5)
                self.assertGreater(condition.call_count, 1)
                post_action.assert_not_called()

    def test_稳定期取消不因跳过补救而被吞掉(self):
        condition = Mock(return_value=True)
        post_action = Mock()
        self.clock.after_wait = lambda: setattr(self.task, '_enabled', False) if self.capture.count == 2 else None

        with self.assertRaises(TaskDisabledException):
            self.task.wait_until(condition, time_out=1, settle_time=0.5, post_action=post_action)

        self.assertEqual(condition.call_count, 1)
        self.assertLess(self.clock(), 1000.1)
        post_action.assert_not_called()

    def test_稳定等待中的输入错误继续向上传播(self):
        error = ForegroundInputError('受控替身模拟识别过程中输入失败')
        condition = Mock(side_effect=[True, error])
        post_action = Mock()

        with self.assertRaises(ForegroundInputError) as failure:
            self.task.wait_until(condition, time_out=1, settle_time=0.5, post_action=post_action)

        self.assertIs(failure.exception, error)
        post_action.assert_not_called()

    def test_抓帧内失焦恢复后稳定期重新计时(self):
        # 先完成首次绑定回调，下面只统计实际失焦恢复。
        self.executor.next_frame()
        self.task.on_foreground_resume.reset_mock()
        observations = []
        post_action = Mock()
        lost_focus = False

        def change_focus():
            nonlocal lost_focus
            if not lost_focus:
                self.native.foreground = 20
                lost_focus = True
            elif self.clock() >= 1000.14:
                self.native.foreground = 10
                self.clock.after_wait = None

        def condition():
            observations.append(self.executor.business_time())
            if len(observations) == 1:
                # 在下一次抓帧内部失焦并恢复，外层循环看不到持续失焦的状态。
                self.clock.after_wait = change_focus
            return True

        self.assertTrue(self.executor.wait_condition(
            condition, time_out=0.3, settle_time=0.08, post_action=post_action))

        self.assertGreater(self.executor.focus_wait_seconds, 0)
        self.assertGreaterEqual(self.executor.business_time() - observations[1], 0.08)
        self.task.on_foreground_resume.assert_called_once()
        post_action.assert_not_called()
        self.window.bring_to_front.assert_not_called()

    def test_海之女确认框稳定后只点击一次且不被Esc关闭(self):
        config_directory = self.enterContext(tempfile.TemporaryDirectory(prefix='稳定等待配置-'))
        self.enterContext(patch('ok.util.config.Config.config_folder', config_directory))
        from src.task.FarmEchoTask import FarmEchoTask

        # 仅初始化公共任务状态，隔离真实配置；保留海之女到通用等待的生产调用链。
        task = FarmEchoTask.__new__(FarmEchoTask)
        BaseTask.__init__(task, self.executor, self.task._app)
        task._enabled = task.running = True
        task.config = {'Boss': 'Lady of the Sea'}
        task.boss_list = ['Lady of the Sea']
        task.in_combat = Mock(return_value=False)
        task.in_world = Mock(return_value=False)
        task.wait_in_team_and_world = Mock()
        task.on_foreground_resume = Mock()
        task.sleep = self.executor.sleep
        self.executor.current_task = task
        self.executor.onetime_tasks = [task]
        self.executor.wait_scene_timeout = 3
        confirm = Box(15, 10, 5, 5, name='确认框')
        dialog_visible = False
        send_key = task.send_key

        def send_escape(key, **kwargs):
            nonlocal dialog_visible
            self.assertEqual(key, 'esc')
            dialog_visible = not dialog_visible
            return send_key(key, **kwargs)

        task.send_key = Mock(side_effect=send_escape)
        task.find_one = Mock(side_effect=lambda *args, **kwargs: confirm if dialog_visible else None)
        task.click_box = Mock(wraps=task.click_box)

        task.manage_boss_interactions()

        task.send_key.assert_called_once_with('esc', after_sleep=0.5)
        task.click_box.assert_called_once_with(confirm, -1, 0.5, after_sleep=0)
        task.wait_in_team_and_world.assert_called_once_with(time_out=120)
        self.assertFalse(self.core._held)

    def test_识别等待的外部绝对期限仍然有效(self):
        self.native.foreground = 20
        condition = Mock()
        self.assertIsNone(self.executor.wait_condition(condition, time_out=10, deadline=1000.2))
        self.assertAlmostEqual(self.clock(), 1000.2, places=5)
        condition.assert_not_called()

    def test_稳定期的外部绝对期限仍然有效(self):
        condition = Mock(return_value=True)
        post_action = Mock()

        self.assertIsNone(self.executor.wait_condition(
            condition, time_out=10, settle_time=1, post_action=post_action, deadline=1000.1))

        self.assertAlmostEqual(self.clock(), 1000.1, places=5)
        self.assertGreater(condition.call_count, 1)
        post_action.assert_not_called()

    def test_普通sleep恢复前取得新帧且不偷占前台(self):
        self.native.foreground = 20
        self.clock.after_wait = lambda: setattr(self.native, 'foreground', 10) if self.clock() >= 1001 else None
        self.executor.sleep(0.2)
        self.assertGreaterEqual(self.clock(), 1001.19)
        self.assertGreater(self.capture.count, 0)
        self.task.on_foreground_resume.assert_called_once()
        self.window.bring_to_front.assert_not_called()

    def test_正常新帧不释放仍有效的外层长按(self):
        self.executor.next_frame()
        self.adapter.send_key_down('w')
        self.executor.next_frame()
        self.assertEqual(len(self.core._held), 1)
        self.adapter.send_key_up('w')

    def test_窗口适配销毁后观察器清理且禁止新输入(self):
        self.adapter.send_key_down('w')
        self.window.stop_event.set()
        self.core.poll_foreground()
        self.assertFalse(self.core._held)
        with self.assertRaises(TaskDisabledException):
            self.adapter.send_key('a')

    def test_抓帧中换目标会丢弃旧代次(self):
        def change_once():
            self.window.foreground_target = ForegroundTarget(10, 2, 1)
            self.clock.after_wait = None

        self.clock.after_wait = change_once
        frame = self.executor.next_frame(time_out=1)
        self.assertEqual(self.capture.count, 2)
        self.assertEqual(int(frame[0, 0, 0]), 2)

    def test_嵌套任务恢复钩子全部清理后再抛轮转中断(self):
        inner = BaseTask(self.executor, self.task._app)
        inner.on_foreground_resume = Mock(side_effect=ForegroundInputInterrupted('旧轮转退出'))
        self.executor._notify_foreground_resume = True
        with self.executor.task_context(inner):
            with self.assertRaises(ForegroundInputInterrupted):
                self.executor.next_frame()
        self.task.on_foreground_resume.assert_called_once()
        inner.on_foreground_resume.assert_called_once()
        self.assertFalse(self.executor._notify_foreground_resume)
        self.assertFalse(self.executor._task_context_stack)

    def test_嵌套阶段反复中断仍保留作用域且只运行run一次(self):
        inner = BaseTask(self.executor, self.task._app)
        original_info = inner.info
        self.task.get_task_by_class = Mock(return_value=inner)
        inner.run = Mock(side_effect=ForegroundInputInterrupted('内层阶段中断'))
        calls = []

        def resume():
            self.assertIs(inner.info, self.task.info)
            self.assertIn(inner, self.executor._task_context_stack)
            calls.append('恢复')
            if len(calls) == 1:
                raise ForegroundInputInterrupted('恢复再次中断')

        inner.resume_after_foreground = Mock(side_effect=resume)
        self.executor.wait_foreground_frame = Mock()
        self.task.run_task_by_class(BaseTask)
        inner.run.assert_called_once()
        self.assertEqual(inner.resume_after_foreground.call_count, 2)
        self.assertEqual(self.executor.wait_foreground_frame.call_count, 2)
        self.assertIs(inner.info, original_info)
        self.assertFalse(self.executor._task_context_stack)

    def test_新绑定屏障在置前与输入准备之前完成(self):
        order = []
        self.executor.device_manager.wait_for_binding.side_effect = lambda **kwargs: order.append('绑定')
        self.window.bring_to_front.side_effect = lambda **kwargs: order.append('置前') or True
        self.assertTrue(self.executor.prepare_foreground(request_focus=True))
        self.assertEqual(order, ['绑定', '置前'])
        self.assertIsNone(self.executor._frame)

    def test_显式准备的期限共用于绑定与置前(self):
        self.executor.device_manager.wait_for_binding.side_effect = lambda **kwargs: self.clock.wait(3)
        with patch.object(self.adapter, 'prepare_foreground', return_value=True) as prepare:
            self.assertTrue(self.executor.prepare_foreground(request_focus=True, time_out=5))
        self.assertEqual(prepare.call_args.kwargs['time_out'], 2)

    def test_准备等待其它所有者时仍有期限并可取消(self):
        acquired, release = threading.Event(), threading.Event()

        def hold_lock():
            with self.core._ownership_lock:
                acquired.set()
                release.wait(1)

        worker = threading.Thread(target=hold_lock, daemon=True)
        worker.start()
        self.assertTrue(acquired.wait(1))
        try:
            self.assertFalse(self.adapter.prepare_foreground(request_focus=True, time_out=0))
            with self.assertRaises(TaskDisabledException):
                self.adapter.prepare_foreground(request_focus=True, cancel_check=lambda: True)
            self.window.bring_to_front.assert_not_called()
        finally:
            release.set()
            worker.join(timeout=1)

    def test_周期与普通排队准备不抢前台_显式MCP才请求一次(self):
        self.executor._prepare_task_for_run(is_trigger_task=True)
        self.executor._prepare_task_for_run(is_trigger_task=False)
        self.window.bring_to_front.assert_not_called()
        self.executor._prepare_task_for_run(is_trigger_task=False, request_focus=True)
        self.window.bring_to_front.assert_called_once()

    def make_start_controller(self):
        controller = StartController.__new__(StartController)
        controller.exit_event = self.executor.exit_event
        controller.start_timeout = 5
        controller.start_exe = False
        controller.check_auto_hdr = Mock()
        controller.check_gpu_driver_post_processing = Mock()
        self.executor.device_manager.do_refresh = Mock()
        self.executor.start = Mock()
        return controller

    def test_GUI仅触发任务总启动也经过绑定与真实置前准备(self):
        controller = self.make_start_controller()
        self.task.enable_after_start = True
        globals_stub = SimpleNamespace(executor=self.executor, device_manager=self.executor.device_manager, app=None)
        with patch('ok.core.start_controller.og', globals_stub), patch('ok.core.start_controller.communicate'):
            self.assertTrue(controller._do_start())
        self.executor.device_manager.wait_for_binding.assert_called_once()
        self.window.bring_to_front.assert_called_once()
        self.executor.start.assert_called_once()

    def test_人工恢复先绑定与置前确认再继续(self):
        controller = self.make_start_controller()
        self.task._paused = True
        order = []
        self.executor.device_manager.wait_for_binding.side_effect = lambda **kwargs: order.append('绑定')
        self.window.bring_to_front.side_effect = lambda **kwargs: order.append('置前') or True
        self.executor.start.side_effect = lambda: order.append('继续')
        globals_stub = SimpleNamespace(executor=self.executor, device_manager=self.executor.device_manager, app=None)
        with patch('ok.core.start_controller.og', globals_stub), patch('ok.core.start_controller.communicate'):
            self.assertTrue(controller._do_start(self.task))
        self.assertEqual(order, ['绑定', '置前', '继续'])

    def test_通知作业失焦延期但不阻塞只读作业(self):
        notification = ExecutorJob(self.executor, lambda: None, defer_while_unfocused=True)
        readonly = ExecutorJob(self.executor, lambda: None)
        self.executor._job_queue = [notification, readonly]
        self.native.foreground = 20
        self.assertIs(self.executor._pop_job(), readonly)
        self.assertIsNone(self.executor._pop_job())
        self.native.foreground = 10
        self.assertIs(self.executor._pop_job(), notification)

    def test_作业完成回调仅在持有清理后发布(self):
        job = ExecutorJob(self.executor, lambda: self.adapter.send_key_down('w'))
        states = []
        job.future.add_done_callback(lambda future: states.append(bool(self.core._held)))
        self.executor._run_job(job)
        self.assertEqual(states, [False])
        self.assertIsNone(self.executor.current_job)

    def test_作业结束清理失败不能发布成功(self):
        def hold_then_fail_cleanup():
            self.adapter.send_key_down('w')
            self.native.results.extend((0, 0))
            return True

        job = ExecutorJob(self.executor, hold_then_fail_cleanup)
        self.executor._run_job(job)
        with self.assertRaises(ForegroundInputError):
            job.future.result()
        self.assertTrue(self.core._held)
        self.native.results.clear()
        self.core.release_all()

    def test_作业取消暂停和停止统一释放(self):
        job = ExecutorJob(self.executor, lambda: None)
        job.future.set_running_or_notify_cancel()
        self.executor.current_job = job
        self.adapter.send_key_down('w')
        self.assertTrue(self.executor.cancel_job(job))
        self.assertFalse(self.core._held)
        self.executor.current_job = None
        self.adapter.refresh_target(force=True)
        self.adapter.mouse_down()
        self.executor.pause()
        self.assertFalse(self.core._held)
        self.executor.paused = False
        self.adapter.refresh_target(force=True)
        self.adapter.send_key_down('w')
        self.executor.stop()
        self.assertFalse(self.core._held)
        self.assertTrue(self.executor.exit_event.is_set())

    def test_观察线程遵守MCP作业忽略暂停但仍观察取消(self):
        self.executor.paused = True
        job = ExecutorJob(self.executor, lambda: None, ignore_pause=True)
        self.executor.current_job = job
        with patch.object(self.executor, 'is_executor_thread', return_value=False):
            self.adapter.send_key_down('w')
            self.core.poll_foreground()
            self.assertEqual(len(self.core._held), 1)
            job._cancel_requested.set()
            self.core.poll_foreground()
        self.assertFalse(self.core._held)

    def test_一次性逃逸中断走明确恢复点而不重跑run(self):
        self.executor._prepare_task_for_run = Mock()
        self.executor.next_task = Mock(return_value=(self.task, False, False))
        self.executor.destroy = Mock()
        self.task.run = Mock(side_effect=ForegroundInputInterrupted('受控中断'))

        def resume():
            self.executor.exit_event.set()

        self.task.resume_after_foreground = Mock(side_effect=resume)
        with patch('ok.task.TaskExecutor.prevent_sleeping'):
            self.executor.execute()
        self.task.run.assert_called_once()
        self.task.resume_after_foreground.assert_called_once()


class TestForegroundDeviceAndWindow(InputFixture):
    @contextmanager
    def fake_window_group(self):
        self.native.windows.update({
            10: (1, 10, 99), 11: (1, 10, 99), 20: (2, 20, 20),
            30: (1, 30, 30), 40: (3, 40, 99), 41: (4, 40, 99),
            60: (5, 60, 99), 99: (6, 99, 99),
        })
        state = SimpleNamespace(
            classes={10: 'GameWindow', 11: 'Button', 20: 'CLoginDlg', 30: 'CLoginDlg',
                     40: 'CLoginDlg', 41: 'Button', 60: 'CLoginDlg', 99: 'Launcher'},
            parents={11: 10, 41: 40}, owners={10: 99, 40: 10, 60: 99},
            bounds={hwnd: (-1800 + hwnd * 5, -100 + hwnd, 600, 400, 1.5)
                    for hwnd in self.native.windows},
            order=[60, 20, 40, 30, 10, 99],
        )
        state.bounds[10] = (-1920, -200, 1920, 1080, 1.5)
        state.bounds[11] = (-1900, -180, 80, 30, 1.5)
        state.bounds[41] = (-1500, 60, 80, 30, 1.5)
        self.native.owners = state.owners

        def enum_windows(callback, extra):
            for hwnd in state.order:
                callback(hwnd, extra)

        def get_window(hwnd, flag):
            if flag == window_module.win32con.GW_OWNER:
                return state.owners.get(hwnd, 0)
            if hwnd in state.order:
                index = state.order.index(hwnd) + 1
                return state.order[index] if index < len(state.order) else 0
            return 0

        def bounds(hwnd):
            x, y, width, height, scaling = state.bounds[hwnd]
            return x, y, width, height, width, height, scaling

        def client_to_screen(hwnd, point):
            return state.bounds[hwnd][0] + point[0], state.bounds[hwnd][1] + point[1]

        gui = {
            'IsWindow': lambda hwnd: hwnd in self.native.windows,
            'IsWindowVisible': lambda hwnd: hwnd in self.native.windows,
            'IsWindowEnabled': lambda hwnd: hwnd in self.native.windows,
            'GetClassName': lambda hwnd: state.classes[hwnd],
            'GetWindowText': lambda hwnd: '受控游戏' if hwnd == 10 else '受控登录窗',
            'EnumWindows': enum_windows,
            'GetTopWindow': lambda hwnd: state.order[0] if hwnd == 0 else {10: 11, 40: 41}.get(hwnd, 0),
            'GetWindow': get_window,
            'GetParent': lambda hwnd: state.parents.get(hwnd, 0),
            'ClientToScreen': client_to_screen,
            'GetClientRect': lambda hwnd: (0, 0, state.bounds[hwnd][2], state.bounds[hwnd][3]),
        }
        with ExitStack() as stack:
            for name, replacement in gui.items():
                stack.enter_context(patch.object(window_module.win32gui, name, replacement))
            stack.enter_context(patch.object(window_module.win32process, 'GetWindowThreadProcessId',
                                            lambda hwnd: (1, self.native.process_id(hwnd))))
            stack.enter_context(patch.object(window_module, 'get_window_bounds', bounds))
            stack.enter_context(patch('ok.device.capture_methods.hwnd_window.get_window_bounds', bounds))
            stack.enter_context(patch.object(window_module, 'get_exe_by_hwnd', lambda hwnd:
                                            ('fake.exe', 'C:/game/fake.exe', [])))
            yield state

    def discovered_window(self):
        classes = ['CLoginDlg', 'Button']
        result = window_module.find_hwnd('受控游戏', ['fake.exe'], 1920, 1080,
                                         class_name='GameWindow', selected_hwnd=10,
                                         top_hwnd_class=classes)
        window = self.make_window()
        window.hwnd = result[1]
        window.hwnd_class = 'GameWindow'
        window.top_hwnd_class = classes
        window.hwnds = result[7]
        window.top_hwnd = result[7][0][0] if result[7] else 0
        return window

    def test_真实发现仅许可选中实例与直接归属窗口(self):
        with self.fake_window_group() as state:
            window = self.discovered_window()
            # 20 是同目录另一实例，60 与主窗只有共同启动器；均不应收到任何新事件。
            for hwnd, allowed in ((20, False), (60, False), (30, True), (40, True), (41, True), (10, True)):
                with self.subTest(hwnd=hwnd):
                    self.native.foreground = hwnd
                    session = self.core.session(f'窗口组-{hwnd}', lambda: window.foreground_target)
                    before = len(self.native.inserted)
                    if allowed:
                        session.send_key('w', down_time=0)
                    else:
                        with self.assertRaises(ForegroundInputInterrupted):
                            session.send_key('w', down_time=0)
                    self.assertEqual(len(self.native.inserted) - before, 2 if allowed else 0)
            permitted = {10, 30, 40, 41}
            self.assertEqual({info[0] for info in window.hwnds}, permitted)
            self.assertEqual(set(window.foreground_target.allowed_hwnds), permitted)
            self.assertTrue(set(window._front_hwnd_candidates()) <= permitted)
            state.classes[60] = '其他独立根窗口'
            self.native.windows[60] = (1, 60, 99)
            self.native.foreground = 60
            session = self.core.session('同进程共同启动器', lambda: window.foreground_target)
            before = len(self.native.inserted)
            with self.assertRaises(ForegroundInputInterrupted):
                session.send_key('w', down_time=0)
            self.assertEqual(len(self.native.inserted), before)
            self.assertNotIn(60, window.foreground_target.allowed_hwnds)

    def test_缓存身份失效与子窗移动会释放旧持有并拒绝新输入(self):
        for change in ('登录PID重用', '子窗类重用', '父窗销毁', '主窗PID重用', '子窗移动'):
            with self.subTest(change=change), self.fake_window_group() as state:
                window = self.discovered_window()
                hwnd = 30 if change == '登录PID重用' else 41
                self.native.foreground = hwnd
                session = self.core.session(change, lambda: window.foreground_target)
                session.key_down('ctrl')
                before = window.foreground_target
                count = len(self.native.inserted)
                if change == '登录PID重用':
                    self.native.windows[30] = (2, 30, 30)
                elif change == '子窗类重用':
                    state.classes[41] = '其他控件'
                elif change == '父窗销毁':
                    del self.native.windows[40]
                elif change == '主窗PID重用':
                    self.native.windows[10] = (7, 10, 99)
                else:
                    x, y, width, height, scaling = state.bounds[41]
                    state.bounds[41] = (x + 100, y + 20, width, height, scaling)
                after = window.foreground_target
                self.assertNotEqual(before.generation, after.generation)
                with self.assertRaises(ForegroundInputInterrupted):
                    session.send_key('a', down_time=0)
                self.assertEqual(self.native.inserted[count:], [InputEvent('keyboard', 0x1D, 8, up=True)])
                self.assertFalse(self.core._held)
                if change == '子窗移动':
                    self.assertNotEqual(next(info[4:6] for info in window.hwnds if info[0] == 41),
                                        (-1500, 60))
                    session.acknowledge()
                    session.send_key('a', down_time=0)
                else:
                    self.assertNotIn(hwnd, after.allowed_hwnds)
                    self.assertNotIn(hwnd, [info[0] for info in window.hwnds])
                    self.assertNotIn(hwnd, window._front_hwnd_candidates())
                    fresh = self.core.session('重新读取但尚未重新绑定', lambda: window.foreground_target)
                    with self.assertRaises(ForegroundInputInterrupted):
                        fresh.send_key('a', down_time=0)

    def test_PC配置归一化覆盖旧名称旧类列表与未知拒绝(self):
        known = ('PostMessage', 'Genshin', 'ForegroundPostMessage', 'PyDirect', 'Pynput')
        for name in known:
            with self.subTest(name=name):
                self.assertEqual(normalize_pc_interaction(name), 'Pynput')
                self.assertEqual(normalize_pc_interaction(type(name + 'Interaction', (), {})), 'Pynput')
        self.assertEqual(normalize_pc_interaction(list(known)), 'Pynput')
        for value in ('未知方案', type('自定义后台', (), {}), ['Pynput', '未知方案']):
            with self.assertRaises(ValueError):
                normalize_pc_interaction(value)

    def test_构造保存迁移不会实例化旧后端或读写用户配置(self):
        config = {'interaction': 'PostMessageInteraction'}
        with patch('ok.device.DeviceManager.Config', return_value=config), \
                patch('ok.device.DeviceManager.Handler', return_value=Mock()), \
                patch('ok.device.DeviceManager.HwndWindow', return_value=Mock()), \
                patch('ok.device.interaction_methods.foreground_input._shared_input', self.core):
            manager = DeviceManager({'windows': {'interaction': ['PostMessage', 'PyDirect']}}, threading.Event())
        self.assertIs(manager.win_interaction_class, PynputInteraction)
        self.assertEqual(manager.windows_capture_config['interaction'], 'Pynput')
        self.assertEqual(config['interaction'], 'Pynput')
        self.assertEqual(manager.available_interaction_methods({'device': 'windows'}), ['Pynput'])

    def test_setter与动态入口同样拒绝未知而不先改变设备(self):
        manager = DeviceManager.__new__(DeviceManager)
        manager.windows_capture_config = {'interaction': 'Pynput'}
        manager.config = {'interaction': 'PostMessage', 'preferred': 'pc'}
        manager.device_dict = {'pc': {'device': 'windows'}}
        manager.win_interaction_class = object
        manager.start = Mock()
        old = Mock(spec=BaseInteraction)
        manager.interaction = old
        manager.set_interaction('GenshinInteraction')
        self.assertIs(manager.win_interaction_class, PynputInteraction)
        self.assertEqual(manager.config['interaction'], 'Pynput')
        old.release_all.assert_called_once()
        manager.clear_devices = Mock()
        for method in (manager.set_interaction,):
            with self.assertRaises(ValueError):
                method('未知输入')
        for method in (manager.ensure_capture, manager.update_capture):
            with self.assertRaises(ValueError):
                method({'windows': {'interaction': '未知输入'}})
        manager.clear_devices.assert_not_called()

    def test_替换输入与捕获对象先清旧持有(self):
        manager = DeviceManager.__new__(DeviceManager)
        old = Mock(spec=BaseInteraction)
        manager.interaction = old
        manager.capture_method = object()
        manager.capture_method = object()
        manager.interaction = Mock(spec=BaseInteraction)
        self.assertEqual(old.release_all.call_count, 2)

    def test_异步绑定屏障超时取消与完成(self):
        manager = DeviceManager.__new__(DeviceManager)
        manager._binding_condition = threading.Condition()
        manager._binding_requested = manager._binding_completed = 0
        manager._binding_error = None
        manager.exit_event = threading.Event()
        manager.handler = Mock()
        manager.interaction = None
        manager.start()
        with self.assertRaises(TimeoutError):
            manager.wait_for_binding(time_out=0.005)
        with self.assertRaises(TaskDisabledException):
            manager.wait_for_binding(cancel_check=lambda: True)
        manager.do_start = lambda: setattr(manager, 'interaction', Mock(spec=BaseInteraction))
        manager.handler.post.call_args.args[0]()
        self.assertIs(manager.wait_for_binding(), manager.interaction)

    def make_window(self):
        window = HwndWindow.__new__(HwndWindow)
        hwnds = [(10, 'fake.exe', 1920, 1080, -1920, -200, '受控游戏', 'GameWindow', 1.5),
                 (11, 'fake.exe', 80, 30, -1900, -180, '受控控件', 'Button', 1.5)]
        window.__dict__.update(hwnd=10, top_hwnd=10, x=-1920, y=-200, scaling=1.5,
                               width=1600, height=900, client_width=1920, client_height=1080,
                               real_x_offset=100, real_y_offset=80, real_width=1600, real_height=900,
                               hwnds=hwnds, _hwnd_group=(tuple(hwnds), {10: (1, 'GameWindow'), 11: (1, 'Button')}),
                               frame_width=0, frame_height=0,
                               app_exit_event=threading.Event(), stop_event=threading.Event(),
                               title='受控游戏', exe_names=['fake.exe'], player_id=-1,
                               hwnd_class=None, top_hwnd_class=None)
        return window

    def test_捕获公共原点包含裁剪DPI与多屏负坐标(self):
        window = self.make_window()
        capture = BaseWindowsCaptureMethod(window)
        self.assertEqual(capture.get_abs_cords(30, 40), (-1790, -80))
        window.real_x_offset = window.real_y_offset = 0
        with patch('ok.device.capture_methods.hwnd_window.get_crop_point', return_value=(160, 90)):
            self.assertEqual(capture.get_abs_cords(0, 0), (-1760, -110))

    def test_真实窗口几何变化即使缓存未刷新也改变目标代次(self):
        window = self.make_window()
        with self.fake_window_group() as state:
            before = window.foreground_target
            state.bounds[10] = (-1600, 10, 1920, 1080, 1.5)
            after = window.foreground_target
        self.assertNotEqual(before.generation, after.generation)
        self.assertEqual(before.allowed_hwnds, (10, 11))

    def test_相同窗口刷新不清空绑定句柄(self):
        window = self.make_window()
        window.do_update_window_size = Mock()
        window.update_window('受控游戏', ['fake.exe'])
        self.assertEqual(window.hwnd, 10)
        window.do_update_window_size.assert_not_called()

    def test_置前API返回不代表获得真实前台且只请求一次(self):
        window = self.make_window()
        window.is_foreground = Mock(return_value=False)
        window.do_update_window_size = Mock()
        with self.fake_window_group(), \
                patch('ok.device.capture_methods.hwnd_window.win32gui.IsIconic', return_value=False), \
                patch('ok.device.capture_methods.hwnd_window.win32gui.BringWindowToTop'), \
                patch('ok.device.capture_methods.hwnd_window.win32gui.SetForegroundWindow') as set_front:
            self.assertFalse(window.bring_to_front(timeout=0))
            set_front.assert_called_once()

    def test_布尔取消在准备与等待阶段阻止置前或继续等待(self):
        # 通知恢复回调返回布尔值，用户切走后的 True 必须停止这次交接。
        for phase, checks, requested in (
                ('入口', [True], False),
                ('请求前', [False, True], False),
                ('等待', [False, False, True], True)):
            with self.subTest(phase=phase):
                window = self.make_window()
                window.is_foreground = Mock(return_value=False)
                window.do_update_window_size = Mock()
                cancelled = Mock(side_effect=checks)
                with self.fake_window_group(), \
                        patch('ok.device.capture_methods.hwnd_window.win32gui.IsIconic', return_value=False), \
                        patch('ok.device.capture_methods.hwnd_window.win32gui.BringWindowToTop') as bring_top, \
                        patch('ok.device.capture_methods.hwnd_window.win32gui.SetForegroundWindow') as set_front, \
                        patch.object(window.app_exit_event, 'wait') as wait:
                    self.assertFalse(window.bring_to_front(timeout=0, cancel_check=cancelled))
                    self.assertEqual(set_front.call_count, int(requested))
                    self.assertEqual(bring_top.call_count, int(requested))
                    self.assertEqual(cancelled.call_count, len(checks))
                    wait.assert_not_called()


class TestMcpForegroundPreparation(InputFixture):
    def make_server(self, outcome='回退成功'):
        manager = self.manager = DeviceManager.__new__(DeviceManager)
        manager.__dict__.update(
            _binding_lock=threading.RLock(), _binding_condition=threading.Condition(),
            _binding_requested=0, _binding_completed=0, _binding_error=None,
            exit_event=threading.Event(), global_config=None, executor=None, hwnd_window=FakeWindow(),
            windows_capture_config={'interaction': 'Pynput', 'title': '受控窗口',
                                    'capture_method': ['WGC', 'BitBlt_RenderFull']},
            config={'preferred': '', 'capture': 'WGC', 'interaction': 'Pynput'}, device_dict={},
        )
        manager.interaction = manager.capture_method = None
        capture = FakeCapture(manager.hwnd_window, self.clock)
        self.binding_entered, self.binding_release = threading.Event(), threading.Event()
        self.binding_threads, self.binding_errors = [], []
        self.addCleanup(self.binding_release.set)

        def post(fn, **kwargs):
            def bind():
                try:
                    fn()
                except Exception as error:
                    self.binding_errors.append(error)

            worker = threading.Thread(target=bind, daemon=True)
            self.binding_threads.append(worker)
            worker.start()

        def enumerate_target():
            manager.device_dict['pc'] = {'imei': 'pc', 'device': 'windows', 'connected': True, 'nick': '受控窗口'}

        def wgc_attempt(*args, **kwargs):
            self.binding_entered.set()
            if not self.binding_release.wait(1):
                raise TimeoutError('受控绑定没有完成')
            # 模拟 OS 拒绝 WGC 后进入项目已有的下一捕获候选。
            return None

        def binding_failure(*args, **kwargs):
            self.binding_entered.set()
            if not self.binding_release.wait(1):
                raise TimeoutError('受控绑定没有完成')
            raise OSError('受控窗口绑定初始化异常')

        manager.handler = SimpleNamespace(post=post)
        manager.refresh_emulators = manager.refresh_phones = manager.update_browser_device = Mock()
        manager.update_pc_device = Mock(side_effect=enumerate_target)
        manager.ensure_hwnd = Mock(side_effect=binding_failure if outcome == '绑定异常' else None)
        for scoped_patch in (
                patch('ok.device.DeviceManager.communicate'),
                patch('ok.device.interaction_methods.foreground_input._shared_input', self.core),
                patch('ok.device.interaction_methods.pynput.get_shared_foreground_input', return_value=self.core),
                patch('ok.device.capture_methods.update.get_win_graphics_capture', side_effect=wgc_attempt),
                patch('ok.device.capture_methods.update.get_capture', return_value=capture if outcome == '回退成功' else None),
                patch('ok.device.capture_methods.bitblt.render_full', False)):
            scoped_patch.start()
            self.addCleanup(scoped_patch.stop)
        executor = SimpleNamespace(check_enabled=Mock(return_value=None), feature_set=None)
        controller = self.controller = StartController.__new__(StartController)
        controller.start_exe, controller.start_timeout = False, 1
        controller.start_device = Mock()
        controller.try_capture_a_frame = Mock(side_effect=lambda: manager.capture_method.get_frame())
        controller.check_resolution = Mock(return_value=None)
        controller.tr = lambda message: message
        controller.check_device_error = Mock(wraps=controller.check_device_error)
        scoped_patch = patch('ok.core.start_controller.og', SimpleNamespace(device_manager=manager, executor=executor))
        scoped_patch.start()
        self.addCleanup(scoped_patch.stop)
        server = LocalMcpServer.__new__(LocalMcpServer)
        server.runtime = SimpleNamespace(task_executor=executor, device_manager=manager)
        server.runner = SimpleNamespace(app=SimpleNamespace(start_controller=controller))
        return server

    def prepare_in_thread(self, server):
        done, errors = threading.Event(), []

        def prepare():
            try:
                server._prepare_device()
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=prepare, daemon=True)
        worker.start()
        self.addCleanup(lambda: worker.join(timeout=1))
        self.assertTrue(self.binding_entered.wait(1))
        return worker, done, errors

    def finish_threads(self, worker):
        self.binding_release.set()
        worker.join(timeout=1)
        for binding in self.binding_threads:
            binding.join(timeout=1)
        self.assertFalse(worker.is_alive())

    def test_MCP无启动分支等延迟回退完成再诊断且不置前(self):
        server = self.make_server()
        worker, done, errors = self.prepare_in_thread(server)
        try:
            self.assertIsNone(self.manager.capture_method)
            self.assertFalse(done.wait(0.01))
            self.controller.check_device_error.assert_not_called()
        finally:
            self.finish_threads(worker)
        self.assertFalse(errors)
        self.assertFalse(self.binding_errors)
        self.assertIsNotNone(self.manager.capture_method)
        self.controller.check_device_error.assert_called_once()
        self.controller.start_device.assert_not_called()
        self.manager.hwnd_window.bring_to_front.assert_not_called()
        self.assertFalse(self.native.inserted)

    def test_MCP等绑定完成后仍保留真实捕获不可用错误(self):
        server = self.make_server(outcome='全部捕获不可用')
        worker, done, errors = self.prepare_in_thread(server)
        self.finish_threads(worker)
        self.assertTrue(done.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn('Selected capture method', str(errors[0]))
        self.controller.check_device_error.assert_called_once()
        self.assertFalse(self.native.inserted)

    def test_MCP绑定异常先传播而不误诊为暂未就绪(self):
        server = self.make_server(outcome='绑定异常')
        worker, done, errors = self.prepare_in_thread(server)
        self.finish_threads(worker)
        self.assertTrue(done.is_set())
        self.assertEqual(len(errors), 1)
        self.assertEqual(str(errors[0]), '设备绑定失败')
        self.assertIsInstance(errors[0].__cause__, OSError)
        self.controller.check_device_error.assert_not_called()
        self.assertFalse(self.native.inserted)

    def test_MCP等待首次绑定时仍可取消(self):
        server = self.make_server()
        cancelled = threading.Event()
        server.runtime.task_executor.check_enabled.side_effect = (
            lambda **kwargs: (_ for _ in ()).throw(TaskDisabledException()) if cancelled.is_set() else None)
        worker, done, errors = self.prepare_in_thread(server)
        try:
            cancelled.set()
            self.assertTrue(done.wait(1))
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], TaskDisabledException)
            self.controller.check_device_error.assert_not_called()
        finally:
            self.finish_threads(worker)
        self.assertFalse(self.native.inserted)


class TestRunnerIsolation(unittest.TestCase):
    def test_隔离入口保留失败码并拒绝后台异常(self):
        project_root = Path(__file__).resolve().parents[1]
        source = '''import os
import threading
import unittest


class RunnerProbe(unittest.TestCase):
    def test_result(self):
        mode = os.environ['NONO_RUNNER_PROBE_CASE']
        if mode == 'assertion':
            self.fail('预期的主线程断言失败')
        if mode in ('thread', 'late_thread'):
            def worker():
                if mode == 'late_thread':
                    threading.main_thread().join()
                raise RuntimeError('预期的后台线程异常')
            thread = threading.Thread(target=worker)
            thread.start()
            if mode == 'thread':
                thread.join()
'''
        with tempfile.TemporaryDirectory(prefix='NoNo-入口回归-') as directory:
            (Path(directory) / 'isolated_runner_probe.py').write_text(source, encoding='utf-8')
            environment = os.environ.copy()
            environment['PYTHONPATH'] = directory
            environment['QT_QPA_PLATFORM'] = 'offscreen'
            for mode, expected in (('normal', 0), ('assertion', 1), ('thread', 1), ('late_thread', 1)):
                with self.subTest(mode=mode):
                    environment['NONO_RUNNER_PROBE_CASE'] = mode
                    result = subprocess.run(
                        [sys.executable, '-X', 'utf8', '-B', '-u',
                         str(project_root / 'run_test.py'), 'isolated_runner_probe'],
                        cwd=project_root, env=environment, capture_output=True,
                        text=True, encoding='utf-8', timeout=30,
                    )
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, expected, output)
                    self.assertIn('Ran 1 test', output)
                    if mode in ('thread', 'late_thread'):
                        # 主线程用例仍成功，失败必须来自实际后台异常及完整子进程收尾。
                        self.assertIn('\nOK\n', output)
                        self.assertIn('Exception in thread', output)


if __name__ == '__main__':
    unittest.main()
