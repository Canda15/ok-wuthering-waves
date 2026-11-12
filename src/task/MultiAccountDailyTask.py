import ctypes
import re
from ctypes import wintypes

import win32gui
import win32process

from ok import Box
from ok.device.interaction_methods.foreground_input import ForegroundInputInterrupted
from ok.task.exceptions import WaitFailedException
from src.task.DailyTask import DailyTask
from src.task.WWOneTimeTask import WWOneTimeTask
from src.task.BaseCombatTask import BaseCombatTask
from src.task.BaseWWTask import LOGIN_TEXTS
from src.task.task_icons import task_icon

account_pattern = re.compile(r'\*\*\*\*')
PER_ACCOUNT_CONFIGURATION = 'Per-account Configuration'
ACCOUNT_CONFIGURATION = 'Account Configuration'

CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXT, CB_GETLBTEXTLEN = 0x146, 0x147, 0x148, 0x149
CB_GETITEMHEIGHT, CB_GETEXTENDEDUI, CB_GETDROPPEDSTATE = 0x154, 0x156, 0x157
_SendMessageW = ctypes.windll.user32.SendMessageW
_SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_SendMessageW.restype = ctypes.c_ssize_t


def normalize_account_name(account):
    if not account:
        return account
    return account.lower().replace('0', 'o').replace('.con', '.com')


def find_login_combo(game_hwnd):
    """国际服 KRSDK 登录框是原生 #32770 对话框，账号列表是标准 ComboBox。
    只读查询用于识别和确认，选项变更必须由真实前台键鼠完成。找不到返回 None。
    """
    if not game_hwnd or not win32gui.IsWindow(game_hwnd):
        return None
    _, game_pid = win32process.GetWindowThreadProcessId(game_hwnd)
    combos = []

    def on_child(child, _):
        if (win32gui.GetClassName(child) == 'ComboBox' and win32gui.IsWindowVisible(child)
                and _SendMessageW(child, CB_GETCOUNT, 0, 0) > 0):
            combos.append(child)

    def on_top(hwnd, _):
        if (win32gui.GetClassName(hwnd) == '#32770' and win32gui.IsWindowVisible(hwnd)
                and win32process.GetWindowThreadProcessId(hwnd)[1] == game_pid):
            win32gui.EnumChildWindows(hwnd, on_child, None)

    win32gui.EnumWindows(on_top, None)
    return combos[0] if combos else None


def combo_items(combo):
    items = []
    count = _SendMessageW(combo, CB_GETCOUNT, 0, 0)
    if count < 0:
        raise WaitFailedException('无法读取登录账号列表')
    for i in range(count):
        length = _SendMessageW(combo, CB_GETLBTEXTLEN, i, 0)
        if length < 0:
            raise WaitFailedException('无法读取登录账号选项')
        buf = ctypes.create_unicode_buffer(length + 1)
        if _SendMessageW(combo, CB_GETLBTEXT, i, ctypes.addressof(buf)) < 0:
            raise WaitFailedException('无法读取登录账号选项')
        items.append(buf.value)
    return items


def combo_selected_item(combo):
    index = _SendMessageW(combo, CB_GETCURSEL, 0, 0)
    items = combo_items(combo)
    return items[index] if 0 <= index < len(items) else None


