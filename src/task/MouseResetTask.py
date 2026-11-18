"""后台鼠标保护的开关与轮询；光标采样和恢复由后台输入后端负责。"""

import threading
from functools import partial

from ok import Logger, TriggerTask
from ok.device.interaction_methods.post_message_test import PostMessageTestInteraction
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)


class MouseResetTask(TriggerTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_config = {'_enabled': True}
        self.trigger_interval = 1
        # 保留原版翻译键与配置开关，已有语言包和用户偏好继续生效。
        self.name = "🖱️ Prevent Wuthering Waves from moving the mouse"
        self.description = "Turn on if you mouse jumps around"
        self.icon = task_icon('settings')
        self._mouse_reset_lock = threading.RLock()
        self._mouse_reset_generation = 0
        self._mouse_reset_callback = None
        self._mouse_reset_interaction = None
        self._mouse_reset_destroyed = False
        self._mouse_reset_error_reported = False

    def _valid(self, generation, interaction):
        return (not self._mouse_reset_destroyed and self.enabled
                and generation == self._mouse_reset_generation
                and self.executor.interaction is interaction)

    def _stop_mouse_reset(self):
        self._mouse_reset_generation += 1
        self._mouse_reset_callback = None
        self._mouse_reset_error_reported = False
        if self._handler is not None:
            self._handler.stop()
            self._handler = None
        if self._mouse_reset_interaction is not None:
            self._mouse_reset_interaction.clear_mouse_reset_state()
            self._mouse_reset_interaction = None

    def enable(self):
        super().enable()
        self.run()

    def disable(self):
        # 先让正在执行的采样失效，关闭完成后不能再写回系统光标。
        self._enabled = False
        with self._mouse_reset_lock:
            self._stop_mouse_reset()
        super().disable()

    def run(self):
        with self._mouse_reset_lock:
            interaction = self.executor.interaction
            if (not self.enabled or self._mouse_reset_destroyed
                    or not isinstance(interaction, PostMessageTestInteraction)):
                self._stop_mouse_reset()
                return
            if self._mouse_reset_callback is not None and self._mouse_reset_interaction is interaction:
                return
            self._stop_mouse_reset()
            self._mouse_reset_interaction = interaction
            self._mouse_reset_callback = partial(self.mouse_reset, self._mouse_reset_generation)
            self.handler.post(self._mouse_reset_callback, 0.02, remove_existing=True)

    def mouse_reset(self, generation):
        with self._mouse_reset_lock:
            # 旧处理线程可能已经取出回调；重启后的代次防止它续接新的轮询。
            if generation != self._mouse_reset_generation:
                return
            interaction = self._mouse_reset_interaction
            if not self._valid(generation, interaction):
                self._stop_mouse_reset()
                return
            try:
                restored = interaction.poll_mouse_reset(lambda: self._valid(generation, interaction))
                self._mouse_reset_error_reported = False
                delay = 1 if restored else 0.02
            except Exception as error:
                if not self._mouse_reset_error_reported:
                    logger.error('后台鼠标保护检查失败，稍后重新采样', error)
                    self._mouse_reset_error_reported = True
                delay = 1
            if self._valid(generation, interaction):
                self.handler.post(self._mouse_reset_callback, delay, remove_existing=True)
            else:
                self._stop_mouse_reset()

    def on_destroy(self):
        self._mouse_reset_destroyed = True
        with self._mouse_reset_lock:
            self._stop_mouse_reset()
        super().on_destroy()
