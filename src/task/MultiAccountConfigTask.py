from ok.task.exceptions import TaskDisabledException, WaitFailedException
from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted

from src.task.BaseWWTask import LOGIN_TEXTS
from src.task.MultiAccountDailyTask import (
    MultiAccountDailyTask, combo_items, combo_selected_item, select_combo_item,
)
from src.task.WWOneTimeTask import WWOneTimeTask
from src.task.account.MultiAccountConfigStore import (
    MultiAccountConfigStore, account_key_from_ocr, display_account,
    resolve_task, validate_accounts,
)
from src.task.account.RuntimeTaskConfig import account_task_config, isolated_task_configs
from src.task.task_icons import task_icon


class MultiAccountConfigTask(MultiAccountDailyTask):
    """账号及任务设置只从本地配置读取，不能在代码中嵌入账号数据。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = 'Multi Account Config Task'
        self.group_name = 'Multi-account'
        self.group_icon = task_icon('multi-account')
        self.description = 'Log in to accounts in card order and run tasks with independent configurations'
        self.default_config = {}
        self.config_description = {}
        self.config_type = {}
        self.visible = False

    def run(self):
        store = MultiAccountConfigStore(self.executor.config_folder)
        accounts = store.load()
        validate_accounts(accounts, self.executor)
        if not accounts:
            raise ValueError(self.tr('Add accounts and save the configuration first.'))
        # Resolve the entire plan before sending any game input.
        plan = [(account, resolve_task(self.executor, account['task'])) for account in accounts
                if account.get('enabled', True)]
        self.info_set('全部配置账号', [display_account(a['account_key']) for a in accounts])
        if not plan:
            self.info_set('状态', '没有启用的账号')
            return

        def record_result(account, status, message=''):
            try:
                store.record_result(account, status, message)
            except (OSError, ValueError) as error:
                self.log_warning(self.tr('Failed to save account run history: {error}').format(error=error))
        completed = []
        self.info_set('状态', '执行中')
        self.info_set('已完成', completed.copy())
        try:
            # Reset for every account, including configs of indirectly called tasks.
            for index, (account, task) in enumerate(plan):
                try:
                    self._target_account_key = account['account_key']
                    with isolated_task_configs(self.executor.get_all_tasks()):
                        if index == 0:
                            WWOneTimeTask.run(self)
                            # Another task or a previous run may have logged out.
                            self.logged_in = False
                            self.ensure_main(time_out=100)
                            self._switch_to_login()
                        self.info_set('当前账号', display_account(account['account_key']))
                        self.info_set('执行任务', self.tr(task.name) if task else '无')
                        self._login_target_account(account['account_key'])
                        if task is None:
                            self.log_info(self.tr('Login successful; task is set to None, skipping.'))
                        else:
                            with account_task_config(task, account['config']):
                                self.run_task_by_class(type(task))
                        self.ensure_main(time_out=100)
                        self._switch_to_login()
                except TaskDisabledException:
                    record_result(account, 'cancelled', '任务已停止')
                    raise
                except Exception as error:
                    record_result(account, 'failed', str(error))
                    raise
                record_result(account, 'success')
                completed.append(display_account(account['account_key']))
                self.info_set('已完成', completed.copy())
            self.info_set('状态', '全部完成')
        except TaskDisabledException:
            self.info_set('状态', '已停止')
            raise
        except Exception as error:
            self.info_set('状态', '执行失败')
            self.info_set('Error', str(error))
            raise

    def _find_target_account(self, account_key):
        matches = [box for box in self.ocr()
                   if account_key_from_ocr(box.name) == account_key]
        if matches:
            # The dropdown can repeat the currently displayed account above the
            # selectable row. Prefer the lowest matching OCR box, regardless of OCR order.
            self.click(max(matches, key=lambda box: box.y + box.height / 2), after_sleep=2)
            return True
        return False

    def do_find_account_drop_down(self):
        texts = self.ocr()
        target = getattr(self, '_target_account_key', None)
        accounts = [box for box in texts if '*' in box.name or '@' in box.name
                    or (target and account_key_from_ocr(box.name) == target)]
        if len(accounts) == 1 and self.find_boxes(texts, LOGIN_TEXTS):
            return accounts[0]
        return None

    def _detect_current_account_from_login(self):
        if not getattr(self, '_ocr_account_selection', False):
            if combo := self._login_combo():
                return combo_selected_item(combo)
        box = self.do_find_account_drop_down()
        return box.name if box else None

    def _select_target_account_once(self, account_key):
        self.sleep(1)
        self.next_frame()
        try:
            combo = self._login_combo()
        except (TaskDisabledException, ForegroundInputError, ForegroundInputInterrupted):
            raise
        except Exception as error:
            # 只在只读发现原生控件失败时改用 OCR；真实输入失败必须上抛，不能重放选号。
            self.log_warning(f'无法查询原生账号列表，改用画面识别：{type(error).__name__}')
            combo = None
        if combo:
            items = combo_items(combo)
            matches = [index for index, text in enumerate(items)
                       if account_key_from_ocr(text) == account_key]
            if not matches:
                raise WaitFailedException(self.tr('Account {account} not found in the login list').format(account=display_account(account_key)))
            # 与 OCR 路径一致，重复标签优先选择靠下的列表项。
            index = matches[-1]
            if not select_combo_item(self, combo, index, items[index]):
                raise WaitFailedException(self.tr('ComboBox account selection had no effect.'))
            self.sleep(1)
            if account_key_from_ocr(combo_selected_item(combo)) != account_key:
                raise WaitFailedException(self.tr('Current ComboBox account does not match the target.'))
            return True
        previous_ocr_mode = getattr(self, '_ocr_account_selection', False)
        try:
            self._ocr_account_selection = True
            self.sleep(1)
            self.click(self.find_account_drop_down(), after_sleep=2)
            if self.do_find_account_drop_down():
                return False
            if not self.wait_until(lambda: self._find_target_account(account_key),
                                   time_out=10, raise_if_not_found=False):
                raise WaitFailedException(self.tr('Account {account} not found in the login list').format(account=display_account(account_key)))
            self.sleep(1)
            return account_key_from_ocr(self._detect_current_account_from_login()) == account_key
        finally:
            self._ocr_account_selection = previous_ocr_mode

    def _select_target_account(self, account_key):
        self._target_account_key = account_key
        attempt = 0
        while attempt < 5:
            try:
                if MultiAccountConfigTask._select_target_account_once(self, account_key):
                    return
                attempt += 1
                self.log_info(self.tr('Account confirmation mismatch; retrying {attempt}/5').format(attempt=attempt))
            except ForegroundInputInterrupted:
                # 输入中断先等新帧，再重新查询目标账号；不能续接旧列表上的按键步骤。
                self.wait_foreground_frame()
        raise WaitFailedException(self.tr('Cannot confirm target account {account}; stopping login.').format(account=display_account(account_key)))

    def _login_target_account(self, account_key):
        MultiAccountConfigTask._select_target_account(self, account_key)
        MultiAccountDailyTask._login_selected_account(
            self, account_key, lambda expected, displayed: account_key_from_ocr(displayed) == expected)
        self.log_info(self.tr('{account} logged in successfully').format(account=display_account(account_key)))
