"""旧 Pynput 入口兼容 nono 前台方案，保留同一类型身份。"""

from ok.device.interaction_methods.nono import NonoInteraction

# 旧脚本必须进入同一套执行器前台等待、释放和恢复流程，不能另建子类。
PynputInteraction = NonoInteraction

__all__ = ['PynputInteraction']
