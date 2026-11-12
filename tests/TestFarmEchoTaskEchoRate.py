import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ok.task.exceptions import TaskDisabledException
from ok.task.task import BaseTask
from src.task.BaseWWTask import BaseWWTask
from src.task.FarmEchoTask import FarmEchoTask
from src.task.WWOneTimeTask import WWOneTimeTask


class FakeFarmEchoTask:
    """FarmEchoTask.run with a scripted farming loop: each do_run picks one
    echo; with fail_first the first one ends on a claim popup after an hour."""

    run = FarmEchoTask.run
    incr_drop = BaseWWTask.incr_drop

    def __init__(self, fail_first=False):
        self.start_time = 0  # ok-script default; only the executor sets it when it starts a task
        self.info = {}
        self.config = {}
        self.fail_first = fail_first

    def do_run(self):
        if self.fail_first:
            self.fail_first = False
            self.start_time -= 3600  # an hour of farming before the popup
            self.incr_drop(True)
            raise Exception('claim popup')
        self.incr_drop(True)

    def handle_claim_button(self):
        return True

    def handle_monthly_card(self):
        return False


class FakeDailyTask:
    run_task_by_class = BaseTask.run_task_by_class

    def __init__(self, farm_echo_task):
        self.info = {}
        self.farm_echo_task = farm_echo_task
        self.executor = SimpleNamespace(task_context=lambda task: nullcontext())

    def get_task_by_class(self, cls):
        assert cls is FarmEchoTask
        return self.farm_echo_task


@patch.object(WWOneTimeTask, 'run')
class TestFarmEchoTaskEchoRate(unittest.TestCase):

    def test_echo_per_hour_counts_from_the_4c_start_after_daily(self, _):
        daily = FakeDailyTask(FakeFarmEchoTask())

        daily.run_task_by_class(FarmEchoTask)

        # one echo right after the 4C start; before the fix the rate was
        # measured from 1970 and rounded to 0
        self.assertEqual(1, daily.info['Echo Count'])
        self.assertEqual(3600, daily.info['Echo per Hour'])

    def test_retry_after_claim_popup_keeps_the_start_time(self, _):
        task = FakeFarmEchoTask(fail_first=True)

        task.run()

        # two echoes in about an hour; restarting the clock on the retry
        # would report 7200
        self.assertEqual(2, task.info['Echo Count'])
        self.assertEqual(2, task.info['Echo per Hour'])

    def test_已完成轮次后取消仍上抛且不进入弹窗恢复(self, _):
        task = FakeFarmEchoTask()

        def cancel_after_pickup():
            task.incr_drop(True)
            raise TaskDisabledException()

        task.do_run = Mock(side_effect=cancel_after_pickup)
        task.handle_claim_button = Mock()
        task.handle_monthly_card = Mock()

        with self.assertRaises(TaskDisabledException):
            task.run()

        # 已确认的拾取计数保留，取消不能成为正常完成或重新进入领奖／月卡恢复。
        self.assertEqual(1, task.info['Echo Count'])
        task.do_run.assert_called_once()
        task.handle_claim_button.assert_not_called()
        task.handle_monthly_card.assert_not_called()


if __name__ == '__main__':
    unittest.main()
