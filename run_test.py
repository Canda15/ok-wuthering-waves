"""在隔离用户目录中运行测试，避免截图清理和 OCR 线程影响开发环境。"""

import faulthandler
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch


BACKGROUND_ERROR_FILE = '.background-thread-error'


def translate_for_test(task, message):
    # 替身和 TaskTestCase 可以在尚未构造应用时创建任务。
    app = getattr(task, '_app', None)
    return app.tr(message) if app is not None else message


def run_tests(directory, arguments):
    # Windows CI 可能报告虚拟机无法执行的 AMX；在加载 OpenVINO 前限制指令集。
    os.environ.setdefault('ONEDNN_MAX_CPU_ISA', 'AVX2')
    os.environ['NONO_USER_DATA_ROOT'] = directory
    os.environ['OK_DISABLE_FILE_LOG'] = '1'
    original_excepthook = threading.excepthook

    def record_background_error(arguments):
        try:
            original_excepthook(arguments)
        finally:
            if not issubclass(arguments.exc_type, SystemExit):
                (Path(directory) / BACKGROUND_ERROR_FILE).touch()

    # 保留到子进程退出，覆盖 unittest 已结束但截图等非守护线程仍在收尾的异常。
    threading.excepthook = record_background_error
    sys.argv = ['run_test.py', *arguments]
    faulthandler.enable()
    faulthandler.dump_traceback_later(120, repeat=True)
    # 部分截图测试强制 debug=True，旧框架会清空默认截图目录。
    # 必须在导入测试模块前隔离全部写入路径，不能只替换配置目录。
    from ok import Config
    Config.config_folder = os.path.join(directory, 'configs')
    from config import config
    config['config_folder'] = Config.config_folder
    config['disable_file_log'] = True
    config['screenshots_folder'] = os.path.join(directory, 'screenshots')
    config['click_screenshots_folder'] = None
    for key, filename in {
        'log_file': 'NoNo.log',
        'error_log_file': 'NoNo_error.log',
        'launcher_log_file': 'launcher.log',
        'launcher_error_log_file': 'launcher_error.log',
    }.items():
        config[key] = os.path.join(directory, 'logs', filename)
    try:
        # 按需同步加载真实 OCR；仅模板测试不启动模型预热线程。
        with patch('ok.task.TaskExecutor.TaskExecutor.init_default_ocr', lambda self: None), \
                patch('ok.task.task.BaseTask.tr', translate_for_test):
            unittest.main(module=None, verbosity=2)
    finally:
        # 失败的 setUpClass 不会执行 tearDownClass，也要停止已创建的测试运行时。
        from ok.test import ok as test_runtime
        if test_runtime is not None and not test_runtime.exit_event.is_set():
            test_runtime.quit()
        faulthandler.cancel_dump_traceback_later()


def main():
    # 父进程持有目录到整个 Python 子进程退出，截图、日志线程不会在目录删除后写入。
    bootstrap = 'import sys; from run_test import run_tests; run_tests(sys.argv[1], sys.argv[2:])'
    with tempfile.TemporaryDirectory(prefix='NoNo-核心测试-') as directory:
        environment = os.environ.copy()
        environment['NONO_USER_DATA_ROOT'] = directory
        environment['OK_DISABLE_FILE_LOG'] = '1'
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-B', '-u', '-c', bootstrap, directory, *sys.argv[1:]],
            cwd=Path(__file__).resolve().parent,
            env=environment,
        )
        return_code = result.returncode
        if return_code == 0 and (Path(directory) / BACKGROUND_ERROR_FILE).exists():
            print('后台线程发生未捕获异常，测试失败。', file=sys.stderr)
            return_code = 1
    raise SystemExit(return_code)


if __name__ == '__main__':
    main()
