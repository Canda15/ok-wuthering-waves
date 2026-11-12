import itertools
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager

from ok.device.interaction_methods.foreground_input import (
    ForegroundInputError, ForegroundInputInterrupted, release_shared_foreground_input,
)
from ok.device.interaction_methods.pynput import PynputInteraction

from ok.core.events import communicate
from ok.core.notifications import alert_info
from ok.task.exceptions import FinishedException, TaskDisabledException, WaitFailedException, CaptureException, \
    HotkeyConfigException, ExecutorBusyException
from ok.util.GlobalConfig import basic_options
from ok.util.logger import Logger, config_logger
from ok.util.process import is_cuda_12_or_above, prevent_sleeping
from ok.util.window import ratio_text_to_number

logger = Logger.get_logger(__name__)

# How long next_frame() waits for another caller to finish its capture request.
NEXT_FRAME_BUSY_WAIT = 0.1


class ExecutorJob:
    """A callable that runs once on the executor thread, outside the task queue.

    ``future`` resolves to the callable's return value, or raises what it
    raised. A job cancelled while running raises TaskDisabledException at its
    next sleep, frame request or wait.
    """
    _ids = itertools.count(1)

    def __init__(self, executor, fn, name=None, owner=None, capture=None, interaction=None, ignore_pause=True,
                 defer_while_unfocused=False):
        self.id = next(self._ids)
        self.executor = executor
        self.fn = fn
        self.name = name or getattr(fn, '__name__', 'job')
        self.owner = owner
        self.capture = capture
        self.interaction = interaction
        self.ignore_pause = ignore_pause
        self.defer_while_unfocused = defer_while_unfocused
        self.future = Future()
        self._cancel_requested = threading.Event()

    @property
    def cancel_requested(self):
        return self._cancel_requested.is_set()

    def cancel(self):
        return self.executor.cancel_job(self)

    def __repr__(self):
        return f'ExecutorJob({self.id}, {self.name}, owner={self.owner})'


