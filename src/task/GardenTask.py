import re


from ok import Logger, run_task, ForegroundInputError, ForegroundInputInterrupted
from ok.feature.Box import relative_box
from config import config
from src.Labels import Labels
from src.task.BaseWWTask import BaseWWTask
from src.task.WWOneTimeTask import WWOneTimeTask
from src.task.task_icons import task_icon

logger = Logger.get_logger(__name__)


class GardenTask(WWOneTimeTask, BaseWWTask):
    GARDEN_TARGET_POINTS = re.compile('6000')
    GARDEN_LIMIT_PATTERN = re.compile(r'已达[到至]?上限|已達[到至]?上限|limit reached', re.I)
    WEEKLY_REWARD_POINT = (.927, .893)
    WEEKLY_REWARD_TITLE_PATTERN = re.compile(
        r'^\s*(?:获得(?:奖励|物品)?|獲得(?:獎勵|物品)?|Rewards?\s+Obtained)\s*$', re.I)
    WEEKLY_PAGE_TITLE_PATTERN = re.compile(r'周度[游遊][历歷]|Weekly\s+Activity', re.I)
    WEEKLY_GARDEN_ENTRY_PATTERN = re.compile(r'幻[梦夢][游遊][园園]|Phantasma\s+Dreamland', re.I)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "🎡 自动周常乐园"
        self.icon = task_icon('garden')
        self.group_name = 'Daily Activities'
        self.group_icon = task_icon('category-daily')
        self.description = "Detect and click garden actions until the task is stopped."
        self.garden_features = [
            label.value for label in Labels
            if label.value.startswith("garden_")
        ]
        self.garden_priority_features = [
            "garden_continue_game",
            "garden_get_skip",
            "garden_not_interested_confirm",
        ]

    def run(self):
        self._garden_phase = 'prepare'
        self._garden_needs_refresh = False
        self._garden_pending_source = None
        WWOneTimeTask.run(self)
        return self.resume_after_foreground()

    def on_foreground_resume(self):
        super().on_foreground_resume()
        self._garden_needs_refresh = True
        if getattr(self, '_garden_active', False) or getattr(self, '_garden_navigation_active', False):
            raise ForegroundInputInterrupted('乐园页面已变化，需要重新确认当前阶段')

    def resume_after_foreground(self):
        if not getattr(self, '_garden_phase', None):
            return super().resume_after_foreground()
        self._garden_active = True
        try:
            while self._garden_phase != 'completed':
                try:
                    if self._garden_needs_refresh:
                        self.wait_foreground_frame()
                        self._garden_needs_refresh = False
                        if self._garden_phase == 'blessing':
                            # 祝福选择尚未开始游戏，可退出局部选择页重新进入；不恢复中断的固定坐标序列。
                            self._garden_phase = 'prepare'
                    if self._garden_phase == 'prepare':
                        self.ensure_main()
                        self.open_garden_weekly_page()
                        if self.is_weekly_garden_completed():
                            self._garden_phase = 'completed'
                            break
                        self._garden_phase = 'enter'
                    if self._garden_phase == 'enter':
                        if not self.find_best_garden_feature():
                            if not self._is_weekly_garden_page():
                                self._garden_unknown('恢复后未找到乐园入口或周常页面，已停止旧坐标操作')
                            self._garden_phase = 'enter_wait'
                            self.click(.246, .486, after_sleep=1)
                        else:
                            self._garden_phase = 'play'
                    if self._garden_phase == 'enter_wait':
                        if not self.wait_until(self.find_best_garden_feature, time_out=10, raise_if_not_found=False):
                            self._garden_unknown('无法确认进入乐园的结果，已停止旧入口操作')
                        self._garden_phase = 'play'
                    if self._garden_phase == 'card_choose':
                        self._choose_garden_card()
                    if self._garden_phase == 'card_confirm':
                        self._submit_garden_transaction('garden_get_skip', 'garden_get_confirm_gray')
                    if self._garden_phase == 'reject_confirm':
                        self._submit_garden_transaction('garden_not_interested_confirm', 'garden_not_interested_confirm')
                    if self._garden_phase == 'pending':
                        self._confirm_garden_transaction()
                        self._garden_pending_source = None
                        self._garden_phase = 'play'
                    if self._garden_phase == 'blessing':
                        self._choose_first_blessing()
                        self._garden_phase = 'play'
                        # 祝福窗口已关闭，必须重新定位开始按钮，不能使用选择前的旧框。
                        start = self.find_one('garden_start_game')
                        if start:
                            self.click(start, after_sleep=1)
                    if self._garden_phase == 'result_return':
                        self._leave_garden_result()
                        self._garden_phase = 'weekly'
                    if self._garden_phase == 'weekly':
                        self.open_garden_weekly_page()
                        self._garden_phase = 'completed'
                        break
                    if self._garden_phase == 'play':
                        self._play_garden_step()
                except ForegroundInputInterrupted:
                    self._garden_needs_refresh = True
        finally:
            self._garden_active = False
        self.log_info(self.tr('Garden task completed; limit reached'), notify=True)

    def _play_garden_step(self):
        # 所有稳定等待在识别之前，失焦返回后不会继续使用旧卡片、按钮或结算框。
        self.sleep(.3)
        target = self.find_best_garden_feature()
        if not target:
            self._check_garden_result()
            return
        self.info_set('current task', target.name)
        if target.name == 'garden_get_skip':
            self._choose_garden_card()
        elif target.name == 'garden_not_interested':
            choices = self.find_feature('garden_not_interested', vertical_variance=.4)
            if choices:
                self._garden_phase = 'reject_confirm'
                self.click(choices[-1], after_sleep=1)
                self._submit_garden_transaction('garden_not_interested_confirm', 'garden_not_interested_confirm')
        elif target.name == 'garden_start_game':
            self._garden_phase = 'blessing'
        elif target.name == 'garden_shop_buy':
            self._submit_garden_transaction('garden_shop_buy', target)
        else:
            self.log_info(self.tr('click {name} {confidence:.3f}').format(
                name=target.name, confidence=target.confidence))
            self.click(target, after_sleep=1)

    def _choose_garden_card(self):
        self._garden_phase = 'card_choose'
        self.sleep(1)
        if not self.find_one('garden_get_skip'):
            self._garden_phase = 'play'
            return
        if gold := self.find_one('garden_get_gold', horizontal_variance=.9):
            self.click(gold, after_sleep=1)
        elif purple := self.find_one('garden_get_purple', horizontal_variance=.9):
            self.click(purple, after_sleep=1)
        else:
            self.click(.5, .2, after_sleep=1)
        self._garden_phase = 'card_confirm'
        self._submit_garden_transaction('garden_get_skip', 'garden_get_confirm_gray')

    def _submit_garden_transaction(self, source, button):
        if isinstance(button, str):
            button = self.wait_feature(button, time_out=3, raise_if_not_found=False)
            if not button:
                self._garden_unknown('当前乐园选择的确认按钮无法识别，已停止旧坐标操作')
        self._garden_pending_source = source
        self._garden_phase = 'pending'
        # 先保存提交状态；失焦后的等待只能确认页面已离开或购买已完成，不能再发旧确认。
        self.click(button, after_sleep=1)

    def _confirm_garden_transaction(self):
        start = self.business_time()
        absent_frames = 0
        source = self._garden_pending_source
        contexts = {
            'garden_get_skip': ['garden_get_skip', 'garden_get_gold', 'garden_get_purple', 'garden_get_confirm_gray'],
            'garden_not_interested_confirm': ['garden_not_interested', 'garden_not_interested_confirm'],
            'garden_shop_buy': ['garden_shop_buy', 'garden_shop_close'],
        }
        context_features = contexts.get(source, [source])
        while self.business_time() - start < 6:
            self.next_frame()
            if source == 'garden_shop_buy':
                status_box = self.get_box_by_name(source).scale(4)
                if self.ocr(box=status_box, match=re.compile(r'已购买|已購買|售罄|Sold Out|Purchased', re.I)):
                    return
            target = self.find_best_garden_feature()
            # 单个背景模板消失可能只是悬停或动画，必须确认旧页面的整组控件退出且新页面已可识别。
            if (not any(self.find_one(name) for name in context_features) and target
                    and target.name not in context_features):
                absent_frames += 1
                if absent_frames >= 2:
                    return
            else:
                absent_frames = 0
            self.sleep(.3)
        self._garden_unknown('无法确认乐园选择或购买结果，已停止以避免重复提交')

    def _check_garden_result(self):
        if not self.find_one('a_garden_restart') or not self.find_one('a_garden_back'):
            return
        self.sleep(2)
        garden_restart = self.find_one('a_garden_restart')
        garden_back = self.find_one('a_garden_back')
        if not garden_restart or not garden_back:
            return
        texts = self.ocr(.373, .346, .859, .615)
        text = ' '.join(str(getattr(box, 'name', box)) for box in texts)
        if not self.GARDEN_TARGET_POINTS.search(text) and not self.GARDEN_LIMIT_PATTERN.search(text):
            self._garden_unknown('乐园结算积分识别不完整，无法确认是否达到周常上限')
        if self.is_garden_done(texts):
            self._garden_phase = 'result_return'
        else:
            self.click(garden_restart, after_sleep=1)

    def _leave_garden_result(self):
        if garden_back := self.find_one('a_garden_back'):
            self.click(garden_back, after_sleep=1)
        if self.wait_feature('garden_start_game', settle_time=1, time_out=5):
            self.back(after_sleep=1)

    def _garden_unknown(self, message):
        self.screenshot('garden_result_unknown')
        raise ForegroundInputError(message)

    def open_garden_weekly_page(self):
        # 正常顺序恢复上游：开书、选周常、同点领取并关闭；不再以红点扫描决定首次点击。
        with self.executor.task_context(self):
            self._garden_navigation_active = True
            deadline = self.business_time() + 60
            needs_navigation = getattr(self, '_garden_weekly_step', None) not in ('first_pending', 'close')
            resume_required = False
            try:
                while self.business_time() < deadline:
                    try:
                        if resume_required:
                            self.wait_foreground_frame()
                            resume_required = False
                            needs_navigation = (getattr(self, '_garden_weekly_step', None) is None
                                                and not self._is_weekly_garden_page())
                        if needs_navigation:
                            self.openF2Book('gray_book_quest')
                            self.sleep(1)
                            self.click(.343, .129, after_sleep=1)
                            needs_navigation = False
                        if getattr(self, '_garden_weekly_step', None) is None:
                            frame = self.next_frame()
                            if not self._is_weekly_garden_page(frame=frame):
                                self._garden_unknown('无法确认乐园周常页面，已停止旧坐标操作')
                            # 点击与 after_sleep 都可能失焦；先记录尝试，恢复只查结果，不能重播领取。
                            self._garden_weekly_step = 'first_pending'
                            self.click(*self.WEEKLY_REWARD_POINT, after_sleep=3)
                        if self._garden_weekly_step == 'first_pending':
                            self._confirm_weekly_first_click(self.next_frame())
                            self._garden_weekly_step = 'close'
                            self.click(*self.WEEKLY_REWARD_POINT, after_sleep=2)
                        else:
                            # 关闭中断后已回周常即可结束；仍有获得弹窗时才续关，不再发送首次领取。
                            frame = self.next_frame()
                            if self._weekly_reward_visible(frame):
                                self.click(*self.WEEKLY_REWARD_POINT, after_sleep=2)
                            elif not self._is_weekly_garden_page(frame=frame):
                                self._garden_unknown('无法确认乐园奖励关闭结果，已停止重复操作')
                        frame = self.next_frame()
                        if self._weekly_reward_visible(frame) or not self._is_weekly_garden_page(frame=frame):
                            self._garden_unknown('无法确认乐园奖励已关闭，已停止重复操作')
                        self._garden_weekly_step = None
                        return
                    except ForegroundInputInterrupted:
                        resume_required = True
                self._garden_unknown('乐园周常领奖等待超时，已停止以避免重复领取')
            finally:
                self._garden_navigation_active = False

    def _weekly_reward_visible(self, frame):
        epoch = getattr(self, '_foreground_epoch', 0)
        # 实际弹窗标题仅为“获得”；限定中央标题区，避开卡片上的“通关可获得游历值”。
        title = self.ocr(.4, .2, .6, .3, match=self.WEEKLY_REWARD_TITLE_PATTERN, frame=frame)
        self._check_weekly_frame_epoch(epoch)
        return bool(title)

    def _confirm_weekly_first_click(self, frame):
        epoch = getattr(self, '_foreground_epoch', 0)
        if self._weekly_reward_visible(frame):
            return
        if not self._is_weekly_garden_page(frame=frame):
            self._garden_unknown('无法确认乐园首次领奖结果，已停止以避免重复领取')
        points = self._read_weekly_garden_points(frame)
        if points >= 6000:
            height, width = frame.shape[:2]
            # 原版在已领时两次点击均无操作；灰勾只作为点击后的正向结果，不作首次点击门槛。
            claimed = self.find_one('weekly_garden_reward_claimed', frame=frame, threshold=.8,
                                    box=relative_box(width, height, .903, .85, .947, .931))
            self._check_weekly_frame_epoch(epoch)
            if not claimed:
                self._garden_unknown('无法确认乐园首次领奖结果，已停止以避免重复领取')
        self._check_weekly_frame_epoch(epoch)
        # 尚未达到末档时，原版点击锁定目标无操作是正常情况，接着保留原第二次点击。

    def _check_weekly_frame_epoch(self, epoch):
        if epoch != getattr(self, '_foreground_epoch', 0):
            raise ForegroundInputInterrupted('乐园周常画面已更新，需要重新识别')

    def _is_weekly_garden_page(self, frame=None):
        epoch = getattr(self, '_foreground_epoch', 0)
        frame = self.frame if frame is None else frame
        # 零分可能被 OCR 读成字母 O；用同一帧的标题和玩法入口确认页面，积分另行判断。
        title = self.ocr(.09, .08, .41, .17, frame=frame)
        weekly_title = bool(self.WEEKLY_PAGE_TITLE_PATTERN.search(' '.join(box.name for box in title)))
        entry = self.ocr(.11, .36, .38, .44, frame=frame) if weekly_title else []
        if epoch != getattr(self, '_foreground_epoch', 0):
            raise ForegroundInputInterrupted('乐园周常画面已更新，需要重新确认页面')
        return bool(weekly_title and self.WEEKLY_GARDEN_ENTRY_PATTERN.search(' '.join(box.name for box in entry)))

    def is_weekly_garden_completed(self):
        # Daily 读取积分时导航上下文已经退出，单独注册才能收到 OCR 等待期间的恢复通知。
        with self.executor.task_context(self):
            epoch = getattr(self, '_foreground_epoch', 0)
            frame = self.frame
            self._check_weekly_frame_epoch(epoch)
            return self._read_weekly_garden_points(frame) >= 6000

    def _read_weekly_garden_points(self, frame, numbers=None):
        epoch = getattr(self, '_foreground_epoch', 0)
        height, width = frame.shape[:2]

        def in_current_area(box):
            return (.102 * width <= box.x and box.x + box.width <= .284 * width
                    and .793 * height <= box.y and box.y + box.height <= .956 * height)

        # 优先保留整帧识别结果；卡片介绍和奖励刻度不参与当前积分判断。
        if numbers is None:
            numbers = self.ocr(frame=frame, match=re.compile(r'\d'))
        self._check_weekly_frame_epoch(epoch)
        current = [box for box in numbers if in_current_area(box)]
        if not current:
            # OCR v6 的整帧检测会漏掉单独的 0；只在同一帧原积分框补读一次。
            # 已有但矛盾或非法的候选仍须报错，不能用局部结果覆盖成成功。
            local = self.ocr(.102, .793, .284, .956, frame=frame, match=re.compile(r'\d'))
            self._check_weekly_frame_epoch(epoch)
            current = [box for box in local if in_current_area(box)]
        self.log_info(self.tr('Garden current: {current}').format(current=current))
        points = {int(progress.group(1).replace(',', '')) for box in current
                  if (progress := re.fullmatch(r'(\d[\d,]*)(?:\s*/\s*6,?000)?', box.name.strip()))}
        if len(points) != 1:
            self._garden_unknown('无法确认乐园当前游历值，已停止以避免重复运行')
        return points.pop()

    def is_garden_done(self, texts):
        text = " ".join(str(getattr(box, "name", box)) for box in texts)
        progress = re.search(r'(\d[\d,]*)\s*/\s*6000', text)
        return (bool(self.GARDEN_LIMIT_PATTERN.search(text)) or text.count('6000') >= 2
                or progress is not None and int(progress.group(1).replace(',', '')) >= 6000)

    def find_best_garden_feature(self):
        matches = []
        for feature_name in self.garden_features:
            if not self.feature_exists(feature_name):
                continue
            if feature_name == 'garden_get_confirm_gray' or feature_name == 'garden_not_interested_confirm':
                continue
            if feature_name == 'garden_not_interested':
                matches.extend(self.find_feature(feature_name, vertical_variance=0.4))
            else:
                matches.extend(self.find_feature(feature_name))
        for priority_feature in self.garden_priority_features:
            priority_matches = [
                match for match in matches
                if match.name == priority_feature
            ]
            if priority_matches:
                return max(priority_matches, key=lambda box: box.confidence)
        return max(matches, key=lambda box: box.confidence, default=None)

    def _choose_first_blessing(self):
        """仅在尚未开始游戏的入口选择第一项祝福；中断后由准备阶段重新进入。"""
        self.click(965 / 1920, 860 / 1080, after_sleep=2)
        self.click(700 / 1920, 666 / 1080, after_sleep=2)
        self.click(1600 / 1920, 900 / 1080, after_sleep=2)


if __name__ == "__main__":
    run_task(config, task=GardenTask, debug=True)
