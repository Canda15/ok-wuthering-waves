"""一次外部开始／恢复操作的激活额度，与设备后端生命周期分离。"""

import threading
import uuid


class StartRequest:
    def __init__(self, source='开始按钮'):
        self.request_id = uuid.uuid4().hex
        self.source = source
        self.activation_attempted = False
        self.activation_accepted = False
        self.target_identity = None
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def cancel(self):
        self._cancelled.set()

    def check_cancelled(self):
        if self.cancelled:
            from ok.task.exceptions import TaskDisabledException
            raise TaskDisabledException()

    def activate_once(self, identity, send_callback):
        """成功只表示系统接收入队；失败同样消耗额度，不能由重绑补发。"""
        with self._lock:
            self.check_cancelled()
            if self.activation_attempted:
                if identity != self.target_identity:
                    raise RuntimeError('开始请求的游戏目标已变化，请重新开始任务')
                if not self.activation_accepted:
                    raise RuntimeError('本次开始请求的激活已失败，请重新开始任务')
                return False
            # 先记尝试再调用系统，避免异常或后端重建让同一操作获得第二次额度。
            self.activation_attempted = True
            self.target_identity = identity
            send_callback()
            self.activation_accepted = True
            return True
