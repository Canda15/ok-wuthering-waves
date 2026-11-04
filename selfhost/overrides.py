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

from ok import Logger

logger = Logger.get_logger(__name__)

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
