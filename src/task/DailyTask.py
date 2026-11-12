import re


from ok import Logger, TaskDisabledException
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.task.BaseWWTask import number_re
from src.task.FarmEchoTask import FarmEchoTask
from src.task.ForgeryTask import ForgeryTask
from src.task.GardenTask import GardenTask
from src.task.MergeEchoTask import MergeEchoTask
from src.task.NightmareNestTask import NightmareNestTask
from src.task.TacetTask import TacetTask
from src.task.SimulationTask import SimulationTask
from src.task.WWOneTimeTask import WWOneTimeTask
from src.task.BaseCombatTask import BaseCombatTask
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)

CHECK_WEEKLY_GARDEN = 'Check Weekly Garden'
AUTO_FARM_NIGHTMARE_NEST = 'Auto Farm all Nightmare Nest'
MERGE_ECHO_IF_DISCARDED_OVER_1000 = 'Merge Echo If discarded > 1000'
TELEPORT_AND_FARM_4C_ECHO = 'Teleport and Farm 4C Echo'
ADDITIONAL_TASKS = 'Additional Tasks to Run After Daily Task'


class DailyTask(WWOneTimeTask, BaseCombatTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "📅 Daily Task"
        self.icon = task_icon('daily')
        self.group_name = 'Daily Activities'
        self.group_icon = task_icon('category-daily')
        self.support_schedule_task = True
        self.support_tasks = ["Tacet Suppression", "Forgery Challenge", "Simulation Challenge"]
        self.default_config = {
            'Which to Farm': self.support_tasks[0],
            'Which Tacet Suppression to Farm': 1,  # starts with 1
            'Which Forgery Challenge to Farm': 1,  # starts with 1
            'Material Selection': 'Shell Credit',
            'Farm Nightmare Nest for Daily Echo': True,
            ADDITIONAL_TASKS: [CHECK_WEEKLY_GARDEN],
        }
        self.config_description = {
            'Which Tacet Suppression to Farm': 'The Tacet Suppression number in the F2 list.',
            'Which Forgery Challenge to Farm': 'The Forgery Challenge number in the F2 list.',
            'Material Selection': 'Resonator EXP / Weapon EXP / Shell Credit',
            'Farm Nightmare Nest for Daily Echo': 'Farm 1 Echo from Nightmare Nest to complete Daily Task when needed.',
            ADDITIONAL_TASKS: 'Select optional tasks. Nightmare Nest runs before stamina farming to help complete '
                              'the daily task; the other tasks run afterward.',
        }
        material_option_list = ['Resonator EXP', 'Weapon EXP', 'Shell Credit']
        self.config_type = {
            'Which to Farm': {
                'type': "drop_down",
                'options': self.support_tasks,
                'sub_configs': {
                    'Tacet Suppression': ['Which Tacet Suppression to Farm'],
                    'Forgery Challenge': ['Which Forgery Challenge to Farm'],
                    'Simulation Challenge': [
                        'Material Selection'],
                }
            },
            'Material Selection': {
                'type': 'drop_down',
                'options': material_option_list
            },
            ADDITIONAL_TASKS: {
                'type': 'multi_selection',
                'options': [
                    CHECK_WEEKLY_GARDEN,
                    AUTO_FARM_NIGHTMARE_NEST,
                    MERGE_ECHO_IF_DISCARDED_OVER_1000,
                    TELEPORT_AND_FARM_4C_ECHO,
                ],
            },
        }
        self.add_exit_after_config()
        self.description = "Login, claim monthly card, farm echo, and claim daily reward"

    def run(self):
        self.validate_additional_tasks()
        self._daily_phase = 'prepare'
        self._daily_additional_index = 0
        WWOneTimeTask.run(self)
        self.logged_in = False
        return self._continue_daily()

    def resume_after_foreground(self):
        if not getattr(self, '_daily_phase', None):
            return super().resume_after_foreground()
        return self._continue_daily()

    def _continue_daily(self):
        """阶段完成后才推进；恢复不能重新启动已完成的资源任务或附加任务。"""
        additional_tasks = self.config.get(ADDITIONAL_TASKS) or []
        condition1 = AUTO_FARM_NIGHTMARE_NEST in additional_tasks
        condition2 = self.config.get('Farm Nightmare Nest for Daily Echo')
        while self._daily_phase != 'completed':
            try:
                if self._daily_phase == 'prepare':
                    self.ensure_main(time_out=180)
                    _, daily_reward_ready = self.open_daily()
                    self._daily_need_nightmare = condition1 or (
                        condition2 and not daily_reward_ready
                        and self.config.get('Which to Farm', self.support_tasks[0]) != self.support_tasks[0])
                    self._daily_phase = 'nightmare'
                if self._daily_phase == 'nightmare':
                    if self._daily_need_nightmare:
                        self._farm_daily_nightmare(condition1)
                    self._daily_phase = 'stamina'
                if self._daily_phase == 'stamina':
                    # 梦魇和登录期间日常进度可能变化，资源决策必须重新读取真实 UI。
                    used_stamina, ready = self.open_daily()
                    if not ready and used_stamina < 180:
                        target = self.config.get('Which to Farm', self.support_tasks[0])
                        task_class, method = {
                            self.support_tasks[0]: (TacetTask, 'farm_tacet'),
                            self.support_tasks[1]: (ForgeryTask, 'farm_forgery'),
                            self.support_tasks[2]: (SimulationTask, 'farm_simulation'),
                        }.get(target, (SimulationTask, 'farm_simulation'))
                        farm_task = self.get_task_by_class(task_class)
                        # 自定义入口也注册实际内层实例，使失焦恢复清到内层角色和导航缓存。
                        with self.executor.task_context(farm_task):
                            getattr(farm_task, method)(daily=True, used_stamina=used_stamina, config=self.config)
                    self._daily_phase = 'daily'
                    self.sleep(4)
                if self._daily_phase == 'daily':
                    self.claim_daily()
                    self._daily_phase = 'mail'
                if self._daily_phase == 'mail':
                    self.claim_mail()
                    self._daily_phase = 'battle_pass'
                    self.sleep(1)
                if self._daily_phase == 'battle_pass':
                    self.claim_battle_pass()
                    self._daily_phase = 'additional'
                if self._daily_phase == 'additional':
                    self.run_additional_tasks()
                    self._daily_phase = 'completed'
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
        self.log_info(self.tr('Daily Task Completed'), notify=True)

    def _farm_daily_nightmare(self, farm_all):
        nightmare_task = self.get_task_by_class(NightmareNestTask)
        try:
            # 保留日常的图鉴衔接；退出时还原实例方法，避免下次独立任务沿用此覆盖。
            nightmare_task.ensure_main = lambda *args, **kwargs: None
            if farm_all:
                self.log_debug('Auto Farm all Nightmare Nest')
                self.run_task_by_class(NightmareNestTask)
            else:
                self.log_debug('Farm Nightmare Nest for Daily Echo')
                with self.executor.task_context(nightmare_task):
                    try:
                        nightmare_task.run_capture_mode()
                    except ForegroundInputInterrupted:
                        while True:
                            try:
                                self.wait_foreground_frame()
                                nightmare_task.resume_after_foreground()
                                break
                            except ForegroundInputInterrupted:
                                continue
        except (ForegroundInputError, ForegroundInputInterrupted, TaskDisabledException):
            raise
        except Exception as e:
            self.log_error(self.tr('NightmareNestTask Failed'), e)
            self.screenshot('NightmareNestTask')
            self.ensure_main(time_out=180)
        finally:
            nightmare_task.__dict__.pop('ensure_main', None)

    def validate_additional_tasks(self):
        additional_tasks = self.config.get(ADDITIONAL_TASKS) or []
        if TELEPORT_AND_FARM_4C_ECHO in additional_tasks:
            farm_echo_task = self.get_task_by_class(FarmEchoTask)
            if farm_echo_task.config.get('Teleport to Boss', 'No') == 'No':
                raise Exception(
                    self.tr(
                        'Teleport and Farm 4C Echo requires "Teleport to Boss" to be enabled in Farm Echo Task.'
                    )
                )
        if AUTO_FARM_NIGHTMARE_NEST in additional_tasks:
            nightmare_task = self.get_task_by_class(NightmareNestTask)
            if not nightmare_task.config.get('Which to Farm'):
                raise Exception(
                    self.tr(
                        'Auto Farm all Nightmare Nest requires at least one "Which to Farm" option.'
                    )
                )
        return True

    def run_additional_tasks(self):
        additional_tasks = self.config.get(ADDITIONAL_TASKS) or []
        actions = []
        if CHECK_WEEKLY_GARDEN in additional_tasks:
            actions.append(self.check_weekly_garden)
        if MERGE_ECHO_IF_DISCARDED_OVER_1000 in additional_tasks:
            actions.append(self.check_discarded_echo)
        if TELEPORT_AND_FARM_4C_ECHO in additional_tasks:
            actions.append(self._farm_additional_echo)
        index = getattr(self, '_daily_additional_index', 0)
        while index < len(actions):
            actions[index]()
            index += 1
            self._daily_additional_index = index

    def _farm_additional_echo(self):
        self.log_info(self.tr('Daily task completed, start teleport to farm 4C echo'), notify=True)
        self.run_task_by_class(FarmEchoTask)

    def check_weekly_garden(self):
        self.info_set('current task', 'check weekly garden')
        self.log_info(self.tr('check weekly garden'))
        try:
            garden_task = self.get_task_by_class(GardenTask)
            garden_task.open_garden_weekly_page()
            if garden_task.is_weekly_garden_completed():
                self.log_info(self.tr('weekly garden already completed'))
                return
            self.log_info(self.tr('weekly garden not completed, run GardenTask'))
            self.run_task_by_class(GardenTask)
        except (ForegroundInputError, ForegroundInputInterrupted, TaskDisabledException):
            raise
        except Exception as e:
            self.log_error(self.tr('GardenTask Failed'), e)
            self.screenshot('GardenTask')
            self.ensure_main(time_out=180)

    def check_discarded_echo(self):
        self.info_set('current task', 'check discarded echo')
        self.log_info(self.tr('check discarded echo'))
        merge_echo_task = self.get_task_by_class(MergeEchoTask)
        old_notify_if_not_enough = merge_echo_task.notify_if_not_enough
        try:
            merge_echo_task.notify_if_not_enough = False
            self.run_task_by_class(MergeEchoTask)
        except (ForegroundInputError, ForegroundInputInterrupted, TaskDisabledException):
            raise
        except Exception as e:
            self.log_error(self.tr('MergeEchoTask Failed'), e)
            self.screenshot('MergeEchoTask')
            self.ensure_main(time_out=180)
        finally:
            merge_echo_task.notify_if_not_enough = old_notify_if_not_enough

    def claim_battle_pass(self):
        self.log_info(self.tr('battle pass'))
        self.ensure_main()
        try:
            self.send_key_down('alt')
            self.sleep(.05)
            self.click_relative(.86, .05)
        finally:
            self.send_key_up('alt')
        if not self.wait_ocr(0.2, 0.13, 0.32, 0.22, match=re.compile(r'\d+'), settle_time=1, raise_if_not_found=False):
            self.log_error(self.tr('can not battle pass, maybe ended'))
        else:
            self.click_relative(0.04, 0.3, after_sleep=1)
            self.click_relative(0.68, 0.91, hcenter=True, after_sleep=3)
            self.click_relative(0.04, 0.17, after_sleep=2)
            self.click_relative(0.68, 0.91, hcenter=True, after_sleep=2)
            self.wait_ocr(0.2, 0.13, 0.32, 0.22, match=re.compile(r'\d+'),
                          post_action=lambda: self.click(0.68, 0.91, after_sleep=1), settle_time=1,
                          raise_if_not_found=False)
        self.ensure_main()

    def open_daily(self):
        self.log_info(self.tr('open_daily'))
        self.openF2Book("gray_book_quest")
        self.click(0.17, 0.12, after_sleep=1)
        progress = self.ocr(0.1, 0.1, 0.5, 0.75, match=re.compile(r'^(\d+)/180$'))
        if not progress:
            self.click(0.974, 0.6, after_sleep=1)
            progress = self.ocr(0.1, 0.1, 0.5, 0.75, match=re.compile(r'^(\d+)/180$'))
        ready = self.get_total_daily_points() >= 100
        if progress:
            current = int(progress[0].name.split('/')[0])
        elif ready:
            current = 180
        else:
            # 已完成任务可能不再显示进度，识别缺失不能当成零再消费一遍波片。
            self.screenshot('daily_stamina_progress_unknown')
            raise ForegroundInputError('无法确认日常已用结晶波片，已停止资源操作')
        self.info_set('current daily progress', current)
        return current, ready

    def get_total_daily_points(self):
        points_boxes = self.ocr(0.19, 0.8, 0.30, 0.93, match=number_re)
        if points_boxes:
            try:
                points = int(re.sub(r'\D', '', points_boxes[0].name))
            except Exception:
                points = 0
        else:
            points = 0
        self.info_set('total daily points', points)
        return points

    def claim_daily(self):
        self.info_set('current task', 'claim daily')
        self.openF2Book('gray_book_quest')
        if not self.find_one('boss_proceed', box=self.box_of_screen(0.803, 0.189, 0.960, 0.312)):
            self.log_info(self.tr('no_boss_proceed, click claim'))
            # Click [Guidebook] in [Terminal] interface
            self.click(0.885, 0.250, after_sleep=2)
        self.log_info(self.tr('claim daily reward via  coordinate'))
        self.click(0.930, 0.882, after_sleep=1)
        self.ensure_main(time_out=10)

    def claim_mail(self):
        self.info_set('current task', 'claim mail')
        self.ensure_main()
        self.back(after_sleep=1.5)
        self.click(0.64, 0.95, after_sleep=1)
        self.click(0.14, 0.9, after_sleep=1)
        self.ensure_main(time_out=10)


from ok import run_task
from config import config

if __name__ == "__main__":
    run_task(config, task=DailyTask, debug=True)
