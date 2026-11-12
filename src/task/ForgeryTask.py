import cv2

from ok import Logger, find_color_rectangles
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted
from src.task.BaseWWTask import TEAM_SCREEN_FEATURES
from src.task.DomainTask import DomainTask
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)


class ForgeryTask(DomainTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = '⚒️ Forgery Challenge'
        self.icon = task_icon('forgery')
        self.group_name = 'Daily Activities'
        self.group_icon = task_icon('category-daily')
        self.description = 'Farms the selected Forgery Challenge. Must be able to teleport (F2).'
        self.support_schedule_task = True
        self.default_config = {
            'Which Forgery Challenge to Farm': 1,  # starts with 1
        }
        self.config_description = {
            'Which Forgery Challenge to Farm': 'The Forgery Challenge number in the F2 list.',
        }
        self.stamina_once = 40
        self.structure = [5, 5, 5, 5]
        self.total_number = sum(self.structure)
        self.material_mat = None

    def run(self):
        super().run()
        self.make_sure_in_world()
        self.farm_forgery()

    def farm_forgery(self, daily=False, used_stamina=0, config=None):
        if daily:
            must_use = 180 - used_stamina
        else:
            must_use = 0
        if config is None:
            config = self.config
        serial = config.get('Which Forgery Challenge to Farm', 1)

        def teleport_once():
            self.teleport_into_domain(serial, daily)

        self.farm_domain_with_recovery_loop(must_use, teleport_once)

    def purification_material(self):
        self.send_key("esc")
        self.sleep(1)
        self.click_relative(0.62, 0.7)
        self.sleep(1)
        box = self.box_of_screen(243 / 2560, 162 / 1440, 928 / 2560, 559 / 1440, name='ascension_materials')
        self.draw_boxes(box.name, box)
        self.wait_book()
        if self.material_mat is not None and \
            (target := self.wait_until(lambda: self.find_one(template=self.material_mat, box=box, threshold=0.7), time_out=1)):
            self.click_box(target, after_sleep=1)
        self.click_relative(0.75, 0.90, after_sleep=1)
        self.ensure_main()

    def teleport_into_domain(self, serial_number, daily=False):
        self.info_set('Teleport to Forgery Challenge', serial_number - 1)
        if serial_number > self.total_number:
            raise IndexError(self.tr('Index out of range, max is {total_number}').format(total_number=self.total_number))
        phase = 'select'
        recovering = False
        while True:
            try:
                if recovering and phase != 'select' and self.in_team_and_world():
                    return
                if phase == 'select':
                    self.openF2Book('gray_book_boss')
                    self.open_boss_book('ningsu')
                    self.click_on_book_target(serial_number, self.total_number, self.structure)
                    phase = 'proceed'
                if phase == 'proceed':
                    if not recovering:
                        self.click(.891, .910, after_sleep=1)
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
                        raise ForegroundInputError('无法确认锻造挑战入口的新画面，已停止操作')
                    phase = 'team'
                if phase == 'team':
                    self.click_team_challenge()
                    phase = 'wait'
                self.wait_in_team_and_world(time_out=self.teleport_timeout)
                return
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
                recovering = True

    def get_material_mat(self):
        min_width = self.width_of_screen(80 / 2560)
        min_height = self.height_of_screen(80 / 1440)
        box = self.box_of_screen(2205 / 2560, 566 / 1440, 2357 / 2560, 984 / 1440)
        self.draw_boxes(box.name, box)
        material_boxes = find_color_rectangles(self.frame, material_box_color, min_width, min_height,
                                               box=box, threshold=0.6)
        if material_boxes:
            box_start = self.width_of_screen(20 / 2560)
            box_len = self.width_of_screen(90 / 2560)
            target = min(material_boxes, key=lambda box: box.y)
            logger.info(f"Found {len(material_boxes)} material boxes, selected target at y={target.y}")
            mat_box = target.copy(box_start, box_start, box_len - target.width, box_len - target.height, 'material_mat')
            self.draw_boxes(mat_box.name, mat_box)
            self.material_mat = cv2.resize(mat_box.crop_frame(self.frame), None,
                                           fx=1.1, fy=1.1, interpolation=cv2.INTER_LINEAR)


material_box_color = {
    'r': (45, 75),  # Red range
    'g': (45, 75),  # Green range
    'b': (45, 75)  # Blue range
}
