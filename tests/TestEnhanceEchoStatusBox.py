import unittest
from itertools import count
from unittest.mock import Mock

from ok import ForegroundInputError, ForegroundInputInterrupted
from ok.feature.Box import Box
from src.task.ChangeEchoTask import ChangeEchoTask
from src.task.EnhanceEchoTask import EnhanceEchoTask


class FakeEnhanceTask(EnhanceEchoTask):
    """只提供隔离状态框，验证较大的另一张模板也被搜索区域覆盖。"""

    tr = staticmethod(str)

    def __init__(self, boxes, match_name):
        self.boxes = boxes
        self.match_name = match_name
        self.search_calls = []
        self.fail_reason = ''
        self.config = {}
        self.counters = {}
        self._clock = count(0, .1)

    def business_time(self):
        return next(self._clock)

    def get_box_by_name(self, name):
        return self.boxes[name]

    def find_best_match_in_box(self, box, names, threshold=0):
        self.search_calls.append((box, names, threshold))
        return Box(0, 0, 1, 1, name=self.match_name)

    def info_incr(self, name):
        self.counters[name] = self.counters.get(name, 0) + 1

    def info_get(self, name):
        return self.counters.get(name, 0)

    def send_key(self, key, after_sleep=0):
        """隔离按键，不调用任何原生输入。"""

    def log_info(self, *args, **kwargs):
        """日志与状态框几何无关。"""

    def screenshot_echo(self, name):
        """不落盘截图。"""

    def esc(self):
        """不操作实际页面。"""

    def wait_ocr(self, *args, **kwargs):
        return None


class FakeSelectTask:
    """脚本化每次重新点第一格后的 +0 状态，不连接捕获或原生输入。"""

    targets = {(0.13, 0.21): 'first card', (0.35, 0.917): 'sort order'}

    def __init__(self, zero_level_results):
        self.zero_level_results = list(zero_level_results)
        self.clicks = []

    def click(self, x, y, after_sleep=0):
        self.clicks.append(self.targets[(x, y)])

    def is_0_level(self):
        return self.zero_level_results.pop(0)


class TestEnhanceEchoStatusBox(unittest.TestCase):
    # 刻意让另一张模板较大，保留 #1238/#1291 中导致 OpenCV 崩溃的关键几何边界。

    def assertCovers(self, search, template_box):
        self.assertLessEqual(search.x, template_box.x)
        self.assertLessEqual(search.y, template_box.y)
        self.assertGreaterEqual(search.x + search.width,
                                template_box.x + template_box.width)
        self.assertGreaterEqual(search.y + search.height,
                                template_box.y + template_box.height)

    def test_drop_search_box_covers_both_drop_templates(self):
        dropped = Box(534, 152, 28, 26, name='echo_dropped')
        not_dropped = Box(534, 151, 28, 29, name='echo_not_dropped')
        task = FakeEnhanceTask({'echo_dropped': dropped,
                                'echo_not_dropped': not_dropped},
                               match_name='echo_dropped')

        EnhanceEchoTask.trash_and_esc(task)

        [(search, names, threshold)] = task.search_calls
        self.assertEqual(names, ['echo_dropped', 'echo_not_dropped'])
        self.assertEqual(threshold, 0.7)
        self.assertCovers(search, dropped)
        self.assertCovers(search, not_dropped)

    def test_lock_search_box_covers_both_lock_templates(self):
        locked = Box(631, 152, 23, 26, name='echo_locked')
        not_locked = Box(631, 151, 24, 29, name='echo_not_locked')
        task = FakeEnhanceTask({'echo_locked': locked,
                                'echo_not_locked': not_locked},
                               match_name='echo_locked')

        EnhanceEchoTask.lock_and_esc(task)

        [(search, names, threshold)] = task.search_calls
        self.assertEqual(names, ['echo_locked', 'echo_not_locked'])
        self.assertEqual(threshold, 0.7)
        self.assertCovers(search, locked)
        self.assertCovers(search, not_locked)

    def test_first_card_is_used_when_it_is_0_level(self):
        task = FakeSelectTask([True])

        self.assertTrue(EnhanceEchoTask.select_0_level_echo(task))

        self.assertEqual(['first card'], task.clicks)

    def test_list_returns_to_top_when_first_visible_card_is_enhanced(self):
        task = FakeSelectTask([False, True])

        self.assertTrue(EnhanceEchoTask.select_0_level_echo(task))

        self.assertEqual(['first card', 'sort order', 'first card'], task.clicks)

    def test_no_0_level_echo_left(self):
        task = FakeSelectTask([False, False, False])

        self.assertFalse(EnhanceEchoTask.select_0_level_echo(task))

        self.assertEqual(['first card', 'sort order', 'first card', 'sort order', 'first card'], task.clicks)

    def test_partial_sort_recovery_checks_both_orders_before_declaring_no_zero_echo(self):
        task = FakeSelectTask([False, False, True])

        self.assertTrue(EnhanceEchoTask.select_0_level_echo(task))
        self.assertEqual(['first card', 'sort order', 'first card', 'sort order', 'first card'], task.clicks)


