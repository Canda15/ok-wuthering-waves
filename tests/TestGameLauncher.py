"""游戏只连接策略与外部开始请求回归，不访问真实窗口或输入设备。"""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from ok import OK
from ok.core.mcp_runner import McpTaskRunner
from ok.core.mcp_server import LocalMcpServer
from ok.core.start_controller import StartController
from ok.core.start_request import StartRequest
from ok.task.TaskExecutor import TaskExecutor
from ok.task.exceptions import TaskDisabledException
from ok.task.task import BaseTask
from ok.ui.qt.shell import run_bar
from ok.ui.qt.tasks import tasks_page
from ok.util.GlobalConfig import GlobalConfig, register_basic_options
from ok.util.config import Config, ConfigOption
from src.task.BaseWWTask import BaseWWTask


class _Task:
    unpause = BaseTask.unpause
    disable = BaseTask.disable

    def __init__(self, executor):
        self.executor = executor
        self._enabled = False
        self._paused = False
        self._foreground_resume_pending = False
        self.info_clear = Mock()
        self.first_run_alert = None
        self.config = {}
        self.name = '合成任务'
        self.enable_after_start = False
        self.exit_after_task = False

    @property
    def enabled(self):
        return self._enabled

    @property
    def paused(self):
        return self._paused


class _Executor:
    has_start_request = TaskExecutor.has_start_request
    queue_start_request = TaskExecutor.queue_start_request
    cancel_start_request = TaskExecutor.cancel_start_request
    remove_onetime_task = TaskExecutor.remove_onetime_task
    _prepare_task_for_run = TaskExecutor._prepare_task_for_run

    def __init__(self, background=True):
        self.lock = threading.RLock()
        self.exit_event = threading.Event()
        self.current_task = None
        self.exclusive_owner = None
        self.paused = True
        self.onetime_tasks = []
        self.onetime_task_queue = []
        self.events = []
        self.requests = []
        self.background = background
        self.scene = None
        self.interaction = None
        self.device_manager = Mock()
        self.release_inputs = Mock()
        self.check_enabled = Mock()
        self.check_input_cancelled = Mock()
        self._wake_executor = Mock()

    def prepare_start(self, request, **kwargs):
        check = kwargs.get('cancel_check')
        if check:
            check()
        self.requests.append(request)
        if self.background:
            request.activate_once(('合成窗口', 1), lambda: self.events.append('激活'))
        self.events.append('首帧')
        return True

    def enqueue_onetime_task(self, task):
        if task not in self.onetime_task_queue:
            self.onetime_task_queue.append(task)

    def get_all_tasks(self):
        return self.onetime_tasks

    def start(self):
        self.paused = False
        self.events.append('继续')

    def pause(self):
        self.release_inputs()
        self.paused = True

    def connected(self):
        return True

    def nullable_frame(self):
        return None

    def active_trigger_task_count(self):
        return 0

    def can_capture(self):
        return True

    @property
    def is_background_input(self):
        return self.background


