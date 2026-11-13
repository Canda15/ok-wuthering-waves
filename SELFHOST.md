# 自维护单仓说明

本仓库是 ok-ww 的自用分支，对外名字已改为 **NoNo**。与上游最大的差别：**框架库 ok-script 不再作为 PyPI 依赖，而是以源码形式内联在 `./ok`**。

## 为什么改成内联

上游的做法是：开发时 `pip install ok-script`，打包时由 `ok/update/inline_ok_requirements.py` 把
site-packages 里的 `ok` 复制进部署树，再由 `pyappify.yml` 的 `git_url` 把**整棵部署树**推给用户当更新包。

也就是说，只要我们要改框架代码（而这里的目标正是如此），改动能否进更新包，取决于每次发布有没有人记得跑那条 inline 命令；
一旦忘了，包里的 `ok` 会静默回退成 PyPI 版本，症状是"改的代码本地有效、更新后失效"。
内联之后 `ok/` 就是普通源码，随仓库提交、随更新树分发，不再有这条隐式步骤。

## 当前内联版本

| 项 | 值 |
| --- | --- |
| 版本 | `ok-script` 3.0.5b7（适配上游应用 v3.8.0） |
| 权威来源 | `selfhost/ok_pin.json`（版本、原始 wheel、完整补丁后树与逻辑差异集合的 SHA-256，由 `scripts/sync_ok.py` 维护） |
| 来源 | PyPI wheel `ok_script-3.0.5b7-py3-none-any.whl` |
| 原始 wheel SHA-256 | `3ddb003bb62bb3e80e7ada0c7261067773f0c0b1ce5784829cc3844a7bc59fb3` |
| 内联范围 | wheel 的 `ok/` 基线共 281 个有效文件；逻辑差异后的实际数量由 `--check` 给出 |
| 未内联 | wheel 的 `diagnostics/`、`examples/` 与 dist-info 不作为框架源码入库；生成依赖和内联版本依据原始 wheel、pin 及严格来源清单，不能回退到另一份已安装的 ok-script |

内联以固定 wheel 为基线，再施加品牌与已登记的逻辑差异，但**没有上游框架的 git 历史**。
最初因本机沙箱禁止 `sh.exe` 创建命名管道、`git subtree` 无法运行，采用了“下载 wheel 解包后入库”的方式。

## 落地改动（相对上游 aa543c4）

1. `ok/` 入库；`.gitignore` 删掉 `ok`、`ok/` 两行——它们是为"发布时才复制"设计的，保留会导致内联源码进不了 git。
2. `.gitignore` 的 `update/` 改为 `/update/`。原规则会连 `ok/update/` 一起忽略，而
   `ok/update/inline_ok_requirements.py` 是发布脚本，丢了会让 `python -m ok.update.inline_ok_requirements`
   直接 `ModuleNotFoundError`（这是内联过程中实际踩到的坑）。
3. `.gitignore` 删掉过期的 `src/gui/CharacterCodeTab.py` 条目。该文件一直被 git 跟踪，忽略规则对它本无效，
   但留着会让后来人以为它不入库。
4. `pyproject.toml` 删掉 `ok-script[ocr,qt]==3.0.1`，并把它的运行时依赖分成两段：应用自己声明的
   6 条由人维护，`# >>> generated:` 与 `# <<< generated:` 之间那份由 `scripts/sync_ok.py` 按 wheel
   元数据整体重写。原因是 pip 现在不再从 wheel 解析这些依赖，不显式声明的话，下次 pip-compile
   会把它们从 `requirements.txt` 里悄悄丢掉。
5. `pyproject.toml` 删掉 `web-test` extra：它引用的 `ok-script[web]` 在 3.0 已不存在，装了也只是打一行 warning。
6. `requirements.txt` 删掉 ok-script 行并留注释，其余锁定版本全部不动（pip 仍按这份文件装依赖）。
7. 删除 `src/task/CharacterCodeTask.py`：它 import 的 `WebCustomTab`、`WebTabConfig`、`task_tab_action`、
   `task_tab_query` 在 3.0 被整体移除，全仓库无任何引用，留着就是一枚"被 import 就炸"的地雷。
8. 新增 `selfhost/`（自有差异目录，见下节），`main.py` 在构造 `OK()` 前调用
   `selfhost.overrides.apply(config)`，`deploy.txt` 增加 `selfhost` 一行。
9. `build.yml` 的 `package` job 使用共享便携打包器，构建经过来源核验的 NoNo 启动器，
   安装完整运行环境并生成 ZIP，不经过上游的 SignPath 与 NSIS 安装包链路。
10. 改名成 NoNo：`pyappify.yml` 的 `name`（决定 exe 名、解压目录、`data/apps/<name>`、资产名）
    与 `selfhost/overrides.py` 的 `APP_NAME`（决定窗口标题等）必须一致。config 侧的品牌项
    ——`gui_title`、两个日志文件名、`links`、`update_pyappify`——全部在 `overrides.apply_branding()`
    里改，不改上游 `config.py`。`deploy.txt` 里那行 `ok-ww.exe` 是上游残留（根目录没有该文件），
    一并删掉。
