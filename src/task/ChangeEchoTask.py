# ChangeEchoTask.py
import os
import re

from ok import FindFeature, Logger, ForegroundInputError, ForegroundInputInterrupted
from src.task.BaseWWTask import BaseWWTask
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)

number_pattern = re.compile(r"^[\d.%]+$")
property_pattern = re.compile(r"^\D*$")


class ChangeEchoTask(BaseWWTask, FindFeature):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "🎛️ 批量修改声骸主属性(仅支持简中游戏语言)"
        self.icon = task_icon('echo-tuning')
        self.group_name = 'Echo Management'
        self.group_icon = task_icon('category-echo')
        self.description = "点击B进入背包, 在过滤器中选择需要修改主属性的声骸, 并按照等级从0排序后开始."
        self.supported_languages = ["zh_CN"]
        self.fail_reason = ""
        self.add_text_fix(
            {'凝夜自霜': '凝夜白霜', '主属性灭伤害加成': '主属性湮灭伤害加成', "灭伤害加成": "主属性湮灭伤害加成",
             '主属性行射伤害加成': '主属性衍射伤害加成'})
        self.default_config.update({
            '目标属性': '攻击',
        })
        self.config_type["目标属性"] = {'type': "drop_down",
                                        'options': ['攻击', '暴击伤害', '暴击', '生命', '防御',
                                                    '共鸣效率', "冷凝伤害加成",
                                                    "热熔伤害加成",
                                                    "导电伤害加成",
                                                    "气动伤害加成", "衍射伤害加成", "湮灭伤害加成", ]}

    def find_echo_enhance(self):
        return self.ocr(0.82, 0.86, 0.97, 0.96, match='培养')

    def is_0_level(self):
        return self.ocr(0.66, 0.48, 0.77, 0.56, match=re.compile('声骸技能'))

    def run(self):
        self.info_set('成功声骸数量', 0)
        self._change_phase = 'bag'
        self._change_needs_refresh = False
        return self.resume_after_foreground()

    def on_foreground_resume(self):
        super().on_foreground_resume()
        self._change_needs_refresh = True
        if getattr(self, '_change_active', False):
            raise ForegroundInputInterrupted('主属性修改页面已变化，需要重新确认当前阶段')

    def resume_after_foreground(self):
        if not getattr(self, '_change_phase', None):
            return super().resume_after_foreground()
        self._change_active = True
        try:
            while self._change_phase != 'completed':
                try:
                    if self._change_needs_refresh:
                        self.wait_foreground_frame()
                        self._change_needs_refresh = False
                        if self._change_phase in ('bag', 'enter', 'choose', 'choice_confirm', 'ready'):
                            # 主属性选择尚未消耗资源，退出弹窗后从当前筛选的 +0 声骸重新确认。
                            self.esc()
                            self._change_phase = 'bag'
                    if self._change_phase == 'bag':
                        enhance = self.find_echo_enhance()
                        if not enhance:
                            raise Exception(self.tr("Start after filtering Echoes on the bag's Echo screen!"))
                        if not self.is_0_level():
                            self._finish_change_echoes()
                            self._change_phase = 'completed'
                            return
                        self._change_target = self.config.get('目标属性')
                        self._change_phase = 'enter'
                    if self._change_phase == 'enter':
                        self._enter_change_page()
                        self.wait_ocr(match='声骸强化', raise_if_not_found=True)
                        current_main = self.wait_ocr(.09, .20, .15, .26)
                        if not current_main:
                            raise Exception(self.tr('Cannot find the current main stat!'))
                        if self._change_target in current_main[0].name:
                            raise Exception(self.tr('The target stat matches the current stat. Change the Echo filter!'))
                        self._change_phase = 'choose'
                    if self._change_phase == 'choose':
                        self.click(.04, .41)
                        self.wait_ocr(match='主音属性', raise_if_not_found=True)
                        self.sleep(.8)
                        self.click(.52, .71)
                        target = self.wait_ocr(match=re.compile(re.escape(self._change_target)), raise_if_not_found=True)
                        self.click(target, after_sleep=.5)
                        self._change_phase = 'choice_confirm'
                    if self._change_phase == 'choice_confirm':
                        # 选择确认不消耗声骸；页面已进入重构按钮时不用重复确认弹窗。
                        if not self.ocr(.37, .82, .64, .99, match='数据重构'):
                            self.wait_click_ocr(match='确认', after_sleep=2)
                        self._change_phase = 'ready'
                    if self._change_phase == 'ready':
                        target = self.ocr(match=re.compile(re.escape(self._change_target)))
                        if not target:
                            self._change_unknown('无法确认重构前选择的目标属性')
                        self._change_phase = 'result'
                        self.wait_click_ocr(.37, .82, .64, .99, match='数据重构',
                                            after_sleep=.5, raise_if_not_found=True)
                    if self._change_phase == 'result':
                        if not self.wait_ocr(match='获得声骸', time_out=5, raise_if_not_found=False):
                            self._change_unknown('无法确认数据重构结果，已停止以避免重复消耗')
                        self.info_incr('成功声骸数量')
                        self._change_phase = 'return'
                    if self._change_phase == 'return':
                        self.esc()
                        self._change_phase = 'bag'
                except ForegroundInputInterrupted:
                    self._change_needs_refresh = True
        finally:
            self._change_active = False

    def _enter_change_page(self):
        start = self.business_time()
        while self.business_time() - start < 5:
            enhance = self.find_echo_enhance()
            if not enhance:
                return
            self.click(enhance, after_sleep=.5)
        raise Exception(self.tr('Cannot find Enhance and Tune!'))

    def _change_unknown(self, message):
        self.screenshot('change_echo_result_unknown')
        raise ForegroundInputError(message)

    def _finish_change_echoes(self):
        total = self.info_get('成功声骸数量')
        if self.debug:
            self.screenshot('无可强化声骸')
        self.log_info(self.tr('No more Echoes to modify. Finished! Enhanced: {total}, matching: {matching}').format(
            total=total, matching=total), notify=True)
        if total >= 1:
            try:
                os.startfile(os.path.abspath('screenshots'))
            except OSError as error:
                self.log_error(self.tr('Cannot open screenshot folder: {e}').format(e=error))

    def esc(self):
        start = self.business_time()
        while not self.find_echo_enhance() and self.business_time() - start < 5:
            self.send_key('esc', after_sleep=1)
        if not self.find_echo_enhance():
            self._change_unknown('无法返回原筛选背包页面')
        self.sleep(.1)
