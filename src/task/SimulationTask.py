from ok import Logger
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.task.BaseWWTask import TEAM_SCREEN_FEATURES
from src.task.DomainTask import DomainTask
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)


class SimulationTask(DomainTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = '🧪 Simulation Challenge'
        self.icon = task_icon('simulation')
        self.group_name = 'Daily Activities'
        self.group_icon = task_icon('category-daily')
        self.description = 'Farms the selected Simulation Challenge. Must be able to teleport (F2).'
        self.support_schedule_task = True
        self.default_config = {
            'Material Selection': 'Shell Credit',
        }
        material_option_list = ['Resonator EXP', 'Weapon EXP', 'Shell Credit']
        self.config_type['Material Selection'] = {'type': 'drop_down', 'options': material_option_list}
        self.config_description = {
            'Material Selection': 'Resonator EXP / Weapon EXP / Shell Credit',
        }
        self.stamina_once = 40

    def run(self):
        super().run()
        self.make_sure_in_world()
        self.farm_simulation()

    def farm_simulation(self, daily=False, used_stamina=0, config=None):
        if daily:
            must_use = 180 - used_stamina
        else:
            must_use = 0
        if config is None:
            config = self.config
        selection = config.get('Material Selection', 'Shell Credit')

        def teleport_once():
            self.teleport_into_domain(selection)

        self.farm_domain_with_recovery_loop(must_use, teleport_once)

    def teleport_into_domain(self, selection):
        self.info_set('Target Simulation Challenge', selection)
        if selection == 'Resonator EXP':
            index = 0
        elif selection == 'Weapon EXP':
            index = 1
        else:  # selection == 'Shell Credit'
            index = 2
        phase = 'select'
        recovering = False
        while True:
            try:
                if recovering and phase != 'select' and self.in_team_and_world():
                    return
                if phase == 'select':
                    self.openF2Book('gray_book_boss')
                    self.open_boss_book('moni')
                    self.click(.9730, .8806, after_sleep=1)
                    self.click(.898, .533 + index * .14, after_sleep=1)
                    phase = 'proceed'
                if phase == 'proceed':
                    if not recovering:
                        self.click_relative(.93, .90, after_sleep=1)
                    elif self.find_one(TEAM_SCREEN_FEATURES):
                        pass
                    elif self._confirm_resumed_skip_dialog():
                        pass
                    elif proceed := self.find_one('boss_proceed', box=self.box_of_screen(.7, .75, 1, 1)):
                        self.click(proceed, after_sleep=1)
                    elif self.wait_in_team_and_world(time_out=self.teleport_timeout, raise_if_not_found=False):
                        return
                    else:
                        self.screenshot('domain_entry_unknown')
                        raise ForegroundInputError('无法确认模拟挑战入口的新画面，已停止操作')
                    phase = 'team'
                if phase == 'team':
                    self.click_team_challenge()
                    phase = 'wait'
                self.wait_in_team_and_world(time_out=self.teleport_timeout)
                return
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
                recovering = True
