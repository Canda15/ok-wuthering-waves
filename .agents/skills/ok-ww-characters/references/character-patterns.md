# OK-WW Character Patterns

## Files To Inspect

- `src/char/BaseChar.py`: shared combat helpers, cooldown tracking, priority, forte and input helpers.
- `src/char/CharFactory.py`: imports, label-to-class registration, cooldowns, element ring index.
- `src/char/Lily.py`：多阶段重击蓄力、持续共鸣解放，以及延奏前补充协奏。
- `src/Labels.py`: enum of feature names generated from annotated assets. New character and mechanic features belong here after developer annotation.
- `src/task/BaseCombatTask.py`: `available`, `get_cd`, `has_cd`, `find_mouse_forte`, `find_e_forte`, `load_chars`, `switch_next_char`.
- `tests/TestChar.py`: screenshot-backed recognition and availability tests.
- `tests/TestLily.py`：就绪、蓄力和状态负例、头像注册及鼠标释放回归。

以当前源码为 API 依据。治疗行为通过 `CharType.HEALER` 配置；
当前代码没有独立的 `Healer.py` 基类。

## Terminology

Use these names in code discussions and comments:

- `echo`: 声骸 / 声骸技能, usually `click_echo()` or `send_echo_key()`.
- `resonance`: 共鸣技能, usually `click_resonance()` or `send_resonance_key()`.
- `forte`: 共鸣回路, usually `is_forte_full()`, `is_mouse_forte_full()`, `is_e_forte_full()`, or character-specific feature checks.
- `liberation`: 共鸣解放, usually `click_liberation()` or `send_liberation_key()`.
- `click`: 普通攻击, usually `click()` or `continues_normal_attack(...)`.
- `heavy`: 重击, usually `heavy_attack(...)`, mouse hold, or `heavy_click_forte(...)`.

## Class Template

```python
import time

from src.char.BaseChar import BaseChar


class NewCharacter(BaseChar):
    def do_perform(self):
        if self.has_intro:
            self.continues_normal_attack(0.8)
        else:
            self.wait_down()

        self.perform_rotation()
        self.switch_next_char()

    def perform_rotation(self):
        start = time.time()
        while self.time_elapsed_accounting_for_freeze(start) < 2.5:
            self.cycle_start()
            self.click_echo(time_out=0)
            if self.click_liberation(wait_if_cd_ready=0):
                self.f_break()
            elif self.is_forte_full():
                self.heavy_click_forte()
            elif self.click_resonance(send_click=False, time_out=1)[0]:
                pass
            else:
                self.click(interval=0.1)
            self.cycle_sleep()
```

该示例假设共鸣解放后恢复常规按钮。应按实际机制安排顺序，并固定整个
循环的截止时间；每次施放都重置时间会使超时边界失效。仅在机制需要时
增加自定义状态或切换优先级。

## Common Rotation Patterns

Simple sub-DPS:

```python
def do_perform(self):
    if self.has_intro:
        self.continues_normal_attack(1.0)
    self.click_echo(time_out=0)
    self.click_liberation()
    self.click_resonance()
    self.continues_normal_attack(0.5)
    self.switch_next_char()
```

Forte-heavy character:

```python
if self.is_mouse_forte_full():
    self.heavy_click_forte(self.is_mouse_forte_full)
elif self.is_e_forte_full():
    self.send_resonance_key()
else:
    self.click()
```

Feature-gated special attack:

```python
def special_available(self):
    return bool(self.task.find_one("new_character_special", threshold=0.7))
```

使用新特征前，按框架的模板流程制作并验证。没有截图时，应说明缺失特征
及证据，不能凭空编造标注。

`click_liberation()` 已处理共鸣解放动画和冻结时间，不重复计算同一动画。
动画结束后仍持续的特殊状态，需要独立的可见状态处理，见下文。

Team synergy or intro priority:

```python
from src.char.BaseChar import SwitchPriority


def get_switch_priority(self, current_char=None, has_intro=False, target_low_con=False):
    if has_intro and current_char and current_char.char_name in {"char_expected_outro"}:
        return SwitchPriority.MUST
    return super().get_switch_priority(current_char, has_intro, target_low_con)
```

## Registration Pattern

In `src/char/CharFactory.py`:

```python
from src.char.BaseChar import CharType, Elements
from src.char.NewCharacter import NewCharacter

_char_dict_raw = {
    Labels.char_new_character: {
        "cls": NewCharacter,
        "char_type": CharType.SUB_DPS,
        "ring_index": Elements.FIRE,
    },
}
```

当前工厂读取角色定位、属性和增益信息，冷却通过实时 HUD 跟踪，不添加
未使用的冷却字段。已知队伍增益时长与定位默认值不同时，设置 `buff_time`。
按名称映射属性：数据站的属性数字编号未必与 `Elements` 一致。

Use existing `Elements`:

- `Elements.SPECTRO`
- `Elements.ELECTRIC`
- `Elements.FIRE`
- `Elements.ICE`
- `Elements.WIND`
- `Elements.HAVOC`

If multiple visual labels identify the same character, use a tuple:

```python
_char_dict_raw[(Labels.char_new_character, Labels.char_new_character_alt)] = {
    "cls": NewCharacter,
    "char_type": CharType.MAIN_DPS,
    "ring_index": Elements.FIRE,
}
```

## Labels and Features

Character portrait labels and combat mechanic labels must exist in `Labels` and template data before detection can work. Good names follow existing conventions:

