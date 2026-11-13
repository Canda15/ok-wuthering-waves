---
name: ok-script-develop
description: Develop or modify game automation functions using ok-script MCP, including tasks, characters, combat helpers, navigation, and recognition. Load the library's own development skills from the linked ok folder first, then the application's installed site-packages.
---

# Game Development with OK Script

Use the library's current consumer-application skills as the basis for development.
This skill adds discovery and project integration; the library supplies the task
contract, MCP workflow, and validation instructions. Work in the consuming
application and use its local virtual environment.

## Locate the library instructions first

Resolve skills in this order before editing game behavior:

1. Check `<cwd>/ok`. If present, resolve its symlink or junction target. For a
   source checkout, look in `<resolved-ok>/../.agents/skills`; for a packaged
   directory, look in `<resolved-ok>/agent_resources/.agents/skills`.
2. If the linked/local directory does not provide the relevant consumer skills,
   locate the application's installed `ok-script` distribution using its Python
   interpreter and `importlib.metadata.distribution('ok-script').locate_file(...)`.
   Look under `ok/agent_resources/.agents/skills` in that environment's
   `site-packages`. Do not use a different interpreter's global installation.
3. Read `develop/SKILL.md` from the selected library. Resolve its relative links
   from that file, including `docs/mcp.md`. Read `reload-tasks` for existing tasks,
   `write-test` for tests, and `generate-templates` for visual assets. Read setup
   skills only when environment or runtime setup is needed.

The library's `ok/core/skills.py` implements its source/package discovery. When
available, run that file with `runpy.run_path` and call its `list_skills()` or
`get_skill()` functions without importing the GUI runtime just to discover files.
For example, from the project root with the linked `ok` folder:

```python
from pathlib import Path
import runpy

catalog = runpy.run_path(str(Path('ok').resolve() / 'core' / 'skills.py'))
for skill in catalog['list_skills']():
    print(skill['id'], skill['path'])
```

Keep library instructions in their original location rather than copying them
into this project. Verify local API examples against the selected library; local
task/character references supplement its skills. If neither source has the
skills, report the missing library resources and use its setup guidance when
available; do not invent MCP tools or claim runtime validation.

## Use the library MCP workflow

Use ok-script MCP for inspecting and exercising game behavior. Discover native
ok-script tools when available; otherwise use the official MCP SDK from the
project environment with a local client script, as described by the library.
Read the selected server's `list_skills()` and relevant `get_skill(skill_id)`
results as well, and check that its project and library match the intended target.

在 Windows 上运行 Python MCP 客户端和测试时，使用 `-X utf8` 或等效的
UTF-8 流设置。任务名称和日志可能包含中文及表情；控制台编码错误不能
直接认定为服务或任务失败。

Reuse a live server for this project. Discover it through the configured artifact
directory, normally `logs/mcp/connections/*.json`, with `connection.json` as a
compatibility fallback. Prefer the library's `discover_mcp_servers()` helper when
available; validate process identity and its listener, use its actual URL and
bearer header, and keep tokens out of output. If no server is running, follow the
library's development skill and the project's launcher/config to start it. On
Windows, launch background helpers hidden. Do not assume a fixed port or replace
an active user GUI merely to start development; follow the library's ownership
rules.

For existing tasks, call `list_tasks()`, edit the returned source in place, and
call `reload_tasks(task_ids=[...])` with discovered IDs. Wait for a successful
terminal `get_run_status()` result before testing. A failed reload leaves old
instances in place. New registrations and changed non-plugin helpers, including
`src/char` modules and combat helpers, require restarting the owned runtime;
reloading a task alone does not refresh those imported dependencies.
将相关辅助函数、注册和素材修改一起完成，通过离线检查后再重启，确保
验证的是同一套代码。不能为了省去重启而测试旧导入模块，或在测试脚本里
另写一套角色循环。

Exercise the registered implementation through a thin `run_task` harness with an
absolute file path. Follow the library's `Task(BaseTask)` contract with
`reset_to_start()`, `verify_start_state()`, and `run()`. Reuse project recovery and
registered tasks, assert visible game results, and keep behavior in production
modules. Wait until `succeeded`, `failed`, or `cancelled` before submitting again;
`stopping` still owns the executor. Inspect returned local logs and screenshots,
including screenshot freshness. Follow the library's GUI lifecycle checks when
the change affects the user task card or normal scheduler.

For Windows input failures, follow the selected library development skill's
Windows input reference when available. Check runtime/target privileges before
changing backends, and verify buttons, keys, and drags separately through MCP.
Keep tool discovery/output narrow: filter tool metadata by relevant names, read
bounded log tails, and capture at state changes or failures rather than repeatedly
printing complete catalogs or unchanged frames.

