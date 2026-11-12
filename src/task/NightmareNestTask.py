import re
import cv2
from dataclasses import dataclass

from ok import Logger
from ok.device.interaction_methods.foreground_input import ForegroundInputInterrupted
from src.task.BaseCombatTask import BaseCombatTask, CharRevivedException
from src.task.BaseWWTask import TEAM_SCREEN_FEATURES
from src.task.WWOneTimeTask import WWOneTimeTask
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)
TRAVEL_FEATURES = ['fast_travel_custom', 'gray_teleport', 'remove_custom']
CONFIRM_FEATURES = ['confirm_btn_hcenter_vcenter', 'confirm_btn_highlight_hcenter_vcenter']


@dataclass
class NestTarget:
    box: object
    cache_key: str


class NightmareNestTask(WWOneTimeTask, BaseCombatTask):
    max_nest_deaths = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_config = {'_enabled': True}
        self.trigger_interval = 0.1
        self.target_enemy_time_out = 10
        self.name = "🌙 Nightmare Nest Task"
        self.icon = task_icon('nightmare')
        self.group_name = 'Daily Activities'
        self.group_icon = task_icon('category-daily')
        self.description = "Auto Farm all Nightmare Nest"
        self.support_schedule_task = True
        self.count_re = re.compile(r"(\d{1,2})/(\d{1,2})")
        self.queues = []
        self._capture_success = False
        self._capture_mode = False
        self._unreachable_nests = set()
        self._nest_deaths = {}
        self._nest_tab_of_current_nest = 'go_nest'
        self.default_config.update({'Which to Farm': ['Nightmare Purification', 'Tacet Discord Nest']})
        self.config_type['Which to Farm'] = {'type': "multi_selection",
                                             'options': ['Nightmare Purification', 'Tacet Discord Nest']}

    def run(self):
        return self._start_nest_run(capture=False)

    def run_capture_mode(self):
        return self._start_nest_run(capture=True)

    def _start_nest_run(self, capture):
        self._capture_mode = capture
        self._capture_success = False
        self._unreachable_nests.clear()
        self._nest_deaths.clear()
        self._nest_phase = 'prepare'
        WWOneTimeTask.run(self)
        return self._continue_nest_run()

    def resume_after_foreground(self):
        if not getattr(self, '_nest_phase', None):
            return super().resume_after_foreground()
        return self._continue_nest_run()

    def _continue_nest_run(self):
        while self._nest_phase != 'completed':
            try:
                if self._nest_phase == 'prepare':
                    self.ensure_main(time_out=30)
                    self._init_queue()
                    self._nest_phase = 'next'
                if self._nest_phase == 'next':
                    self._current_nest = self.get_nest_to_go()
                    self._nest_phase = 'nest' if self._current_nest else 'leave'
                if self._nest_phase == 'nest':
                    self.combat_nest(self._current_nest)
                    self._nest_phase = 'leave' if self._capture_mode and self._capture_success else 'next'
                if self._nest_phase == 'leave':
                    self.ensure_main(time_out=30)
                    self._nest_phase = 'completed'
            except ForegroundInputInterrupted:
                # 保留队列和已经完成的聚落，不把失焦记成死亡或不可达。
                self.wait_foreground_frame()

    def on_combat_check(self):
        if self._capture_mode:
            self.pick_f(handle_claim=False)
            if self.has_echo_notification():
                return self.reset_to_false(reason='echo captured')
        return True

    def has_echo_notification(self):
        if self.find_best_match_in_box(self.box_of_screen(0.078, 0.488, 0.094, 0.514),
                                       ['char_1_text', 'char_3_text'], 0.6,
                                       frame_processor=convert_image_to_negative):
            self._capture_success = True
        return self._capture_success

    def combat_nest(self, nest):
        target_box = nest.box if isinstance(nest, NestTarget) else nest
        phase = 'select'
        recovering = False
        is_team = False
        wait_combat_time = 10
        while True:
            if phase == 'completed':
                return
            try:
                if recovering and phase in ('select', 'feature'):
                    if self.in_team_and_world():
                        is_team = bool(self.in_realm())
                        phase = 'combat'
                    elif feature := self.find_one([*TRAVEL_FEATURES, *TEAM_SCREEN_FEATURES]):
                        is_team = feature.name in TEAM_SCREEN_FEATURES
                        phase = 'enter'
                    else:
                        # 列表被用户滚动或目标已完成，旧行坐标不能继续使用。
                        fresh = self.get_nest_to_go()
                        if not fresh or (isinstance(nest, NestTarget) and fresh.cache_key != nest.cache_key):
                            return
                        nest = fresh
                        target_box = fresh.box
                        phase = 'select'
                if phase == 'select':
                    self.click(target_box, after_sleep=2)
                    phase = 'feature'
                if phase == 'feature':
                    feature = self.wait_feature([*TRAVEL_FEATURES, *TEAM_SCREEN_FEATURES], time_out=10,
                                                settle_time=.5, raise_if_not_found=True)
                    is_team = feature.name in TEAM_SCREEN_FEATURES
                    phase = 'enter'
                if phase == 'enter':
                    if is_team:
                        self.click_team_challenge()
                    elif not self._travel_to_nest_or_skip(nest):
                        return
                    phase = 'approach'
                if phase == 'approach':
                    self.wait_in_team_and_world(time_out=120)
                    if not is_team:
                        while self.find_f_with_text():
                            self.pick_f(handle_claim=False)
                            self.wait_in_team_and_world(time_out=40, raise_if_not_found=False)
                        self.sleep(2)
                        self.run_until(self.in_combat, 'w', time_out=10, running=False, target=True)
                    phase = 'combat'
                if phase == 'combat':
                    need_find = self.combat_once(wait_combat_time=wait_combat_time, target=True,
                                                 raise_if_not_found=False)
                    phase = 'pickup'
                if phase == 'pickup':
                    if not (self._capture_mode and (self._capture_success
                            or self.wait_until(self.has_echo_notification, time_out=3))):
                        self._capture_success = self._pick_nest_echo(nest, need_find, is_team)
                    phase = 'recheck'
                if phase == 'recheck':
                    if self._should_continue_combat_after_pickup():
                        self.log_info(self.tr('nightmare nest: combat detected after pickup'))
                        wait_combat_time = 1
                        phase = 'combat'
                        continue
                    phase = 'leave'
                if is_team:
                    self.esc_world_confirm()
                phase = 'completed'
                self.sleep(1)
                return
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
                recovering = True
            except CharRevivedException:
                if isinstance(nest, NestTarget):
                    deaths = self._nest_deaths.get(nest.cache_key, 0) + 1
                    self._nest_deaths[nest.cache_key] = deaths
                    if deaths >= self.max_nest_deaths:
                        self._unreachable_nests.add(nest.cache_key)
                        self.log_info(self.tr('nightmare nest: died {deaths} times, skip this run: {cache_key}').format(deaths=deaths, cache_key=nest.cache_key),
                                      notify=True)
                        return
                self.log_info(self.tr('nightmare nest: death recovered, re-enter from F2 book'))
                return

    def _pick_nest_echo(self, nest, need_find, is_team):
        self.sleep(3)
        if not need_find or self.walk_find_echo(time_out=5, backward_time=2.5):
            self.log_info(self.tr('farm echo walk find true'))
            return True
        dropped = self.yolo_find_echo(turn=True, use_color=False, time_out=30)[0]
        if dropped or is_team:
            return dropped
        self.log_info(self.tr('no echo collected, re-teleport to current nest as fallback'))
        self.ensure_main(time_out=30)
        self.openF2Book('gray_book_boss')
        getattr(self, self._nest_tab_of_current_nest)()
        self.sleep(1)
        fresh = self._refresh_nest_target(nest)
        if not fresh:
            return False
        self.click(fresh.box, after_sleep=2)
        if (self.wait_feature(TRAVEL_FEATURES, time_out=5, settle_time=.5, raise_if_not_found=False)
                and self._travel_to_nest_or_skip(fresh)):
            self.sleep(2)
            self.run_until(lambda: False, 'w', time_out=2, running=True)
            return bool(self.walk_find_echo(time_out=5, backward_time=2.5)
                        or self.yolo_find_echo(turn=True, use_color=False, time_out=30)[0])
        return False

    def _should_continue_combat_after_pickup(self):
        return not self._capture_mode and self.wait_combat(
            target=True, time_out=3, raise_if_not_found=False)

    def _travel_to_nest_or_skip(self, nest):
        recovering = False
        while True:
            try:
                if recovering and self.in_team_and_world():
                    return True
                travel = self.wait_until(self._find_travel_button, raise_if_not_found=False, time_out=1)
                if travel:
                    self.click(travel, after_sleep=1)
                if confirm := self._find_first_feature(CONFIRM_FEATURES, threshold=.6):
                    self.click(confirm, after_sleep=1)
                button_still_visible = travel and self.find_one(travel.name, threshold=.7)
                if travel and not button_still_visible and self.wait_in_team_and_world(
                        time_out=120, raise_if_not_found=False):
                    return True
                self.back(after_sleep=1)
                if isinstance(nest, NestTarget):
                    self._unreachable_nests.add(nest.cache_key)
                    self.log_info(self.tr('nightmare nest unreachable, skip this run: {cache_key}').format(cache_key=nest.cache_key))
                else:
                    self.log_info(self.tr('nightmare nest unreachable, skip this run'))
                return False
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
                recovering = True

    def _find_travel_button(self):
        return self._find_first_feature(TRAVEL_FEATURES, threshold=0.7)

    def _find_first_feature(self, feature_names, threshold):
        for feature_name in feature_names:
            if feature := self.find_one(feature_name, threshold=threshold):
                return feature

    def get_nest_to_go(self):
        self.openF2Book("gray_book_boss")

        while self.queues:
            self.queues[0]()
            if nest := self.find_nest():
                self._nest_tab_of_current_nest = self.queues[0].__name__
                return nest
            self.queues.pop(0)

    def _init_queue(self):
        quests = self.config.get('Which to Farm') or ['Nightmare Purification', 'Tacet Discord Nest']
        actions = []
        if 'Tacet Discord Nest' in quests:
            actions.append(self.go_nest)
            actions.append(self.go_nest_scroll)
        if 'Nightmare Purification' in quests:
            actions.append(self.go_nightmare)
            actions.append(self.go_nightmare_scroll)
        self.queues = actions

    def go_nightmare(self):
        self.open_boss_book('mengyan')
        self.log_info(self.tr('go nightmare'))

    def go_nightmare_scroll(self):
        self.open_boss_book('mengyan')
        self.click(3737 / 3840, 0.54, after_sleep=1)
        self.log_info(self.tr('go nightmare scroll'))

    def go_nest(self):
        self.open_boss_book('canxiang')

    def go_nest_scroll(self):
        self.open_boss_book('canxiang')
        self.click(0.9730, 0.8806, after_sleep=1)

    def find_nest(self):
        counts = self.ocr(0.35, 0.25, 1, 0.96, match=self.count_re)
        for count_box in counts:
            for match in re.finditer(self.count_re, count_box.name):
                numerator = match.group(1)
                denominator = match.group(2)
                if numerator != denominator and denominator in ['24', '36', '48', '41'] and numerator == '0':
                    cache_key = self._make_nest_cache_key(count_box, denominator)
                    if cache_key in self._unreachable_nests:
                        self.log_info(self.tr('skip cached unreachable nightmare nest: {cache_key}').format(cache_key=cache_key))
                        continue
                    self.log_info(self.tr('{count_box} is not complete').format(count_box=count_box))
                    count_box.x = self.width_of_screen(0.9)
                    count_box.y -= count_box.height * 0.9
                    count_box.height = 1
                    count_box.width = 1
                    return NestTarget(count_box, cache_key)

    def _make_nest_cache_key(self, count_box, denominator):
        action_name = self.queues[0].__name__ if self.queues else 'unknown'
        screen_height = max(self.height_of_screen(1), 1)
        row_y = (count_box.y + count_box.height / 2) / screen_height
        row_slot = round(row_y / 0.02)
        # 使用粗粒度行槽位，避免 OCR 坐标轻微抖动导致同一目标被重复点击。
        return f'{action_name}:{denominator}:{row_slot}'

    def _refresh_nest_target(self, nest):
        """保底收取仍允许回到已完成聚落，但必须从当前列表重新定位同一行。"""
        if not isinstance(nest, NestTarget):
            return self.find_nest()
        for box in self.ocr(.35, .25, 1, .96, match=self.count_re):
            for match in re.finditer(self.count_re, box.name):
                if self._make_nest_cache_key(box, match.group(2)) == nest.cache_key:
                    fresh = box.copy()
                    fresh.x = self.width_of_screen(.9)
                    fresh.y -= fresh.height * .9
                    fresh.height = fresh.width = 1
                    return NestTarget(fresh, nest.cache_key)
        return None


def convert_image_to_negative(img):
    to_gray = False
    _mat = img
    if len(_mat.shape) == 3:
        to_gray = True
        _mat = cv2.cvtColor(_mat, cv2.COLOR_BGR2GRAY)
    _, _mat = cv2.threshold(_mat, 80, 255, cv2.THRESH_BINARY)
    _mat = cv2.bitwise_not(_mat)
    if to_gray:
        _mat = cv2.cvtColor(_mat, cv2.COLOR_GRAY2BGR)
    return _mat


from ok import run_task
from config import config

if __name__ == "__main__":
    run_task(config, task=NightmareNestTask, debug=True)
