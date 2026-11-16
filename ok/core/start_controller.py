import threading
import time

from ok import Handler, og
from ok import Logger
from ok.device.capture import BaseWindowsCaptureMethod, BrowserCaptureMethod
from ok.device.interaction_methods.post_message_test import PostMessageTestInteraction
from ok.core.events import communicate
from ok.core.notifications import alert_error, alert_info
from ok.core.start_request import StartRequest
from ok.task.exceptions import TaskDisabledException

logger = Logger.get_logger(__name__)


class StartController:
    def __init__(self, app_config, exit_event):
        self.config = app_config
        self.exit_event = exit_event
        self.handler = Handler(exit_event, __name__)
        self.start_timeout = app_config.get('start_timeout', 60)
        # 保留旧属性供扩展读取，但任何配置值都不能恢复自动开游戏。
        self.start_exe = False
        self.starting = False

    @property
    def _cancel_requested(self):
        event = self.__dict__.get('_cancel_event')
        if event is None:
            event = self.__dict__['_cancel_event'] = threading.Event()
        return event

    @property
    def _pending(self):
        state = self.__dict__.get('_pending_state')
        if state is None:
            state = self.__dict__['_pending_state'] = (threading.RLock(), {})
        return state

    def is_task_pending(self, task):
        """等待连接和已排队到执行器的外部请求均可取消。"""
        lock, requests = self._pending
        with lock:
            if task in requests:
                return True
        return og.executor.has_start_request(task)

    def _register_request(self, task, source):
        lock, requests = self._pending
        with lock:
            if task in requests or (task is not None and og.executor.has_start_request(task)):
                return None
            request = StartRequest(source)
            requests[task] = request
            self.starting = True
        if task is not None and not isinstance(task, int):
            communicate.task.emit(task)
        logger.info(f'收到开始请求：{request.request_id}，来源：{request.source}')
        return request

    def _discard_pending(self, task, request):
        lock, requests = self._pending
        with lock:
            if requests.get(task) is request:
                del requests[task]
            self.starting = bool(requests)
        if task is not None and not isinstance(task, int):
            communicate.task.emit(task)

    def cancel_task(self, task):
        lock, requests = self._pending
        with lock:
            request = requests.pop(task, None)
            if request is not None:
                request.cancel()
            self.starting = bool(requests)
        queued = og.executor.cancel_start_request(task)
        if request is not None and self.__dict__.get('_active_request') is request:
            self._cancel_requested.set()
        if request is not None or queued:
            communicate.task.emit(task)
            return True
        return False

    def cancel_start(self):
        """取消尚未准备完的请求；不关闭游戏，也不产生任何激活。"""
        self._cancel_requested.set()
        lock, requests = self._pending
        with lock:
            dropped = list(requests.items())
            requests.clear()
            self.starting = False
        for task, request in dropped:
            request.cancel()
            if task is not None and not isinstance(task, int):
                communicate.task.emit(task)
        og.executor.cancel_start_request()

    def _check_prepare_cancelled(self):
        if self.exit_event.is_set() or self._cancel_requested.is_set():
            raise TaskDisabledException()
        if request := self.__dict__.get('_active_request'):
            request.check_cancelled()
        # 绑定和首帧准备会等待；等待期间 MCP 可能接管，不能只在入口判断一次。
        if (owner := getattr(og.executor, 'exclusive_owner', None)) is not None:
            raise RuntimeError(f'助手已由 {owner} 接管，本次开始请求已停止')

    def _prepare_explicit_start(self, request):
        self._check_prepare_cancelled()
        if not og.executor.prepare_start(
                request, time_out=min(5, self.start_timeout), cancel_check=self._check_prepare_cancelled):
            raise RuntimeError('游戏输入或画面尚未就绪，请检查游戏窗口后重试')
        self._check_prepare_cancelled()
        error = self.check_device_error(frame=og.executor.nullable_frame(), cancel_check=self._check_prepare_cancelled)
        if error:
            raise RuntimeError(error)

    def tr(self, message):
        app = getattr(og, "app", None)
        return app.tr(message) if app is not None else message

    @staticmethod
    def _mark_task_enabled(task):
        if not task.enabled:
            task._enabled = True
            task._foreground_resume_pending = False
            task.info_clear()
            task.executor.enqueue_onetime_task(task)
            logger.info(f"已启用任务：{task}")
        communicate.task.emit(task)

    def _resolve_task(self, task):
        if not isinstance(task, int):
            return task
        tasks = getattr(og.executor, 'onetime_tasks', None)
        try:
            return tasks[task]
        except (IndexError, TypeError):
            return task

    def start(self, task=None, exit_after=False, *, source='开始按钮'):
        """统一接收热键、鼠标和命令行请求，游戏必须已由用户打开。"""
        task = self._resolve_task(task)
        request = self._register_request(task, source)
        if request is None:
            return False
        try:
            self.handler.post(lambda: self.do_start(task, exit_after, from_queue=True, request=request))
        except Exception:
            request.cancel()
            self._discard_pending(task, request)
            raise
        return True

    def do_start(self, task=None, exit_after=False, from_queue=False, *, request=None, source='无界面任务'):
        task = self._resolve_task(task)
        if from_queue:
            lock, requests = self._pending
            with lock:
                if requests.get(task) is not request or request.cancelled:
                    return False
        else:
            request = self._register_request(task, source)
            if request is None:
                return False
        lock = self.__dict__.setdefault('_start_lock', threading.Lock())
        with lock:
            if request.cancelled:
                self._discard_pending(task, request)
                return False
            self._cancel_requested.clear()
            self.__dict__['_active_request'] = request
            try:
                return self._do_start(task, exit_after, request=request)
            finally:
                self.__dict__['_active_request'] = None
                self._discard_pending(task, request)

    def _do_start(self, task=None, exit_after=False, *, request=None):
        request = request or StartRequest('显式开始')
        executor = og.executor
        if (owner := getattr(executor, 'exclusive_owner', None)) is not None:
            communicate.starting_emulator.emit(
                True, self.tr('Cannot start: the app is controlled by {owner}').format(owner=owner), 0)
            return False
        communicate.starting_emulator.emit(False, None, 0)
        error_message = None
        enabled_here = []
        try:
            request.check_cancelled()
            self._check_prepare_cancelled()
            if isinstance(task, int):
                task = executor.onetime_tasks[task]
            current = executor.current_task
            # 同一任务已在运行时，重复按钮不应得到第二次激活机会。
            if task is not None and task.enabled and not task.paused and not executor.paused:
                return True
            if task is None and not executor.paused and not (current and current.paused):
                return True
            if task and ((current and current is not task)
                         or (not executor.paused and not (current is task and task.paused))):
                # 正在执行的手势不被插入激活；新任务在其真正执行前消费请求。
                if not executor.queue_start_request(task, request):
                    request.check_cancelled()
                    return True
                if exit_after:
                    task.exit_after_task = True
                if not task.enabled:
                    enabled_here.append(task)
                self._mark_task_enabled(task)
                # 取消可能与排队并发；不能在取消已清除额度后重新启用任务。
                if request.cancelled:
                    task.disable()
                    request.check_cancelled()
                # 排队另一个任务不等于恢复当前任务；暂停者等待自己的显式恢复请求。
                return True

            self.connect_existing_device(prepare_only=True, cancel_check=self._check_prepare_cancelled)
            self.check_auto_hdr()
            self.check_gpu_driver_post_processing()
            self._prepare_explicit_start(request)
            if task and task.enabled and task.paused:
                if exit_after:
                    task.exit_after_task = True
                task.unpause()
                return True
            if task is None and current and current.paused:
                current.unpause()
                return True
            tasks_to_enable = [item for item in executor.get_all_tasks()
                               if getattr(item, 'enable_after_start', False)]
            if task is not None and task not in tasks_to_enable:
                tasks_to_enable.append(task)
            if task and exit_after:
                task.exit_after_task = True
            self._check_prepare_cancelled()
            for enable_task in tasks_to_enable:
                if not enable_task.enabled:
                    enabled_here.append(enable_task)
                self._mark_task_enabled(enable_task)
            self._check_prepare_cancelled()
            executor.start()
            return True
        except TaskDisabledException:
            request.cancel()
            for enable_task in enabled_here:
                enable_task.disable()
            logger.info(f'开始请求已取消：{request.request_id}')
            return False
        except Exception as error:
            request.cancel()
            for enable_task in enabled_here:
                enable_task.disable()
            logger.error(f'开始请求失败：{request.request_id}，{error}', error)
            error_message = self.tr('Start failed: {error}').format(error=error)
            return False
        finally:
            communicate.starting_emulator.emit(True, error_message, 0)

    def connect_existing_device(self, *, initial_refresh_done=False, prepare_only=False, cancel_check=None):
        """只连接用户已打开的游戏；设备缺失立即失败，不等待虚假的启动倒计时。"""
        manager = og.device_manager
        check_cancelled = cancel_check or self._check_prepare_cancelled
        if check_cancelled() is True:
            raise TaskDisabledException()
        if not initial_refresh_done:
            manager.do_refresh(True)
        device = manager.get_preferred_device()
        if not device or not device.get('connected'):
            raise RuntimeError('请先手动打开游戏，并选择正确的游戏窗口')
        # 首次发现窗口时，捕获初始化仍可能异步回退，须等绑定完成再诊断。
        manager.wait_for_binding(time_out=min(5, self.start_timeout), cancel_check=check_cancelled)
        if check_cancelled() is True:
            raise TaskDisabledException()
        error = self.check_device_error(prepare_only=prepare_only, cancel_check=check_cancelled)
        if error:
            raise RuntimeError(error)
        return True

    def start_device(self, initial_refresh_done=False):
        """兼容旧任务接口；只做连接检查，绝不创建或重启游戏进程。"""
        try:
            return self.connect_existing_device(initial_refresh_done=initial_refresh_done)
        except TaskDisabledException:
            return False
        except Exception as error:
            logger.error(f'连接已运行游戏失败：{error}')
            communicate.starting_emulator.emit(True, self.tr(str(error)), 0)
            return False

    def check_auto_hdr(self):
        if not (self.config.get('windows') or {}).get('check_auto_hdr', False):
            return
        try:
            from ok.util.gpu_driver_settings import is_auto_hdr_enabled

            device_manager = og.device_manager
            device = device_manager.get_preferred_device()
            if not device or device.get('device') != 'windows':
                return
            from ok.device.capture_methods.windows_graphics_hdr import WindowsGraphicsCaptureHDRMethod

            if isinstance(getattr(device_manager, 'capture_method', None), WindowsGraphicsCaptureHDRMethod):
                return
            hwnd_window = getattr(device_manager, 'hwnd_window', None)
            target_exe_path = getattr(hwnd_window, 'exe_full_path', None) or device.get('full_path')
            if is_auto_hdr_enabled(target_exe_path):
                message = self.tr('Auto HDR is enabled, tasks might not work correctly!')
                logger.error(message)
                alert_error(message, tray=True)
        except Exception as e:
            logger.error(f'check_auto_hdr exception: {e}', e)

    def check_gpu_driver_post_processing(self):
        try:
            from ok.util.gpu_driver_settings import get_enabled_gpu_driver_post_processing
            device_manager = getattr(og, 'device_manager', None)
            hwnd_window = getattr(device_manager, 'hwnd_window', None)
            target_exe_path = getattr(hwnd_window, 'exe_full_path', None) if hwnd_window else None
            target_hwnd = getattr(hwnd_window, 'hwnd', None) if hwnd_window else None
            if not target_exe_path and device_manager:
                device = device_manager.get_preferred_device()
                target_exe_path = device.get('full_path') if device else None
            enabled_features = get_enabled_gpu_driver_post_processing(target_exe_path, target_hwnd)
        except Exception as e:
            logger.error(f'check_gpu_driver_post_processing exception: {e}', e)
            return

        if enabled_features:
            warning_lines = [
                self.tr('{vendor} {feature} is enabled and may cause malfunctions!').format(
                    vendor=feature.vendor,
                    feature=feature.feature,
                )
                for feature in enabled_features
            ]
            if any(feature.vendor == 'NVIDIA' and feature.feature in (
                    'Filter Profile', 'RTX HDR', 'RTX Dynamic Vibrance') for feature in enabled_features):
                warning_lines.append(self.tr('Try using Alt+F3 to turn it off.'))
            logger.warning('\n'.join(warning_lines))
            communicate.notification.emit(
                '\n'.join(warning_lines),
                self.tr('GPU Driver Warning'),
                True,
                True,
                'start',
                None,
                None,
            )

    def check_resolution(self, frame=None):
        error = None
        supported_resolution = self.config.get(
            'supported_resolution', {})
        supported_ratio = supported_resolution.get('ratio')
        min_size = supported_resolution.get('min_size')
        resize_to = supported_resolution.get('resize_to')
        force_ratio = supported_resolution.get('force_ratio')
        capture_method = og.device_manager.capture_method
        is_windows_capture = isinstance(capture_method, BaseWindowsCaptureMethod)
        auto_resize_enabled = (
            not is_windows_capture
            or (not isinstance(og.device_manager.interaction, PostMessageTestInteraction)
                and og.global_config.get_config('Basic Options').get('Auto Resize Game Window', True))
        )
        supported, resolution = og.executor.check_frame_and_resolution(supported_ratio, min_size, frame=frame)
        if not supported:
            resize_success = False
            if resize_to and is_windows_capture and auto_resize_enabled:
                resize_success = capture_method.hwnd_window.try_resize_to(resize_to)
            if not resize_success:
                error = self.tr(
                    'Resolution {resolution} check failed, some tasks might not work correctly!').format(
                    resolution=resolution)
                if supported_ratio:
                    error += self.tr(', the supported ratio is {supported_ratio}').format(
                        supported_ratio=supported_ratio)
                if min_size:
                    error += self.tr(', the supported min resolution is {min_size}').format(
                        min_size=f'{min_size[0]}x{min_size[1]}')
        if error and not auto_resize_enabled:
            logger.info(f'check_resolution: {error}')
            alert_info(error)
            return None
        if force_ratio:
            return error
        elif error:
            alert_error(error, tray=True)

    def check_device_error(self, *, prepare_only=False, frame=None, cancel_check=None):
        try:
            if cancel_check is not None and cancel_check() is True:
                raise TaskDisabledException()
            device = og.device_manager.get_preferred_device()
            if not device:
                return '请先手动打开游戏，并选择正确的游戏窗口'
            error_msg = self.tr("{} is not connected, please select the game window.").format(
                device.get('nick', '游戏'))
            logger.info(f'test check_device_error msg: {error_msg}')
            if og.device_manager.capture_method is None:
                return self.tr("Selected capture method is not supported by the game or your system!")
            if not og.device_manager.device_connected():
                logger.error(f'Emulator is not connected {og.device_manager.device}')
                return self.tr("Emulator is not connected, start the emulator first!")
            if isinstance(og.device_manager.capture_method,
                          BrowserCaptureMethod) and not og.device_manager.capture_method.connected():
                logger.info("start browser")
                og.device_manager.capture_method.start_browser()
            if not og.device_manager.capture_method.connected():
                logger.error(f'Game window is not connected {og.device_manager.capture_method}')
                return error_msg
            if prepare_only:
                # nono 必须先完成显式置前，不能用尚在后台的截图否定一次合法开始。
                return None
            if isinstance(og.device_manager.capture_method, BaseWindowsCaptureMethod):

                if not og.device_manager.capture_method.hwnd_window.pos_valid:
                    hwnd_window = og.device_manager.capture_method.hwnd_window
                    if (not isinstance(og.device_manager.interaction, PostMessageTestInteraction)
                            and hwnd_window.hwnd and hwnd_window.window_width > 0 and hwnd_window.window_height > 0):
                        from ok.util.window import resize_window
                        logger.info(
                            f"Window pos invalid, trying to center window with size {hwnd_window.window_width}x{hwnd_window.window_height}")
                        resize_window(hwnd_window.hwnd, hwnd_window.window_width, hwnd_window.window_height)
                        hwnd_window.do_update_window_size()
                    if not og.device_manager.capture_method.hwnd_window.pos_valid:
                        return self.tr(
                            'Window is minimized or out of screen, and don\'t use full-screen exclusive mode!')
            if frame is None:
                frame = self.try_capture_a_frame(cancel_check=cancel_check)
            if frame is None:
                logger.error('check_device_error: try_capture_a_frame returned None')
                return self.tr('Capture failed, please check game window')
            logger.info(f'check_device_error: capturing frame {frame.shape[1], frame.shape[0]}')
            if og.executor.feature_set is not None:
                logger.info('check_device_error: checking feature_set size')
                if not og.executor.feature_set.check_size(frame):
                    logger.error('check_device_error: feature_set check_size failed')
                    return self.tr(
                        'Image resource load failed, please try install again.(Don\'t put the app in Downloads folder)')
            else:
                logger.info('check_device_error: feature_set is None')

            logger.info('check_device_error: checking resolution')
            resolution_error = self.check_resolution(frame=frame)
            if resolution_error:
                logger.error(f'check_device_error: resolution_error: {resolution_error}')
                return resolution_error

            if device and device['device'] == "adb" and self.config.get('adb'):
                started = og.device_manager.adb_ensure_in_front()
                if not started:
                    return self.tr("Can't start game, make sure the game is installed")
        except (FileNotFoundError, TaskDisabledException):
            raise
        except Exception as e:
            logger.error(f'check_device_error exception: {str(e)}', e)
            return self.tr(str(e))

    @staticmethod
    def try_capture_a_frame(*, time_out=5, cancel_check=None):
        deadline = time.monotonic() + max(0, time_out)
        capture = og.device_manager.capture_method
        while True:
            if cancel_check is not None and cancel_check() is True:
                raise TaskDisabledException()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            # WGC 正常截图可长等；开始诊断采用短探测，取消不必等整段原生截图超时。
            probe = getattr(capture, 'get_frame_for_probe', None)
            frame = (probe(min(0.1, remaining)) if callable(probe)
                     else capture.get_frame(busy_wait=min(0.1, remaining)))
            if cancel_check is not None and cancel_check() is True:
                raise TaskDisabledException()
            if frame is not None:
                return frame
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