class TestGameLauncher(unittest.TestCase):
    def setUp(self):
        self.executor = _Executor()
        self.task = _Task(self.executor)
        self.executor.onetime_tasks = [self.task]
        self.manager = Mock()
        self.manager.get_preferred_device.return_value = {'device': 'windows', 'connected': True}
        self.posted = []
        self.controller = StartController.__new__(StartController)
        self.controller.config = {'windows': {'start_exe': True, 'launch_arguments': Mock()}}
        self.controller.exit_event = self.executor.exit_event
        self.controller.handler = SimpleNamespace(post=self.posted.append)
        self.controller.start_timeout = 5
        self.controller.starting = False
        self.controller.check_device_error = Mock(return_value=None)
        self.controller.check_auto_hdr = Mock()
        self.controller.check_gpu_driver_post_processing = Mock()
        self.app = SimpleNamespace(start_controller=self.controller)
        self.globals = SimpleNamespace(executor=self.executor, device_manager=self.manager, app=self.app)
        self.app.tr = lambda text: text
        for target, value in (
                ('ok.core.start_controller.og', self.globals),
                ('ok.ui.qt.tasks.tasks_page.og', self.globals),
                ('ok.og', self.globals)):
            scoped = patch(target, value)
            scoped.start()
            self.addCleanup(scoped.stop)
        for target in ('ok.core.start_controller.communicate', 'ok.task.task.communicate',
                       'ok.task.TaskExecutor.communicate'):
            scoped = patch(target)
            scoped.start()
            self.addCleanup(scoped.stop)
        scoped = patch('ok.util.process.execute')
        self.execute_game = scoped.start()
        self.addCleanup(scoped.stop)

    def flush(self):
        callback = self.posted.pop(0)
        return callback()

    def test_热键和鼠标总开始暂停恢复各只有一次激活(self):
        for background in (True, False):
            for source in ('开始热键', '总开始按钮'):
                with self.subTest(background=background, source=source):
                    self.executor.background = background
                    self.executor.paused = True
                    self.executor.events.clear()
                    bar = SimpleNamespace(_compute_state=lambda: run_bar.compute_run_state(
                        self.executor, self.manager, app_name='NoNo'))
                    run_bar.RunBar.toggle_executor(bar, source)
                    self.assertTrue(self.flush())
                    run_bar.RunBar.toggle_executor(bar, source)
                    self.assertEqual(self.executor.events.count('激活'), int(background))
                    self.assertTrue(self.executor.paused)
                    run_bar.RunBar.toggle_executor(bar, source)
                    self.assertTrue(self.flush())
                    self.assertEqual(self.executor.events.count('激活'), 2 * int(background))
                    self.assertEqual(self.executor.requests[-1].source, source)
        self.execute_game.assert_not_called()

    def test_任务卡首次开始和双重暂停恢复不重跑任务(self):
        tasks_page.start_task(self.task, None)
        self.assertTrue(self.flush())
        self.executor.current_task = self.task
        self.task._paused = True
        self.executor.paused = True
        tasks_page.start_task(self.task, None)
        self.assertTrue(self.flush())
        self.assertFalse(self.task.paused)
        self.assertFalse(self.executor.paused)
        self.assertEqual(self.executor.events.count('激活'), 2)
        self.assertEqual(self.executor.onetime_task_queue, [self.task])
        tasks_page.stop_task(self.task)
        self.assertEqual(self.executor.events.count('激活'), 2)
        self.assertFalse(self.task.enabled)

    def test_相同任务准备去重并允许取消后重新开始(self):
        self.assertTrue(self.controller.start(self.task))
        self.assertFalse(self.controller.start(self.task))
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(self.controller.cancel_task(self.task))
        self.assertTrue(self.controller.start(self.task))
        self.assertFalse(self.flush())
        self.assertTrue(self.flush())
        self.assertEqual(self.executor.events.count('激活'), 1)

    def test_运行中新任务留到任务边界再激活(self):
        current = _Task(self.executor)
        current._enabled = True
        self.executor.current_task = current
        self.executor.paused = False
        self.assertTrue(self.controller.do_start(self.task))
        self.assertFalse(self.executor.events)
        self.assertTrue(self.controller.is_task_pending(self.task))
        self.assertFalse(self.controller.start(self.task))
        self.executor.current_task = self.task
        self.executor._prepare_task_for_run(False)
        self.assertEqual(self.executor.events.count('激活'), 1)
        self.executor._prepare_task_for_run(False)
        self.assertEqual(self.executor.events.count('激活'), 1)
        self.assertFalse(self.controller.is_task_pending(self.task))

    def test_暂停中新任务只排队且取消前不会激活或恢复旧任务(self):
        current = _Task(self.executor)
        current._enabled = current._paused = True
        self.executor.current_task = current
        self.assertTrue(self.controller.do_start(self.task))
        self.assertTrue(current.paused)
        self.assertTrue(self.executor.paused)
        self.assertTrue(self.controller.is_task_pending(self.task))
        self.assertFalse(self.executor.events)
        self.assertTrue(self.controller.cancel_task(self.task))
        self.assertFalse(self.executor.events)

    def test_取消已经排队的任务不留下无准备执行入口(self):
        self.executor.current_task = _Task(self.executor)
        self.executor.paused = False
        self.assertTrue(self.controller.do_start(self.task))
        self.assertTrue(self.controller.cancel_task(self.task))
        self.assertFalse(self.task.enabled)
        self.assertFalse(self.executor.onetime_task_queue)
        self.assertFalse(self.executor.events)

    def test_无游戏的UI无界面MCP和兼容入口不启动进程(self):
        self.manager.get_preferred_device.return_value = {'device': 'windows', 'connected': False}
        self.assertTrue(self.controller.start(self.task))
        self.assertFalse(self.flush())
        self.assertFalse(self.controller.do_start(0))
        self.assertFalse(self.controller.start_device())
        server = LocalMcpServer.__new__(LocalMcpServer)
        server.runtime = SimpleNamespace(task_executor=self.executor)
        server.runner = SimpleNamespace(app=self.app)
        with self.assertRaisesRegex(RuntimeError, '请先手动打开游戏'):
            server._prepare_device()
        self.execute_game.assert_not_called()
        self.manager.wait_for_binding.assert_not_called()
        self.controller.config['windows']['launch_arguments'].assert_not_called()
        self.assertFalse(self.executor.events)
        self.assertFalse(self.task.enabled)

    def test_连接期间取消不产生激活或启用任务(self):
        self.manager.wait_for_binding.side_effect = lambda **kwargs: self.controller.cancel_start()
        self.controller.start(self.task)
        self.assertFalse(self.flush())
        self.assertFalse(self.executor.events)
        self.assertFalse(self.task.enabled)

    def test_连接期间MCP接管会拒绝GUI准备且零激活(self):
        self.manager.wait_for_binding.side_effect = lambda **kwargs: setattr(
            self.executor, 'exclusive_owner', 'mcp:测试')
        self.assertFalse(self.controller.do_start(self.task, source='任务卡开始'))
        self.assertFalse(self.executor.events)
        self.assertFalse(self.task.enabled)
        self.assertFalse(self.executor.onetime_task_queue)
        self.execute_game.assert_not_called()

    def test_连接只检查绑定并在模式准备后使用已有帧诊断(self):
        frame = np.zeros((20, 30, 3), dtype=np.uint8)
        self.executor.feature_set = None
        self.executor.nullable_frame = lambda: frame
        self.controller.check_device_error = StartController.check_device_error.__get__(self.controller)
        self.controller.check_resolution = Mock(return_value=None)
        capture = self.manager.capture_method
        capture.get_frame.side_effect = AssertionError('连接阶段不应提前抓帧')
        capture.get_frame_for_probe.side_effect = AssertionError('已有准备帧不应重新抓帧')
        self.assertTrue(self.controller.do_start(self.task))
        self.controller.check_resolution.assert_called_once_with(frame=frame)
        self.assertEqual(self.executor.events, ['激活', '首帧', '继续'])

    def test_只读捕获诊断用短探测且取消原样上抛(self):
        cancelled = threading.Event()
        capture = self.manager.capture_method
        capture.get_frame_for_probe.side_effect = lambda timeout: cancelled.set()
        self.controller.check_device_error = StartController.check_device_error.__get__(self.controller)
        with self.assertRaises(TaskDisabledException):
            self.controller.check_device_error(cancel_check=cancelled.is_set)
        capture.get_frame_for_probe.assert_called_once()
        self.assertLessEqual(capture.get_frame_for_probe.call_args.args[0], 0.1)
        capture.get_frame.assert_not_called()
        self.assertFalse(self.executor.events)

    def test_MCP只在运行请求准备而设备查询不激活(self):
        server = LocalMcpServer.__new__(LocalMcpServer)
        server.runtime = SimpleNamespace(task_executor=self.executor)
        server.runner = SimpleNamespace(app=self.app)
        server._prepare_device()
        self.assertFalse(self.executor.events)
        with tempfile.TemporaryDirectory() as directory:
            runner = McpTaskRunner.__new__(McpTaskRunner)
            runner.executor = self.executor
            runner.app = self.app
            runner.prepare = server._prepare_device
            runner._phase = Mock()
            runner._runs = {'合成请求': {'record': {'artifact_directory': directory}}}
            source = b"""from ok.task.task import BaseTask
class Task(BaseTask):
    def __init__(self, executor, app): self._executor = executor
    def after_init(self, **kwargs): pass
    def post_init(self): pass
    def ensure_capture(self): pass
    def reset_to_start(self): self.executor.events.append('reset')
    def verify_start_state(self): return True
    def run(self): self.executor.events.append('run')
    def on_destroy(self): pass
"""
            runner._execute('合成请求', Path(directory) / 'task.py', source)
        self.assertEqual(self.executor.events, ['激活', '首帧', 'reset', 'run'])
        self.assertEqual(self.executor.requests[0].source, 'MCP运行任务')
        self.execute_game.assert_not_called()

    def test_热键注册禁止长按重复(self):
        native = SimpleNamespace(user32=Mock())
        with patch.object(run_bar, 'windll', native):
            run_bar.RunBar._rebind_hotkey('F9')
        native.user32.RegisterHotKey.assert_called_once_with(None, run_bar.HOTKEY_ID, 0x4000, 0x78)

    def test_旧自动开游戏设置被定向移除其它用户值保留(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Config, 'config_folder', directory):
            option = ConfigOption('Basic Options', {'Auto Start Game When App Starts': True,
                'Launch with DX11': True, 'Start/Stop': 'F10', '保留配置': 7})
            global_config = GlobalConfig([option])
            settings = register_basic_options(global_config)
            self.assertNotIn('Auto Start Game When App Starts', settings)
            self.assertNotIn('Launch with DX11', settings)
            self.assertEqual(settings['保留配置'], 7)
            self.assertEqual(settings['Start/Stop'], 'F10')
            self.assertTrue(option.config_type['Launch with DX11']['hidden'])

    def test_旧自动开游戏开关不触发运行且命令行仍可显式请求(self):
        runtime = SimpleNamespace(_runtime_start_lock=threading.Lock(), _runtime_started=False,
            args={}, app=SimpleNamespace(start_controller=Mock(), initialize_overlay=Mock()),
            should_init_task_manager_headless=lambda: False, device_manager=Mock(),
            global_config=Mock())
        runtime.global_config.get_config.return_value = {'Auto Start Game When App Starts': True}
        OK.start_runtime(runtime)
        runtime.app.start_controller.start.assert_not_called()
        runtime.device_manager.refresh.assert_called_once()
        runtime._runtime_started = False
        runtime.args = {'task': 1, 'exit': False}
        OK.start_runtime(runtime)
        runtime.app.start_controller.start.assert_called_once_with(0, exit_after=False, source='命令行任务')

    def test_游戏更新提示在重启确认之前终止(self):
        task = SimpleNamespace(logged_in=False, in_team_and_world=Mock(return_value=False),
            handle_monthly_card=Mock(), find_one=Mock(return_value=None), ocr=Mock(return_value=[]),
            debug=False, box_of_screen=Mock(), find_boxes=Mock(side_effect=[[], [], ['即将重启']]),
            click=Mock(), start_device=Mock(), executor=self.executor)
        with self.assertRaisesRegex(RuntimeError, '手动完成更新'):
            BaseWWTask.wait_login(task)
        task.click.assert_not_called()
        task.start_device.assert_not_called()
        self.executor.release_inputs.assert_called_once()


