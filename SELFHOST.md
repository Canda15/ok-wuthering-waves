# 自维护单仓说明

本仓库是 ok-ww 的自用分支。与上游最大的差别：**框架库 ok-script 不再作为 PyPI 依赖，而是以源码形式内联在 `./ok`**。

## 为什么改成内联

上游的做法是：开发时 `pip install ok-script`，打包时由 `ok/update/inline_ok_requirements.py` 把
site-packages 里的 `ok` 复制进部署树，再由 `pyappify.yml` 的 `git_url` 把**整棵部署树**推给用户当更新包。

也就是说，只要我们要改框架代码（而这里的目标正是如此），改动能否进更新包，取决于每次发布有没有人记得跑那条 inline 命令；
一旦忘了，包里的 `ok` 会静默回退成 PyPI 版本，症状是"改的代码本地有效、更新后失效"。
内联之后 `ok/` 就是普通源码，随仓库提交、随更新树分发，不再有这条隐式步骤。

## 当前内联版本

| 项 | 值 |
| --- | --- |
| 版本 | `ok-script` 3.0.1 |
| 权威来源 | `selfhost/ok_pin.json`（版本、wheel SHA-256、整树 SHA-256，由 `scripts/sync_ok.py` 维护） |
| 来源 | PyPI wheel `ok_script-3.0.1-py3-none-any.whl`（1081899 字节） |
| SHA-256 | `b170babcc3cda3e0014ad02dbf890b29a32b4a52e39eddade0b03dda5c317531` |
| 内联范围 | 仅 wheel 内的 `ok/` 包，共 278 个文件 |
| 未内联 | wheel 另外三个顶层内容：`diagnostics/`、`examples/`、`ok_script-3.0.1.dist-info/`。前两个 `ok/` 内没有任何引用，dist-info 只用于包元数据，`ok/` 也没有 `importlib.metadata` 之类的读取 |

内联内容和上游打 3.0.1 wheel 时用的源码一致，但**没有 git 历史**（本机沙箱禁止 `sh.exe` 创建命名管道，
`git subtree` 无法运行，所以采用"下载 wheel 解包后入库"的方式）。

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
9. `build.yml` 的 `package` job 不再调用 `ok-oldking/pyappify-action`，改为"下载上一次发布里的
   启动器 → 跑一次 setup → 打成便携 zip"，并删掉 SignPath 签名与 NSIS 安装包那条链路
   （详见"产物与构建耗时"）。

## 自有差异放在哪（重要）

上游跟踪的路径（`config.py`、`src/`、`pyproject.toml`、`pyappify.yml` …）尽量保持与上游一致，
**我们的改动一律放进 `selfhost/`**。理由：上游的 git 树里没有 `selfhost/`，所以
`git merge upstream/<tag>` 不会在这里产生任何冲突；反之，往 `config.py` 里插三行，
上游每改一次该文件我们就要手工解一次冲突。

当前 `selfhost/overrides.py` 负责把上游取消注册的两个页面（角色代码、多账号配置）加回
`config['custom_tabs']`。时序上必须在 `OK(config)` 之前——`OK.__init__` 按引用保存这个
dict，`MainWindow` 读的是同一个对象。

`tests/TestSelfhostSmoke.py` 是为这个补丁面准备的三条冒烟断言（框架 import 链、注册键仍被
`MainWindow` 消费、两个页面能在 offscreen 下实例化）。上游每次发版后由 CI 执行，红了就不许
合并——上游的测试测的是"没有我们补丁"的树，覆盖不到这些。

## 不许做的事

- 不要 `pip install ok-script`：site-packages 会多出一份 `ok`，和仓库里的 `ok/` 抢 `sys.path`，
  症状是"改了 `ok/` 不生效"。
- 不要脱离 `ok/` 单独 pip-compile `requirements.txt`：ok 的依赖现在由 `pyproject.toml` 声明，
  框架升级时要两边同步改。

## 更新框架的流程

版本 pin 记在 `selfhost/ok_pin.json`（版本、wheel SHA-256、整树 SHA-256）。日常只需要：

