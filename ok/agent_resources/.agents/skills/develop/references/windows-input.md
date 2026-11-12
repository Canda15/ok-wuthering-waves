# 通过 MCP 验证 Windows 前台输入

截图正常但输入失败、权限被拒绝或手势中断时使用本指南。本应用 Windows
始终使用共享前台系统键鼠，先诊断具体动作，不切回窗口消息或另建控制器。

## Separate permission, delivery, and recognition

- Confirm the configured target, capture dimensions, coordinate mapping, and a
  fresh frame. A cached observation or a successful input API return does not
  prove that the game received the action.
- 系统输入被拒绝时，使用只读诊断核对**实际提供 MCP 服务的 ok-script 进程**
  与目标进程的权限。通过 MCP 观察任务调用 `ok.util.process.is_admin()` 才能
  说明运行时权限；客户端中调用描述的是另一个进程。截图正常不代表输入权限匹配。
- If a lower-privilege runtime is confirmed against an elevated target, restart
  only the verified, owned runtime at the required privilege level before
  experimenting with recognition thresholds or input backends. Do not elevate
  by default, disable security controls, automate UAC, or bypass execution
  policy. If elevation is unavailable, report the specific blocker.
- 没有权限错误时核对真实前台、屏幕坐标、缩放、按下时长、拖拽终点与释放。
  不用缓存的窗口可见状态或接口未抛异常代替实际输入结果。

## Restart an owned runtime when elevation is needed

1. Preserve the failing log and observation. Respect ownership: an existing user
   GUI is not an agent-owned console to terminate merely for development.
2. For an owned MCP runtime, request `exit_process(reason=...)`, wait locally for
   its returned PID to exit, then relaunch with the consuming project's absolute
   venv interpreter, working directory, and actual launcher/config arguments.
3. On Windows, an allowed elevation path is PowerShell `Start-Process` with
   `-Verb RunAs` and `-WindowStyle Hidden`. The user handles any OS UAC prompt;
   explain why it appears. Preserve the project's config and selected target.
4. Rediscover the connection and validate the new PID, listener, and project.
   Do not reuse the old URL, credentials, or run IDs. Verify runtime privilege,
   capture freshness, and the same input action again. Restarting ok-script does
   not reset the game UI.

## Verify input by action and window state

Use a small MCP harness that restores and verifies an appropriate starting
state. Exercise an action with a known, visible effect; bound retries and stop
to diagnose repeated no-effects instead of continuing the full task.

| Action | Evidence of delivery |
| --- | --- |
| Ordinary button click | The intended page, selection, or modal changes |
| Key | Its intended visible menu or state changes |
| Drag or swipe | The item actually moves, the list scrolls, or the game accepts the move |

For example, a successful match-three swap changes pieces and an accepted-move
counter or elimination result. Mouse motion alone is insufficient evidence.

在真实前台逐项验证必要动作，记录真实前台、光标、目标可见变化与新帧时间。
分别验证失焦后的持有释放、可取消等待和返回游戏后的新画面；菜单点击成功
不能证明拖拽成功。每次只改变一个因素，保留输入前后的成功或失败证据。

## 统一前台输入与恢复

- 通过任务标准接口复用 `PynputInteraction` 和共享前台输入核心。实际注入由
  受检 `SendInput`／`SetCursorPos` 边界完成，不私建 pynput Controller。
- 说明操作占用真实键盘、鼠标和前台。显式启动可请求一次真实置前；周期任务、
  运行中失焦等待和已失焦的通知不得抢回前台。系统拒绝置前或输入时明确失败。
- 游戏坐标使用实际捕获到屏幕的转换；通知菜单与子窗使用各自 HWND 的
  `ClientToScreen`，只允许目标根、子窗与拥有关系。
- 失焦、停止、异常和所有权交接均释放本程序持有。回到游戏后更新目标代次、
  坐标和画面，在安全检查点重新判断，不续接旧长按或重放部分文本。
- QQ／微信通知在执行器空闲或安全边界串行发送，长任务可能延后；发送结束
  或失败都释放，只有仍保持程序切换且用户未切走时才恢复游戏并取得新帧。
- 通过 MCP 重跑实际注册任务，分别记录离线替身、受控窗口和真实游戏验证。
  桌面通知只能使用替身或临时受控窗口测试，不向真实联系人发送消息。

Do not use Computer Use skills, plugins, or tools for input validation or desktop
task-card testing. Keep capture and interaction in ok-script APIs and MCP. If the
GUI Start action cannot be tested through permitted APIs or an application-local
test harness, report that check as unverified.