class TestStartRequest(unittest.TestCase):
    def test_并发调用同一请求最多发送一次(self):
        request = StartRequest('并发测试')
        sent = []
        barrier = threading.Barrier(5)
        results = []
        def send():
            barrier.wait(timeout=2)
            results.append(request.activate_once(1, lambda: sent.append(1)))
        workers = [threading.Thread(target=send) for _ in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sent, [1])
        self.assertEqual(sorted(results), [False, False, False, True])

    def test_激活失败和窗口替换都不能重试同一请求(self):
        request = StartRequest('失败测试')
        send = Mock(side_effect=OSError('受控发送失败'))
        with self.assertRaises(OSError):
            request.activate_once(1, send)
        self.assertTrue(request.activation_attempted)
        self.assertFalse(request.activation_accepted)
        for identity in (1, 2):
            with self.subTest(identity=identity), self.assertRaises(RuntimeError):
                request.activate_once(identity, send)
        send.assert_called_once()

    def test_取消请求从未发送激活(self):
        request = StartRequest('取消测试')
        request.cancel()
        send = Mock()
        with self.assertRaises(TaskDisabledException):
            request.activate_once(1, send)
        send.assert_not_called()
        self.assertFalse(request.activation_attempted)


if __name__ == '__main__':
    unittest.main()
