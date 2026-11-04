"""对 vendored 框架源码(./ok)的文字级补丁。

## 为什么是源码补丁,而不是运行时改 config / monkeypatch

这些字符串在**对象构造或模块导入的那一刻**就已经交给 Windows 了:通知的
AppUserModelID 在 ``WindowsSystemNotifier.__init__`` 里立刻
``SetCurrentProcessExplicitAppUserModelID`` 并启动注册窗口的线程,overlay 的窗口类名
在 ``RegisterClass`` 时写入内核。等 ``main.py`` 拿到 config 再去改,已经晚了。

## 为什么是"补丁清单"而不是直接改 ok/ 里的文件

``ok/`` 是 vendored 的上游源码,上游每次发版都要被整树替换(scripts/sync_ok.py)。
手改的文件会在下一次替换时消失,而且没有任何报错 —— 品牌标识悄悄回来,直到
某天又被人按名字认出来。所以补丁必须是可以被**重新施加**的数据:
``scripts/sync_ok.py`` 在解包 wheel 之后、计算 pin 哈希之前调用 ``apply_to()``,
于是"补丁是否生效"就包含在 ``ok/`` 的哈希里,``--check`` / 漂移检测天然覆盖。

## 维护规则

- ``old`` 必须在新版本里**恰好出现一次**,否则 ``apply_to()`` 直接抛错要求人工更新:
  宁可同步失败,也不要静默地少打一个补丁。
- 只改"能被外部观察到"的字符串(app_id、窗口类名、计划任务作者、临时文件名、
  邮件主题);日志文案、MCP 服务名这类只在本机自己看得到的不动,免得维护面变大。
- 补丁只做字符串替换,不改变控制流;任何需要改逻辑的需求都应该另写机制。
"""

from __future__ import annotations

from pathlib import Path

# path 相对 ./ok 目录(即 wheel 里的 ok/ 包),便于直接作用于解包后的源码树。
PATCHES: tuple[dict[str, str], ...] = (
    {
        "path": "notification/system.py",
        "old": 'self.app_id = "ok-script." + re.sub(',
        "new": "self.app_id = re.sub(",
        "why": "AppUserModelID 会出现在通知中心的历史记录与注册表 Toast 条目里,前缀等于自报家门",
    },
    {
        "path": "notification/system.py",
        "old": 'f"ok-script.SystemNotifier.{id(self)}"',
        "new": 'f"{self.app_id}.SystemNotifier.{id(self)}"',
        "why": "通知窗口的 Win32 类名:EnumWindows + GetClassName 就能读到,不需要任何权限",
    },
    {
        "path": "ui/overlay/win32_gdi.py",
        "old": '"ok-script.Win32GdiOverlay"',
        "new": '"OverlayWindow"',
        "why": "叠层窗口的 Win32 类名,和上面同理;这里用中性名,改名时不必再维护一次",
    },
    {
        "path": "util/windows_schedule.py",
        "old": "<Author>ok-script</Author>",
        "new": "<Author>NoNo</Author>",
        "why": "计划任务的 XML 作者,任务计划程序里右键就能看到",
    },
    {
        "path": "util/process.py",
        "old": "f'ok-script-{mutex_name}.pid'",
        "new": "f'{mutex_name}.pid'",
        "why": "%TEMP% 下的单实例 pid 文件被占了名字;mutex_name 本身已是 cwd 的 md5,足够唯一",
    },
    {
        "path": "notification/providers.py",
        "old": "'ok-script Notification'",
        "new": "'NoNo Notification'",
        "why": "邮件通知的默认主题,收件人一眼就能看到",
    },
    {
        "path": "ui/qt/about/AboutTab.py",
        "old": "return [project for project in projects if normalize(project['url']) != current]",
        "new": (
            "return []  # NoNo:自维护分支不展示框架作者的其他项目"
            "(见 selfhost/framework_patches.py)"
        ),
        "why": "关于页硬编码了 12 个上游项目;config 只能过滤掉本项目的 github 链接,"
        "其余条目会把上游站点和同系列项目直接列出来",
    },
)


def _read_text(path: Path) -> str:
    # 刻意用 bytes:Windows 上 Path.read_text 的换行转换会把整份文件改成 CRLF,
    # 让补丁后的 diff 变成全文重写。
    return path.read_bytes().decode("utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def apply_to(root: Path) -> tuple[list[str], list[str]]:
    """把补丁打到 ``root``(解包后的 ok/ 源码树)上。

    返回 ``(applied, already)``:本次实际改写的文件、原文已被替换过而跳过的文件。
    原文缺失或出现次数不为 1 时抛 SystemExit —— 这是上游改了同一段代码的信号,
    必须人工确认新写法后再更新本清单。
    """
    applied: list[str] = []
    already: list[str] = []
    for patch in PATCHES:
        path = root / patch["path"]
        if not path.is_file():
            raise SystemExit(
                f"源码补丁失效:ok/{patch['path']} 在新版本里不存在,请更新 selfhost/framework_patches.py"
            )
        text = _read_text(path)
        if patch["new"] in text:
            already.append(patch["path"])
            continue
        seen = text.count(patch["old"])
        if seen != 1:
            raise SystemExit(
                f"源码补丁失效:ok/{patch['path']} 里原文出现 {seen} 次(期望 1 次),"
                f"上游可能改写了这一段,请核对后更新 selfhost/framework_patches.py:\n  {patch['old']}"
            )
        _write_text(path, text.replace(patch["old"], patch["new"]))
        applied.append(patch["path"])
    return applied, already


def missing(root: Path) -> list[str]:
    """返回 ``root`` 上尚未生效的补丁文件(文件缺失也算)。"""
    absent: list[str] = []
    for patch in PATCHES:
        path = root / patch["path"]
        if not path.is_file() or patch["new"] not in _read_text(path):
            absent.append(patch["path"])
    return absent
