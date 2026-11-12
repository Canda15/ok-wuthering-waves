import io
import time
import ctypes

import cv2
import numpy as np
import psutil
import win32clipboard
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image
from pynput.keyboard import KeyCode

from ok.device.interaction_methods.foreground_input import (
    ForegroundInputError, ForegroundInputInterrupted, ForegroundTarget,
    get_shared_foreground_input,
)
from ok.util.logger import Logger

logger = Logger.get_logger(__name__)


class MessengerAutomation:
    """在共享输入会话内识别并操作本地通知窗口。"""

    def __init__(self, process_names, ocr, exit_event=None, window_titles=None,
                 search_point_96dpi=(180, 60), left_panel_width_96dpi=340,
                 search_first_word=False, cancel_check=None, switch_from_hwnd=None,
                 image_method='clipboard_hotkey', paste_match_end=False,
                 dismiss_search_after_contact=False):
        self.process_names = {name.lower() for name in process_names}
        self.window_titles = {title.strip().casefold() for title in (window_titles or ())}
        self.search_point_96dpi = search_point_96dpi
        self.left_panel_width_96dpi = left_panel_width_96dpi
        self.search_first_word = search_first_word
        self.cancel_check = cancel_check
        self.switch_from_hwnd = switch_from_hwnd
        self.image_method = image_method
        self.paste_match_end = paste_match_end
        self.dismiss_search_after_contact = dismiss_search_after_contact
        self.ocr_engine = ocr
        self.exit_event = exit_event
        self._last_frame = None
        self._input = None
        self._input_target = None
        self._lost_focus = False
        self.restore_allowed = False
        self.switched_foreground = False

    def send(self, nickname, title, message, images):
        self._last_frame = None
        self._lost_focus = False
        self.restore_allowed = False
        self.switched_foreground = False
        self._input_target = None
        nickname = (nickname or '').strip()
        if not nickname:
            logger.warning('桌面通知已启用，但尚未填写联系人昵称')
            return False
        if self._should_stop():
            return False
        hwnd = self._find_hwnd()
        if not hwnd:
            logger.warning('未找到已打开的桌面通知客户端窗口')
            return False
        _, process_id = win32process.GetWindowThreadProcessId(hwnd)
        self._input_target = ForegroundTarget(
            hwnd, generation=time.monotonic_ns(), process_id=process_id)
        self._input = get_shared_foreground_input().session(
            self, self._input_target, cancel_check=self._should_stop)
        try:
            # 先交接所有权，置前失败时仍由会话退出清理旧手势和本次持有。
            with self._input:
                try:
                    self._request_foreground(hwnd)
                    return self._send(hwnd, nickname, title, message, images)
                finally:
                    # 释放失败时保留错误和台账，不能带着遗留修饰键恢复游戏。
                    self._input.release_all()
                    self._update_restore_allowed()
        except Exception:
            self._save_error_screenshot()
            raise
        finally:
            self._input = None

    def _send(self, hwnd, nickname, title, message, images):
        search_region, contact_region, send_region = self._layout_regions(hwnd)

        # Fast path 1: the contact is already visible in the left chat list.
        # This avoids opening QQ's search overlay for the common case.
        left_contact = self._wait_text(
            hwnd, {nickname}, region=contact_region, full_match=True, timeout=.6)
        if left_contact is not None:
            logger.debug(f'Using left-list contact shortcut at {left_contact}')
            self._click(hwnd, left_contact)
        else:
            # Fast path 2: confirm both the conversation header and Send
            # button before focusing the composer directly. The two checks
            # prevent a stale header or unrelated button from stealing focus.
            send_box = self._wait_text(
                hwnd, {'Send', '发送'}, region=send_region, timeout=.6)
            header_target = self._wait_text(
                hwnd, {nickname}, region=self._header_region(hwnd),
                full_match=True, timeout=.6)
            if send_box is not None and header_target is not None:
                logger.debug(
                    f'Using header/composer shortcut header={header_target} '
                    f'send={send_box}')
                input_point = (
                    max(20, send_box[0] - 80), max(20, send_box[1] - 45))
                input_target = self._click(hwnd, input_point)
                return self._send_content(
                    hwnd, send_box, input_point, title, message, images,
                    focus_input=False, input_target=input_target)

            search = self._wait_text(
                hwnd, {'Search', '搜索'}, region=search_region, timeout=1.5)
            if search is None:
                search = self._search_field_point(hwnd)
            search_target = self._click(hwnd, search)
            self._wait(1)
            self._clear_text(search_target)
            self._wait(1)
            self._type_text(search_target, self._search_query(nickname))
            self._wait(1)

            contact_hwnd, contact = self._wait_contact(
                hwnd, nickname, contact_region, timeout=8)
            if contact is None:
                raise RuntimeError('无法确认桌面通知联系人')
            self._click(contact_hwnd, contact)
            if self.dismiss_search_after_contact:
                # QQ can leave its recent/comprehensive-search overlay open even
                # after a result click. Only dismiss it while the clicked popup is
                # still visible; an unconditional Escape can close/minimize the
                # main QQ window when the result click already closed the overlay.
                self._wait(1)
                try:
                    overlay_open = (
                        contact_hwnd != hwnd
                        and win32gui.IsWindowVisible(contact_hwnd)
                    )
                except Exception:
                    overlay_open = False
                if overlay_open:
                    self._send_key(hwnd, win32con.VK_ESCAPE)
                    self._wait(1)
            else:
                self._wait(1)

        send_box = self._wait_text(
            hwnd, {'Send', '发送'}, region=send_region, timeout=8)
        if send_box is None:
            raise RuntimeError('无法在桌面通知窗口中确认发送按钮')
        input_point = (max(20, send_box[0] - 80), max(20, send_box[1] - 45))
        return self._send_content(hwnd, send_box, input_point, title, message, images)

    def _send_content(self, hwnd, send_box, input_point, title, message, images,
                      focus_input=True, input_target=None):
        if self._should_stop():
            return False
        if focus_input:
            input_target = self._click(hwnd, input_point)
        elif input_target is None:
            input_target, _ = self._target(hwnd, input_point)
        self._wait(1)
        self._clear_unsent_text(input_target)
        self._wait(1)

        text = f'{title}\n{message}' if title else message
        if text:
            self._type_text(input_target, text)
            self._wait(1)
            self._click(hwnd, send_box)
            self._wait(1)

        uses_clipboard = bool(images)
        clipboard_text = self._get_clipboard_text() if uses_clipboard else None
        try:
            for image in images or []:
                if self._should_stop():
                    return False
                from ok.notification.messenger_images import insert_messenger_image
                input_target = self._click(hwnd, input_point)
                self._wait(1)
                insert_messenger_image(
                    self, self.image_method, hwnd, input_target, input_point, image)
                self._click(hwnd, send_box)
                self._wait(1)
        finally:
            if uses_clipboard:
                self._restore_clipboard_text(clipboard_text)
        return True

    def _search_query(self, nickname):
        if self.search_first_word:
            return nickname.split(maxsplit=1)[0]
        return nickname

    def _wait_contact(self, hwnd, nickname, root_region, timeout=8):
        """Find a contact in the root list or a separate Qt search popup."""
        deadline = time.monotonic() + timeout
        wanted = nickname.strip().casefold()
        while time.monotonic() < deadline:
            self._check()
            for candidate in self._contact_windows(hwnd):
                frame = self._capture(candidate)
                self._last_frame = frame
                region = root_region if candidate == hwnd else None
                boxes = self._ocr(frame, region=region)
                point = self._matching_contact_point(
                    boxes, wanted, contacts_section=candidate != hwnd)
                if point is not None:
                    return candidate, point
            self._wait(.3)
        return hwnd, None

    @staticmethod
    def _matching_contact_point(boxes, wanted, contacts_section=False):
        if not contacts_section:
            for text, x, y, width, height in boxes:
                if text.strip().casefold() == wanted:
                    return x + width // 2, y + height // 2
            return None

        # WeChat's popup also contains chat history and internet suggestions.
        # Only an exact result under Contacts or built-in Features is safe to
        # click (File Transfer is exposed as a Feature in recent WeChat).
        contact_headers = {'contacts', 'contact', '联系人', '聯絡人'}
        feature_headers = {'features', 'feature', '功能'}
        allowed_headers = contact_headers | feature_headers
        section_headers = allowed_headers | {
            'chat history', '聊天记录', '聊天記錄', 'group chats', '群聊',
            'more', '更多', 'internet search results', '网络搜索结果',
            '網路搜尋結果', 'official accounts', '公众号', '公眾號',
        }
        normalized = [
            (text.strip().casefold(), x, y, width, height)
            for text, x, y, width, height in boxes
        ]
        allowed_sections = [
            (y + height, min(
                (next_y for next_text, _, next_y, _, _ in normalized
                 if next_text in section_headers and next_y >= y + height),
                default=float('inf')))
            for text, _, y, _, height in normalized
            if text in allowed_headers
        ]
        for start, stop in allowed_sections:
            for text, x, y, width, height in normalized:
                center_y = y + height // 2
                if text == wanted and start <= center_y < stop:
                    return x + width // 2, center_y
        return None

    @staticmethod
    def _contact_windows(hwnd):
        """Return the main window plus transient Qt search-result popups."""
        _, root_pid = win32process.GetWindowThreadProcessId(hwnd)
        popups = []

        def callback(candidate, _):
            if candidate == hwnd or not win32gui.IsWindowVisible(candidate):
                return True
            _, pid = win32process.GetWindowThreadProcessId(candidate)
            class_name = win32gui.GetClassName(candidate)
            if (pid == root_pid and 'QWindowToolSaveBits' in class_name
                    and MessengerAutomation.window_belongs_to(hwnd, candidate)):
                left, top, right, bottom = win32gui.GetWindowRect(candidate)
                area = max(0, right - left) * max(0, bottom - top)
                if area:
                    popups.append((area, candidate))
            return True

        win32gui.EnumWindows(callback, None)
        return [candidate for _, candidate in sorted(popups, reverse=True)] + [hwnd]

    def _find_hwnd(self):
        pids = set()
        for process in psutil.process_iter(['pid', 'name']):
            try:
                if (process.info['name'] or '').lower() in self.process_names:
                    pids.add(process.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        candidates = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetParent(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in pids:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(0, right - left) * max(0, bottom - top)
                title = win32gui.GetWindowText(hwnd).strip().casefold()
                title_match = int(title in self.window_titles)
                candidates.append((title_match, area, hwnd))
            return True

        win32gui.EnumWindows(callback, None)
        return max(candidates, default=(0, 0, 0))[2]

    @staticmethod
    def window_belongs_to(hwnd, candidate):
        """同进程的根、子窗或拥有窗口才属于本次通知目标。"""
        if not hwnd or not candidate:
            return False
        try:
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindow(candidate):
                return False
            if hwnd == candidate:
                return True
            if (win32process.GetWindowThreadProcessId(hwnd)[1]
                    != win32process.GetWindowThreadProcessId(candidate)[1]):
                return False
            return (win32gui.IsChild(hwnd, candidate)
                    or win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
                    == win32gui.GetAncestor(candidate, win32con.GA_ROOT)
                    or win32gui.GetAncestor(hwnd, win32con.GA_ROOTOWNER)
                    == win32gui.GetAncestor(candidate, win32con.GA_ROOTOWNER))
        except Exception:
            return False

    def owns_foreground(self):
        target = self._input_target
        try:
            return (target is not None
                    and win32process.GetWindowThreadProcessId(target.hwnd)[1] == target.process_id
                    and self.window_belongs_to(target.hwnd, win32gui.GetForegroundWindow()))
        except Exception:
            return False

    def _request_foreground(self, hwnd, timeout=5):
        if (not win32gui.IsWindow(hwnd)
                or win32process.GetWindowThreadProcessId(hwnd)[1] != self._input_target.process_id):
            # 取得会话前窗口可能已销毁或句柄已复用，不得置前其它应用。
            raise ForegroundInputInterrupted('桌面通知窗口身份已变更')
        before = win32gui.GetForegroundWindow()
        if self.window_belongs_to(hwnd, before):
            self._check()
            return
        if (self.switch_from_hwnd is not None
                and self.switch_from_hwnd != before
                and not self.window_belongs_to(self.switch_from_hwnd, before)):
            self._lost_focus = True
            raise ForegroundInputInterrupted('通知交接前用户已切走，不请求前台')
        if self._should_stop():
            raise ForegroundInputError('桌面通知已取消')
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            # Windows 可能拒绝置前；只请求一次，随后核对真实前台。
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            logger.warning('系统尚未允许桌面通知窗口取得前台')
        deadline = time.monotonic() + timeout
        while True:
            if self._should_stop():
                raise ForegroundInputError('桌面通知已取消')
            current = win32gui.GetForegroundWindow()
            if self.window_belongs_to(hwnd, current):
                self.switched_foreground = True
                self._check()
                return
            if current != before and not self.window_belongs_to(before, current):
                self._lost_focus = True
                raise ForegroundInputInterrupted('等待通知前台时用户已切走')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ForegroundInputError('无法取得桌面通知窗口的真实前台')
            self._wait_cancel(min(.05, remaining))

    def _update_restore_allowed(self):
        try:
            # 也检查核心观察到的中断，用户切走后再回来不能触发自动恢复。
            self._check()
            self.restore_allowed = not self._lost_focus and self.owns_foreground()
        except Exception:
            self.restore_allowed = False

    def _check(self):
        if self._should_stop():
            raise ForegroundInputError('桌面通知已取消')
        if self._input is None:
            raise ForegroundInputError('桌面通知尚未取得共享输入会话')
        try:
            self._input.check()
        except ForegroundInputInterrupted:
            self._lost_focus = True
            raise

    def _wait_cancel(self, seconds):
        if self.exit_event is not None:
            self.exit_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self._check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._wait_cancel(min(.05, remaining))

    def _should_stop(self):
        return ((self.exit_event is not None and self.exit_event.is_set())
                or (self.cancel_check is not None and bool(self.cancel_check())))

    def _wait_text(self, hwnd, names, region=None, full_match=False, timeout=5):
        deadline = time.monotonic() + timeout
        wanted = {name.strip().casefold() for name in names}
        while time.monotonic() < deadline:
            self._check()
            frame = self._capture(hwnd)
            self._last_frame = frame
            boxes = self._ocr(frame, region=region)
            height, width = frame.shape[:2]
            for text, x, y, box_width, box_height in boxes:
                normalized = text.strip().casefold()
                matched = normalized in wanted if full_match else any(name in normalized for name in wanted)
                center = (x + box_width // 2, y + box_height // 2)
                if matched and self._in_region(center, width, height, region):
                    return center
            self._wait(.3)
        return None

    def _ocr(self, frame, region=None):
        x_offset = y_offset = 0
        ocr_frame = frame
        if region is not None:
            height, width = frame.shape[:2]
            x_offset = max(0, min(width, round(region[0])))
            y_offset = max(0, min(height, round(region[1])))
            to_x = max(x_offset, min(width, round(region[2])))
            to_y = max(y_offset, min(height, round(region[3])))
            ocr_frame = frame[y_offset:to_y, x_offset:to_x]
        if self.ocr_engine is None:
            raise RuntimeError('QQ／微信桌面通知需要 PP-OCR')
        boxes = self.ocr_engine.recognize(ocr_frame, threshold=.1)
        return [
            (text, x + x_offset, y + y_offset, width, height)
            for text, x, y, width, height in boxes
        ]

    def _save_error_screenshot(self, reason=None):
        if self._last_frame is None:
            return
        try:
            from ok.core.events import communicate
            provider = next(iter(sorted(self.process_names)), 'messenger').removesuffix('.exe')
            suffix = f'{provider}_error' if not reason else f'{provider}_{reason}'
            communicate.screenshot.emit(
                self._last_frame.copy(), f'notification/{suffix}', False, None)
        except Exception as e:
            logger.error('保存桌面通知错误截图失败', e)

    def _search_field_point(self, hwnd):
        frame = self._capture(hwnd)
        self._last_frame = frame
        height, width = frame.shape[:2]
        try:
            scale = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
        except Exception:
            scale = 1.0
        x = round(self.search_point_96dpi[0] * scale)
        y = round(self.search_point_96dpi[1] * scale)
        return max(0, min(width - 1, x)), max(0, min(height - 1, y))

    def _layout_regions(self, hwnd):
        _, _, width, height = win32gui.GetClientRect(hwnd)
        try:
            scale = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
        except Exception:
            scale = 1.0
        panel_right = min(width, round(self.left_panel_width_96dpi * scale))
        nav_left = min(panel_right, round(55 * scale))
        search_bottom = min(height, round(120 * scale))
        contact_top = min(height, round(75 * scale))
        send_left = max(panel_right, width - round(260 * scale))
        send_top = max(0, height - round(150 * scale))
        return (
            (nav_left, 0, panel_right, search_bottom),
            (nav_left, contact_top, panel_right, height),
            (send_left, send_top, width, height),
        )

    def _header_region(self, hwnd):
        """Return the conversation header area to the right of the nav panel."""
        _, _, width, height = win32gui.GetClientRect(hwnd)
        try:
            scale = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
        except Exception:
            scale = 1.0
        panel_right = min(width, round(self.left_panel_width_96dpi * scale))
        return panel_right, 0, width, min(height, round(120 * scale))

    @staticmethod
    def _in_region(point, width, height, region):
        if region is None:
            return True
        x, y = point
        return region[0] <= x <= region[2] and region[1] <= y <= region[3]

    @staticmethod
    def _capture(hwnd):
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise RuntimeError('桌面通知窗口已最小化或没有有效客户区')
        window_dc = win32gui.GetDC(hwnd)
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            memory_dc.SelectObject(bitmap)
            # 保留完整客户区截图，识别结果在实际输入前按该 HWND 转成屏幕坐标。
            rendered = ctypes.windll.user32.PrintWindow(
                hwnd, memory_dc.GetSafeHdc(), 0x00000003)
            if not rendered:
                raise RuntimeError('桌面通知窗口完整截图失败')
            return MessengerAutomation._bitmap_frame(bitmap, width, height)
        finally:
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, window_dc)
            win32gui.DeleteObject(bitmap.GetHandle())

    @staticmethod
    def _bitmap_frame(bitmap, width, height):
        pixels = bitmap.GetBitmapBits(True)
        frame = np.frombuffer(pixels, dtype=np.uint8).reshape((height, width, 4))
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    def _target(self, hwnd, point):
        if self._input_target is None or not self.window_belongs_to(self._input_target.hwnd, hwnd):
            raise ForegroundInputInterrupted('通知控件不属于当前输入目标')
        # 菜单和渲染子窗的原点可能不同，必须使用实际截图 HWND 转换。
        return hwnd, win32gui.ClientToScreen(hwnd, (int(point[0]), int(point[1])))

    def _click(self, hwnd, point, key='left'):
        self._check()
        target, screen = self._target(hwnd, point)
        self._input.click(*screen, key=key)
        return target

    def _type_text(self, hwnd, text):
        self._target(hwnd, (0, 0))
        self._check()
        self._input.input_text(str(text))

    def _send_key(self, hwnd, key):
        self._target(hwnd, (0, 0))
        self._check()
        self._input.send_key(KeyCode.from_vk(key) if isinstance(key, int) else key)

    def _hotkey(self, hwnd, modifier, key):
        self._target(hwnd, (0, 0))
        self._check()
        keys = tuple(KeyCode.from_vk(value) if isinstance(value, int) else value
                     for value in (modifier, key))
        self._input.hotkey(*keys)

    def _clear_text(self, hwnd):
        self._hotkey(hwnd, win32con.VK_CONTROL, ord('A'))
        self._send_key(hwnd, win32con.VK_BACK)

    def _clear_unsent_text(self, hwnd):
        # 真实组合键可可靠选中已有内容，不再排队数百次删键干扰新文本。
        self._clear_text(hwnd)

    @staticmethod
    def _set_clipboard_image(frame):
        image = frame
        if len(image.shape) == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        else:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(image)
        bmp_output = io.BytesIO()
        png_output = io.BytesIO()
        pil_image.save(bmp_output, 'BMP')
        pil_image.save(png_output, 'PNG')
        dib = bmp_output.getvalue()[14:]
        png = png_output.getvalue()
        bmp_output.close()
        png_output.close()
        png_format = win32clipboard.RegisterClipboardFormat('PNG')
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
            win32clipboard.SetClipboardData(png_format, png)
        finally:
            win32clipboard.CloseClipboard()

    @staticmethod
    def _get_clipboard_text():
        try:
            win32clipboard.OpenClipboard()
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception:
            return None
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
        return None

    @staticmethod
    def _restore_clipboard_text(text):
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            if text is not None:
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
