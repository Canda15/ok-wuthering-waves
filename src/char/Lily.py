import time

from ok.device.interaction_methods.foreground_input import ForegroundInputInterrupted
from src.Labels import Labels
from src.char.BaseChar import BaseChar


class Lily(BaseChar):
    """先完成重击引导与协奏积累，再进入梦乡并向队伍交接增益。"""

    FORTE_TIMEOUT = 10
    HEAVY_START_TIMEOUT = 2
    HEAVY_TIMEOUT = 6
    CONCERTO_TIMEOUT = 3

    def skip_combat_check(self):
        # 梦乡期间无法正常锁敌；仅在结束按钮可见时放行交接，切人后恢复通常检查。
        return self.in_slumberland()

    def do_perform(self):
        if self.has_intro:
            self.continues_normal_attack(0.8)
        if not self.in_slumberland():
            self.click_echo(time_out=0)
            self.build_forte()
            if self.is_mouse_forte_full() or self.is_forte_full():
                if not self.perform_heavy():
                    # 未确认开始或收尾时仍可能正在引导，后续技能与切人会取消本次重击。
                    self._raise_rotation_timeout('莉莉重击开始或收尾未在规定时间内确认',
                                                 '莉莉重击结果无法确认')
            can_liberate = self.task.use_liberation and self.liberation_available()
            if not self.fill_concerto(0.8 if can_liberate else 1):
                self._raise_rotation_timeout('莉莉协奏未在规定时间内达到交接要求',
                                             '莉莉协奏补充结果无法确认')
            # 梦乡会隐藏共鸣技能、声骸和普攻，必须先完成重击终结再交接治疗与延奏增益。
            liberated = self.click_liberation(wait_if_cd_ready=0, click_f=False)
            # 解放结果识别失败时也可能已经进入梦乡，此时不能尝试隐藏的 E／普攻。
            if can_liberate and not liberated and not self.in_slumberland():
                if not self.fill_concerto():
                    self._raise_rotation_timeout('莉莉解放失败后协奏未在规定时间内补满',
                                                 '莉莉协奏补充结果无法确认')
            # 解放会刷新 HUD；交接前重读任务的实时协奏，不能沿用角色的满协奏缓存。
            if not self.task.is_con_full():
                self._raise_rotation_timeout('莉莉协奏未满，无法确认延奏交接',
                                             '莉莉延奏交接无法确认')
        self.switch_next_char()

    def _raise_rotation_timeout(self, message, screenshot_name):
        """统一记录已确定的轮转失败，诊断取图的恢复信号不能启动下一轮。"""
        failure = TimeoutError(message)
        try:
            self.task.screenshot(screenshot_name)
        except ForegroundInputInterrupted as error:
            raise failure from error
        raise failure

    def fill_concerto(self, target=1):
        # 共鸣解放补足最后的协奏；不可用时，在重击增益窗口内用共鸣技能／普攻补满。
        start = time.time()
        while self.time_elapsed_accounting_for_freeze(start) < self.CONCERTO_TIMEOUT:
            self.cycle_start()
            if self.get_current_con() >= target:
                return True
            if not self.click_resonance(send_click=False, time_out=0.8)[0]:
                self.click(interval=0.1)
            self.cycle_sleep()
        self.logger.warning(f'莉莉协奏补充超时：目标={target}')
        return False

    def build_forte(self):
        start = time.time()
        while self.time_elapsed_accounting_for_freeze(start) < self.FORTE_TIMEOUT:
            self.cycle_start()
            if self.is_mouse_forte_full() or self.is_forte_full():
                return True
            if not self.click_resonance(send_click=False, time_out=0.8)[0]:
                self.click(interval=0.1)
            self.cycle_sleep()
        self.logger.warning('莉莉共鸣回路未在规定时间内充满')
        return False

    def heavy_in_progress(self):
        # 就绪和引导共用花朵图案；通用满回路提示消失才能证明资源已被消耗、引导已开始。
        return (not self.is_mouse_forte_full()
                and bool(self.task.find_one(Labels.lily_heavy, threshold=0.8)))

    def in_slumberland(self):
        return bool(self.task.find_one(Labels.lily_slumberland, threshold=0.8))

    def liberation_available(self, check_color=True):
        # 梦乡中的解放按钮会结束当前状态，不能把它当成可再次施放的解放。
        return (not self.in_slumberland()
                and bool(self.task.find_one(Labels.lily_liberation, threshold=0.8))
                and super().liberation_available(check_color=check_color))

    def perform_heavy(self):
        self.check_combat()
        # 正常轮转由 perform 标记活动角色；等待内恢复前台时会退出旧轮转，并由 finally 松键。
        self.task.mouse_down()
        try:
            started = bool(self.task.wait_until(
                self.heavy_in_progress, time_out=self.HEAVY_START_TIMEOUT,
                pre_action=self.check_combat))
            if not started:
                self.logger.warning('莉莉重击引导未开始')
                return False
            finished = bool(self.task.wait_until(
                lambda: not self.heavy_in_progress(), time_out=self.HEAVY_TIMEOUT,
                settle_time=0.15, pre_action=self.check_combat))
        finally:
            self.task.mouse_up()
        if finished:
            # 提示消失时终结动画刚开始，等待其结束后才允许后续技能与切人。
            self.sleep(0.7)
            self.logger.info('莉莉重击终结已完成')
        else:
            self.logger.warning('莉莉重击引导未在规定时间内结束')
        return finished
