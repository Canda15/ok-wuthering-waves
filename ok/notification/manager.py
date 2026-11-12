import threading
from concurrent.futures import CancelledError, TimeoutError

import win32gui

from ok.notification.pipeline import NotificationPipeline
from ok.notification.ppocr import NotificationPPOCR
from ok.notification.providers import (
    DiscordProvider, QQBotProvider, SmtpProvider, TelegramBotProvider,
    WeComWebhookProvider)
from ok.notification.windows_messenger import MessengerAutomation
from ok.task.exceptions import ExecutorBusyException, TaskDisabledException
from ok.util.GlobalConfig import (
    DISCORD_NOTIFICATION_ENABLED, DISCORD_WEBHOOK, NOTIFICATION_OPTION_NAME,
    QQ_NICKNAME, QQ_NOTIFICATION_ENABLED, SYSTEM_NOTIFICATION_ENABLED,
    WECHAT_NICKNAME, WECHAT_NOTIFICATION_ENABLED,
    QQ_BOT_APP_ID, QQ_BOT_CHANNEL_ID, QQ_BOT_NOTIFICATION_ENABLED,
    QQ_BOT_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TELEGRAM_NOTIFICATION_ENABLED, WECOM_NOTIFICATION_ENABLED, WECOM_WEBHOOK,
    SMTP_DEFAULT_RECIPIENT, SMTP_DEFAULT_SENDER, SMTP_HOST,
    SMTP_NOTIFICATION_ENABLED, SMTP_PASSWORD, SMTP_PORT, SMTP_USE_TLS,
    SMTP_USERNAME,
)
from ok.util.logger import Logger

logger = Logger.get_logger(__name__)
_DEFAULT_SYSTEM_NOTIFIER = object()


