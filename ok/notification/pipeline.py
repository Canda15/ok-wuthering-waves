import queue
import threading
import time
from dataclasses import dataclass

from ok.util.logger import Logger

logger = Logger.get_logger(__name__)


@dataclass(frozen=True)
class NotificationRequest:
    title: str
    message: str
    images: list


class NotificationPipeline:
    """一个固定消费者按顺序处理请求，统一节流与退出排空。"""

    def __init__(self, handler, exit_event=None, interval=5, worker_name='notifications'):
        self.handler = handler
        self.exit_event = exit_event
        self.interval = interval
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self._stop_lock = threading.Lock()
        if exit_event is not None:
            if hasattr(exit_event, 'bind_stop'):
                exit_event.bind_stop(self)
            elif hasattr(exit_event, 'bind_queue'):
                exit_event.bind_queue(self.queue)
        self.thread = threading.Thread(
            target=self._worker, name=worker_name, daemon=True)
        self.thread.start()

    def submit(self, title, message, images):
        with self._stop_lock:
            if self._should_stop():
                return False
            self.queue.put(NotificationRequest(title, message, images))
        return True

    def _should_stop(self):
        return (self.stop_event.is_set()
                or (self.exit_event is not None and self.exit_event.is_set()))

    def stop(self, wait=False, timeout=5):
        with self._stop_lock:
            if not self.stop_event.is_set():
                self.stop_event.set()
                self.queue.put(None)
        if wait and threading.current_thread() is not self.thread:
            self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    def _worker(self):
        last_sent = 0.0
        try:
            while not self._should_stop():
                try:
                    item = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    if item is None:
                        break
                    deadline = last_sent + self.interval
                    while not self._should_stop():
                        delay = deadline - time.monotonic()
                        if delay <= 0:
                            break
                        self.stop_event.wait(min(.1, delay))
                    if self._should_stop():
                        break
                    self.handler(item.title, item.message, item.images)
                except Exception as e:
                    logger.error('通知处理失败，本条不自动重发', e)
                finally:
                    last_sent = time.monotonic()
                    self.queue.task_done()
        finally:
            # 退出与提交共用锁，避免结束后仍留下没有消费者的请求。
            with self._stop_lock:
                self.stop_event.set()
                while True:
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:
                        break
                    self.queue.task_done()
