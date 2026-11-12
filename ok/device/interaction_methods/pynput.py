"""公共任务坐标到共享前台系统输入的薄适配。"""

from dataclasses import replace
import time

from pynput.mouse import Button

from ok.device.capture_methods.base import BaseCaptureMethod
from ok.device.interaction_methods.base import BaseInteraction
from ok.device.interaction_methods.foreground_input import (
    ForegroundInputInterrupted, KEY_ALIASES, get_shared_foreground_input,
)
from ok.task.exceptions import TaskDisabledException


class PynputInteraction(BaseInteraction):
    KEY_MAP = KEY_ALIASES

    def __init__(self, capture: BaseCaptureMethod, hwnd_window):
        super().__init__(capture)
        self.hwnd_window = hwnd_window
        self._input = get_shared_foreground_input().session(
            ('game', id(self)), self._target, self._check_cancel)

    def _target(self):
        target = self.hwnd_window.foreground_target
        return replace(target, generation=(id(self.capture), target.generation))

    @property
    def check_clickable(self):
        return True

    @check_clickable.setter
    def check_clickable(self, value):
        if not value:
            raise ValueError('本应用前台输入不能关闭焦点检查')

    def _check_cancel(self):
        exit_event = getattr(self.hwnd_window, 'app_exit_event', None)
        stop_event = getattr(self.hwnd_window, 'stop_event', None)
        if (exit_event is not None and exit_event.is_set()) or (stop_event is not None and stop_event.is_set()):
            raise TaskDisabledException()
        manager = getattr(self.hwnd_window, 'device_manager', None)
        executor = getattr(manager, 'executor', None)
        if executor is not None:
            executor.check_input_cancelled()

    def clickable(self):
        return self._input.is_foreground()

    @property
    def target_generation(self):
        return self._input.target().generation

    @property
    def needs_refresh(self):
        return self._input._interrupted is not None or self._input._expected != self._input.target()

    def refresh_target(self, force=False):
        self.hwnd_window.do_update_window_size()
        if not self.clickable():
            return False
        if force or self.needs_refresh:
            self._input.activate()
            self._input.acknowledge()
        return True

    def prepare_foreground(self, request_focus=False, time_out=5, cancel_check=None):
        # GUI 准备与通知交接串行，绑定准备也不能在通知会话中途抢窗口。
        deadline = time.monotonic() + max(0, time_out)
        lock = self._input.core._ownership_lock
        while True:
            if self.hwnd_window.app_exit_event.is_set() or cancel_check is not None and cancel_check():
                raise TaskDisabledException()
            remaining = max(0, deadline - time.monotonic())
            if lock.acquire(timeout=min(0.05, remaining)):
                break
            if remaining <= 0:
                return False
        try:
            self._input.activate()
            self.hwnd_window.do_update_window_size()
            remaining = max(0, deadline - time.monotonic())
            if request_focus and not self.hwnd_window.bring_to_front(timeout=remaining, cancel_check=cancel_check):
                return False
            return self.refresh_target(force=True)
        finally:
            lock.release()

    def _point(self, x, y):
        if x == -1 and y == -1:
            return None, None
        return self.capture.get_abs_cords(x, y)

    def send_key(self, key, down_time=0.01):
        return self._input.send_key(key, down_time)

    def send_key_down(self, key):
        return self._input.key_down(key)

    def send_key_up(self, key):
        return self._input.key_up(key)

    def scroll(self, x, y, scroll_amount):
        abs_x, abs_y = self.capture.get_abs_cords(x, y)
        return self._input.scroll(scroll_amount, x=abs_x, y=abs_y)

    def move(self, x, y):
        return self._input.move(*self.capture.get_abs_cords(x, y))

    def swipe(self, x1, y1, x2, y2, duration, settle_time=0):
        x1, y1 = self.capture.get_abs_cords(x1, y1)
        x2, y2 = self.capture.get_abs_cords(x2, y2)
        # BaseTask 保留既有毫秒适配签名，此处仅转换一次为核心的秒。
        return self._input.swipe(x1, y1, x2, y2, duration / 1000, settle_time=settle_time)

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=False, key='left'):
        super().click(x, y, name=name)
        return self._input.click(*self._point(x, y), key=key, down_time=down_time, move_back=move_back)

    def mouse_down(self, x=-1, y=-1, name=None, key='left'):
        button = self.get_mouse_button(key)
        abs_x, abs_y = self._point(x, y)
        if abs_x is not None:
            self._input.move(abs_x, abs_y)
        return self._input.key_down(button)

    def get_mouse_button(self, key):
        return {1: Button.left, 2: Button.right, 3: Button.middle,
                4: Button.x1, 5: Button.x2}[self._input.core._button(key).code]

    def mouse_up(self, key='left'):
        return self._input.key_up(self.get_mouse_button(key))

    def input_text(self, text):
        return self._input.input_text(text)

    def release_all(self):
        return self._input.release_all()

    def should_capture(self):
        return self.clickable()

    def on_run(self):
        if not self.prepare_foreground(request_focus=True):
            raise ForegroundInputInterrupted('无法取得目标前台，请等待并重新识别')

    def on_destroy(self):
        self.release_all()
