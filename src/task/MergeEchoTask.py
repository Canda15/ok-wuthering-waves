import re
from ok import ForegroundInputError, ForegroundInputInterrupted

from src.Labels import Labels
from src.task.BaseWWTask import BaseWWTask
from src.task.task_icons import task_icon

FULL_BATCH_PATTERN = re.compile(r"100\s*/\s*100")
MERGE_RESULT_PATTERN = re.compile(r'获得声骸|獲得聲骸|获得物品|獲得物品|Rewards? Obtained', re.I)


class MergeEchoTask(BaseWWTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "♻️ Merge Discarded Echoes"
        self.icon = task_icon('synthesis')
        self.group_name = 'Echo Management'
        self.group_icon = task_icon('category-echo')
        self.description = (
            "Must have 1000 discarded Echo to Run, "
            "Merge discarded Echoes in full batches of 100."
        )
        self.notify_if_not_enough = True

    def run(self):
        self._merge_phase = 'prepare'
        self._merge_eligible = False
        self._merge_result_known = False
        self._merge_needs_refresh = False
        return self.resume_after_foreground()

    def on_foreground_resume(self):
        super().on_foreground_resume()
        self._merge_needs_refresh = True
        if getattr(self, '_merge_active', False):
            raise ForegroundInputInterrupted('声骸融合页面已变化，需要重新确认当前阶段')

    def resume_after_foreground(self):
        if not getattr(self, '_merge_phase', None):
            return super().resume_after_foreground()
        self._merge_active = True
        try:
            while self._merge_phase != 'completed':
                try:
                    if self._merge_needs_refresh:
                        self.wait_foreground_frame()
                        self._merge_needs_refresh = False
                        if self._merge_phase in ('prepare', 'open', 'batch'):
                            self._merge_phase = 'prepare'
                        elif self._merge_phase == 'result':
                            self._recover_merge_result()
                        elif self._merge_phase == 'close':
                            self._close_merge_result(recovering=True)
                    if self._merge_phase == 'prepare':
                        if not self._prepare_merge_bag():
                            self._merge_phase = 'completed'
                            return
                        self._merge_phase = 'open'
                    if self._merge_phase == 'open':
                        self.open_merge_page()
                        self._merge_phase = 'batch'
                    if self._merge_phase == 'batch' and not self.merge_full_batch():
                        self._merge_phase = 'finish'
                    if self._merge_phase == 'finish':
                        self.ensure_main()
                        self._merge_phase = 'completed'
                except ForegroundInputInterrupted:
                    self._merge_needs_refresh = True
        finally:
            self._merge_active = False

    def _prepare_merge_bag(self):
        self.ensure_main()
        hotkey = self.key_config.get('Bag Key', 'b')
        self.send_key(hotkey)
        if not self.wait_until(lambda: not self.in_team_and_world(), time_out=5, raise_if_not_found=False):
            self.log_error(self.tr('can not open bag with hotkey {hotkey}').format(hotkey=hotkey), notify=True)
            return False
        self.sleep(3)
        if self.has_discard_merge_dialog():
            # 资格在只读确认后保存；关闭提示后的恢复不要求提示再次出现。
            self._merge_eligible = True
            self.click_dialog_left_button()
            self.sleep(2)
        elif not self._merge_eligible:
            if self.notify_if_not_enough:
                self.log_error(self.tr('Must have 1000 discarded Echo to Run'), notify=True)
            self.ensure_main()
            return False
        self.click_relative(0.034, 0.293, after_sleep=1)
        self.click_relative(0.580, 0.911, after_sleep=4)
        return True

    def _merge_result(self, time_out=3):
        return self.wait_ocr(match=MERGE_RESULT_PATTERN, time_out=time_out, raise_if_not_found=False)

    def _merge_unknown(self):
        self.screenshot('merge_echo_result_unknown')
        raise ForegroundInputError('无法确认声骸融合是否完成，已停止以避免重复消耗')

    def _recover_merge_result(self):
        if self._merge_result():
            self._merge_result_known = True
            self._merge_phase = 'close'
            self._close_merge_result()
            return
        # 仍可取消的确认窗属于未提交阶段；取消后重新选择，不能重复确认旧批次。
        if self.find_one(['confirm_btn_hcenter_vcenter', 'confirm_btn_highlight_hcenter_vcenter']) and self.has_discard_merge_dialog():
            self.click_dialog_left_button()
            self._merge_phase = 'prepare'
            return
        self._merge_unknown()

    def _close_merge_result(self, recovering=False):
        if recovering and not self._merge_result(time_out=1):
            if self._merge_result_known:
                # 资源结果已确认，但关闭可能已生效；重新进入背包核对剩余批次，不能靠背景图标猜弹窗已关闭。
                self._merge_phase = 'prepare'
                return
            self._merge_unknown()
        self.click_relative(0.496, 0.972, after_sleep=1, hcenter=True)
        self._merge_result_known = False
        self._merge_phase = 'batch'

    def has_discard_merge_dialog(self):
        return bool(self.find_one([
            Labels.cancel_button_hcenter_vcenter,
            Labels.cancel_button_highlight_hcenter_vcenter,
        ]))

    def merge_echoes(self):
        self.open_merge_page()
        while self.merge_full_batch():
            pass

        self.ensure_main()

    def open_merge_page(self):
        self.click_relative(0.602, 0.124, after_sleep=0.5, hcenter=True)
        self.click_relative(0.520, 0.904, after_sleep=2, hcenter=True)
        self.click_relative(0.041, 0.918, after_sleep=1)
        self.click_relative(0.826, 0.840, after_sleep=0.5)
        self.click_relative(0.717, 0.204, after_sleep=0.5)
        self.click_relative(0.041, 0.918, after_sleep=0.5)

    def merge_full_batch(self):
        self.click_relative(0.310, 0.915, after_sleep=0.5)
        self.wait_click_feature(Labels.echo_select_all, horizontal_variance=0.3, after_sleep=1)
        full_batch = self.ocr(0.670, 0.660, 0.895, 0.958, match=FULL_BATCH_PATTERN)
        if not full_batch:
            self.log_info(self.tr('All full batches of discarded Echoes have been merged.'))
            return False
        # 提交前保存阶段；点击或等待中断后只确认结果，不重新消耗同一批次。
        self._merge_phase = 'result'
        self.click_relative(0.782, 0.910)
        self.wait_click_skip_dialog_confirm()
        self.sleep(3)
        if not self._merge_result():
            self._merge_unknown()
        self._merge_result_known = True
        self._merge_phase = 'close'
        self._close_merge_result()
        return True
