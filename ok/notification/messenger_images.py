import time

import win32con
import win32gui
import win32process


def insert_messenger_image(automation, method, hwnd, input_target, input_point, frame):
    """在通知持有的前台会话内粘贴一张图片。"""
    automation._check()
    if method == 'context_menu':
        return _paste_from_context_menu(automation, hwnd, input_point, frame)

    automation._set_clipboard_image(frame)
    automation._hotkey(input_target, win32con.VK_CONTROL, ord('V'))
    automation._wait(1)
    return True


def _paste_from_context_menu(automation, hwnd, input_point, frame):
    automation._check()
    automation._set_clipboard_image(frame)
    automation._click(hwnd, input_point)
    automation._wait(1)
    automation._click(hwnd, input_point, key='right')
    # 等前台菜单完成绘制再识别；等待期间仍检查焦点和取消。
    automation._wait(1)
    popup, paste = _wait_popup_text(
        automation, hwnd, {'Paste', '粘贴', '貼上'}, timeout=8,
        match_end=automation.paste_match_end)
    if paste is None:
        try:
            automation._save_error_screenshot('paste_menu')
        except Exception:
            pass
        raise RuntimeError('无法在桌面通知菜单中确认粘贴选项')
    automation._click(popup, paste)
    automation._wait(1)
    return True


def _render_surface(hwnd):
    targets = []

    def callback(candidate, _):
        if win32gui.GetClassName(candidate) == 'Chrome_RenderWidgetHostHWND':
            targets.append(candidate)
        return True

    win32gui.EnumChildWindows(hwnd, callback, None)
    return targets[0] if targets else hwnd


def _wait_popup_text(automation, hwnd, names, timeout, match_end=False):
    wanted = {name.casefold() for name in names}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        automation._check()
        try:
            candidates = _tool_popups(hwnd)
        except Exception:
            candidates = []
        try:
            render_surface = _render_surface(hwnd)
            if render_surface not in candidates:
                candidates.insert(0, render_surface)
        except Exception:
            pass
        candidates.append(hwnd)
        for popup in candidates:
            if not automation.window_belongs_to(hwnd, popup):
                continue
            automation._check()
            try:
                frame = automation._capture(popup)
                automation._last_frame = frame
                for text, x, y, width, height in automation._ocr(frame):
                    normalized = text.strip().casefold()
                    matched = (any(normalized.endswith(name) for name in wanted)
                               if match_end else normalized in wanted)
                    if matched:
                        return popup, (x + width // 2, y + height // 2)
            except Exception:
                continue
        automation._wait(.15)
    return hwnd, None


def _tool_popups(hwnd):
    _, root_pid = win32process.GetWindowThreadProcessId(hwnd)
    root_left, root_top, root_right, root_bottom = win32gui.GetWindowRect(hwnd)
    root_area = max(1, (root_right - root_left) * (root_bottom - root_top))
    popups = []

    def callback(candidate, _):
        if candidate == hwnd or not win32gui.IsWindowVisible(candidate):
            return True
        _, pid = win32process.GetWindowThreadProcessId(candidate)
        owner = win32gui.GetWindow(candidate, win32con.GW_OWNER)
        if pid == root_pid or owner == hwnd:
            left, top, right, bottom = win32gui.GetWindowRect(candidate)
            area = max(0, right - left) * max(0, bottom - top)
            if area and area < root_area // 2:
                popups.append((area, candidate))
        return True

    win32gui.EnumWindows(callback, None)
    return [candidate for _, candidate in sorted(popups)]
