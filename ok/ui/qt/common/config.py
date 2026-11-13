# coding:utf-8
"""界面语言、主题与窗口背景共用稳定用户目录中的配置文件。"""

from enum import Enum

from PySide6.QtCore import QLocale, QObject, Signal

from ok.util.file import get_user_data_path, read_json_file, write_json_file
from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

BACKDROPS = ("mica", "acrylic", "none")
DEFAULT_BACKDROP = "acrylic"


class Language(Enum):
    """ Language enumeration """

    CHINESE_SIMPLIFIED = QLocale(QLocale.Chinese, QLocale.SimplifiedChineseScript)
    CHINESE_TRADITIONAL = QLocale(QLocale.Chinese, QLocale.TraditionalChineseScript)
    ENGLISH = QLocale(QLocale.English)
    SPANISH = QLocale(QLocale.Spanish)
    JAPANESE = QLocale(QLocale.Japanese)
    KOREAN = QLocale(QLocale.Korean)
    AUTO = QLocale()


def serialize_language(language: Language) -> str:
    return language.value.name() if language != Language.AUTO else "Auto"


def deserialize_language(value) -> Language:
    if not value or value == "Auto":
        return Language.AUTO
    try:
        return Language(QLocale(value))
    except ValueError:
        return Language.AUTO


class UiConfig(QObject):
    """The language, the theme mode and the window backdrop, all applied live.

    The file also accepts the older qfluentwidgets layout
    (``MainWindow.Language`` and ``QFluentWidgets.ThemeMode``).
    """

    language_changed = Signal()
    theme_changed = Signal()

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        data = self._read()
        # 界面切换只更新已知偏好，不能删除扩展项或旧布局字段。
        self._raw_data = data
        legacy_window = data.get('MainWindow')
        legacy_window = legacy_window if isinstance(legacy_window, dict) else {}
        legacy_widgets = data.get('QFluentWidgets')
        legacy_widgets = legacy_widgets if isinstance(legacy_widgets, dict) else {}
        legacy_language = legacy_window.get('Language')
        legacy_theme = legacy_widgets.get('ThemeMode')
        self.language = deserialize_language(data.get("language", legacy_language))
        self.theme_mode = str(data.get("theme", legacy_theme or "auto")).lower()
        if self.theme_mode not in ("light", "dark", "auto"):
            self.theme_mode = "auto"
        # Older files store only whether the effect is on ("mica", or qfluentwidgets'
        # MicaEnabled, which was on by default); on maps to the current default.
        legacy_on = data.get('mica', legacy_window.get('MicaEnabled', True))
        self.backdrop = str(data.get("backdrop", DEFAULT_BACKDROP if legacy_on else "none")).lower()
        if self.backdrop not in BACKDROPS:
            self.backdrop = DEFAULT_BACKDROP

    def _read(self) -> dict:
        data = read_json_file(self.path, strict=True)
        return data if data is not None else {}

    def save(self) -> None:
        data = {**self._raw_data, 'language': serialize_language(self.language),
                'theme': self.theme_mode, 'backdrop': self.backdrop}
        try:
            write_json_file(self.path, data)
        except Exception:
            logger.error('界面配置保存失败（UI_CONFIG_WRITE_FAILED）')
            raise
        self._raw_data = data

    def set_language(self, language: Language) -> None:
        if language == self.language:
            return
        self.language = language
        self.save()
        self.language_changed.emit()

    def set_theme_mode(self, mode: str) -> None:
        mode = getattr(mode, "value", mode)
        if mode == self.theme_mode:
            return
        self.theme_mode = mode
        self.save()
        self.theme_changed.emit()

    def set_backdrop(self, backdrop: str) -> None:
        backdrop = getattr(backdrop, "value", backdrop)
        if backdrop == self.backdrop or backdrop not in BACKDROPS:
            return
        self.backdrop = backdrop
        self.save()
        self.theme_changed.emit()


cfg = UiConfig(get_user_data_path('configs', 'ui_config.json'))
