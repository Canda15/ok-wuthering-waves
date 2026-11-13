---
name: ok-ww-characters
description: Create, modify, review, and test Wuthering Waves character automation under src/char using the ok-script library's development skills and MCP workflow. Use for character rotations, BaseChar logic, CharFactory registration, Labels features, and English/Chinese combat terminology.
---

# OK-WW Characters

## Overview

Use this skill for character combat logic in `src/char`. A character implementation translates game mechanics from a skill-description page or user notes into a `BaseChar` subclass, then registers the class in `src/char/CharFactory.py`.

Before developing or modifying character behavior, follow
[ok-script-develop](../ok-script-develop/SKILL.md). Read the library's own skills
from the linked `<cwd>/ok` folder first, falling back to the project's installed
`site-packages`, and use the library's MCP development workflow. The project
patterns here supplement those instructions. Character modules are imported
helpers rather than registered tasks: restart the owned runtime after changing
them before exercising the consuming combat task; task reload alone leaves
non-plugin character imports cached.

Read `references/character-patterns.md` for the detailed API, templates, and validation checklist.

## Workflow

1. Inspect nearby character classes before editing.
   Prefer recent characters with similar mechanics, for example aerial/stance characters, healer classes, liberation stance characters, or forte-heavy characters.
2. Read the provided character description page if one is given.
   Extract only automation-relevant mechanics: normal attack chain, resonance skill behavior, forte trigger, liberation state, echo timing, intro/outro interactions, cooldowns, and required UI indicators.
3. Map terminology consistently:
   - `echo` = 声骸 / 声骸技能
   - `resonance` = 共鸣技能
   - `forte` = 共鸣回路
   - `liberation` = 共鸣解放
   - `click` = 普通攻击
   - `heavy` = 重击
4. Create or update `src/char/<CharacterName>.py`.
   实现 `BaseChar` 子类，以 `do_perform()` 作为主要循环。
   复制循环或切换优先级钩子前，先核对当前源码签名。
5. Register the character in `src/char/CharFactory.py`.
   添加导入和 `_char_dict_raw` 项，包含 `Labels.<char_label>`、
   `char_type`、`ring_index`，按角色机制设置 `buff_time`。
   仅使用当前工厂实际读取的字段。冷却由 HUD 识别，不添加未使用的
   `res_cd`、`echo_cd` 或 `liberation_cd`。
6. If the rotation needs new image/template detections, follow the library's
   `generate-templates` skill with real screenshots and the project's annotations
   and exporter. Preserve exact `Labels` names and verify the resulting detection.
   头像裁剪应围绕有辨识度的面部区域，技能裁剪应围绕内部符号，排除周围
   场景像素。收紧裁剪后重新验证相近状态；相同符号可能需要额外状态线索。
   If source images are unavailable, report the required feature and evidence gap
   rather than fabricating labels or annotations.
7. 识别修改补截图测试；循环或输入释放问题补有实际价值的回归测试。
   使用 `run_test.py` 及现有 `tests/TestChar.py`、`tests/TestLily.py` 模式。
8. Exercise the changed character through its consuming combat task using MCP,
   following the library's recovery, starting-state checks, and visible success
   assertions. 检查相关入口，尤其是共鸣解放不可用时带变奏返回的情况；
   单次开场循环不能证明延奏交接可靠。已获授权的深塔重置应复用
   `restart_combat_in_abyss()` 及其可选角色介绍关闭逻辑，并报告未测状态。

## Bilingual Rules

- Answer in the user's language. For mixed English/Chinese requests, include both key game terms when it helps.
- Keep Python method names and local variables in English unless the surrounding code already uses another style.
- 本项目关键逻辑和输入恢复决策使用简体中文注释，解释业务背景和原因。
- Preserve exact `Labels` enum names used by assets and annotations.

## Character Description Pages

When a page such as `https://ww.nanoka.cc/character/1210/` is provided, use it as a mechanics reference, not as code truth. Convert the page's combat sections into automation decisions:

- Which action starts the damage loop?
- Which UI indicator proves a stance, forte, enhanced skill, or special attack is ready?
- Which action consumes the state?
- Should the character stay on field, switch after one action, or fast-switch on intro?
- Does liberation cause animation freeze or a long alternate state?

Always verify the implementation against this repo's helpers and available labels.
若页面没有可读技能文本，按 [角色开发参考](references/character-patterns.md#skill-page-extraction)
检查其数据请求载荷。

## Essential Rules

- 输入通过 `self.task` 的公共键鼠方法进入同一个前台核心，遵循 [前台系统键鼠契约](../ok-script-develop/SKILL.md#本项目的-windows-输入契约)；禁止局部 Controller、窗口消息键鼠或直接 SetCursorPos。
- 失焦会释放已持有输入，回到前台后刷新画面自动继续；重新识别角色状态后决定后续动作，不能把已释放的长按或拖拽当作仍在进行。
- 角色识别或技能兜底的宽泛异常处理必须继续抛出 `ForegroundInputInterrupted`、`ForegroundInputError` 和取消异常，让公共战斗循环退出旧轮转并从新队伍判断；不能在角色兜底内吞错或重放技能。
- Do not add a character class without registering it in `CharFactory.py`.
- Do not invent `Labels` feature names silently. Create and verify new features through the library's template workflow when evidence is available; otherwise document the required label and missing source image.
- Use `self.time_elapsed_accounting_for_freeze(...)` for rotation timing when liberation or intro animation can freeze time.
- Prefer `click_resonance`, `click_liberation`, `click_echo`, `heavy_attack`, `continues_normal_attack`, `heavy_click_forte`, `is_forte_full`, `has_long_action`, and `f_break` over raw key calls.
- Keep loops bounded by timeouts and call `self.task.next_frame()` or `self.sleep(...)` inside polling loops.
- 手动长按键鼠必须在 `finally` 中释放，包含取消和识别超时路径。
  共鸣回路消耗提示消失，不能单独证明多阶段重击收尾已经完成。
- 按角色机制安排动作顺序。持续共鸣解放状态可能隐藏常规按钮，或将解放
  按钮改为结束按钮；通用可用性识别不能单独证明此时适合按下。
- 依赖延奏的辅助角色应在交接时确认满协奏。如果最后一段能量通常来自
  共鸣解放，要为冷却和失败路径提供符合增益窗口的有界补能。
- 仅在切换确实依赖变奏、冷却、治疗时机或长增益窗口时，使用
  `get_switch_priority()` 和 `SwitchPriority` 定制优先级。