class TestEchoForegroundRecovery(unittest.TestCase):
    def make_enhance_task(self, phase):
        task = EnhanceEchoTask.__new__(EnhanceEchoTask)
        task.tr = str
        task.config = {}
        task.fail_reason = '首条无效'
        task._enhance_phase = phase
        task._enhance_needs_refresh = False
        task._enhance_baseline_count = 1
        task._enhance_baseline_signature = (('攻击',), ('10%',))
        properties = [Box(1, 1, 1, 1, name='攻击'), Box(1, 2, 1, 1, name='暴击')]
        values = [Box(2, 1, 1, 1, name='10%'), Box(2, 2, 1, 1, name='7%')]
        task._enhance_snapshot = task._stat_signature(properties, values)
        task._read_echo_stats = Mock(return_value=(properties, values))
        task.wait_foreground_frame = Mock()
        task.sleep = Mock()
        clock = count(0, .1)
        task.business_time = lambda: next(clock)
        task.ocr = Mock(return_value=[])
        task.click = Mock()
        task.wait_ocr = Mock(return_value=[Box(1, 1, 1, 1, name='强化并调谐')])
        task.get_box_by_name = lambda name: Box(1, 1, 20, 20, name=name)
        task.find_best_match_in_box = Mock(return_value=Box(1, 1, 1, 1, name='echo_dropped'))
        task.find_echo_enhance = Mock(return_value=False)
        task.select_0_level_echo = Mock(return_value=False)
        task.check_echo_stats = Mock(return_value=False)
        task.esc = Mock()
        task.screenshot_echo = Mock()
        task.screenshot = Mock()
        task.send_key = Mock()
        task.log_info = Mock()
        task._finish_enhance_batch = Mock()
        counters = {'成功声骸数量': 2, '失败声骸数量': 3}
        task.info_incr = lambda key: counters.__setitem__(key, counters[key] + 1)
        task.info_get = counters.get
        task.info_set = Mock()
        task.esc.side_effect = lambda: task.find_echo_enhance.configure_mock(return_value=True)
        return task, counters

    def test_focus_loss_after_enhance_submission_uses_new_stats_without_resubmitting(self):
        task, counters = self.make_enhance_task('submit')
        task.click.side_effect = ForegroundInputInterrupted('强化已发出后失焦')

        task.resume_after_foreground()

        task.click.assert_called_once()
        task.wait_foreground_frame.assert_called_once()
        self.assertEqual(4, counters['失败声骸数量'])
        self.assertEqual(2, counters['成功声骸数量'])
        self.assertEqual('completed', task._enhance_phase)

    def test_native_failure_after_submission_propagates_without_focus_retry(self):
        task, counters = self.make_enhance_task('submit')
        task.click.side_effect = ForegroundInputError('SendInput 部分成功')

        with self.assertRaises(ForegroundInputError):
            task.resume_after_foreground()

        task.click.assert_called_once()
        task.wait_foreground_frame.assert_not_called()
        self.assertEqual(3, counters['失败声骸数量'])

    def test_mark_applied_before_focus_loss_is_not_toggled_or_counted_twice(self):
        task, counters = self.make_enhance_task('mark')
        task._enhance_outcome = 'discard'
        task.find_best_match_in_box.return_value = Box(1, 1, 1, 1, name='echo_not_dropped')

        def interrupt_after_mark(*args, **kwargs):
            task.find_best_match_in_box.return_value = Box(1, 1, 1, 1, name='echo_dropped')
            raise ForegroundInputInterrupted('丢弃标记已生效后失焦')

        task.send_key.side_effect = interrupt_after_mark
        task.resume_after_foreground()

        task.send_key.assert_called_once_with('z', after_sleep=1)
        task.wait_foreground_frame.assert_called_once()
        self.assertEqual(4, counters['失败声骸数量'])

    def test_focus_loss_during_result_record_keeps_the_confirmed_counter(self):
        task, counters = self.make_enhance_task('mark')
        task._enhance_outcome = 'discard'
        task.screenshot_echo.side_effect = [ForegroundInputInterrupted('截图时失焦'), None]

        task.resume_after_foreground()

        self.assertEqual(4, counters['失败声骸数量'])
        self.assertEqual(1, task.find_best_match_in_box.call_count)
        task.wait_foreground_frame.assert_called_once()

    def test_material_resume_preserves_current_partially_enhanced_echo(self):
        task, counters = self.make_enhance_task('materials')
        task._enhance_needs_refresh = True
        task.ocr.return_value = [Box(1, 1, 1, 1, name='声骸强化')]
        task._put_staged_materials = Mock()
        task._submit_enhance = Mock(side_effect=lambda: setattr(task, '_enhance_phase', 'completed'))

        task.resume_after_foreground()

        task.esc.assert_not_called()
        task.select_0_level_echo.assert_not_called()
        task._put_staged_materials.assert_called_once()
        self.assertEqual(3, counters['失败声骸数量'])

    def test_unchanged_or_replaced_stats_stop_before_more_material_consumption(self):
        task, _ = self.make_enhance_task('inspect')
        task._enhance_baseline_signature = (('生命',), ('10%',))

        with self.assertRaisesRegex(ForegroundInputError, '避免重复消耗'):
            task.resume_after_foreground()

        task.screenshot.assert_called_once_with('enhance_echo_result_unknown')
        task.click.assert_not_called()
        task.send_key.assert_not_called()

    def make_change_task(self):
        task = ChangeEchoTask.__new__(ChangeEchoTask)
        task.tr = str
        task._change_phase = 'result'
        task._change_needs_refresh = False
        task.wait_ocr = Mock(return_value=[Box(1, 1, 1, 1, name='获得声骸')])
        task.wait_click_ocr = Mock()
        task.wait_foreground_frame = Mock()
        task.find_echo_enhance = Mock(return_value=True)
        task.is_0_level = Mock(return_value=False)
        task._finish_change_echoes = Mock()
        task.screenshot = Mock()
        task.click = Mock()
        task.esc = Mock()
        counters = {'成功声骸数量': 4}
        task.info_incr = lambda key: counters.__setitem__(key, counters[key] + 1)
        return task, counters

    def test_change_result_and_focus_loss_during_return_preserve_progress(self):
        task, counters = self.make_change_task()
        task.esc.side_effect = [ForegroundInputInterrupted('已获得声骸，返回时失焦'), None]

        task.resume_after_foreground()

        self.assertEqual(5, counters['成功声骸数量'])
        task.wait_ocr.assert_called_once()
        task.wait_click_ocr.assert_not_called()
        task.wait_foreground_frame.assert_called_once()

    def test_unknown_change_result_never_repeats_reconstruction(self):
        task, counters = self.make_change_task()
        task.wait_ocr.return_value = []

        with self.assertRaisesRegex(ForegroundInputError, '避免重复消耗'):
            task.resume_after_foreground()

        self.assertEqual(4, counters['成功声骸数量'])
        task.wait_click_ocr.assert_not_called()
        task.wait_foreground_frame.assert_not_called()
        task.screenshot.assert_called_once_with('change_echo_result_unknown')


if __name__ == '__main__':
    unittest.main()
