from ok import Logger
from ok.device.interaction_methods.foreground_input import ForegroundInputInterrupted
from src.task.BaseCombatTask import BaseCombatTask, NotInCombatException, CharDeadException
from src.task.WWOneTimeTask import WWOneTimeTask

logger = Logger.get_logger(__name__)


class DomainTask(WWOneTimeTask, BaseCombatTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.teleport_timeout = 100
        self.stamina_once = 0

    def revive_action(self):
        """副本内死亡恢复：关闭弹窗 → 退出副本 → 传最近传送点回血。"""
        with self._foreground_revive_context():
            phase = 'leave'
            while True:
                try:
                    if phase == 'leave':
                        self.close_revive_popup()
                        self.make_sure_in_world()
                        phase = 'heal'
                    if self.realm_entry_at_heal_point:
                        logger.info('副本死亡恢复已回到信标，跳过重复传送')
                        return True
                    self.revive_at_tower_and_heal()
                    return True
                except ForegroundInputInterrupted:
                    self.wait_foreground_frame()

    def make_sure_in_world(self):
        while True:
            try:
                confirm = self.find_one('gray_confirm_exit_button', threshold=.7)
                if confirm:
                    self.click_box(confirm, relative_x=-1, after_sleep=1)
                    self.wait_in_team_and_world(time_out=self.teleport_timeout)
                elif self.in_realm():
                    self.send_key('esc', after_sleep=1)
                    self.wait_click_feature('gray_confirm_exit_button', relative_x=-1, raise_if_not_found=False,
                                            time_out=3, click_after_delay=.5, threshold=.7, after_sleep=1)
                    self.wait_in_team_and_world(time_out=self.teleport_timeout)
                else:
                    self.ensure_main()
                return
            except ForegroundInputInterrupted:
                self._recover_foreground_input()

    def open_F2_book_and_get_stamina(self):
        self.openF2Book('gray_book_boss')
        return self.get_stamina()

    def farm_domain_with_recovery_loop(self, must_use, teleport_into_domain_once, max_recovery_retries=3):
        """包装副本刷取循环：死亡恢复后自动从 F2 重新进入，并限制重试次数。"""
        recovery_retries = 0
        self.realm_entry_at_heal_point = False
        phase = 'prepare'
        while True:
            try:
                if phase == 'prepare':
                    current, _, total = self.open_F2_book_and_get_stamina()
                    if total < self.stamina_once or total < must_use or (must_use == 0 and current < self.stamina_once):
                        self.log_info(self.tr('not enough stamina'), notify=True)
                        self.make_sure_in_world()
                        return
                    phase = 'enter'
                if phase == 'enter':
                    teleport_into_domain_once()
                    phase = 'farm'
                    self.sleep(1)
                finished, must_use = self.farm_in_domain(must_use=must_use)
            except ForegroundInputInterrupted:
                # 焦点等待不占用死亡重试，也不重新初始化已消费的日常额度。
                self.wait_foreground_frame()
                continue
            if finished:
                return
            recovery_retries += 1
            if recovery_retries >= max_recovery_retries:
                self.log_info(self.tr('farm_domain: exceeded recovery retries ({max_recovery_retries}), stop farming').format(max_recovery_retries=max_recovery_retries),
                              notify=True)
                self.make_sure_in_world()
                return
            self.log_info(self.tr('farm_domain: death recovered, re-enter from F2 book'))
            self.realm_entry_at_heal_point = True  # 恢复后站在信标上, 从 F2 直接进本
            phase = 'prepare'
            self.sleep(1)

    def farm_in_domain(self, must_use=0):
        """刷本循环；返回 (是否整段正常结束, 剩余 must_use)。

        第二项在死亡提前退出时仍会带上本局内已扣过的额度，供外层恢复循环继续传参。
        """
        if self.stamina_once <= 0:
            raise RuntimeError(self.tr('"self.stamina_once" must be override'))
        self.info_incr('used stamina', 0)
        phase = 'start'
        while True:
            try:
                if phase == 'start':
                    if not self.in_combat() and not self.has_claim_stamina():
                        self.walk_until_f(time_out=4, backward_time=0, raise_if_not_found=True)
                        self.pick_f()
                    phase = 'fight'
                if phase == 'fight':
                    if not self.has_claim_stamina():
                        self.combat_once()
                    phase = 'treasure'
                    self.sleep(3)
                if phase == 'treasure':
                    if not self.has_claim_stamina():
                        self.walk_to_treasure()
                        self.pick_f(handle_claim=False)
                    phase = 'claim'
                if phase == 'claim':
                    can_continue, used = self.use_stamina(once=self.stamina_once, must_use=must_use)
                    # 领取 UI 已确认后立即提交额度；结果页失焦不得重新进入领取阶段。
                    must_use -= used
                    phase = 'result'
                    self.info_incr('used stamina', used)
                    self.sleep(4)
                self.finish_challenge_result(can_continue, team_timeout=self.teleport_timeout)
                if not can_continue:
                    self.log_info(self.tr('used all stamina'))
                    self.make_sure_in_world()
                    return True, must_use
                phase = 'start'
                self.sleep(1)
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
                continue
            except (NotInCombatException, CharDeadException):
                self.log_info(self.tr('farm_in_domain: death recovered, exiting domain'))
                self.make_sure_in_world()
                return False, must_use