- `char_<name>` for portrait recognition.
- `<name>_heavy`, `<name>_jump`, `<name>_lib_forte`, `<name>_e1`, `<name>_e2`, `<name>_human`, etc. for mechanic indicators.

When adding code that depends on a missing feature, include a clear note:

```text
Needs developer-added feature label: Labels.<name>_<mechanic>, used to detect <state/action>.
```

Do not claim the character is fully testable until the label asset exists.

### 收紧裁剪与状态区分

头像区域应位于脸部或头发内，并落在工厂头像搜索框中。技能区域应位于
特征符号内，尽量排除场景、快捷键文字、冷却数字和动画光效。导出前检查预览。

修改裁剪后，同时测试目标状态和最相近的其它状态。归一化模板匹配可能
将不同颜色的同一符号视为相同。Lily 的就绪花朵与蓄力花朵需要额外的
通用满槽线索；只保留内部符号后，提高阈值不足以区分。其它角色按实际证据选择线索。

源截图旁保留独立正例和有价值的负例。头像注册要对其它已有角色槽位验证，
不能只做模板与自身截图的匹配。

### 保留历史素材的局部导出

拆分导出器会删除目标目录中未出现在输入 COCO 的特征，因此不能将仅含
新特征的子集直接导出到主模板目录。若无关历史源图缺失，保留完整规范标注，
将相关子集导出到独立暂存目录，仅合入对应名称的产物。删除这些名称已被
替代的旧几何文件，避免同一特征保留多套裁剪。保留其它素材，并遵循框架
的 `generate-templates` 流程。

## 多阶段动作与延奏交接

蓄力动作须区分就绪、资源已消耗、蓄力中，以及收尾或动画完成。满槽提示
消失可能只表示动作刚开始。每段等待都有截止时间、刷新画面，并在 `finally`
释放按住的键鼠。仅在预期动作期间长按，防止收尾后又开始下一次攻击。

将共鸣解放放入通用循环前，先确认第二次按下的作用及之后仍可用的按钮。
Lily 的梦境会隐藏 E、声骸和普攻，并将解放按钮改为结束，所以收尾和补能
应先完成。若该状态影响交接时锁定，角色特例必须依赖可见状态，并在状态
结束或角色切换后失效，不能全局关闭战斗检查。

依靠延奏提供增益的辅助角色，应观察满协奏和切换成功，同时考虑受冷却
限制的变奏、被动能量，以及共鸣解放不可用或失败。有界 E、普攻补能可以
弥补短缺，但必须落在收尾增益窗口内。Lily 已验证的三秒补能时限仅适用于
该角色，不能作为其它辅助角色的默认值。

## Skill Page Extraction

若 Nanoka 页面没有可读文本，检查原始 HTML 的
`script[data-sveltekit-fetched]` 项，解析 JSON 和其中 `body` 的 JSON，
读取该页面实际引用的角色数据 URL。此方法曾无需浏览器会话便获取 Lily
技能树、冷却、变奏和共鸣回路说明。不能写死观察到的数据版本，也不能
假设其它页面具有相同结构；对用户引用仍使用原始页面链接。

From a character page, extract:

- Element and role clues.
- Cooldowns for resonance, echo, and liberation.
- Whether normal attack, heavy attack, resonance, echo, or liberation starts the core loop.
- Forte condition and consumption action.
- Enhanced resonance/libration states and their UI indicators.
- Intro/outro timing or teammate dependencies.
- Whether a skill is considered Basic Attack DMG, Echo Skill DMG, Resonance Skill DMG, or Liberation DMG only if it affects automation decisions.

Ignore damage multipliers unless timing, state duration, or priority depends on them.

## Validation Checklist

- New class file imports only what it uses.
- `super().__init__(*args, **kwargs)` is called before custom state setup.
- `do_perform()` always eventually calls `switch_next_char()` or intentionally returns after switching.
- Every polling loop has a timeout and frame/sleep progression.
- New feature labels are documented and not silently invented.
- `CharFactory.py` imports and registers the class.
- `char_type`、`buff_time`、`ring_index` 符合角色机制和当前工厂。
- Existing tests pass. Add screenshot tests when new recognition labels or cooldown checks are introduced.

按角色机制选择 MCP 场景，不要求固定运行次数：

| 场景 | 验证证据 |
| --- | --- |
| 无变奏开场 | 工厂识别实际槽位；补能、特殊动作和交接成功。 |
| 带变奏返回 | 实际发生变奏；共鸣解放冷却时仍能完成增强动作和满协奏延奏。 |
| 多阶段或持续状态 | 就绪与激活状态可区分；收尾完成；第二次按下不会取消预期状态。 |
| 队伍集成 | 注册战斗任务推进到场地的可见完成条件。 |

切换冷却可能拒绝首次输入。复用有界切换辅助函数，确认当前槽位已变化
再继续。`load_chars()` 会重置变奏和每轮状态；只有确认真实满协奏切换后
才能设置测试变奏标志，不能制造未观察到的游戏状态。

截图测试调用可用性或冷却辅助函数时，应填充 `task.chars` 并标记当前角色，
因为冷却查询会调用 `get_current_char()`。使用仓库测试入口，例如
`.\.venv\Scripts\python.exe -X utf8 run_test.py tests.TestLily`；各
`TaskTestCase` 模块使用独立进程，隔离运行时状态。

报告时区分源图自身匹配、独立截图识别、输入替身意图和实际 MCP 输入结果。
保留可复用夹具与标注。用户要求清理时，在确认没有正在运行的测试依赖后，
删除仅供本次会话使用的探针和重置脚本。
