"""在隔离用户目录中运行测试，避免截图清理和 OCR 线程影响开发环境。"""

import faulthandler
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import weakref
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch


BACKGROUND_ERROR_FILE = '.background-thread-error'
TEMPORARY_CLEANUP_ERROR_FILE = '.temporary-cleanup-error'


class _TestTemporaryDirectory(tempfile.TemporaryDirectory):
    """只对本测试进程创建的目录兼容 Windows 短暂的目录非空错误。"""

    _cleanup_error_file = None

    def __init__(self, suffix=None, prefix=None, dir=None, ignore_cleanup_errors=False, *, delete=True):
        if ignore_cleanup_errors:
            raise ValueError('隔离测试不能忽略临时目录清理错误。')
        # 自行安装 finalizer，避免标准库回调在所有权校验失败后绕过保护。
        self.name = tempfile.mkdtemp(suffix, prefix, dir)
        self._ignore_cleanup_errors = False
        self._delete = delete
        path = Path(os.fsdecode(self.name))
        identity = path.lstat()
        self._owner = (os.getpid(), self.name, path.resolve(strict=True), identity.st_dev, identity.st_ino)
        # 回调只持有创建时的身份快照，不持有 self 或实例绑定方法。
        self._finalizer = weakref.finalize(
            self, type(self)._finalize_owned, self._owner, delete, self._cleanup_error_file,
        )

    @classmethod
    def _cleanup_owned(cls, owner):
        process_id, name, resolved_path, device, inode = owner
        if os.getpid() != process_id:
            raise RuntimeError('临时目录不属于当前测试进程，拒绝清理。')
        path = Path(os.fsdecode(name))
        for attempt in range(5):
            try:
                identity = path.lstat()
            except FileNotFoundError:
                return
            if (not stat.S_ISDIR(identity.st_mode)
                    or getattr(identity, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or (identity.st_dev, identity.st_ino) != (device, inode)
                    or path.resolve(strict=True) != resolved_path):
                raise RuntimeError('临时目录身份已变化，拒绝清理。')
            try:
                cls._rmtree(name)
                return
            except OSError as error:
                if sys.platform != 'win32' or getattr(error, 'winerror', None) != 145 or attempt == 4:
                    raise
                # 已在单线程标准库写入后复现；总等待最多 0.4 秒，重试前重新核验身份。
                time.sleep(0.1)

    @classmethod
    def _finalize_owned(cls, owner, delete, error_file):
        if not delete:
            return
        try:
            cls._cleanup_owned(owner)
        except Exception:
            # finalizer 的异常不会改变退出码，交由持有外层目录的父进程判为失败。
            if error_file is not None:
                error_file.touch()
            raise
        warnings.warn(f'隐式清理测试临时目录：{owner[1]}', ResourceWarning)

    def cleanup(self):
        # 显式拒绝或失败后不能遗留另一个绕过校验的隐式删除入口。
        self._finalizer.detach()
        if self.name != self._owner[1]:
            raise RuntimeError('临时目录路径已被修改，拒绝清理。')
        self._cleanup_owned(self._owner)


@contextmanager
def _periodic_tracebacks(interval=120):
    # CPython 3.12 原生定时器可能与线程退出竞争；Python 线程持有 GIL 后同步抓栈。
    stopped = threading.Event()

    def dump_periodically():
        while not stopped.wait(interval):
            faulthandler.dump_traceback(all_threads=True)

    worker = threading.Thread(target=dump_periodically, name='测试定时诊断', daemon=True)
    worker.start()
    try:
        yield worker
    finally:
        # 唤醒等待并完整结束线程，避免收尾后仍访问 stderr。
        stopped.set()
        worker.join()


def translate_for_test(task, message):
    # 替身和 TaskTestCase 可以在尚未构造应用时创建任务。
    app = getattr(task, '_app', None)
    return app.tr(message) if app is not None else message


def run_tests(directory, arguments):
    # 在导入应用或测试前覆盖工厂；错误标记固定在父进程持有的目录，不跟随测试环境变更。
    _TestTemporaryDirectory._cleanup_error_file = Path(directory) / TEMPORARY_CLEANUP_ERROR_FILE
    tempfile.TemporaryDirectory = _TestTemporaryDirectory
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
    with _periodic_tracebacks():
        _run_test_suite(directory)


def _run_test_suite(directory):
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


def main():
    # 父进程持有目录到整个 Python 子进程退出，截图、日志线程不会在目录删除后写入。
    bootstrap = 'import sys; from run_test import run_tests; run_tests(sys.argv[1], sys.argv[2:])'
    with _TestTemporaryDirectory(prefix='NoNo-核心测试-') as directory:
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
        if (Path(directory) / TEMPORARY_CLEANUP_ERROR_FILE).exists():
            print('临时目录自动清理失败，测试失败。', file=sys.stderr)
            if return_code == 0:
                return_code = 1
    raise SystemExit(return_code)


if __name__ == '__main__':
    main()