If the target game or MCP prerequisites cannot be made available within the
authorized work, continue useful code and offline checks and state the specific
runtime validation gap. Do not present an import, reload, or mocked click as
proof that the game behavior succeeded.

## 本项目的 Windows 输入契约

本应用统一使用前台系统键鼠，游戏、原生登录器和已启用的 QQ／微信桌面通知共用输入核心。包内或历史资料中推荐 PostMessage、伪激活或局部前台兜底的指引不适用于本项目。

- 任务使用 `self.click`、`self.send_key`、`self.swipe_relative`、`self.input_text` 等公共接口；拖拽时长以秒传递。角色通过 `self.task` 调用同一接口。
- 不新建 `PynputInteraction` 或 Controller，不直接调用 SendInput／SetCursorPos，不用 `check_clickable=False` 绕过守卫。释放、失焦、取消和异常清理由共享后端负责。
- 正常新动作核对真实前台；失焦立即释放已持有输入并等待，游戏回到前台后刷新目标及画面自动继续。运行中禁止主动置前或发送旧激活通知。
- 拖拽、长按或选号中断后，从新的画面和安全检查点重新判断；不能续接旧手势，也不能无条件重放部分文本。
- `ForegroundInputInterrupted` 在具体战斗、导航、列表或任务阶段捕获，调用 `wait_foreground_frame()` 后重读 UI；`on_foreground_resume()` 只清旧状态，不执行输入。普通识别预算使用 `executor.business_time()`，明确的外部期限仍使用真实单调时钟。
- `ForegroundInputError` 表示注入失败或结果无法确认，必须上抛，不能进入焦点或业务重试。资源动作以领取、备用确认、奖励或继续挑战的新 UI 确认提交，波片差值只作辅助；未知结果留图停止。
- 一次性任务按真实完成阶段继续；需要 `resume_after_foreground()` 时保存明确检查点，禁止通用装饰器或递归 `run()` 重放整段任务。搜索文本中断后重新定位并清空编辑框，再输入完整查询。
- 原生控件消息只用于只读查询。账号选择用真实点击及键盘导航，回读确认失败或登录前账号改变时必须停止登录。
- 前台验证会占用真实键鼠，按实际权限、焦点、坐标和可见结果分别验证点击、键盘、拖拽及文本。离线替身测试和实际游戏结果分开记录，桌面通知测试只用替身或临时窗口。

任务、注释及新增交付文档遵循本项目 AGENTS.md，使用简体中文；稳定的配置键和 OCR 的多语言匹配保留原有兼容性。

For temporary event tasks, verify OCR against complete status-label regions,
distinguish completed from locked/time-gated stages, and handle first-time help
dialogs and board entrance/cascade animations before declaring recognition
failure. Validate visible reward settlement and a second run that skips completed
content. Respect a requested current-screen baseline rather than automatically
returning to the open world.

游戏开发及真实输入、截图验证使用 ok-script MCP，并遵守所选框架的接口
限制，包括该流程对 Computer Use 的禁止要求。若范围包含 GUI 生命周期，
仅通过允许的 API 操作任务卡，并说明尚未完成的 GUI 验证。

## 鸣潮深塔验证

已获授权的重置应复用注册战斗任务的 `restart_combat_in_abyss()`。
角色介绍和环境帮助可能不出现：先识别再关闭，未出现时直接继续。
新增浮层恢复逻辑应放入公共生产辅助函数，同时覆盖出现和不出现的情况，
不能只在临时测试脚本中修复。避免盲按 Esc 或关闭按钮，以免打开其它菜单。

重置后验证实际队伍 HUD 和加载的角色身份。辅助函数开始后会暂时强制
战斗识别，因此该标志本身不能证明场地或目标角色已经就绪。

根据实际场地选择完成断言。有些模拟场地以出现退出目标结束，并不弹出
独立成功窗口。必须确认对应的可见结束状态；丢失目标或战斗任务返回真值
不能单独证明完成。场地完成验证与单角色循环、满协奏交接验证分别记录。

## Apply project conventions

- For tasks, continue with [ok-script-tasks](../ok-script-tasks/SKILL.md) for local
  bases, metadata, registration, and bilingual behavior.
- For characters, continue with [ok-ww-characters](../ok-ww-characters/SKILL.md)
  for `BaseChar`, `CharFactory`, rotation timing, and feature labels.
- Use [ok-script-i18n](../ok-script-i18n/SKILL.md) when task UI strings change.
- Use the library's `write-test` skill and existing project fixtures for meaningful
  offline checks; verify actual capture and input effects through MCP.