class TaskExecutor:
    _frame: object
    paused: bool
    pause_start: float
    pause_end_time: float
    _last_frame_time: float
    wait_until_timeout: float
    device_manager: object
    feature_set: object
    wait_until_settle_time: float
    wait_scene_timeout: float
    exit_event: object
    debug_mode: bool
    debug: bool
    global_config: object
    _ocr_lib: dict
    ocr_target_height: int
    current_task: object
    config_folder: str
    trigger_task_index: int
    trigger_tasks: list
    onetime_tasks: list
    onetime_task_queue: list
    thread: object
    locale: object
    scene: object
    text_fix: dict
    ocr_po_translation: object
    config: object
    basic_options: object
    lock: object
    _ocr_lib_lock: object
    _ocr_init_thread: object

    def __init__(self, device_manager,
                 wait_until_timeout=10, wait_until_settle_time=-1,
                 exit_event=None, feature_set=None,
                 ocr_lib=None,
                 config_folder=None, debug=False, global_config=None, ocr_target_height=0, config=None):
        self._frame = None
        device_manager.executor = self
        self.pause_start = time.time()
        self.pause_end_time = time.time()
        self._last_frame_time = 0
        self._frame_target_generation = None
        self._foreground_waiting = False
        self._focus_wait_started = None
        self._focus_wait_total = 0.0
        self._needs_foreground_frame = False
        self._notify_foreground_resume = False
        self._snapshot_enabled = False
        self._frame_snapshot = None
        self.paused = True
        self.config = config
        self.scene = None
        self.locale = config.get("locale", "en_US")
        self.text_fix = {}
        self.ocr_po_translation = None
        self.load_tr()
        self.ocr_target_height = ocr_target_height
        self.device_manager = device_manager
        self.feature_set = feature_set
        self.wait_until_settle_time = wait_until_settle_time
        self.wait_scene_timeout = wait_until_timeout
        self.exit_event = exit_event
        self.debug_mode = False
        self.debug = debug
        self.global_config = global_config
        self._ocr_lib = {}
        if self.config.get('ocr') and not self.config.get('ocr').get('default', False):
            self.config['ocr']['default'] = self.config.get('ocr')
        self.current_task = None
        self._task_context_stack = []
        self.config_folder = config_folder or "config"
        self.trigger_task_index = -1
        self.basic_options = global_config.get_config(basic_options)

        self.trigger_tasks = []
        self.onetime_tasks = []
        self.onetime_task_queue = []
        self.current_job = None
        self._job_queue = []
        self._exclusive_owner = None
        self.thread = None
        self.lock = threading.Lock()
        self._wake_condition = threading.Condition()
        self._wake_version = 0
        self._destroy_lock = threading.Lock()
        self._destroyed = False
        if hasattr(self.exit_event, 'bind_condition'):
            self.exit_event.bind_condition(self._wake_condition)
        self._ocr_lib_lock = threading.Lock()
        self._ocr_init_thread = None
        self.blur_overlay_processor = None
        if callable(self.config.get('blur_area')):
            from ok.util.blur import BlurOverlayProcessor, DEFAULT_BLUR_ALGORITHM
            self.blur_overlay_processor = BlurOverlayProcessor(
                self.config.get('blur_area'),
                lambda: bool(self.basic_options.get('Enable Blur', False)),
                communicate.blur_overlay.emit,
                communicate.clear_blur_overlay.emit,
                self.exit_event,
                algorithm=lambda: self.basic_options.get('Blur Algorithm', DEFAULT_BLUR_ALGORITHM),
                interval=lambda: self.basic_options.get('Blur Interval', 1))
            communicate.window.connect(self.blur_overlay_processor.set_visible)
        self.init_default_ocr()

    def load_tr(self):
        locale_name = self.locale.name() if hasattr(self.locale, "name") else str(self.locale)
        try:
            from ok.core.translation import get_ocr_translations
            self.ocr_po_translation = get_ocr_translations(locale_name)
            self.ocr_po_translation.install()
            logger.info(f'translation ocr installed for {locale_name}')
        except:
            logger.info(f'install ocr translations error for {locale_name}')
            self.ocr_po_translation = None

    @property
    def interaction(self):
        override = self._job_override('interaction')
        return override if override is not None else self.device_manager.interaction

    @property
    def method(self):
        override = self._job_override('capture')
        return override if override is not None else self.device_manager.capture_method

    def _job_override(self, name):
        # Overrides apply only to the job's own code, which runs on the
        # executor thread; other threads keep seeing the device manager.
        job = self.current_job
        if job is not None and self.is_executor_thread():
            return getattr(job, name)
        return None

    def ocr_lib(self, name="default"):
        if name not in self._ocr_lib:
            with self._ocr_lib_lock:
                if name not in self._ocr_lib:
                    self._ocr_lib[name] = self._create_ocr_lib(name)
        return self._ocr_lib[name]

    def init_default_ocr(self):
        ocr_config = self.config.get('ocr')
        if not ocr_config:
            return
        default_ocr = ocr_config.get('default')
        if not default_ocr or not default_ocr.get('lib'):
            return
        self._ocr_init_thread = threading.Thread(target=self._init_default_ocr, name="DefaultOCRInit", daemon=True)
        self._ocr_init_thread.start()

    def _init_default_ocr(self):
        start = time.time()
        try:
            logger.info('start init default ocr')
            self.ocr_lib()
            logger.info(f'default ocr init end, cost: {time.time() - start:.2f}s')
        except Exception as e:
            logger.error(f'init default ocr error, cost: {time.time() - start:.2f}s', e)

    def _create_ocr_lib(self, name):
        ocr_config = self.config.get('ocr').get(name)
        lib = ocr_config.get('lib')
        to_download = ocr_config.get('download_models')
        if to_download:
            models = self.config.get('download_models').get(to_download)
            from ok.core.downloads import download_models
            download_models(models)

        config_params = ocr_config.get('params')
        if config_params is None:
            config_params = {}
        else:
            config_params = dict(config_params)
        if lib == 'paddleocr':
            logger.info('use paddleocr as ocr lib')
            from paddleocr import PaddleOCR
            config_params['use_textline_orientation'] = False
            config_params['use_doc_unwarping'] = False
            config_params['use_doc_orientation_classify'] = False
            config_params['device'] = "gpu" if is_cuda_12_or_above() else "cpu"
            logger.info(f'init PaddleOCR with {config_params}')
            ocr_lib = PaddleOCR(**config_params)
            import logging
            logging.getLogger('ppocr').setLevel(logging.ERROR)
            config_logger(self.config)
        elif lib == 'dgocr':
            if config_params.get('use_dml', True):
                config_params['use_dml'] = True
            from dgocr import DGOCR
            ocr_lib = DGOCR(**config_params)
        elif lib == 'onnxocr':
            from onnxocr.onnx_paddleocr import ONNXPaddleOcr
            logger.info(f'init onnxocr {config_params}')
            ocr_lib = ONNXPaddleOcr(use_angle_cls=False,
                                    logger=logger,
                                    use_npu=config_params.get('use_npu', True),
                                    use_openvino=config_params.get('use_openvino', False))
        elif lib == 'rapidocr':
            from rapidocr import RapidOCR
            params = {"Global.use_cls": False, "Global.max_side_len": 100000, "Global.min_side_len": 0,
                      "EngineConfig.onnxruntime.use_dml": False}
            params.update(config_params)
            logger.info(f'init rapidocr {params}')
            ocr_lib = RapidOCR(params=params)
        else:
            raise Exception(f'ocr lib not supported: {lib}')
        logger.info(f'ocr_lib init {ocr_lib} {lib}')
        return ocr_lib

    def nullable_frame(self):
        return self._frame

    def enable_frame_snapshots(self):
        """Keep an independent observation frame while an MCP runner is attached."""
        with self.lock:
            self._snapshot_enabled = True

    def get_frame_snapshot(self):
        """Return a copied frame and capture time without calling a capture backend.

        Unlike the scene cache this survives reset_scene(), so another thread
        can observe a running job without touching its capture backend.
        """
        with self.lock:
            snapshot = getattr(self, '_frame_snapshot', None)
        # Published snapshots are never mutated. Keep the reference alive while
        # copying without blocking queue operations on a potentially large image.
        return (snapshot[0].copy(), snapshot[1]) if snapshot is not None else None

    def check_frame_and_resolution(self, supported_ratio, min_size, time_out=8.0):
        if supported_ratio is None or min_size is None:
            return True, '0x0'
        logger.info('start check_frame_and_resolution')
        self.device_manager.update_resolution_for_hwnd()
        start = time.time()
        frame = None
        while frame is None and (time.time() - start) < time_out:
            frame = self.method.get_frame()
            time.sleep(0.1)
        if frame is None:
            logger.error(f'check_frame_and_resolution failed can not get frame after {time_out} {time.time() - start}')
            return False, '0x0'
        width = self.method.width
        height = self.method.height
        actual_ratio = 0
        if height == 0:
            actual_ratio = 0
        else:
            actual_ratio = width / height
        supported_ratio = ratio_text_to_number(supported_ratio)
        # Calculate the difference between the actual and supported ratios
        difference = abs(actual_ratio - supported_ratio)
        support = difference <= 0.01 * supported_ratio
        if not support:
            logger.error(f'resolution error {width}x{height} {frame is None}')
        if not support and frame is not None:
            communicate.screenshot.emit(frame, "resolution_error", False, None)
        # Check if the difference is within 1%
        if support and min_size is not None:
            if width < min_size[0] or height < min_size[1]:
                support = False
        return support, f"{width}x{height}"

    def can_capture(self):
        if self._job_override('capture') is None and self.device_manager.get_preferred_device() is None:
            return False
        return (self.method is not None and self.method.connected()
                and self.interaction is not None and self.interaction.should_capture())

    def release_inputs(self, raise_on_error=False):
        """生命周期清理先释放真实持有状态，不经过任务检查或截图。"""
        errors = []
        interactions = (self.interaction, self.device_manager.interaction)
        for interaction in dict.fromkeys(item for item in interactions if item is not None):
            try:
                interaction.release_all()
            except Exception as error:
                logger.error(f'输入清理失败：{error}')
                errors.append(error)
        try:
            release_shared_foreground_input()
        except ForegroundInputError as error:
            logger.error(f'共享输入仍有待释放状态：{error}')
            errors.append(error)
        if raise_on_error and errors:
            raise ForegroundInputError('本程序持有输入未全部清理，禁止开始下一步操作') from errors[0]
        return not errors

    def check_input_cancelled(self, allow_pause=False):
        """输入监测线程也可调用；只读取取消状态，不等待、不抓帧。"""
        if self.exit_event.is_set():
            raise TaskDisabledException()
        job = self.current_job
        if job is not None and job.cancel_requested:
            raise TaskDisabledException()
        task = self.current_task
        if task is not None and not task._enabled:
            raise TaskDisabledException()
        if (task is not None and self._exclusive_owner is not None
                and (job is None or job.owner != self._exclusive_owner)):
            raise TaskDisabledException()
        # 观察器不在执行器线程；MCP/通知作业的 ignore_pause 仍适用于它持有的输入。
        paused = self.paused and not self.debug_mode and not (job is not None and job.ignore_pause)
        if not allow_pause and (paused or task is not None and task.paused):
            raise ForegroundInputInterrupted('任务已暂停，持有输入已清理')

    @property
    def foreground_waiting(self):
        interaction = self.interaction
        scheduled = self.current_task is not None or any(task.enabled for task in self.get_all_tasks())
        return (isinstance(interaction, PynputInteraction) and not interaction.should_capture()
                and (getattr(self, '_foreground_waiting', False) or not self.paused and scheduled))

    @property
    def focus_wait_seconds(self):
        elapsed = getattr(self, '_focus_wait_total', 0.0)
        started = getattr(self, '_focus_wait_started', None)
        return elapsed + (time.monotonic() - started if started is not None else 0)

    def business_time(self):
        """识别与重试的单调时钟；普通失焦等待不消耗业务预算。"""
        return time.monotonic() - self.focus_wait_seconds

    def _foreground_ready(self):
        interaction = self.interaction
        needs_foreground = isinstance(interaction, PynputInteraction)
        ready = not needs_foreground or interaction.should_capture()
        if not ready:
            if not getattr(self, '_foreground_waiting', False):
                self._foreground_waiting = True
                self._focus_wait_started = time.monotonic()
                self.release_inputs(raise_on_error=True)
                self.reset_scene(check_enabled=False)
                logger.info('目标失焦，已释放输入并等待真实前台')
            return False
        resumed = getattr(self, '_foreground_waiting', False)
        if resumed:
            self._focus_wait_total = getattr(self, '_focus_wait_total', 0.0) + time.monotonic() - self._focus_wait_started
            self._focus_wait_started = None
            self._foreground_waiting = False
        if needs_foreground and (resumed or interaction.needs_refresh):
            if not interaction.refresh_target(force=True):
                return False
            self.reset_scene(check_enabled=False)
            self._needs_foreground_frame = True
            self._notify_foreground_resume = resumed or self.current_task is not None
        return True

    def _capture_generation(self):
        interaction = self.interaction
        generation = interaction.target_generation if isinstance(interaction, PynputInteraction) else None
        return id(self.method), generation

    @contextmanager
    def task_context(self, task):
        """嵌套执行保持调度所有权，但恢复必须通知真实运行的内层任务。"""
        stack = getattr(self, '_task_context_stack', None)
        if stack is None:
            stack = self._task_context_stack = []
        stack.append(task)
        try:
            yield
        finally:
            stack.pop()

    def _notify_tasks_resumed(self):
        tasks = [self.current_task, *getattr(self, '_task_context_stack', [])]
        interrupted = None
        for task in dict.fromkeys(task for task in tasks if task is not None):
            try:
                task.on_foreground_resume()
            except ForegroundInputInterrupted as error:
                # 一个轮转要求退出时仍先清其它活跃实例，避免留下旧角色引用。
                interrupted = error
        if interrupted is not None:
            raise interrupted

    def prepare_foreground(self, request_focus=False, time_out=5, cancel_check=None):
        """高层显式准备：绑定完成、真实前台、更新目标并清除旧帧。"""
        deadline = time.monotonic() + max(0, time_out)
        check = cancel_check or (lambda: self.check_input_cancelled(allow_pause=True))
        wait = getattr(self.device_manager, 'wait_for_binding', None)
        if callable(wait):
            wait(time_out=time_out, cancel_check=check)
        if check():
            raise TaskDisabledException()
        interaction = self.interaction
        if isinstance(interaction, PynputInteraction):
            remaining = max(0, deadline - time.monotonic())
            if not interaction.prepare_foreground(request_focus=request_focus, time_out=remaining, cancel_check=check):
                return False
        elif interaction is None:
            return False
        elif request_focus:
            interaction.on_run()
        self.reset_scene(check_enabled=False)
        self._frame_target_generation = None
        self._needs_foreground_frame = True
        return True

    def wait_foreground_frame(self, time_out=None, deadline=None):
        """安全检查点等待新帧；外部期限与取消有效，运行中绝不置前。"""
        self.release_inputs(raise_on_error=True)
        self.reset_scene(check_enabled=False)
        if time_out is not None:
            own_deadline = time.monotonic() + max(0, time_out)
            deadline = min(deadline, own_deadline) if deadline is not None else own_deadline
        while not self.exit_event.is_set():
            frame = self.next_frame(time_out=0.25, deadline=deadline)
            if frame is not None:
                return frame
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('等待目标前台新帧超时')
        raise TaskDisabledException()

    def next_frame(self, time_out=6, *, deadline=None):
        self.reset_scene(check_enabled=False)
        if time_out is not None:
            own_deadline = time.monotonic() + max(0, time_out)
            deadline = min(deadline, own_deadline) if deadline is not None else own_deadline
        while not self.exit_event.is_set():
            self.check_enabled(check_pause=False)
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                return None
            paused = self.blocked_by_pause() or self.current_task is not None and self.current_task.paused
            ready = self._foreground_ready()
            if not paused and ready and self.can_capture():
                generation = self._capture_generation()
                busy_wait = min(NEXT_FRAME_BUSY_WAIT, remaining) if remaining is not None else NEXT_FRAME_BUSY_WAIT
                frame = self.method.get_frame(busy_wait=max(0, busy_wait))
                self.check_enabled(check_pause=False)
                if (frame is not None and generation == self._capture_generation()
                        and self.interaction.should_capture()):
                    height, width = frame.shape[:2]
                    if height <= 0 or width <= 0:
                        raise CaptureException('截图尺寸无效')
                    self._frame = frame
                    self._frame_target_generation = generation
                    self._last_frame_time = time.time()
                    self._needs_foreground_frame = False
                    if getattr(self, '_snapshot_enabled', False):
                        snapshot = (frame.copy(), self._last_frame_time)
                        with self.lock:
                            self._frame_snapshot = snapshot
                    if self.blur_overlay_processor:
                        self.blur_overlay_processor.next_frame(frame)
                    if getattr(self, '_notify_foreground_resume', False):
                        task = self.current_task
                        if task is not None:
                            self._notify_foreground_resume = False
                            self._notify_tasks_resumed()
                    return self._frame
                if frame is not None:
                    # 抓帧期间目标变化，丢弃旧代次，下一轮刷新后再取帧。
                    self.release_inputs()
                    self.reset_scene(check_enabled=False)
            remaining = deadline - time.monotonic() if deadline is not None else 0.05
            if remaining <= 0:
                return None
            # 不经过可能无限等待前台的 sleep，外部期限始终在本循环检查。
            self._wait_for_activity(min(0.05, remaining))
        raise FinishedException()

    def is_executor_thread(self):
        return self.thread == threading.current_thread()

    def connected(self):
        return self.method is not None and self.method.connected()

    @property
    def frame(self):
        while not self.exit_event.is_set():
            if (not self.blocked_by_pause() and self._foreground_ready() and self._frame is not None
                    and self._frame_target_generation == self._capture_generation()):
                return self._frame
            self.next_frame()
        raise FinishedException()

    def blocked_by_pause(self):
        """Whether the calling code should wait because the executor is paused.

        debug_mode ignores pause everywhere; a job ignores it only for its own
        code on the executor thread.
        """
        if not self.paused or self.debug_mode:
            return False
        job = self.current_job
        return not (job is not None and job.ignore_pause and self.is_executor_thread())

    def check_enabled(self, check_pause=True):
        if check_pause and self.blocked_by_pause():
            self.sleep(1)
        if not self.is_executor_thread():
            if self.current_task and not self.current_task._enabled:
                logger.info(f'{self.current_task} is disabled, raise Exception')
                self.current_task = None
                raise TaskDisabledException()
            return
        job = self.current_job
        if job is not None and job.cancel_requested:
            logger.info(f'{job} is cancelled, raise Exception')
            raise TaskDisabledException()
        if self.current_task and not self.current_task._enabled:
            logger.info(f'{self.current_task} is disabled, raise Exception')
            self.current_task = None
            raise TaskDisabledException()
        if (self.current_task and self._exclusive_owner is not None
                and (job is None or job.owner != self._exclusive_owner)):
            # Stop scheduled tasks, but allow the exclusive owner's job to
            # supply a task context for normal lifecycle checks.
            logger.info(f'{self.current_task} stopped, executor held by {self._exclusive_owner}')
            raise TaskDisabledException()

    def sleep(self, timeout: float):
        """业务等待排除失焦时间，恢复后取得新帧；取消检查不依赖输入守卫。"""
        self.reset_scene(check_enabled=False)
        if timeout <= 0:
            return
        deadline = self.business_time() + timeout
        self.pause_end_time = time.time() + timeout
        while not self.exit_event.is_set():
            self.check_enabled(check_pause=False)
            ready = self._foreground_ready()
            if (self.blocked_by_pause() or self.current_task is not None and self.current_task.paused
                    or self.interaction is None or not ready):
                self._wait_for_activity(0.05)
                continue
            if getattr(self, '_needs_foreground_frame', False):
                if self.next_frame(time_out=0.25) is None:
                    continue
            task = self.current_task
            next_sleep_check = None
            if task is not None and task.sleep_check_interval >= 0 and not task.in_sleep_check:
                elapsed = time.time() - task.last_sleep_check_time
                next_sleep_check = max(0, task.sleep_check_interval - elapsed)
                if next_sleep_check <= 0:
                    task.in_sleep_check = True
                    try:
                        if self.next_frame(time_out=0.25) is not None:
                            task.sleep_check()
                            self.reset_scene(check_enabled=False)
                    finally:
                        task.last_sleep_check_time = time.time()
                        task.in_sleep_check = False
                    next_sleep_check = task.sleep_check_interval
            remaining = deadline - self.business_time()
            if remaining <= 0:
                return
            if next_sleep_check is not None:
                remaining = min(remaining, max(0.001, next_sleep_check))
            self._wait_for_activity(min(0.05, remaining))
        raise FinishedException()

    def _wake_executor(self):
        condition = getattr(self, '_wake_condition', None)
        if condition is None:
            return
        with condition:
            self._wake_version += 1
            condition.notify_all()

    def _get_wake_version(self):
        condition = getattr(self, '_wake_condition', None)
        if condition is None:
            return None
        with condition:
            return self._wake_version

    def _wait_for_activity(self, timeout, wake_version=None):
        if timeout <= 0:
            return False
        condition = getattr(self, '_wake_condition', None)
        if condition is None:
            time.sleep(timeout)
            return False
        with condition:
            if wake_version is not None and wake_version != self._wake_version:
                return True
            return condition.wait(timeout)

    def pause(self, task=None):
        if task is not None:
            if self.current_task != task:
                raise Exception(f"Can only pause current task {self.current_task}")
            self.release_inputs()
        elif not self.paused:
            self.release_inputs()
            self.paused = True
            self._wake_executor()
            communicate.executor_paused.emit(self.paused)
            self.reset_scene(check_enabled=False)
            self.pause_start = time.time()
            return True

    def stop_current_task(self):
        self.release_inputs()
        if task := self.current_task:
            task.disable()
            task.unpause()

    def _start_thread_locked(self):
        if self.thread is None:
            # Application shutdown must not be held hostage by a custom
            # task or interaction backend that ignores exit_event.
            self.thread = threading.Thread(
                target=self.execute, name="TaskExecutor", daemon=True)
            self.thread.start()

    def start(self):
        with self.lock:
            self._start_thread_locked()
            if self.paused:
                self.paused = False
                communicate.executor_paused.emit(self.paused)
                self.pause_end_time += self.pause_start - time.time()
            self._wake_executor()

    @property
    def exclusive_owner(self):
        return self._exclusive_owner

    def submit(self, fn, name=None, owner=None, capture=None, interaction=None, ignore_pause=True,
               defer_while_unfocused=False):
        """Run ``fn`` once on the executor thread and return its ExecutorJob.

        Jobs run before queued tasks, as soon as the running task finishes, and
        also while the executor is paused unless ``ignore_pause`` is False.
        ``capture`` and ``interaction`` replace the device manager's methods for
        the job's own code. While the executor is held exclusively, only the
        holder's jobs are accepted.
        """
        job = ExecutorJob(self, fn, name=name, owner=owner, capture=capture, interaction=interaction,
                          ignore_pause=ignore_pause, defer_while_unfocused=defer_while_unfocused)
        with self.lock:
            if self._exclusive_owner is not None and owner != self._exclusive_owner:
                raise ExecutorBusyException(self._exclusive_owner)
            if self.exit_event.is_set():
                job.future.cancel()
                return job
            self._job_queue.append(job)
            self._start_thread_locked()
        logger.info(f'submitted {job}')
        self._wake_executor()
        return job

    def cancel_job(self, job):
        """Cancel a queued job, or make a running job stop at its next check."""
        with self.lock:
            if job in self._job_queue:
                self._job_queue.remove(job)
        # Future callbacks can release ownership or submit follow-up work.
        # Never invoke them while holding the executor's queue lock.
        if job.future.cancel():
            return True
        if job.future.done():
            return False
        job._cancel_requested.set()
        if self.current_job is job:
            self.release_inputs()
        self._wake_executor()
        return True

    def acquire_exclusive(self, owner):
        """Hold the executor for ``owner`` until release_exclusive(owner).

        Stops the running task, disables enabled one-time tasks and cancels
        other owners' jobs. Trigger tasks are skipped but keep their enabled
        setting, so they resume on release; one-time tasks stay stopped.
        Returns False if ``owner`` already holds the executor.
        """
        if owner is None:
            raise ValueError('owner is required')
        with self.lock:
            if self._exclusive_owner == owner:
                return False
            if self._exclusive_owner is not None:
                raise ExecutorBusyException(self._exclusive_owner)
            self._exclusive_owner = owner
            stale_jobs = [job for job in self._job_queue if job.owner != owner]
            self._job_queue = [job for job in self._job_queue if job.owner == owner]
        logger.info(f'executor held exclusively by {owner}')
        for job in stale_jobs:
            job.future.cancel()
        job = self.current_job
        if job is not None and job.owner != owner:
            job._cancel_requested.set()
        for task in list(self.onetime_tasks):
            if task.enabled:
                task.disable()
        self._wake_executor()
        communicate.executor_exclusive.emit(owner)
        return True

    def release_exclusive(self, owner):
        """Release the executor held by ``owner`` and cancel its remaining jobs."""
        with self.lock:
            if owner is None or self._exclusive_owner != owner:
                return False
            self._exclusive_owner = None
            owned_jobs = [job for job in self._job_queue if job.owner == owner]
            self._job_queue = [job for job in self._job_queue if job.owner != owner]
        logger.info(f'executor released by {owner}')
        for job in owned_jobs:
            job.future.cancel()
        job = self.current_job
        if job is not None and job.owner == owner:
            job._cancel_requested.set()
        self._wake_executor()
        communicate.executor_exclusive.emit(None)
        return True

    def _pop_job(self):
        with self.lock:
            for index, job in enumerate(self._job_queue):
                if job.defer_while_unfocused and self.foreground_waiting:
                    continue
                return self._job_queue.pop(index)
        return None

    def _run_job(self, job):
        owner = self._exclusive_owner
        if owner is not None and job.owner != owner:
            job.future.cancel()
            return
        if not job.future.set_running_or_notify_cancel():
            return
        logger.info(f'start {job}')
        self.current_job = job
        start = time.time()
        error, result = None, None
        try:
            self.release_inputs(raise_on_error=True)
            self.reset_scene(check_enabled=False)
            result = job.fn()
        except BaseException as caught:
            error = caught
            if isinstance(caught, TaskDisabledException):
                logger.info(f'{job} cancelled')
            elif not isinstance(caught, (FinishedException, SystemExit)):
                logger.error(f'{job} raised', caught)
        finally:
            # Future 终态发布在真实清理之后，调用方才可开始下一轮工作。
            try:
                self.release_inputs(raise_on_error=True)
            except ForegroundInputError as cleanup_error:
                if error is None:
                    error = cleanup_error
            self.current_job = None
            self.reset_scene(check_enabled=False)
        if error is not None:
            job.future.set_exception(error)
        else:
            job.future.set_result(result)
        logger.info(f'end {job}, cost: {time.time() - start:.2f}s')

    def wait_condition(self, condition, time_out=0, pre_action=None, post_action=None, settle_time=-1,
                       raise_if_not_found=False, *, deadline=None):
        self.reset_scene()
        time_out = self.wait_scene_timeout if time_out == 0 else time_out
        start = self.business_time()
        settled = None
        while not self.exit_event.is_set():
            self.check_enabled(check_pause=False)
            if deadline is not None and time.monotonic() >= deadline:
                break
            if not self._foreground_ready():
                settled = None
                self.next_frame(time_out=0.25, deadline=deadline)
                continue
            if self.business_time() - start >= time_out:
                break
            if pre_action is not None:
                pre_action()
            remaining = max(0, time_out - (self.business_time() - start))
            if self.next_frame(time_out=min(0.25, remaining), deadline=deadline) is None:
                continue
            result = condition()
            if result:
                settle = self.wait_until_settle_time if settle_time == -1 else settle_time
                now = self.business_time()
                if settle <= 0 or settled is not None and now - settled >= settle:
                    return result
                if settled is None:
                    settled = now
            else:
                settled = None
            if post_action is not None:
                post_action()
        if self.exit_event.is_set():
            raise FinishedException()
        if raise_if_not_found:
            raise WaitFailedException()
        return None

    def reset_scene(self, check_enabled=True):
        if check_enabled:
            self.check_enabled()
        self._frame = None
        if self.scene:
            self.scene.reset()

    def enqueue_onetime_task(self, task):
        if task not in self.onetime_tasks:
            self._wake_executor()
            return False
        with self.lock:
            if task not in self.onetime_task_queue:
                self.onetime_task_queue.append(task)
                logger.info(f'queued onetime_task {task.name}')
        self._wake_executor()
        return True

    def remove_onetime_task(self, task):
        if task not in self.onetime_tasks:
            return False
        removed = False
        with self.lock:
            while task in self.onetime_task_queue:
                self.onetime_task_queue.remove(task)
                removed = True
        if removed:
            self._wake_executor()
        return removed

    def waiting_for_task(self, task):
        if task not in self.onetime_tasks or not task.enabled or task.running:
            return None
        with self.lock:
            queue = [queued_task for queued_task in self.onetime_task_queue
                     if queued_task.enabled and not queued_task.running]
        if task not in queue:
            return None
        index = queue.index(task)
        if index > 0:
            return queue[index - 1]
        if self.current_task and self.current_task != task and self.current_task.running:
            return self.current_task
        return None

    def next_task(self) -> tuple:
        if self.exit_event.is_set():
            logger.error("next_task exit_event.is_set exit")
            return None, False, False
        with self.lock:
            while self.onetime_task_queue:
                onetime_task = self.onetime_task_queue.pop(0)
                if onetime_task.enabled:
                    logger.info(f'get queued onetime_task {onetime_task.name}')
                    return onetime_task, True, False
        for onetime_task in self.onetime_tasks:
            if onetime_task.enabled:
                logger.info(f'get one enabled onetime_task {onetime_task.name}')
                return onetime_task, True, False
        cycled = False
        for _ in range(len(self.trigger_tasks)):
            if self.trigger_task_index == len(self.trigger_tasks) - 1:
                self.trigger_task_index = -1
                self.trigger_sleep()
                cycled = True
            self.trigger_task_index += 1
            task = self.trigger_tasks[self.trigger_task_index]
            if task.enabled and task.should_trigger():
                return task, cycled, True
        return None, cycled, False

    def active_trigger_task_count(self):
        return len([x for x in self.trigger_tasks if x.enabled])

    def trigger_sleep(self):
        if interval := self.basic_options.get('Trigger Interval', 1):
            self.sleep(interval / 1000)

    def next_trigger_delay(self, default=1.0):
        now = time.time()
        delays = [
            max(0, task.trigger_interval - (now - task.last_trigger_time))
            for task in self.trigger_tasks
            if task.enabled and task.trigger_interval > 0
        ]
        return min(delays, default=default)

    def _prepare_task_for_run(self, is_trigger_task, request_focus=False):
        """保留首帧前准备；只有显式请求才允许 PC 后端置前一次。"""
        wait = getattr(self.device_manager, 'wait_for_binding', None)
        if callable(wait):
            wait(cancel_check=lambda: self.check_input_cancelled(allow_pause=True))
        interaction = self.interaction
        if isinstance(interaction, PynputInteraction):
            if request_focus and not is_trigger_task:
                interaction.on_run()
            else:
                interaction.refresh_target()
        elif not is_trigger_task and interaction is not None:
            interaction.on_run()

    def execute(self):
        logger.info("start execute")
        while not self.exit_event.is_set():
            wake_version = self._get_wake_version()
            job = self._pop_job()
            if job is not None:
                self._run_job(job)
                continue
            if self.paused or self._exclusive_owner is not None:
                # Only jobs run now; wait for one, or for start() / release.
                self._wait_for_activity(1, wake_version)
                continue
            task, cycled, is_trigger_task = self.next_task()
            if not task:
                self._wait_for_activity(self.next_trigger_delay(), wake_version)
                continue
            if cycled:
                self.reset_scene()
            elif time.time() - self._last_frame_time > 0.2:
                self.reset_scene()
            try:
                task.start_time = time.time()
                task.running = True
                self.current_task = task
                if not is_trigger_task:
                    communicate.task.emit(task)
                self._prepare_task_for_run(is_trigger_task)
                if cycled or self._frame is None:
                    if self.next_frame(time_out=4) is None:
                        if is_trigger_task:
                            logger.info("no frame available, skip remaining trigger tasks")
                            self.trigger_task_index = len(self.trigger_tasks) - 1
                            continue
                        self.wait_foreground_frame()
                if is_trigger_task:
                    if task.run():
                        self.trigger_task_index = -1
                        self.reset_scene()
                        continue
                else:
                    prevent_sleeping(True)
                    try:
                        logger.debug(f'start running onetime_task {task.name}')
                        if getattr(task, '_foreground_resume_pending', False):
                            self.wait_foreground_frame()
                            task._foreground_resume_pending = False
                            task.resume_after_foreground()
                        else:
                            task.run()
                        logger.debug(f'end running onetime_task {task.name}')
                    finally:
                        prevent_sleeping(False)
                    self.release_inputs(raise_on_error=True)
                    task.disable()
                    communicate.task_done.emit(task)
                    if task.exit_after_task or task.config.get('Exit After Task'):
                        logger.info('Successfully Executed Task, Exiting Game and App!')
                        alert_info('Successfully Executed Task, Exiting Game and App!')
                        time.sleep(5)
                        self.device_manager.stop_hwnd()
                        time.sleep(5)
                        communicate.quit.emit()
            except ForegroundInputInterrupted as error:
                self.release_inputs()
                self.reset_scene(check_enabled=False)
                self._notify_foreground_resume = True
                logger.info(f'输入中断，等待安全恢复点：{error}')
                if not is_trigger_task:
                    task._foreground_resume_pending = True
            except TaskDisabledException:
                logger.info(f"TaskDisabledException, continue {task}")
                self._finish_task(task, is_trigger_task)
                communicate.notification.emit('Stopped', task.name, False,
                                              False, None, None, None)
            except FinishedException:
                logger.info("FinishedException, breaking")
                break
            except Exception as e:
                if isinstance(e, CaptureException):
                    communicate.capture_error.emit()
                name = task.name
                task.running = False
                task.disable()
                params = None
                if isinstance(e, HotkeyConfigException):
                    error = "{key} is invalid, please check the hotkey config!"
                    params = {"key": e.key}
                else:
                    error = str(e)
                communicate.notification.emit(error, name, True, True, None, params, None)
                task.info_set(task._app.tr('Error'), error)
                logger.error(f"{name} exception stopped", e)
                if self._frame is not None:
                    communicate.screenshot.emit(self._frame, name, True, None)
                self.current_task = None
                communicate.task.emit(None)
            finally:
                self._finish_task(task, is_trigger_task)
        self.destroy()

    def _finish_task(self, task, is_trigger_task):
        self.release_inputs()
        if self.current_task is None and not task.running:
            return
        task.running = False
        self.current_task = None
        if not is_trigger_task:
            communicate.task.emit(task)

    def stop(self):
        logger.info('stop')
        self.release_inputs()
        self.exit_event.set()
        self._wake_executor()

    def destroy(self):
        lock = getattr(self, '_destroy_lock', None)
        if lock is None:
            lock = self._destroy_lock = threading.Lock()
        with lock:
            if getattr(self, '_destroyed', False):
                return
            self._destroyed = True
        logger.info('Executor destroy')
        self.release_inputs()
        with self.lock:
            jobs, self._job_queue = self._job_queue, []
        for job in jobs:
            job.future.cancel()
        onetime_tasks, self.onetime_tasks = self.onetime_tasks, []
        trigger_tasks, self.trigger_tasks = self.trigger_tasks, []
        for task in (*onetime_tasks, *trigger_tasks):
            try:
                task.on_destroy()
            except Exception as error:
                logger.error(f'task on_destroy failed for {task}: {error}')
        if self.interaction:
            try:
                self.interaction.on_destroy()
            except Exception as error:
                logger.error(f'interaction on_destroy failed: {error}')

    def request_destroy(self):
        """Run cleanup away from the GUI close event when needed."""
        if self.thread is not None and self.thread.is_alive():
            # execute() owns normal cleanup after observing exit_event.
            return
        threading.Thread(
            target=self.destroy, name="TaskExecutorCleanup", daemon=True).start()

    def wait_until_done(self):
        self.thread.join()

    def get_all_tasks(self):
        return self.onetime_tasks + self.trigger_tasks

    def get_task_by_class_name(self, class_name):
        for onetime_task in self.onetime_tasks:
            if onetime_task.__class__.__name__ == class_name:
                return onetime_task
        for trigger_task in self.trigger_tasks:
            if trigger_task.__class__.__name__ == class_name:
                return trigger_task

    def get_task_by_class(self, cls):
        logger.debug(f'get_task_by_class {cls} {self.onetime_tasks} {self.trigger_tasks}')
        # Imports held by other tasks may refer to the previous class object.
        key = (cls.__module__, cls.__qualname__)
        tasks = self.get_all_tasks()
        for task in tasks:
            if (type(task).__module__, type(task).__qualname__) == key:
                return task
        for task in tasks:
            if isinstance(task, cls):
                return task


def list_or_obj_to_str(val):
    if val is not None:
        if isinstance(val, list):
            return ', '.join(str(obj) for obj in val)
        else:
            return str(val)
    else:
        return None