```powershell
python scripts/sync_ok.py            # 按 pin 重新下载 wheel → 校哈希 → 整体替换 ok/ → 重写依赖块
python scripts/sync_ok.py --version 3.0.2
python scripts/sync_ok.py --dry-run  # 只看差异
python scripts/sync_ok.py --check    # 离线校验 ok/ 未被手改
```

`sync_ok.py` 做五件事，替代了原先的手工步骤：按 pin 从 PyPI 取 wheel、核对 SHA-256 与 pin 一致
（不一致直接中止，这是供应链断点）、把 wheel 内的 `ok/` **整树替换**（不是逐文件覆盖，否则上游删掉的
模块会以旧文件形式残留）、按 wheel METADATA 重写 `pyproject.toml` 的依赖生成块、更新 pin。

手改了 `ok/` 里的某个文件时，`--check` 会红。这不等于是错误，但要求把新哈希登记回 pin，
免得下次同步把改动悄悄覆盖掉。

## 上游发版后由 CI 接手

`.github/workflows/sync-upstream.yml` 每 12 小时轮询上游最新 release（也可手动
`workflow_dispatch`，可指定 tag）：

1. tag 已在 master 里 → 什么都不做；
2. `git merge --no-ff <tag>`；
3. 冲突只允许出现在 `pyproject.toml` / `requirements.txt`——我们删过那里的 ok-script pin，上游每次
   bump 版本都会改那两行，属于必然冲突且完全可脚本复现：取上游版本后由 `sync_ok.py --reapply` 重新删掉。
   其他文件冲突一律失败退出，交给人；
4. `python scripts/sync_ok.py --reapply`：重建 `ok/`、重写依赖块；
5. `python -u run_test.py tests/TestSelfhostSmoke.py`（`QT_QPA_PLATFORM=offscreen`）；
6. 全绿才 `git push origin HEAD:master`，再把上游 tag 改指到本次合并提交并强推，然后
   `gh workflow run build.yml --ref <tag>` 显式派发打包。

第 5 步是这套自动化的关键：上游的 CI 测的是**没有我们补丁**的树，`custom_tabs` 被塞回去后两个页面
能不能在新框架下站起来、`kit.*` 的 API 有没有漂移，上游全绿也覆盖不到。红了就不推，等于把
"合并干净、构建成功、启动 ImportError"挡在发版之前。

标签必须沿用上游版本号本身（`v3.7.17`），不能加 `+xmod` 之类的后缀。约束来自启动器而不是
`ok/util/version.py`：`pyappify` 认标签的正则是 `^v?\d+\.\d+\.\d+(-alpha|beta|rc\.\d+)?$`，
`ok-ww.exe -c setup --setup-version v3.7.17+xmod` 会直接报 `Invalid setup version tag`，便携包
在 setup 这一步就失败；而客户端拉版本列表用同一个正则，带后缀的标签也进不了列表，等于自更新
永远看不到新版本。所以 fork 里同名标签被改指到自己的合并提交，语义是"同一版本号 + 我们的补丁"。

第 6 步的显式派发同样不是可选项：用 `GITHUB_TOKEN` 推的 tag 不会触发 push 类工作流
（GitHub 禁止 token 递归触发），不派发就会出现"源码合了、包没编译"。

### 更新仓库：部署树镜像 `Canda15/ok-ww-update`

`pyappify.yml` 的 `git_url` 指向的不是源码仓库，而是 `Canda15/ok-ww-update`——一个只含
部署树的镜像仓库（与上游的 `ok-ww-update` 同构）。

原因是启动器的行为：`ok-ww.exe -c setup` 会把 `git_url` 指向的仓库**整棵克隆**到
`data/apps/ok-ww/repo`，这棵树随后原样进了便携包。
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
| `Canda15/ok-ww-update` | 便携包与客户端自更新实际克隆的树（只有 `deploy.txt` 白名单内容） |

`deploy.txt` 因此是"发布内容"的唯一权威：新增一个需要随包下发的目录（比如 `selfhost`），
必须同时加进 `deploy.txt`，否则该目录不会出现在镜像里，运行时 `ImportError`。