11. `selfhost/framework_patches.py`：`ok/` 源码里有几处字符串改 config 根本来不及（通知的
    AppUserModelID 在构造时就被交给 Windows，窗口类名在 `RegisterClass` 时写进内核），
    只能改源码。它们是"补丁清单"而不是手改：`sync_ok.py` 每次在隔离解包树内自动重放，
    并在算 pin 哈希之前施加，所以补丁丢失会被 `--check` 与冒烟测试同时发现。
12. `selfhost/logic_patches.py` 与 `logic_patches.json`：独立维护框架控制流差异。文本保存可审查的
    统一 diff，编译资源使用明确标记的二进制载荷；两者都必须匹配路径、品牌后基线与修改后摘要。
    全部预检和重放成功后才整体替换 `ok/`，不把完整 Python 后端复制进 `selfhost/`。

## 自有差异放在哪（重要）

上游跟踪的路径（`config.py`、`src/`、`pyproject.toml`、`pyappify.yml` …）尽量保持与上游一致，
**我们的配置覆盖和框架差异来源放进 `selfhost/`**；框架实现仍在 `ok/`，由差异清单持久保存其改动。
前台输入默认值、游戏／登录任务的恢复与旧任务撤出同时需要维护应用源码；这些正常源码差异参与上游合并，
发生冲突时按现有同步规则停止并人工审查。
理由：上游的 git 树里没有 `selfhost/`，所以
`git merge upstream/<tag>` 不会在这里产生任何冲突；反之，往 `config.py` 里插三行，
上游每改一次该文件我们就要手工解一次冲突。

`selfhost/` 的自有配置与源码如下：

| 文件 | 作用 | 何时生效 |
| --- | --- | --- |
| `overrides.py` | 运行时改 config dict：把上游取消注册的两个页面加回 `custom_tabs`，并改品牌项 | `main.py` 里 `OK(config)` 之前 |
| `framework_patches.py` | vendored `ok/` 源码的文字补丁清单（品牌标识） | `scripts/sync_ok.py` 每次内联时 |
| `logic_patches.py`、`logic_patches.json`、`patches/` | 严格逻辑重放器、支持版本及基线清单、文本差异和二进制载荷 | 品牌补丁之后、覆盖 `ok/` 之前 |
| `framework_tree.py` | 框架源码、差异来源及部署校验共用的树摘要算法 | 同步 / 校验时 |
| `ok_pin.json` | 框架版本、原始 wheel、补丁后树与差异集合摘要的权威来源 | 同步 / 校验时 |
| `runtime/host.c`、`runtime/host.rc` | 原生业务宿主及图标、产品名等 EXE 资源 | `scripts/build_runtime.ps1` 编译时 |
| `portable.py` | 当前部署快照、启动器证据、完整运行时和 incoming 包的校验及归档 | 普通编译与正式发布共用 |

启动器差异单独放在 `launcher/`：官方 Rust 与 Action 的固定来源、最小统一差异、NoNo 接收与
持久化模块，以及全部载荷的摘要都可审查。构建先校验固定来源，应用差异，再重编译 Action 的
`dist/index.js` 和实际 EXE，不能只修改未执行的 Action 源文件，也不能复用未验证的现成二进制。

当前 `selfhost/overrides.py` 负责把上游取消注册的两个页面（角色代码、多账号配置）加回
`config['custom_tabs']`。时序上必须在 `OK(config)` 之前——`OK.__init__` 按引用保存这个
dict，`MainWindow` 读的是同一个对象。

`tests/TestSelfhostSmoke.py` 覆盖框架 import 链、页面注册和实例化、品牌覆盖、原生宿主及严格差异重放。
逻辑差异用例只操作隔离树，覆盖文本与二进制、新增／修改／删除、重复施加、覆盖前漂移失败、
原始 wheel 摘要和部署一致性；它们不操作真实游戏或联系人。
上游每次发版后由 CI 执行，红了就不许合并——上游的测试测的是"没有我们补丁"的树，覆盖不到这些。

## 业务进程为什么显示 NoNo.exe

根目录的 `NoNo.exe` 是 pyappify 启动器。原先配置 `main_script: "main.py"` 时，它实际启动
`pythonw.exe main.py`，所以任务管理器里仍显示 Python。修改启动器的产品名只影响启动器自身。

