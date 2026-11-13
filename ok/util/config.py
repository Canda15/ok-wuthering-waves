## Config.py
import os

from ok.util.file import get_relative_path, get_user_data_path, read_json_file, write_json_file
from ok.util.logger import Logger

logger = Logger.get_logger("Config")


class ConfigOption:

    def __init__(self, name, default=None, description="", config_description=None, config_type=None,
                 validator=None, icon=None, show_at_tab=False):
        self.name = name
        self.description = description
        self.default_config = default or {}
        self.config_description = config_description or {}
        self.config_type = config_type
        self.validator = validator
        self.icon = icon
        self.show_at_tab = show_at_tab


class Config(dict):
    # 便携进程在导入时就进入稳定根，提前创建的配置对象也不能落回 working。
    config_folder = get_user_data_path('configs') if os.environ.get('NONO_USER_DATA_ROOT') else 'configs'

    def __init__(self, name, default, folder=None, validator=None):
        """
        Initialize the Config object.

        :param default: Default configuration values.
        :param folder: Folder where the config file is stored.
        :param name: Name of the config file.
        :param validator: Optional function to validate key-value pairs.
        """
        self.default = default
        self.validator = validator
        if folder is None:
            folder = self.config_folder
        self.config_file = get_relative_path(folder, f"{name}.json")

        # 坏配置必须交给用户恢复；不能把读取失败当作首次安装而覆写默认值。
        config = read_json_file(self.config_file, strict=True)
        if config is None:
            self.reset_to_default()
        else:
            super().__init__()
            modified = self.verify_config(config, default)
            if modified:
                self.save_file()
        logger.debug(f'配置已加载，字段数：{len(self)}')

    def save_file(self):
        """
        Save the current configuration to the file.
        """
        try:
            write_json_file(self.config_file, self)
        except Exception:
            logger.error('配置保存失败（CONFIG_WRITE_FAILED）')
            raise

    def get_default(self, key):
        return self.default.get(key)

    def reset_to_default(self):
        """
        Reset the configuration to the default values.
        """
        super().clear()
        self.update(self.default)
        self.save_file()
        logger.debug(f'配置已重置，字段数：{len(self)}')

    def pop(self, key, default=None):
        """
        Remove and return a value from the configuration.

        :param key: The key to remove.
        :param default: The default value if the key does not exist.
        :return: The removed value.
        """
        result = super().pop(key, default)
        self.save_file()
        return result

    def popitem(self):
        """
        Remove and return the last key-value pair from the configuration.
        """
        result = super().popitem()
        self.save_file()
        return result

    def clear(self):
        """
        Clear all configuration values.
        """
        super().clear()
        self.save_file()

    def __setitem__(self, key, value):
        if (key not in self or value != self.get(key)) and self.validate(key, value):
            super().__setitem__(key, value)
            self.save_file()

    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            logger.error('配置字段不存在（CONFIG_KEY_MISSING）')
            raise

    def has_user_config(self):
        return not all(key.startswith('_') for key in self)

    def validate(self, key, value):
        """
        Validate a configuration key-value pair.

        :param key: The key to validate.
        :param value: The value to validate.
        :return: True if valid, False otherwise.
        """
        if self.validator is not None:
            valid, message = self.validator(key, value)
            if not valid:
                from ok.core.events import communicate
                communicate.config_validation.emit(message)
                return False
        return True

    def verify_config(self, current, default_config):
        """
        Verify the configuration against the default configuration.

        :param default_config: The default configuration.
        :return: True if the config was modified, False otherwise.
        """
        # 新版本只补缺省项；账号、插件及历史扩展字段不由已知默认键决定去留。
        modified = False
        verified = dict(current)

        for key in default_config:
            if key not in current or not isinstance(current[key], type(default_config[key])):
                value = default_config[key]
                modified = True
            elif self.validator is not None:
                valid = self.validate(key, current[key])
                if not valid:
                    value = default_config[key]
                    modified = True
                else:
                    value = current[key]
            else:
                value = current[key]
            verified[key] = value

        # 初始化直接更新内存，避免逐字段触发持久化；有效配置加载时无需写盘。
        dict.clear(self)
        dict.update(self, verified)

        return modified