## 产物与构建耗时

每次发布只有一个能用的包：

| 资产 | 用途 |
| --- | --- |
| `ok-ww-win32-portable.zip` | 便携版，解压即用，整个目录可以直接搬走 |
| `ok-ww-win32.zip` | 启动器（4.7 MB，里面只有 `ok-ww.exe`），同时是下一次构建的底座 |
| `win32_sha256.txt` | 便携包的 SHA-256 |

便携版由 `package` job 自己拼，不再经过 `ok-oldking/pyappify-action`：
下载上一次发布里的启动器 → 把 `pyappify.yml` 放在 exe 旁边 → 跑一次
`ok-ww.exe -c setup -p China --setup-version <tag>` → 删掉 `cache/` 与 `data/logs/` → 打包成 zip。

之所以成立：启动器的路径全部相对自身所在目录（`utils/path.rs` 用 `current_dir`，
`lib.rs` 启动时强制 `set_current_dir(exe_dir)`），既不写注册表也不写 `%APPDATA%`，
NSIS 安装包只是把这个目录复制到 Program Files——那我们直接发这个目录就行。
`pyappify.yml` 必须和 exe 放在一起：启动器在 cwd 里读它（`app.rs:198 read_embedded_app`），
读不到就退回 pyappify 自带的默认配置，setup 会跑去克隆上游。

| 阶段 | v3.7.17（旧流程，两个 profile） | 现在（单 profile + 便携包，待实测） |
| --- | --- | --- |
| validate（checkout / pip install / 跑测试） | 3.1 min | 3.1 min |
| synchronize（推镜像仓库） | 1.6 min | 1.6 min |
| package | 10.9 min | 预估 3~5 min |
| publish（上传 Release 资产） | 0.6 min | 0.6 min |
| **合计** | **16.2 min** | **预估 8~10 min** |

旧流程的 `package` 里 7.7 分钟是 makensis：每个 profile 都要把 224 MB 载荷压成一个 NSIS
离线安装包。现在没有 NSIS，剩下的成本只有 setup（克隆镜像仓库 + pip 装依赖）和一次压缩。
更早还有一版 29.3 分钟，那时 `package` 里含一次 9m17s 的 Tauri 冷编译，启动器复用之后
这段也不再出现。

启动器二进制仍然是冻结的（一直是首次发布的那份）：它与版本号无关（版本由 setup 按 tag
生成），但要重建就得本地跑一次 `pnpm tauri build`，或者临时把 `BASE_EXE_URL` 换成上游
release 里的 `ok-ww-win32.zip`。

能缓存的只有 pip 依赖：三个 job 的 `actions/setup-python` 都带 `cache: pip`。
便携目录每次内容都变（版本号、源码），压缩产物没有缓存价值。

## 尚未完成

- 构建参数（`pip_args`、`PYTHONOPTIMIZE`、`opencv-python-headless` 等）保持上游默认。评估过
  后两项：收益是包体/启动速度的百分比级，代价是 `requirements.txt`（上游也在改的文件）上多一条
  需要长期维护的合并覆盖规则，不划算。
- SignPath 相关步骤已从 `build.yml` 删除（上游证书绑 ok-oldking 组织，自维护分支没有也不需要）。
  代价是产物未签名，首次运行时可能被 SmartScreen 提示；要签名得换成自己的证书。
- `secrets.UPDATE_TOKEN` 是有期限的 PAT，过期后 `synchronize` job 会推不动镜像仓库、打包会失败。
  换 token 时同时更新 secret 即可，仓库里没有任何硬编码凭据。
- fork 的 schedule 在仓库 60 天无活动后会被 GitHub 自动禁用，手动跑一次即可恢复。

## 许可

`ok/` 与 `src/` 均为 AGPL-3.0（见 `LICENSE.txt`）。自用、不对外分发时不产生额外义务；
一旦对外分发，修改后的框架代码同样需要以 AGPL-3.0 开源。