class NotificationManager:
    def __init__(self, global_config, executor, exit_event=None, app_name=None, app_icon=None,
                 system_notifier=_DEFAULT_SYSTEM_NOTIFIER):
        self.config = global_config.get_config(NOTIFICATION_OPTION_NAME)
        self.executor = executor
        self.exit_event = exit_event
        self.app_name = str(app_name or 'ok-script')
        self.app_icon = str(app_icon or '')
        if system_notifier is _DEFAULT_SYSTEM_NOTIFIER:
            from ok.notification.system import WindowsSystemNotifier
            system_notifier = WindowsSystemNotifier(self.app_name, self.app_icon)
        self.system_notifier = system_notifier
        self._desktop_jobs = set()
        self._desktop_job_lock = threading.Lock()
        self.ocr = NotificationPPOCR(executor.ocr_lib)
        # 桌面等待不能占用网络消费者；只增加一个固定、顺序消费的桌面队列。
        self.desktop_pipeline = NotificationPipeline(
            self._deliver_desktop, exit_event=exit_event, interval=5, worker_name='桌面通知')
        self.pipeline = NotificationPipeline(self._send, exit_event=exit_event, interval=5)
        self.queue = self.pipeline.queue
        self.thread = self.pipeline.thread

    @property
    def system_enabled(self):
        return bool(self.config.get(SYSTEM_NOTIFICATION_ENABLED, True))

    @property
    def external_provider_enabled(self):
        return any((
            self.config.get(DISCORD_NOTIFICATION_ENABLED),
            self.config.get(QQ_NOTIFICATION_ENABLED),
            self.config.get(WECHAT_NOTIFICATION_ENABLED),
            self.config.get(TELEGRAM_NOTIFICATION_ENABLED),
            self.config.get(WECOM_NOTIFICATION_ENABLED),
            self.config.get(QQ_BOT_NOTIFICATION_ENABLED),
            self.config.get(SMTP_NOTIFICATION_ENABLED),
        ))

    def submit(self, title, message, images=None):
        """仅返回请求是否入队；发送结果在提供者实际完成后处理。"""
        if not self.external_provider_enabled:
            return False
        if images is None:
            frames = []
        elif isinstance(images, (list, tuple)):
            frames = list(images)
        else:
            frames = [images]
        return self.pipeline.submit(title or '', message, frames)

    def notify_system(self, title, message, error=False, tray=True):
        if not tray or not self.system_enabled or self.system_notifier is None:
            return False
        return self.system_notifier.show(title, message, error)

    def stop(self):
        pipelines = (self.pipeline, self.desktop_pipeline)
        for pipeline in pipelines:
            pipeline.stop(wait=False)
        with self._desktop_job_lock:
            jobs = tuple(self._desktop_jobs)
        for job in jobs:
            job.cancel()
        # 执行器不得等通知线程；后者可能正在等当前桌面作业收口。
        stopped = [pipeline.stop(wait=not self.executor.is_executor_thread())
                   for pipeline in pipelines]
        if self.system_notifier is not None:
            self.system_notifier.close()
        return all(stopped)

    def _send(self, title, message, images):
        if self.pipeline.stop_event.is_set():
            return
        if self.config.get(DISCORD_NOTIFICATION_ENABLED):
            self._safe_send('Discord', DiscordProvider().send,
                            self.config.get(DISCORD_WEBHOOK), title, message, images,
                            self.app_name, self.app_icon)
        if self.config.get(TELEGRAM_NOTIFICATION_ENABLED):
            self._safe_send('Telegram', TelegramBotProvider().send,
                            self.config.get(TELEGRAM_BOT_TOKEN),
                            self.config.get(TELEGRAM_CHAT_ID), title, message, images)
        if self.config.get(WECOM_NOTIFICATION_ENABLED):
            self._safe_send('WeCom', WeComWebhookProvider().send,
                            self.config.get(WECOM_WEBHOOK), title, message, images)
        if self.config.get(QQ_BOT_NOTIFICATION_ENABLED):
            self._safe_send('QQ Bot', QQBotProvider().send,
                            self.config.get(QQ_BOT_APP_ID), self.config.get(QQ_BOT_TOKEN),
                            self.config.get(QQ_BOT_CHANNEL_ID), title, message, images)
        if self.config.get(SMTP_NOTIFICATION_ENABLED):
            self._safe_send('SMTP', SmtpProvider(
                self.config.get(SMTP_HOST), self.config.get(SMTP_PORT),
                self.config.get(SMTP_USERNAME), self.config.get(SMTP_PASSWORD),
                self.config.get(SMTP_USE_TLS), self.config.get(SMTP_DEFAULT_SENDER),
                self.config.get(SMTP_DEFAULT_RECIPIENT)).send,
                            None, title, message, images)
        if self.pipeline.stop_event.is_set():
            return
        if (self.config.get(QQ_NOTIFICATION_ENABLED)
                or self.config.get(WECHAT_NOTIFICATION_ENABLED)):
            # 此处只接受排队，不等待前台，也不把接受请求当成发送成功。
            if not self.desktop_pipeline.submit(title, message, images):
                logger.warning('桌面通知队列已停止，本条未入队')

    def _desktop_stopped(self):
        return (self.pipeline.stop_event.is_set() or self.desktop_pipeline.stop_event.is_set()
                or (self.exit_event is not None and self.exit_event.is_set()))

    def _desktop_cancelled(self):
        if self._desktop_stopped():
            return True
        # 持有观察器也从独立线程调用，不能漏掉当前作业的取消请求。
        self.executor.check_input_cancelled(allow_pause=True)
        return False

    def _desktop_job_finished(self, job):
        with self._desktop_job_lock:
            self._desktop_jobs.discard(job)
        # 完成回调只记录终态；排队接受与真实执行结果分开，不重复发送。
        if job.future.cancelled():
            logger.info('桌面通知作业已取消')
            return
        error = job.future.exception()
        if job.cancel_requested or isinstance(error, TaskDisabledException):
            logger.info('桌面通知作业已取消')
        elif error is not None:
            logger.error('桌面通知执行失败，本条不自动重发', error)
        elif not job.future.result():
            logger.warning('桌面通知发送未完成，本条不自动重发')
        else:
            logger.debug('桌面通知发送流程完成')

    def _submit_desktop(self, message, images):
        while True:
            try:
                with self._desktop_job_lock:
                    if self._desktop_stopped():
                        return None
                    job = self.executor.submit(
                        lambda: self._send_desktop(message, images),
                        name='桌面通知', owner=self, defer_while_unfocused=True)
                    self._desktop_jobs.add(job)
                break
            except ExecutorBusyException:
                # 独占期间通知尚未入队，线程可等待解除，不重试已执行的发送。
                if self.executor.is_executor_thread():
                    raise
                if self.desktop_pipeline.stop_event.wait(.1):
                    return None
        job.future.add_done_callback(lambda _: self._desktop_job_finished(job))
        return job

    def _deliver_desktop(self, title, message, images):
        """桌面消费者等待实际终态，网络线程只向它入队。"""
        if self.executor.is_executor_thread():
            logger.error('桌面顺序消费不能在执行器线程等待自身作业')
            return False
        job = None
        try:
            job = self._submit_desktop(self._messenger_message(title, message), images)
            if job is None:
                return False
            while True:
                # 排队时不继承其它游戏作业的取消；输入观察器仍检查自己的作业。
                if self._desktop_stopped():
                    job.cancel()
                    return False
                try:
                    return job.future.result(timeout=.1)
                except TimeoutError:
                    # 已完成作业自身也可能抛超时，不能把失败误当成仍在排队。
                    if job.future.done():
                        return job.future.result()
        except CancelledError:
            return False
        except BaseException as error:
            if job is not None:
                job.cancel()
            else:
                logger.error('桌面通知入队失败，本条不自动重发', error)
            return False

    def _send_desktop(self, message, images):
        if self._desktop_cancelled():
            return False
        device_manager = self.executor.device_manager
        game_window = getattr(device_manager, 'hwnd_window', None)
        game_hwnd = getattr(game_window, 'hwnd', None)
        game_interaction = device_manager.interaction
        before = win32gui.GetForegroundWindow()
        restore_game = MessengerAutomation.window_belongs_to(game_hwnd, before)
        if game_hwnd and not restore_game:
            # 入队筛选后仍需防止用户恰好在实际交接前切走。
            logger.info('用户已切走，桌面通知本次不取得前台')
            return False
        last_switch = None
        sent = False
        providers = (
            (QQ_NOTIFICATION_ENABLED, QQ_NICKNAME, ('QQ.exe',), {
                'window_titles': ('QQ',), 'search_point_96dpi': (200, 65),
                'left_panel_width_96dpi': 377, 'paste_match_end': True,
                'dismiss_search_after_contact': True,
            }),
            (WECHAT_NOTIFICATION_ENABLED, WECHAT_NICKNAME, ('WeChat.exe', 'Weixin.exe'), {
                'window_titles': ('WeChat', 'Weixin', '微信'),
                'search_point_96dpi': (163, 56), 'left_panel_width_96dpi': 295,
                'search_first_word': True,
            }),
        )
        try:
            for enabled, nickname, processes, options in providers:
                if self._desktop_cancelled():
                    break
                if not self.config.get(enabled):
                    continue
                automation = MessengerAutomation(
                    processes, self.ocr, exit_event=self.desktop_pipeline.stop_event,
                    cancel_check=self._desktop_cancelled, switch_from_hwnd=before,
                    image_method='context_menu', **options)
                result = self._safe_send(
                    'QQ' if enabled == QQ_NOTIFICATION_ENABLED else '微信',
                    automation.send, self.config.get(nickname), '', message, images)
                sent = bool(result) or sent
                if automation.switched_foreground:
                    last_switch = automation
                if automation._input_target is not None and not automation.restore_allowed:
                    last_switch = None
                    break
                before = win32gui.GetForegroundWindow()
        finally:
            if restore_game and last_switch is not None:
                self._restore_game(game_window, game_hwnd, game_interaction, last_switch)
        return sent

    def _restore_game(self, game_window, game_hwnd, game_interaction, automation):
        def cancelled():
            device_manager = self.executor.device_manager
            if (self._desktop_cancelled() or device_manager.hwnd_window is not game_window
                    or game_window.hwnd != game_hwnd
                    or device_manager.interaction is not game_interaction):
                return True
            current = win32gui.GetForegroundWindow()
            return not (automation.owns_foreground()
                        or MessengerAutomation.window_belongs_to(game_hwnd, current))

        try:
            if not automation.restore_allowed or not automation.owns_foreground() or cancelled():
                return
            if self.executor.prepare_foreground(
                    request_focus=True, time_out=5, cancel_check=cancelled):
                # 输入会话已经退出；统一准备更新代次后才能取得游戏的新帧。
                self.executor.next_frame(time_out=5)
        except Exception as e:
            logger.error('桌面通知后恢复游戏前台失败，等待用户返回', e)

    def _messenger_message(self, title, message):
        content = f'{title}\n{message}' if title else message
        return f'{self.app_name}:\n{content}'

    @staticmethod
    def _safe_send(name, sender, *args):
        try:
            return sender(*args)
        except Exception as e:
            logger.error(f'{name}通知失败，本条不自动重发', e)
            return False
