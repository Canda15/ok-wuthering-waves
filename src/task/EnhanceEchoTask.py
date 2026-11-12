# EnhanceEchoTask.py
import re
import os

from ok import FindFeature, Logger, ForegroundInputError, ForegroundInputInterrupted
from ok.feature.Box import get_bounding_box
from ok.util.file import clear_folder
from src.task.BaseWWTask import BaseWWTask
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)

number_pattern = re.compile(r"^[\d.%％ ]+$")
property_pattern = re.compile(r"[\u4e00-\u9fff]{2,}")
TUNING_RESULT_PATTERN = re.compile(r'调谐成功|調諧成功|点击任意|點擊任意')
TUNING_DIALOG_PATTERN = re.compile(r'不再提示|' + TUNING_RESULT_PATTERN.pattern)


class EnhanceEchoTask(BaseWWTask, FindFeature):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "⬆️ 批量强化声骸(游戏与okww语言必须为简体/繁体中文)"
        self.icon = task_icon('echo-upgrade')
        self.group_name = 'Echo Management'
        self.group_icon = task_icon('category-echo')
        self.description = "点击B进入背包, 在过滤器中选择需要强化的声骸, 并按照等级从0排序后开始."
        self.fail_reason = ""
        self.supported_languages = ["zh_CN", "zh_TW"]
        self.default_config.update({
            '必须有双爆': True,
            '双爆出现之前必须全有效词条': True,
            '双爆总计>=': 13.8,
            '首条双爆>=': 6.9,
            '有效词条>=': 3,
            '第一条必须为有效词条': True,
            '有效词条': ['暴击', '暴击伤害', '攻击百分比'],
            'Pause after Success': True,
        })
        self.config_type["有效词条"] = {'type': "multi_selection",
                                        'options': ['暴击伤害', '暴击', '攻击百分比', '生命百分比', '防御百分比',
                                                    '攻击', '生命', '防御',
                                                    '共鸣效率', '普攻伤害加成',
                                                    '重击伤害加成', '共鸣解放伤害加成',
                                                    '共鸣技能伤害加成']}
        self.config_description = {
            '必须有双爆': '如果开启，声骸最终必须同时拥有暴击和暴击伤害。如果剩余孔位不足以凑齐双爆，则丢弃',
            '双爆出现之前必须全有效词条': '开启后，在暴击或暴击伤害词条出现之前，前面的所有词条必须都在有效词条列表中',
            '双爆总计>=': '当声骸同时存在暴击和爆伤时，需要满足 暴击 + (爆伤/2) >= 此数值',
            '首条双爆>=': '仅检查第一条出现的暴击或暴击伤害是否满足条件, 爆伤/2',
            '有效词条>=': '声骸满级时需达到的有效词条数量，若剩余孔位无法凑齐该数量，则停止强化并丢弃',
            '第一条必须为有效词条': '如果开启，第一个副词条必须在有效词条列表中且符合数值要求，否则直接丢弃',
            '有效词条': '定义哪些属性被视为有效',
            'Pause after Success': 'When a success occurs, send notification and pause task',
        }

    def find_echo_enhance(self):
        return self.ocr(0.82, 0.86, 0.97, 0.96, match='培养')

    def is_0_level(self):
        return self.ocr(0.65, 0.35, 1, 0.57, match=re.compile('声骸技能'))

    def select_0_level_echo(self):
        # 3.7 返回背包会选中刚强化的声骸；每次排序后都检查第一格，失焦中断一次排序也能恢复。
        self.click(0.13, 0.21, after_sleep=0.5)
        if self.is_0_level():
            return True
        for _ in range(2):
            self.click(0.35, 0.917, after_sleep=0.5)
            self.click(0.13, 0.21, after_sleep=0.5)
            if self.is_0_level():
                return True
        return False

    def run(self):
        self.info_set('成功声骸数量', 0)
        self.info_set('失败声骸数量', 0)
        clear_folder('screenshots')
        self._enhance_phase = 'bag'
        self._enhance_needs_refresh = False
        return self.resume_after_foreground()

    def on_foreground_resume(self):
        super().on_foreground_resume()
        self._enhance_needs_refresh = True
        if getattr(self, '_enhance_active', False):
            raise ForegroundInputInterrupted('声骸强化画面已变化，需要重新确认当前阶段')

    def resume_after_foreground(self):
        if not getattr(self, '_enhance_phase', None):
            return super().resume_after_foreground()
        self._enhance_active = True
        try:
            while self._enhance_phase != 'completed':
                try:
                    if self._enhance_needs_refresh:
                        self.wait_foreground_frame()
                        self._enhance_needs_refresh = False
                        if self._enhance_phase in ('bag', 'enter'):
                            self.esc()
                            self._enhance_phase = 'bag'
                        elif self._enhance_phase in ('materials', 'submit'):
                            # 当前声骸可能已经强化数次；只核对原属性并重新放入未消耗的材料，不能换回下一只 +0。
                            if not self.ocr(match=re.compile('声骸强化|聲骸強化')):
                                self._enhance_unknown('恢复后已离开当前声骸强化页面，无法安全继续放入材料')
                            if hasattr(self, '_enhance_snapshot'):
                                self._verify_enhance_snapshot()
                            self._enhance_phase = 'materials'
                        elif self._enhance_phase in ('resolve', 'inspect'):
                            self._enhance_resolving_after_resume = True
                        elif self._enhance_phase == 'mark':
                            self._verify_enhance_snapshot()
                        elif self._enhance_phase == 'record':
                            if self.find_echo_enhance():
                                self._enhance_phase = 'pause' if (self._enhance_outcome == 'lock'
                                    and self.config.get('Pause after Success')) else 'return'
                            else:
                                self._verify_enhance_snapshot()
                    if self._enhance_phase == 'bag':
                        if not self.find_echo_enhance():
                            raise Exception(self.tr("Start after filtering Echoes on the bag's Echo screen!"))
                        if not self.select_0_level_echo():
                            self._finish_enhance_batch()
                            self._enhance_phase = 'completed'
                            return
                        self._enhance_materials_requested = False
                        self.__dict__.pop('_enhance_snapshot', None)
                        self._enhance_phase = 'enter'
                    if self._enhance_phase == 'enter':
                        self._enter_enhance_page()
                        self._enhance_phase = 'materials'
                    if self._enhance_phase == 'materials':
                        properties, values = self._read_echo_stats()
                        self._enhance_snapshot = self._stat_signature(properties, values)
                        self._enhance_baseline_signature = self._enhance_snapshot
                        self._enhance_baseline_count = len(properties)
                        self._enhance_result_seen = False
                        self._enhance_resolving_after_resume = False
                        self._put_staged_materials()
                        self._enhance_phase = 'submit'
                    if self._enhance_phase == 'submit':
                        self._submit_enhance()
                    if self._enhance_phase == 'resolve':
                        self._resolve_enhance_result()
                        self._enhance_phase = 'inspect'
                    if self._enhance_phase == 'inspect':
                        properties, values = self._read_echo_stats()
                        if not self._is_enhance_progress(properties, values):
                            self._enhance_unknown('无法确认本次强化新增的副词条，已停止以避免重复消耗')
                        self._enhance_snapshot = self._stat_signature(properties, values)
                        self.info_set('属性', properties)
                        self.info_set('值', values)
                        if not self.check_echo_stats(properties, values):
                            self.trash_and_esc()
                        elif len(properties) >= 5:
                            self.lock_and_esc()
                        else:
                            self._enhance_phase = 'materials'
                    if self._enhance_phase in ('mark', 'record', 'pause', 'return'):
                        self._finish_echo()
                except ForegroundInputInterrupted:
                    self._enhance_needs_refresh = True
        finally:
            self._enhance_active = False

    def _enter_enhance_page(self):
        start = self.business_time()
        while self.business_time() - start < 5:
            enhance = self.find_echo_enhance()
            if not enhance:
                self.wait_ocr(match=re.compile('声骸强化|聲骸強化'), raise_if_not_found=True)
                return
            self.click(enhance, after_sleep=.5)
        raise Exception(self.tr('Cannot find Enhance and Tune!'))

    def _put_staged_materials(self):
        start = self.business_time()
        have_add_mat = getattr(self, '_enhance_materials_requested', False)
        while self.business_time() - start < 5:
            add_mat = self.find_add_mat()
            if add_mat:
                have_add_mat = True
                self._enhance_materials_requested = True
                self.click(add_mat, after_sleep=.3)
            elif have_add_mat:
                self._enhance_materials_requested = False
                return
            else:
                self.next_frame()
        raise Exception(self.tr('Enable staged material insertion in enhancement settings!'))

    def _submit_enhance(self):
        button = self.wait_ocr(.1, .88, .29, .96, match=['强化并调谐'], settle_time=.1)
        if not button:
            if self.ocr(.17, .88, .29, .96, match=['强化']):
                raise Exception(self.tr('Enable simultaneous tuning in enhancement settings!'))
            raise Exception(self.tr('Cannot find Enhance and Tune!'))
        # 先保存可能已消耗的阶段；点击或后续等待中断只能查结果，不能重新点击消耗按钮。
        self._enhance_phase = 'resolve'
        self.click(button, after_sleep=1.5)

    def _resolve_enhance_result(self):
        start = self.business_time()
        while self.business_time() - start < 6:
            handle = self.ocr(.24, .18, .75, .98, match=TUNING_DIALOG_PATTERN)
            result = next((box for box in handle if TUNING_RESULT_PATTERN.search(box.name)), None)
            if result:
                self._enhance_result_seen = True
                self.click(result, after_sleep=1)
                continue
            if handle:
                if getattr(self, '_enhance_resolving_after_resume', False):
                    # 勾选可能已经生效；只重新定位仍在前台的确认，避免把“不再提示”反向切换。
                    self._confirm_resumed_skip_dialog()
                else:
                    self.click_skip_dialog_confirm()
                self.sleep(.2)
                continue
            properties, _ = self._read_echo_stats()
            if len(properties) > self._enhance_baseline_count:
                return
            self.sleep(.2)
        self._enhance_unknown('无法确认强化与调谐结果，已停止以避免重复消耗')

    def _read_echo_stats(self):
        texts = self.ocr(.09, .3, .40, .53)
        properties = [p for p in self.find_boxes(texts, match=property_pattern) if '辅音' not in p.name and '輔音' not in p.name]
        for prop in properties:
            prop.name = property_pattern.search(prop.name).group()
        values = self.find_boxes(texts, match=number_pattern)
        if len(properties) != len(values) or len(properties) > 5:
            self._enhance_unknown('声骸副词条与数值识别不完整，已停止操作')
        return properties, values

    @staticmethod
    def _stat_signature(properties, values):
        return tuple(prop.name for prop in properties), tuple(value.name for value in values)

    def _is_enhance_progress(self, properties, values):
        before_properties, before_values = self._enhance_baseline_signature
        after_properties, after_values = self._stat_signature(properties, values)
        count = self._enhance_baseline_count
        return (len(after_properties) > count and after_properties[:count] == before_properties
                and after_values[:count] == before_values)

    def _verify_enhance_snapshot(self):
        properties, values = self._read_echo_stats()
        if self._stat_signature(properties, values) != self._enhance_snapshot:
            self._enhance_unknown('恢复后声骸属性已变化，无法确认原声骸的标记结果')

    def _enhance_unknown(self, message):
        self.screenshot('enhance_echo_result_unknown')
        raise ForegroundInputError(message)

    def _finish_enhance_batch(self):
        total = self.info_get('成功声骸数量') + self.info_get('失败声骸数量')
        if self.debug:
            self.screenshot('无可强化声骸')
        self.log_info(self.tr('No more Echoes to enhance. Finished! Enhanced: {total}, matching: {matching}').format(
            total=total, matching=self.info_get('成功声骸数量')), notify=True)
        if self.info_get('成功声骸数量') >= 1:
            try:
                os.startfile(os.path.abspath('screenshots'))
            except OSError as error:
                self.log_error(self.tr('Cannot open screenshot folder: {e}').format(e=error))

    def find_confirm(self):
        button_box = self.box_of_screen(0.60, 0.65, 0.82, 0.82)
        if confirm := self.find_one('echo_enhance_confirm', box=button_box, threshold=0.7):
            return [confirm]
        return self.ocr(box=button_box, match='确认')

    def check_echo_stats(self, properties, values):
        self.fail_reason = ""
        invalid_count = 0

        paired_stats = []
        unmatched_values = values.copy()
        for prop in properties:
            matched_val_text = "0"
            if unmatched_values:
                closest_val = min(unmatched_values, key=lambda v: abs(prop.y - v.y))
                matched_val_text = closest_val.name
                unmatched_values.remove(closest_val)
            paired_stats.append((prop.name, matched_val_text))

        total_count = len(paired_stats)

        crit_rate_val = 0
        crit_dmg_val = 0
        has_crit_rate = False
        has_crit_dmg = False

        checked_first_crit = False
        has_encountered_crit = False

        valid_stats = self.config.get('有效词条') or []

        for p_raw, v_str in paired_stats:
            p = p_raw
            if '暴击伤害' in p:
                p = '暴击伤害'
            elif '暴击' in p:
                p = '暴击'
            elif '攻击' in p:
                p = '攻击' + ('百分比' if '%' in v_str or '％' in v_str else '')
            elif '生命' in p:
                p = '生命' + ('百分比' if '%' in v_str or '％' in v_str else '')
            elif '防御' in p:
                p = '防御' + ('百分比' if '%' in v_str or '％' in v_str else '')
            elif '效率' in p:
                p = '共鸣效率'
            elif '普攻' in p:
                p = '普攻伤害加成'
            elif '重击' in p:
                p = '重击伤害加成'
            elif '解放' in p:
                p = '共鸣解放伤害加成'
            elif '技能' in p:
                p = '共鸣技能伤害加成'

            v = parse_number(v_str)

            is_valid_prop = True
            is_crit_stat = p in ['暴击', '暴击伤害']

            if self.config.get(
                    '双爆出现之前必须全有效词条') and '暴击' in valid_stats and '暴击伤害' in valid_stats and not has_encountered_crit:
                if not is_crit_stat:
                    if p not in valid_stats:
                        self.fail_reason = f'双爆前含无效_{p}'
                        self.log_info(self.tr('Invalid substat {p} before both CRIT stats; discarding').format(p=p))
                        return False
                else:
                    has_encountered_crit = True

            if is_valid_prop and p not in valid_stats:
                is_valid_prop = False
                self.log_debug(f'非有效词条, {p} 不符合条件')

            if p == '暴击':
                has_crit_rate = True
                crit_rate_val += v
                if '暴击' in valid_stats and not checked_first_crit:
                    checked_first_crit = True
                    if v < self.config.get('首条双爆>='):
                        self.fail_reason = f'首条暴击不足_{v}'
                        self.log_info(self.tr('First CRIT Rate {v} < {minimum}; discarding').format(v=v, minimum=self.config.get('首条双爆>=')))
                        return False

            elif p == '暴击伤害':
                has_crit_dmg = True
                crit_dmg_val += v
                if '暴击伤害' in valid_stats and not checked_first_crit:
                    checked_first_crit = True
                    if v / 2 < self.config.get('首条双爆>='):
                        self.fail_reason = f'首条爆伤不足_{v}'
                        self.log_info(self.tr('First CRIT DMG {v} < {minimum}; discarding').format(v=v, minimum=self.config.get('首条双爆>=')))
                        return False

            if not is_valid_prop:
                invalid_count += 1

        self.info_set('不符合条件属性', invalid_count)

        if self.config.get('必须有双爆'):
            missing_crit = (0 if has_crit_rate else 1) + (0 if has_crit_dmg else 1)
            remaining_slots = 5 - total_count
            if remaining_slots < missing_crit:
                self.fail_reason = f'无法凑齐双爆_缺{missing_crit}'
                self.log_info(self.tr('Cannot obtain both CRIT stats ({missing_crit} missing, {remaining_slots} slots left); discarding').format(missing_crit=missing_crit, remaining_slots=remaining_slots))
                return False

        if has_crit_rate and has_crit_dmg:
            total_score = crit_rate_val + (crit_dmg_val / 2)
            if total_score < self.config.get('双爆总计>='):
                self.fail_reason = f'双爆总计不足_{total_score:.1f}'
                self.log_info(self.tr('Combined CRIT score {total_score:.1f} < {minimum}; discarding').format(total_score=total_score, minimum=self.config.get('双爆总计>=')))
                return False

        if total_count == 1 and self.config.get('第一条必须为有效词条') and invalid_count == 1:
            self.fail_reason = '首条无效'
            self.log_info(self.tr('The first substat must be valid; discarding'))
            return False

        valid_count = total_count - invalid_count
        remaining_slots = 5 - total_count
        if (valid_count + remaining_slots) < self.config.get('有效词条>='):
            self.fail_reason = f'有效词条不足_上限{valid_count + remaining_slots}'
            self.log_info(self.tr('Not enough remaining slots for the required valid substats; discarding'))
            return False

        return True

    def find_add_mat(self):
        return self.wait_ocr(0.09, 0.6, 0.38, 0.86, match=['阶段放入'], time_out=1)

    def esc(self):
        start = self.business_time()
        while not self.find_echo_enhance() and self.business_time() - start < 10:
            self.send_key('esc', interval=4, after_sleep=0.2)
        if not self.find_echo_enhance():
            self._enhance_unknown('无法返回原筛选背包页面')
        self.sleep(0.1)

    def trash_and_esc(self):
        self._enhance_outcome = 'discard'
        self._enhance_phase = 'mark'
        self._finish_echo()

    def screenshot_echo(self, name):
        echo = self.box_of_screen(0.09, 0.09, 0.37, 0.55).crop_frame(self.frame)
        self.screenshot(name=name, frame=echo)

    def lock_and_esc(self):
        self._enhance_outcome = 'lock'
        self._enhance_phase = 'mark'
        self._finish_echo()

    def _mark_echo_flag(self):
        discard = self._enhance_outcome == 'discard'
        names = ['echo_dropped', 'echo_not_dropped'] if discard else ['echo_locked', 'echo_not_locked']
        # 搜索框必须同时覆盖两张状态模板，防止另一张模板较大时 OpenCV 匹配崩溃。
        status_box = get_bounding_box([self.get_box_by_name(name) for name in names]).scale(1.05)
        start = self.business_time()
        while self.business_time() - start < 5:
            status = self.find_best_match_in_box(status_box, names, threshold=.7)
            if not status:
                self._enhance_unknown('无法确定声骸的丢弃状态' if discard else '无法确定声骸的锁定状态')
            if status.name == names[0]:
                return
            self.send_key('z' if discard else 'c', after_sleep=1)
        self._enhance_unknown('声骸标记未能确认，已停止操作')

    def _finish_echo(self):
        discard = self._enhance_outcome == 'discard'
        counter = '失败声骸数量' if discard else '成功声骸数量'
        if self._enhance_phase == 'mark':
            self._mark_echo_flag()
            # 标记结果确认后才计数；之后的截图、暂停和返回中断都不会重复计数或反向切换。
            self.info_incr(counter)
            self._enhance_phase = 'record'
        if self._enhance_phase == 'record':
            if discard:
                safe_reason = re.sub(r'[<>:"/\\|?*]', '', self.fail_reason)
                self.screenshot_echo(f'failed/{self.info_get(counter)}_{safe_reason}')
                self.log_info(self.tr('Does not meet the requirements; discarding'))
            else:
                self.screenshot_echo(f'success/{self.info_get(counter)}')
                self.log_info(self.tr('Succeeded and locked'))
            self._enhance_phase = 'pause' if not discard and self.config.get('Pause after Success') else 'return'
        if self._enhance_phase == 'pause':
            self.log_info(self.tr('Matching Echo found; task paused'), notify=True)
            self._enhance_phase = 'return'
            self.pause()
        if self._enhance_phase == 'return':
            self.esc()
            self.wait_ocr(.82, .86, .97, .96, match='培养', settle_time=.1, raise_if_not_found=True)
            self._enhance_phase = 'bag'


def parse_number(text):
    try:
        return float(text.replace('％', '%').split('%')[0])
    except (ValueError, IndexError):
        return 0.0
