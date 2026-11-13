"""本仓库相对上游 ok-wuthering-waves 的全部自有差异。

背景:上游在 aa543c4 里删掉了 config.py 的 ``custom_tabs`` 键,连带取消了
「角色代码」和「多账号配置」两个 Qt 页面的注册(src/gui/ 下这两个文件仍随上游
一起更新,只是没人再引用它们)。我们要把这两个页面拿回来,但不能直接改
config.py:

- 上游一天能改 config.py 好几次,往里插三行会持续产生合并冲突;
- 上游的 git 树里不存在 ``selfhost/``,所以这里的差异不参与三方合并。

因此自有差异统一收拢到本模块,由 main.py 在构造 ``OK(config)`` 之前调用
apply()。配套的自动同步流程见 SELFHOST.md,冒烟断言见
tests/TestSelfhostSmoke.py。
"""

from importlib.util import find_spec
import os

from ok import Logger
from ok.util.file import get_user_data_path

logger = Logger.get_logger(__name__)

# 对外只用一个名字,必须与 pyappify.yml 的 name 一致:那个决定 exe 名与解压
# 目录名,这个决定窗口标题。两边不一致时用户会看到"程序叫 NoNo、目录叫别的"。
APP_NAME = "NoNo"

# 顺序即侧边栏顺序,与上游删除前的注册顺序保持一致。
CUSTOM_TABS = (
    ("src.gui.CharacterCodeTab", "CharacterCodeTab"),
    ("src.gui.MultiAccountConfigTab", "MultiAccountConfigTab"),
)


def apply(config):
    """把自有差异写进 config,返回最终生效的页面清单(元素为 (模块, 类名))。

    config 是普通字典且按引用传递:ok/__init__.py 的 OK.__init__ 直接保存这个
    引用,MainWindow 读的是同一个对象,所以必须在 ``OK(config)`` 构造之前调用,
    构造之后再改就不会生效了。
    """
    apply_branding(config)
    apply_user_data_paths(config)
    apply_input_policy(config)
    return apply_custom_tabs(config)


def apply_user_data_paths(config):
    """启动器保护用户原件后提供稳定根，源码开发继续使用原配置路径。"""
    if os.environ.get('NONO_USER_DATA_ROOT'):
        # OK 初始化会把这一目录赋给 Config，所有任务和账号共用同一存储位置。
        config['config_folder'] = get_user_data_path('configs')


def apply_input_policy(config):
    """上游配置同步后继续使用前台后端，并撤出后台光标补救任务。"""
    windows = config.get('windows')
    if windows is not None:
        # 与 DeviceManager 的历史配置迁移共同守住输入边界，不保留后台备选。
        windows['interaction'] = 'Pynput'
    if 'trigger_tasks' in config:
        config['trigger_tasks'] = [
            task for task in config['trigger_tasks']
            if tuple(task) != ('src.task.MouseResetTask', 'MouseResetTask')
        ]


def apply_branding(config):
    """改写所有由 config 决定的对外名字,并关掉会指回上游的两处运行时行为。

    gui_title 是品牌信息的源头:窗口标题与任务栏名(ok/__init__.py:326-328)、
    关于页大标题(ok/ui/qt/about/AboutTab.py:180)、通知 app_id
    (ok/notification/system.py:84)、诊断包名(ok/ui/qt/shell/diagnostics.py:43)、
    计划任务文件夹 ``\\<gui_title>``(ok/util/windows_schedule.py:319-320)。
    """
    config["gui_title"] = APP_NAME
    portable_user_data = bool(os.environ.get('NONO_USER_DATA_ROOT'))
    config['log_file'] = get_user_data_path('logs', f'{APP_NAME}.log') if portable_user_data else f'logs/{APP_NAME}.log'
    config['error_log_file'] = get_user_data_path('logs', f'{APP_NAME}_error.log') if portable_user_data else f'logs/{APP_NAME}_error.log'

    # 必须删：上游窗口首次显示时可能按这个键把启动器
    # 换成上游 v3.5.30 的 ok-ww-win32.zip。我们的 exe 已改名为 NoNo.exe,留着它
    # 会凭空多出一个 ok-ww.exe,而且版本会被降级回上游。
    config.pop("update_pyappify", None)

    # 关于页那排按钮(github / discord / faq)原本全部指向上游项目与社区,
    # 清空后 AboutTab 逐个取不到地址就直接跳过(ok/ui/qt/about/AboutTab.py:192-195),
    # 不会留下空按钮。更新不受影响:更新走 pyappify 读镜像仓库的 tag,与本键无关。
    config["links"] = {}


def apply_custom_tabs(config):
    """把上游取消注册的两个 Qt 页面挂回侧边栏。"""
    tabs = list(config.get("custom_tabs") or [])
    registered = {tuple(tab) for tab in tabs}
    for module_name, class_name in CUSTOM_TABS:
        # 上游若把某个页面自己注册回来,这里不重复添加,避免侧边栏出现两份。
        if (module_name, class_name) in registered:
            continue
        if not _importable(module_name):
            # 上游删掉某个页面不必拖垮整个 GUI;冒烟测试会把这种情况暴露出来。
            logger.warning(f'自定义页面 {module_name}.{class_name} 已不存在,跳过注册')
            continue
        tabs.append([module_name, class_name])
        registered.add((module_name, class_name))
    if tabs:
        config["custom_tabs"] = tabs
    return [tuple(tab) for tab in tabs]


def _importable(module_name):
    """只探测模块能否定位,不真正 import。

    两个页面自身依赖很重(字符代码页会拉起 CustomCharLoader 和 CharFactory),
    在这里提前 import 只会让"文件丢了"和"依赖坏了"混成同一个报错。
    """
    try:
        return find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False