现在发布流程先编译 `selfhost/NoNo.exe`，再随 `selfhost/` 推入更新仓库。`pyappify.yml` 的
`main_script` 改为 `selfhost/NoNo.exe`，启动器直接执行这个业务宿主。宿主加载同一便携目录中的
`python3.dll`，通过 `Py_Main` 在自身进程执行 `main.py`，因此业务进程的映像名和产品名都为
NoNo。图标复用 `icons/icon.ico`。这是 [CPython 官方说明的自定义宿主方式](https://docs.python.org/3.12/using/windows.html#the-embeddable-package)，
[pyappify 官方也支持把 EXE 配为入口](https://github.com/ok-oldking/pyappify/tree/v1.2.7)。

选择此方案是为了继续使用现有的源码更新、依赖管理和动态角色加载。PyInstaller 也能生成业务
EXE，但需要另外维护依赖收集和冻结后的更新机制。这里仍然分发整个便携目录，不能只复制
`selfhost/NoNo.exe`；它没有把 Python、Qt 和 OCR 模型压进单个文件。

宿主按自身位置定位源码和运行时，并统一业务工作目录。正常启动自动加入 `main.py`；计划任务
和提权重启的 `NoNo.exe main.py ...`、模块子进程的 `NoNo.exe -m ...` 也继续兼容。
`tests/TestSelfhostSmoke.py` 中的原生宿主用例验证实际进程身份、中文路径、资源加载、启动参数
和退出码。便携包生成后还会使用 setup 安装的真实运行时再次检查进程身份。

本地编译需安装 Visual Studio C++ 生成工具和 Windows SDK，在仓库根目录运行：

```powershell
.\scripts\build_runtime.ps1
```

构建产物 `selfhost/NoNo.exe` 不提交到源码仓库，只由发布流程推入部署树。便携包使用自建
v1.2.7 根启动器与原生业务宿主；快捷方式默认关闭，已有明确开启的设置保留。覆盖升级不创建、
删除或重建系统计划任务。更早仍指向旧 Python 的外部计划任务，需要用户按实际入口自行调整。
源码调试仍使用 Python；`main_updater.py` 是源码独立更新管理器，不是便携版必需入口。

除前台输入与任务恢复迁移外，工坊数据源也保留一处**有意偏差**：`src/gui/CharacterCodeTab.py:44-63`。
三个历史入口全断了——上游自建的 `cfokwwchar.ok-script.com`（DNS NXDOMAIN）、旧 Pages 的
`okwwcharcode.ok-script.com`（证书失效）、原仓库 `ok-oldking/ok-ww-char-code`（已删除，API 404）。
社区把工坊改成 raw.githubusercontent.com 托管（见该仓库 `scripts/generate.js` 的 `BASE_DOMAIN`），
因此我们把 `WORKSHOP_REPO` 指向自己的 fork `Canda15/ok-ww-char-code`：索引、按队详情、代码包
都来自这一个仓库。

`workshop_archive_url()` 负责把索引里的下载地址归一到这个仓库——因为索引不由我们生成：
我们 fork 的 `package.json` 里 `repository` 仍写父仓库 `yamadafz/ok-ww-char-code`，将来重跑
`generate.js` 会再次生成父仓库 URL；旧索引还可能带已下线的站点域名。归一只改 owner 与分支，
`codes/` 下的文件名保持原样（实测索引里 72 个文件名与我们是同一份）。

哪天上游自己修这几个常量会产生冲突：先看上游改成了什么，指向一个活着的源就取上游，
否则保留我们的。

## 不许做的事

- 不要 `pip install ok-script`：site-packages 会多出一份 `ok`，和仓库里的 `ok/` 抢 `sys.path`，
  症状是"改了 `ok/` 不生效"。
- 不要脱离 `ok/` 单独 pip-compile `requirements.txt`：ok 的依赖现在由 `pyproject.toml` 声明，
  框架升级时要两边同步改。

## 更新框架的流程

版本 pin 记在 `selfhost/ok_pin.json`。`wheel_sha256` 始终描述原始下载内容，`tree_sha256` 描述
品牌及逻辑差异全部重放后的有效 `ok/` 树，`logic_patches_sha256` 描述清单和全部载荷。日常只需要：

```powershell
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py'            # 优先取上游 pyproject 版本，没有声明才用 pin
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py' --version 3.0.5b7   # 明确固定已经审查的版本
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py' --dry-run  # 下载并验证重放，只报告差异
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py' --check    # 离线校验框架、基线及差异来源
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py' --check --root 'E:\deploy\NoNo'  # 示例：校验另一棵部署树
```

**版本以对方声明为准，不是以 pin 为准。** 上游每次发版都会在 `pyproject.toml` 里写
`ok-script[ocr,qt]==x.y.z`，那一行就是"这次的应用代码是按哪个框架写的"。合并之后如果继续按 pin
内联旧框架，就会出现 src/ 比 ok/ 新：冒烟测试只覆盖我们自己的页面，这种漂移它测不出来，只会在
某个功能被调用时炸。所以 `sync_ok.py` 先读出那一行再动手删它（顺序很重要：`--reapply` 一执行，
版本依据就没了），`--version` 只在需要固定到某个版本时用。

新版本还必须匹配逻辑清单的支持版本与精确基线；不匹配时在覆盖源码或执行 `--reapply` 前中止，
等待人工审查和重新收集，不能只改清单里的版本号。即使当前清单为空，也保留完整基线约束。

`sync_ok.py` 先从 PyPI 取 wheel，核对原始 SHA-256，隔离解包 `ok/`，依次施加品牌补丁和逻辑差异，
验证最终树后才**整树替换**，随后按 wheel METADATA 重写依赖块并更新 pin。逐文件覆盖会让上游删除的
模块残留，因此不采用。`--dry-run` 包括真实重放验证，但不修改源码、依赖、差异清单或 pin。

改 `pyproject.toml` 时有两处坑是踩过的，脚本里已经处理，改动那段逻辑时别丢：上游的 `dependencies`
数组最后一项**不带尾逗号**，生成块插在它后面会让整个文件变成非法 TOML（`Unclosed array`），而
pip 只在装依赖时才报错——测试全绿、包打不出来，所以写盘前会用 `tomllib` 解析一遍；上游的
`web-test` extra 只剩一条 `ok-script[web]==x`，这个 extra 在框架 3.0 里已经没有内容，留着只会在
被 cue 到时把 ok-script 从 PyPI 装进 site-packages、和内联的 `./ok` 并存，所以整块删掉。

补丁清单里的原文在新版本里找不到（或出现多次）时，同步会直接失败并要求人工核对：
宁可停下来，也不要静默地少打一个补丁——那种情况没有任何报错，只是名字又回来了。

### 维护框架逻辑差异

修改 `ok/` 后必须收集可重放来源，不能只更新 pin。完成同一轮源码及资源编辑后串行执行：

```powershell
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py' --record-patches --dry-run
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py' --record-patches
& '.\.venv\Scripts\python.exe' 'scripts/sync_ok.py' --check
```

收集命令重新校验原始 wheel，在隔离树施加品牌补丁，再比较当前完整 `ok/`。只有生成差异能够精确
重放为当前树时，才更新清单、载荷与 pin；它不覆盖当前 `ok/`，也不改写依赖文件。审查时同时检查
`ok/` 的源码 diff 与 `selfhost/patches/`，确认没有把意外改动登记成自有行为。

清单使用 `schema_version: 1`，顶层记录 `framework_version`、`baseline_tree_sha256` 和 `entries`。
每条记录含相对 `ok/` 的 `path`、`operation`（`add`／`modify`／`delete`）、`format`（`unified_diff`／`binary`）、
`payload` 及 `before_sha256`／`after_sha256`；缺失文件的摘要为 `null`。文本使用三行上下文且保留原换行，
二进制资源保存结果载荷，Python 源码禁止借二进制格式保存完整副本。删除也须有明确记录，目录里
未登记、缺失或路径不规范的载荷均使同步失败。重复重放可识别已经满足后摘要的文件。

离线检查逆向还原各条差异的基线文件摘要，并核对清单里的完整品牌后基线树，因此漏登记的文件
变化即使重新填写 `tree_sha256` 也不能通过。部署检查从指定根目录读取同一套 `ok/`、清单、载荷与 pin，
复用相同算法，仅延续既有 `__pycache__`、`.pyc`、`.pyo` 缓存例外，不排除源码或编译资源。

升级框架时先人工合并并审查新版 `ok/` 与自有逻辑，再用 `--record-patches --version <已审查版本>`
重建支持版本和基线；直接拿旧框架工作树与新版 wheel 比较可能把上游新增能力误登记为删除，不能
用收集命令替代这一步审查。清单、载荷与 pin 的写入不是磁盘事务；若因 IO 中断，校验会保持失败，
应修复来源文件并重新完整收集。

## 上游发版后由 CI 接手

`.github/workflows/sync-upstream.yml` 每 12 小时轮询上游最新 release（也可手动
`workflow_dispatch`，可指定 tag）：

1. 检出前固定 Git 为 LF，防止 Windows 将严格校验的补丁头和载荷改成 CRLF；
2. 上游标签保存在独立 `refs/upstream-tags/<tag>`。仅当其已合并、正式 Release 和完整 ZIP／校验附件均存在时跳过；缺发布或缺附件则继续，查询失败明确停止。需要合并时使用该独立引用；
3. 冲突只允许出现在 `pyproject.toml` / `requirements.txt`——我们删过那里的 ok-script pin，上游每次
   bump 版本都会改那两行，属于必然冲突且完全可脚本复现：取上游版本后由 `sync_ok.py --reapply` 重新删掉。
   其他文件冲突一律失败退出，交给人；
4. `python scripts/sync_ok.py --reapply`：在隔离树重放品牌与逻辑差异，成功后重建 `ok/`、重写依赖块；
5. 复用 `.github/scripts/install_dependencies.py` 安装依赖，再运行 `scripts/check_foreground.ps1`：
   同时核对完整框架来源和前台输入、通知、多账号、无音消除、公共恢复及自维护冒烟；
6. 全绿才 `git push origin HEAD:master`。首次创建指向适配提交的注释标签；重试复用已有标签，
   不强推或移动历史版本，再用 `gh workflow run build.yml --ref <tag> --repo <本仓库>` 显式派发打包。

`force` 只强制重跑检查及已有版本发布，不改变已有标签的提交。人工合并后缺少 Release 时，
默认运行也会补发，不再要求勾选 `force`。需要发布新代码时应使用尚未发布的新版本标签。

第 6 步的 `--repo` 不是可省的啰嗦：本仓库是 fork，`gh` 在 checkout 里会把"这个仓库"解析成**基仓库**
（上游），于是拿着只能访问自己仓库的 token 去派发上游的 `build.yml`，报
`HTTP 403: Resource not accessible by integration`。第一次无人值守同步就撞上了这个：master 和 tag
都推上去了，只有构建没开始，而这一步之前每一步都是绿的。同理 `permissions` 必须带 `actions: write`
——派发是 Workflow dispatch API，`contents: write` 不够。

第 5 步是这套自动化的关键：上游的 CI 测的是**没有我们补丁**的树，`custom_tabs` 被塞回去后两个页面
能不能在新框架下站起来、`kit.*` 的 API 有没有漂移，上游全绿也覆盖不到。红了就不推，等于把
"合并干净、构建成功、启动 ImportError"挡在发版之前。

标签必须沿用上游版本号本身（`v3.7.17`），不能加 `+xmod` 之类的后缀。约束来自启动器而不是
`ok/util/version.py`：当前 PyAppify 支持 `v?数字.数字.数字` 及可选 `[-.](alpha|beta|rc)[.数字]`，
`NoNo.exe -c setup --setup-version v3.7.17+xmod` 会直接报版本标签无效，便携包
在 setup 这一步就失败；而客户端拉版本列表用同一个正则，带后缀的标签也进不了列表，等于自更新
永远看不到新版本。因此 fork 首次创建同名标签时指向自己的适配提交，语义是"同一版本号 + 我们的补丁"；
上游原标签使用独立引用，不能覆盖本仓库已经发布的标签。

第 6 步的显式派发同样不是可选项：用 `GITHUB_TOKEN` 推的 tag 不会触发 push 类工作流
（GitHub 禁止 token 递归触发），不派发就会出现"源码合了、包没编译"。

### 在 Actions 页面手动点哪个

| 想做的事 | 点哪个工作流 | 说明 |
| --- | --- | --- |
| 现在就要跟上上游最新 release | **Sync upstream release** | 自动合并、校验并发布；已合并但缺 Release／完整附件时仍会补发，已完整发布才跳过。`force` 可强制重跑 |
| 只是重新打一次已有的版本 | **Windows 发布** | 必须把 `Branch` 下拉切到对应 **tag**；选分支会在第一步就报错（版本号取自 ref 名，`master` 不是合法版本） |
| 编译普通源码提交并下载便携包 | **Windows 编译检查** | master 推送自动运行，也可手动派发；生成完整 NoNo-win32-portable.zip，不要求发布 tag |
| 重新编启动器 | **构建 NoNo 启动器** | 固定官方来源并应用 NoNo 差异；先产出候选，发布需显式授权 |

`Windows Release` 不能替代 `Sync upstream release`：它不查上游、也不合并，只会拿当前 ref 的
源码打包，用的是 `pyappify.yml` 里 `git_url` 指向的镜像仓库里已有的那份部署树。

### 自维护工作流

上游仓库面向社区，带了一堆对我们无意义的自动化。自维护源码保留以下工作流：

| 保留 | 作用 |
| --- | --- |
| `Windows Release` | tag 一推就打包（由 `Sync upstream release` 派发） |
| `Sync upstream release` | 12 小时轮询上游 release |
| `构建 NoNo 启动器` | 手动重编候选启动器；默认只上传工件，显式发布使用新的 launcher-2 |
| `Windows 编译检查` | master 推送验证完整框架及前台核心，编译原生业务 EXE 并上传对应 SHA 的产物 |

其余全部在 GitHub 侧 `disabled_manually`：`Documentation`、`close_stale_issues`、
`issue-triage.lock`、`pull-request-review.lock`、`sync_issue_labels`、`sign_exe`、
`mirrorchyan_uploading`、`mirrorchyan_release_note`、`test`。它们要么需要上游的
`COPILOT_GITHUB_TOKEN` / SignPath 证书 / MirrorChyan 账号，要么面向 issue、PR、Pages 这些
本仓库没有的东西（`has_issues` 是关的）。

只改 GitHub 侧状态、不删文件：删掉上游跟踪的文件，等于在每次上游改动它时制造一次
"deleted by us" 冲突。万一上游合并把这些工作流改回 `active`（比如改了 YAML 里的触发条件），
重停一次即可：Actions 页面左侧选中该工作流 → 右上角 `...` → `Disable workflow`。

### master 编译与前台核心门禁

`.github/workflows/windows-compile.yml` 对每次 master 推送运行，不使用分支名生成发布版本，也不写入
更新镜像。顺序为安装现有依赖 → `scripts/check_foreground.ps1` → `scripts/build_runtime.ps1` →
共享 `.github/actions/build-nono-launcher` 构建并核验启动器 →
`python -m selfhost.portable --source-sha <本次 SHA> --launcher-exe build/launcher-dist/NoNo.exe` → 上传完整便携包。
仓库权限仅为 `contents: read`，检出固定到本次 SHA，测试进程不继承 CI 凭据。

`check_foreground.ps1` 默认先运行 `sync_ok.py --check`，再通过既有 `run_test.py` 执行集中输入、桌面通知、
多账号、无音消除、一次性战斗、资源恢复、角色识别、Lily、绯雪皮肤、深塔和自维护测试。
每个文件使用独立进程。`run_test.py` 的父进程创建临时用户目录，持有到测试进程完全退出；
在任何框架导入前设置 `NONO_USER_DATA_ROOT`、`OK_DISABLE_FILE_LOG=1`，并明确配置、截图和日志路径。
不能仅替换 `config_folder`：截图测试会强制开启 debug，旧框架会清空默认截图目录。
父进程在测试进程完全退出后检查后台线程错误标记，不能仅凭 unittest 的成功字样判定通过。
开发期间可以用 `-TestsOnly` 验证尚在编辑的源码；CI 调用默认完整门禁，
只有最终差异收集后框架检查通过才能继续编译。

`build_runtime.ps1` 继续负责唯一宿主编译入口和原生进程验证。之后共享打包器生成
`build/portable-dist/NoNo-win32-portable.zip` 与 `NoNo-win32-sha256.txt`，Actions 工件名为
`NoNo-portable-windows-<本次完整 SHA>`，保留 14 天；运行摘要提供完整便携包的下载链接。
ZIP 内是 `NoNo/` 目录，仅直接投递根启动器和 `data/incoming/NoNo/` 下的完整包。
incoming 包包含裸 Git 仓库、部署代码、原生业务宿主、Python、依赖及文件清单；
首次双击根 `NoNo.exe` 时，接收守卫验证并安装为 `data/apps/NoNo/` 下的正式目录。
GitHub Actions 的工件下载容器内含上述 ZIP 和校验文件；正式 Release 的资产仍直接叫
`NoNo-win32-portable.zip`。业务宿主仍在自己的进程执行原有 `main.py` 和内联框架。

普通编译先运行一次官方 setup，取得真实正式更新锚点及运行环境，再按已跟踪文件与
`deploy.txt` 白名单构造本次 working 快照。随后通过新增的
`--nono-install-working <manifest> -p China` 调用官方依赖安装后半段，安装当前 requirements，
严格保存 App 状态；不能再调用可能重新检出或覆盖 working 的 setup。
裸仓库保留真实正式锚点，当前源码仅部署到 working；原生宿主、内联 PyAppify 和忽略规则有明确生成来源。
正式标签模式仅执行一次真实带标签 setup，并保留官方 alpha、beta、rc 标签格式；普通快照仍使用稳定锚点。
两种模式都校验 working、框架来源、真实便携 Python 与原生宿主，
再生成带完整大小和 SHA-256 清单的 incoming 包；版本查询不能替代真实安装。
`tests/`、`ok_templates/`、未跟踪的账号配置、缓存和运行日志不会成为部署快照。

`build-info.json` 与接收 manifest 分开记录本次 `source_sha`、正式更新底座的版本和提交、
working 文件摘要、框架 pin，以及启动器来源证据。Git 锚点不能冒充源码提交，普通构建不创建
临时远端标签、不改镜像标签。接收后的第一次启动有独立更新检查保护，避免旧错误状态或
“当前版本缺失”路径立刻检出镜像并覆盖当前快照；用户明确保存的自动更新策略继续保留。

提交后的验收按本次推送 SHA 查找这个工作流，确认核心检查、编译和上传均成功，不能用旧 SHA 的成功
代替本次结果。`GITHUB_TOKEN` 产生的推送不会递归触发 push 工作流，既有上游同步仍通过显式派发标签
发布完成编译交付。[GitHub 的触发规则](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/triggering-a-workflow)

Windows Release 保留全仓库测试与镜像发布链路，打包步骤调用同一
`python -m selfhost.portable --release-tag <tag>`。正式模式按该 tag setup 镜像部署树，不覆盖为普通快照；
两种模式都使用 `sync_ok.py --check --root` 核对 pin、逻辑载荷、完整框架和生成资源，再验证真实运行时中的
原生进程。缺少运行环境、错误的部署内容或字节漂移都会在上传便携包前失败。

### 直接覆盖升级与用户数据

先退出正在运行的 NoNo，将新 ZIP 中 `NoNo/` 的内容覆盖到原 `NoNo/` 目录，再启动根 `NoNo.exe`。
无需安装器、手工删除旧 repo 或重新设置账号。压缩包不能直接包含正式 `app.json`、全局启动器
配置、用户目录或 live repo／working／python，否则解压时会先覆盖旧数据，启动守卫无法保护。

| 目录 | 用途 |
| --- | --- |
| `data/incoming/NoNo/` | 新包与选择清单，启动前逐文件核验 |
| `data/apps/NoNo/` | 接收后的裸 repo、working、Python 与 App 状态 |
| `data/user/NoNo/` | 稳定用户配置、任务开关、账号数据、快捷键、自定义任务、插件和素材 |
| `data/state/NoNo/` | 接收事务、收据、首次更新检查保护及运行租约 |
| `data/backups/NoNo/` | 旧目录原件、未知文件和冲突副本 |

根启动器在任何 App／CLI 读取前接收包：验证来源、文件大小和摘要、真实 Git 锚点及 Python，
保护旧原件，准备同卷新目录，再按持久化事务整体交换。每次移动先保存意图，重复启动按实际目录
身份恢复；失败不启动半安装目录。新包选择变化时先处理上一事务，不混用两份载荷。
同一包已接收后不会再次播种状态或重置用户后来修改的偏好。

旧 working 用户目录自动迁到 `data/user/NoNo`，已有稳定目录优先，两份冲突均保留。
明确指向便携目录外的截图路径不移动、不跟随、不删除。未知配置键与嵌套内容保留，缺项补默认值；
损坏 JSON 明确失败并保留原字节，保存采用原子写，日志不输出账号、配置值或完整用户对象。
严格读取拒绝 NaN、Infinity 和 -Infinity，写入也拒绝非有限数值，不能重新序列化覆盖坏原件。
普通双击启动的接收失败显示固定中文提示；构建 CLI 保留标准错误和非零退出，不弹出桌面窗口。
运行时键 `NONO_USER_DATA_ROOT` 连接所有消费者；源码运行未设置此键时沿用旧路径。

### 更新仓库：部署树镜像 `Canda15/nono-update`

`pyappify.yml` 的 `git_url` 指向的不是源码仓库，而是 `Canda15/nono-update`——一个只含
部署树的镜像仓库（与上游的 `ok-ww-update` 同构）。它原名 `ok-ww-update`，改名应用时一并
改成中性名；GitHub 对旧名保留重定向，所以改名之前装出去的客户端仍能照常更新。

原因是启动器的行为：`ok-ww.exe -c setup` 会把 `git_url` 指向的仓库**整棵克隆**到
`data/apps/NoNo/repo`，这棵树随后原样进了便携包。
直接指向源码仓库时，光 `ok_templates/` 就有 839 MB（工作树）、`tests/` 124 MB，便携包会白白
胖出近 1 GB；同一棵树上还有 180+ 字符的素材文件名，让 libgit2 报 `path too long`
（默认 `core.longpaths = false`，`build.yml` 的 `package` job 保留了
`git config --global core.longpaths true` 作为防御）。
镜像仓库只有部署树，两件事一起消失。

于是 `build.yml` 的 `synchronize` job 用 `ok-oldking/partial-sync-repo@master` 按 `deploy.txt`
白名单把文件复制到镜像仓库（源里不存在的项从目标删除）、提交、把同名 tag 重新打到新提交上并强推。
认证用 runner 上的 `secrets.UPDATE_TOKEN`（带 repo 权限的 PAT，已写入仓库 secret；action 不接受
token 输入，所以凭据嵌在 `repos` 的 URL 里）。

两侧的关系：

| 仓库 | 作用 |
| --- | --- |
| `Canda15/ok-wuthering-waves` | 源码、CI、tag 与 Release 产物 |
| `Canda15/nono-update` | 便携包与客户端自更新实际克隆的树（只有 `deploy.txt` 白名单内容） |

`deploy.txt` 因此是"发布内容"的唯一权威：新增一个需要随包下发的目录（比如 `selfhost`），
必须同时加进 `deploy.txt`，否则该目录不会出现在镜像里，运行时 `ImportError`。

## 产物与启动器构建

每次发布只有一个能用的包：

| 资产 | 用途 |
| --- | --- |
| `NoNo-win32-portable.zip` | 便携版，解压即用，整个目录可以直接搬走 |
| `NoNo-win32-sha256.txt` | 便携包的 SHA-256 |

新启动器基于官方 v1.2.7 固定提交，保留官方 Git／版本模型，添加 NoNo 接收与严格持久化。
旧 `launcher-1` 资产保留，不能将新裸库包交给旧 v1.2.4 启动器。
新的 `launcher-2` 固定发布与应用版本分开，未经发布验证不作为默认下载来源。
普通 CI 直接构建候选并交给打包器，因此不依赖尚未发布的固定资产。

在仓库根目录构建：

```powershell
.\scripts\build_launcher.ps1
```

默认产出 `build/launcher-dist/NoNo.exe` 与 `launcher-build.json`。可指定 `-OutputDirectory`；
`-PrepareActionOnly` 仅验证并准备固定 Action、应用 hook 和重建 `dist/index.js`，不能代替 EXE 编译。
启动器证据关联官方提交、差异载荷和二进制摘要，并核验 v1.2.7、NoNo 品牌、图标与 UAC 资源。

打包器接受 `--launcher-exe <候选>`，默认读取候选旁的 `launcher-build.json`；
`--launcher-proof` 可明确指定证据。没有候选时要求先构建；固定发布下载必须显式使用
`--published-launcher`，不能回退到旧包或应用发布页的 `latest`。

便携目录的路径相对根 EXE，不写注册表或 `%APPDATA%`。根守卫先接收根配置，再交给官方读取链，
避免缺少根配置时误用上游默认源。启动器是 GUI 子系统 EXE，构建 CLI 用
`Start-Process -WindowStyle Hidden -Wait -PassThru` 或等效等待方式，必须检查实际退出码。

新版每次 CI 包含启动器原生编译，冷构建耗时不能沿用旧 `launcher-1` 下载复用流程的历史数字。
包体与耗时以对应 SHA 的实际 Actions 记录为准。便携目录每次源码与 manifest 都会变化，
不复用旧 ZIP；依赖缓存不能替代完整来源及运行时检查。

## 尚未完成

- 构建参数（`pip_args`、`PYTHONOPTIMIZE`、`opencv-python-headless` 等）保持上游默认。评估过
  后两项：收益是包体/启动速度的百分比级，代价是 `requirements.txt`（上游也在改的文件）上多一条
  需要长期维护的合并覆盖规则，不划算。
- SignPath 相关步骤已从 `build.yml` 删除（上游证书绑 ok-oldking 组织，自维护分支没有也不需要）。
  代价是产物未签名，首次运行时可能被 SmartScreen 提示；要签名得换成自己的证书。
- `secrets.UPDATE_TOKEN` 是有期限的 PAT，过期后 `synchronize` job 会推不动镜像仓库、打包会失败。
  换 token 时同时更新 secret 即可，仓库里没有任何硬编码凭据。
- fork 的 schedule 在仓库 60 天无活动后会被 GitHub 自动禁用，手动跑一次即可恢复。

## 改名的边界

改的是"程序自己报出的名字"：exe 文件名、解压目录、窗口标题与任务栏、关于页标题、
日志文件名、通知名、计划任务文件夹（`\NoNo`）、诊断包名、发布资产名。

改不了或刻意不改的：

- **exe 内嵌的 PE 版本信息**（文件属性/任务管理器里的产品名）由启动器编译时写入，已由
  `.github/workflows/build-launcher.yml` 自建启动器解决：用本仓库的 `pyappify.yml` 编一次，
  tauri 会把 `productName` 等字符串全部换成 `NoNo`，工作流里还会复核。
- **通知 app_id 前缀** `ok-script.`、通知/叠层窗口的 Win32 类名、`%TEMP%` 下的
  `ok-script-*.pid`、计划任务 XML 的作者、邮件通知的默认主题：这些都在 `ok/` 框架里硬编码，
  已由 `selfhost/framework_patches.py` 改成中性名或 `NoNo`（见上文"落地改动"第 11 条）。
  刻意不动的只剩"只在本机自己看得到"的那几个：日志首行的 `ok-script init ...`、MCP 服务名、
  skills 的 `ok-script://` scheme。
- 关于页"其他项目"列表（`ok/ui/qt/about/AboutTab.py:212-232`）是框架硬编码的，其中仍列有
  上游项目——属于对原作者的署名，保留。
- `pyproject.toml` / `setup.py` 的分发名、README 与 docs、i18n（所有 `.po`/`.mo` 里没有
  `OK-WW` 这个 msgid，窗口名来自 config，所以不用重编翻译）。

对新旧安装的影响：

- 已使用 NoNo 的便携目录按上文直接覆盖，旧用户数据自动保护并迁移到稳定目录。
- 更早名为 `ok-ww` 的应用不会自动改名，本轮接收协议只处理 `data/apps/NoNo`。
- 系统计划任务和用户显式开启的快捷方式偏好保留；自动创建快捷方式的默认值仍为关闭。

## 许可

`ok/` 与 `src/` 均为 AGPL-3.0（见 `LICENSE.txt`）。自用、不对外分发时不产生额外义务；
一旦对外分发，修改后的框架代码同样需要以 AGPL-3.0 开源。