def select_combo_item(task, combo, index, expected_item):
    """通过任务公共输入真实选号，随后只读确认；不伪造控件或父窗口通知。"""
    task.next_frame()
    if combo != task._login_combo():
        return False
    items = combo_items(combo)
    if not 0 <= index < len(items) or items[index] != expected_item:
        return False
    if (_SendMessageW(combo, CB_GETCURSEL, 0, 0) == index
            and not _SendMessageW(combo, CB_GETDROPPEDSTATE, 0, 0)):
        return True

    left, top, right, bottom = win32gui.GetClientRect(combo)
    field_height = _SendMessageW(combo, CB_GETITEMHEIGHT, -1, 0)
    if right <= left or bottom <= top or field_height <= 0:
        return False
    # 原生查询返回屏幕位置，公共输入接收捕获像素；两边使用同一裁剪原点，
    # 不能把登录子窗口的客户坐标直接当作游戏坐标，也不能再次乘 DPI 比例。
    screen_x, screen_y = win32gui.ClientToScreen(
        combo, ((left + right) // 2, top + min(bottom - top, field_height) // 2))
    origin_x, origin_y = task.executor.interaction.capture.get_abs_cords(0, 0)
    x, y = round(screen_x - origin_x), round(screen_y - origin_y)
    if not (0 <= x < task.width and 0 <= y < task.height):
        return False
    task.click(x, y, after_sleep=.2)
    if not _SendMessageW(combo, CB_GETDROPPEDSTATE, 0, 0):
        # 普通下拉框用 F4 展开，扩展界面用向下键展开；展开后 Home 才选择列表首项。
        key = 'down' if _SendMessageW(combo, CB_GETEXTENDEDUI, 0, 0) else 'f4'
        task.send_key(key, after_sleep=.1)
    if not _SendMessageW(combo, CB_GETDROPPEDSTATE, 0, 0):
        return False
    task.send_key('home', after_sleep=.05)
    for _ in range(index):
        task.send_key('down', after_sleep=.05)
    task.send_key('enter', after_sleep=.3)

    def confirmed():
        return (combo == task._login_combo()
                and _SendMessageW(combo, CB_GETCURSEL, 0, 0) == index
                and not _SendMessageW(combo, CB_GETDROPPEDSTATE, 0, 0)
                and combo_selected_item(combo) == expected_item)

    return bool(task.wait_until(confirmed, time_out=2, raise_if_not_found=False))


class MultiAccountDailyTask(WWOneTimeTask, BaseCombatTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "👥 Multi Account Daily Task"
        self.icon = task_icon('multi-account')
        self.group_name = 'Multi-account'
        self.group_icon = task_icon('multi-account')
        self.description = "Automatically switch accounts and run Daily Task for each account"
        self.add_exit_after_config()
        self.default_config[PER_ACCOUNT_CONFIGURATION] = False
        self.config_description.update({
            PER_ACCOUNT_CONFIGURATION: 'Configure tasks separately for each account. When disabled, run the original multi-account daily task.',
            ACCOUNT_CONFIGURATION: 'Edit account order, enabled accounts, tasks and task settings.',
        })
        self.config_type.update({
            PER_ACCOUNT_CONFIGURATION: {'sub_configs': {True: [ACCOUNT_CONFIGURATION]}},
            ACCOUNT_CONFIGURATION: {
                'type': 'button', 'text': 'Configure Accounts', 'callback': self.configure_accounts,
            },
        })
        self.done_set = set()
        self.all_accounts = set()
        self.support_schedule_task = True

    def _mark_done(self, account):
        normalized = normalize_account_name(account)
        if normalized:
            self.done_set.add(normalized)

    def _is_done(self, account):
        return normalize_account_name(account) in self.done_set

    def _same_account(self, left, right):
        return normalize_account_name(left) == normalize_account_name(right)

    def run(self):
        if self.config.get(PER_ACCOUNT_CONFIGURATION, False):
            # Import lazily: the configured runner inherits the shared login helpers.
            from src.task.MultiAccountConfigTask import MultiAccountConfigTask
            return self.run_task_by_class(MultiAccountConfigTask)
        WWOneTimeTask.run(self)
        self.done_set.clear()
        self.all_accounts.clear()

        self.run_task_by_class(DailyTask)
        self.ensure_main(time_out=100)
        self._switch_to_login()
        detected = self._detect_current_account_from_login()
        self._mark_done(detected)

        self.info_set('Completed', self.done_set)

        while next_account := self._select_and_login_account():
            self.info_set('Completed', self.done_set)
            self.run_task_by_class(DailyTask)
            self._mark_done(next_account)
            self.ensure_main(time_out=100)
            self._switch_to_login()

    def configure_accounts(self):
        # Config button callbacks run on the Qt thread; game work stays in run().
        from PySide6.QtWidgets import QApplication
        from src.gui.MultiAccountConfigDialog import MultiAccountConfigDialog

        dialog = MultiAccountConfigDialog(QApplication.activeWindow(), self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _click_center_offset(self, offset_x, offset_y, after_sleep=0.5):
        h, w = self.frame.shape[:2]
        rel_x = 0.5 + offset_x / w
        rel_y = 0.5 + offset_y / h
        self.click_relative(rel_x, rel_y, after_sleep=after_sleep)

    def _switch_to_login(self):
        self.log_info(self.tr('Switching back to login screen'))
        while True:
            try:
                self.next_frame()
                if self._login_combo() or self.do_find_account_drop_down():
                    break
                # 退出点击可能已被接受；恢复后先检查确认框和设置页，不能再次按 Esc 关掉它们。
                if not self.find_one(['confirm_btn_hcenter_vcenter', 'confirm_btn_highlight_hcenter_vcenter']):
                    if not self.find_one('esc_setting'):
                        self.send_key('esc', after_sleep=1.5)
                        self.wait_feature('esc_setting')
                    self.click_relative(0.04, 0.96, after_sleep=1)
                self.click_confirm(timeout=10)
                # 非掩码账号可由原生 ComboBox 确认登录页，不依赖 OCR 的 **** 匹配。
                self.wait_until(lambda: self._login_combo() or self.do_find_account_drop_down(),
                                time_out=60, settle_time=2, raise_if_not_found=True)
                break
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
        self.log_info(self.tr('Back at login screen'))

    def _login_combo(self):
        return find_login_combo(self.hwnd.hwnd if self.hwnd else 0)

    def _select_account_by_combo(self, combo):
        accounts = combo_items(combo)
        for name in accounts:
            self.all_accounts.add(normalize_account_name(name))
        self.info_set('All Accounts', self.all_accounts)
        for index, name in enumerate(accounts):
            if self._is_done(name):
                continue
            if not select_combo_item(self, combo, index, name):
                raise WaitFailedException(self.tr('Failed to switch account'))
            self.log_info(self.tr('Confirmed selected account: {account}').format(account=name))
            return name
        return None

    def _detect_current_account_from_login(self):
        if combo := self._login_combo():
            if account := combo_selected_item(combo):
                self.log_info(self.tr('Current account: {account}').format(account=account))
                return account
        texts = self.ocr(match=account_pattern)
        if texts:
            self.log_info(self.tr('Current account: {account}').format(account=texts[0]))
            return texts[0].name
        return None

    def _click_account_in_list(self):
        accounts = self.ocr(match=account_pattern)
        next_account = None
        # self.screenshot('_click_account_in_list')
        for account in accounts:
            self.all_accounts.add(normalize_account_name(account.name))
            self.info_set('All Accounts', self.all_accounts)
            if next_account is None and not self._is_done(account.name):
                next_account = account.name
                self.click(account, after_sleep=2)
        self.log_info(self.tr('Click next account: {account}').format(account=next_account))
        return next_account

    def _select_next_login_account(self):
        attempt = 0
        while attempt < 5:
            try:
                self.next_frame()
                if combo := self._login_combo():
                    return self._select_account_by_combo(combo)
                # 每次从新画面打开列表；失焦中断不会消耗正常识别失败预算。
                self.sleep(1)
                drop_down = self.find_account_drop_down()
                if drop_down:
                    self.click(drop_down, after_sleep=2)
                if self.do_find_account_drop_down():
                    self.log_error(self.tr('click drop down no effect'))
                    self.screenshot('multi')
                else:
                    account = self.wait_until(self._click_account_in_list,
                                              time_out=10, raise_if_not_found=False)
                    if not account:
                        if self.all_accounts and all(self._is_done(name) for name in self.all_accounts):
                            return None
                        raise WaitFailedException()
                    self.sleep(1)
                    displayed = self._detect_current_account_from_login()
                    self.log_info(self.tr('Selected account: {selected}, displayed account: {displayed}').format(
                        selected=account, displayed=displayed))
                    if self._same_account(account, displayed):
                        self.log_info(self.tr('Confirmed selected account: {account}').format(account=account))
                        return account
                attempt += 1
            except ForegroundInputInterrupted:
                self.wait_foreground_frame()
        raise WaitFailedException(self.tr('Failed to switch account'))

    def _login_selected_account(self, account, account_matches):
        submitted = False

        def confirm_account(require_visible=True):
            displayed = self._detect_current_account_from_login()
            if (require_visible and not displayed) or (displayed and not account_matches(account, displayed)):
                stage = '恢复后' if submitted else '登录前'
                raise WaitFailedException(f'{stage}账号确认失败，已停止登录')

        missing = object()
        previous = getattr(self, '_login_account_guard', missing)
        # 保护贯穿 ensure_main 的内层恢复，不能只在外层等待前确认一次。
        self._login_account_guard = confirm_account
        try:
            while True:
                try:
                    if not submitted:
                        self.sleep(4)
                        self.next_frame()
                        login = self.find_boxes(self.ocr(), boundary=self.box_of_screen(.3, .3, .7, .8),
                                                match=LOGIN_TEXTS)
                        confirm_account()
                        # 鼠标按下后失焦时，登录可能已发生；先登记进入确认阶段，避免重放旧点击。
                        submitted = True
                        if login:
                            self.click(login, after_sleep=3)
                        else:
                            self.click_relative(.5, .568, hcenter=True, vcenter=True, after_sleep=3)
                    else:
                        self.next_frame()
                        confirm_account(require_visible=False)
                    self.logged_in = False
                    # 已提交或中断的点击只通过现有登录页／加载页／主界面识别继续，不重发旧坐标。
                    self.ensure_main(time_out=180)
                    return
                except ForegroundInputInterrupted:
                    self.wait_foreground_frame()
        finally:
            if previous is missing:
                self.__dict__.pop('_login_account_guard', None)
            else:
                self._login_account_guard = previous

    def _select_and_login_account(self):
        current_account = MultiAccountDailyTask._select_next_login_account(self)
        if not current_account:
            self.log_info(self.tr('No remaining account to run; finishing multi-account daily task'))
            return None
        MultiAccountDailyTask._login_selected_account(self, current_account, self._same_account)
        self.log_info(self.tr('Login successful'))
        return current_account

    def find_account_drop_down(self):
        return self.wait_until(self.do_find_account_drop_down, time_out=60, settle_time=2, raise_if_not_found=True)

    def do_find_account_drop_down(self) -> Box | None:
        texts = self.ocr()
        account_boxes = self.find_boxes(texts, account_pattern)
        login_boxes = self.find_boxes(texts, LOGIN_TEXTS)
        if len(account_boxes) == 1 and login_boxes:
            return account_boxes[0]
        return None


from ok import run_task
from config import config

if __name__ == "__main__":
    run_task(config, task=MultiAccountDailyTask, debug=True)
