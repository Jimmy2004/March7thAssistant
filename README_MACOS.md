# 三月七小助手：Apple Silicon 终端版

目标平台为 Apple Silicon Mac、国服官服云·星穹铁道。正常任务在无窗口模式运行，终端显示菜单与日志。需要登录时自动打开临时浏览器供扫码，手机确认登录后自动关闭窗口并回到后台；调试运行可保持浏览器可见。

本分支正在移植中，尚不等同于 Windows 全功能版。锄大地已接入原生云游戏截图和输入，自动剧情使用纯终端引擎；真实账号整轮任务仍需验收。外部模拟宇宙和抽卡记录尚待移植。

## 安装

需要 Git、原生 ARM64 Python 3.12，以及可连接国服云游戏的网络。使用原生终端，避免以 Rosetta 运行 Python。

```sh
git clone --branch macos-terminal https://github.com/Jimmy2004/March7thAssistant.git
cd March7thAssistant
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-macos.txt
python terminal.py
```

首次使用可能自动下载专用 Chrome for Testing 和对应驱动。无需安装 Qt 或打开原 Launcher。

## 使用

首次启动在终端逐项配置。输入 `b` 返回上一题；菜单中输入 `0` 返回。完成初始化后，用数字选择执行一次、每日指定时间运行、修改设置、调试运行或查看设置。运行时按 Ctrl+C 停止并关闭云游戏。

游戏有效画面始终固定为 **1920×1080，缩放比例 1**，不随 Mac 屏幕、Retina 缩放或调试窗口大小改变。菜单 **3 运行设置 → 2 云游戏画质** 可选择高画质、省流画质、极致省流三档，对应云游戏 `graphicsMode` 0/1/2；每次启动都会加载所选档位，保留登录资料和其它网页偏好。

### 锄大地

进入 **3 运行设置 → 3 锄大地**，选择路线、优先星球、队伍和特殊物品领取；每级输入 `0` 或 `b` 返回。默认路线要求 **1 号位为普攻远程角色**；黄泉路线要求 1 号位为黄泉。对应地图必须已解锁、机关状态符合上游路线要求。

- 选择「执行一次」中的 `fight` 可直接开锄，也可为 `fight` 设置每日运行时刻。
- 设置中的「启用锄大地」控制是否纳入「完整运行」；单独执行 `fight` 不受此开关影响。
- 默认运行选定路线版本的全部地图；高级设置可指定起始地图并开启单地图模式。建议首次先试一张。
- 支持默认、精简、黄泉、秘技和特殊物品路线。`technique` 为上游已停止维护的历史路线。
- 自动切换队伍、购买代币/过期邮包、购买秘技零食、领取记忆代币均可单独设置。特殊领取总开关关闭时，不执行对应购买/领取路线。
- 首次使用自动下载固定版本的 Fhoe-Rail 路线、图片和识图代码并校验 SHA-256；也可提前选择子菜单 `11` 下载。下载后保留 GPL 许可证，丢弃 Windows 程序、桌面驱动和图形界面。
- 全部截图和键鼠操作发往项目专用云浏览器。无窗口视角通过浏览器相对输入和小地图方向校准；不申请桌面录屏或辅助功能权限。
- 单图默认 15 分钟超时，整轮默认 12 小时，可调整整轮时限。传送、交互、机关或视角校准失败会报错；成功、重试耗尽或 Ctrl+C 后关闭项目专用云游戏。

```sh
# 通过本机启动器运行一次
march7th --task fight

# 显示浏览器检查路线
march7th --task fight --debug

# 每天按 Mac 本地时间运行（终端进程须保持运行）
march7th --task fight --schedule 08:00

# 从源码目录执行的等价命令
python terminal.py --task fight
```

本地已通过五种真实路线引擎加载、云浏览器相对输入及退出/超时测试；**尚未完成真实账号整条路线的跑图和奖励验收**。更多实现与验收说明见 [锄大地说明](docs/macos/OVERWORLD.md)。

账号未登录或登录失效时，程序自动打开专用浏览器并切换到官网二维码登录。用米游社 App 扫描网页二维码，在手机上确认；程序检测到登录成功后自动关闭窗口，以同一独立浏览器资料目录返回无窗口模式，并验证登录状态后继续任务。已登录时不弹窗。二维码过期可在网页刷新，登录等待超时或中断会关闭窗口。调试模式下浏览器保持可见。

```sh
# 不启动游戏的帮助与任务列表
python terminal.py --help
python terminal.py --list

# 初始化后，直接运行每日实训
python terminal.py --task daily

# 仅此轮显示浏览器，便于调试
python terminal.py --task daily --debug

# 每天在 Mac 本地时区的指定时间运行
python terminal.py --task daily --schedule 08:00,20:00
```

定时运行需要终端进程保持运行、Mac 保持唤醒。锁屏与休眠不同：真实休眠期间无法执行任务。错过的多个时间点不会密集补跑。每次任务结束或重试耗尽后关闭本项目云游戏浏览器；等待下次计划时不占用游戏会话。不会关闭普通 Chrome 窗口。

### 差分宇宙循环

选择 **1 执行一次 → 15 差分宇宙循环**，或直接运行：

```sh
march7th --task divergentloop

# 显示浏览器调试
march7th --task divergentloop --debug

# 从源码目录运行
python terminal.py --task divergentloop
```

每轮完成后自动开始下一轮，不设次数上限。按 **Ctrl+C** 停止并关闭项目专用云游戏浏览器。某轮失败时，按运行设置中的重试次数重新启动；重试耗尽后停止并关闭游戏。原有「差分宇宙」仍只执行一轮。

`config.yaml` 保存任务参数；`settings/terminal.json` 保存终端偏好和计划。任务菜单还提供自动剧情和保存云游戏截图。需要细调体力、队伍、奖励和推送时，可按 `assets/config/config.example.yaml` 的注释修改 `config.yaml`。默认禁止使用付费时长。

## 更新

每次正常启动，先检查官方 `moesnow/March7thAssistant` 的 `main` 源码更新。发现新提交后显示提示，输入 `Y` 才下载并合并，`N`、回车、`0` 或 `b` 跳过。也可选择菜单 **7 检查更新**，或运行：

```sh
python terminal.py --check-update
```

只合并云游戏代码、国服图片、识别模型和任务配置；Windows 安装包、图形界面、多余语言图片及本地游戏工具不写入项目。Mac 终端入口、更新器、依赖清单和个人配置保持独立；已有 Mac 修改通过三方合并保留。配置、登录状态、浏览器资料和日志不会被上游包覆盖。

更新先暂存、检查代码与依赖，再整体替换。冲突或新增不兼容依赖会中止，原文件保留，并显示待处理路径；写入失败会回滚。备份和恢复记录位于 `settings/updates/`。更新成功后退出，请重新启动程序。运行任务时禁止同时更新；无交互终端只提示更新，不擅自下载。帮助和任务列表不联网。

此入口同步官方源码；Mac 分支自身的改造更新仍通过 `git pull` 获取。不会下载或运行 Windows 的更新程序。

## 范围

- 日常、体力、奖励、货币战争、内置差分宇宙和三种高难挑战复用现有云游戏自动化，仍需逐项实机测试。
- 锄大地复用固定版本 Fhoe-Rail 的地图、识图、移动和自动战斗逻辑，通过原生云浏览器适配层执行。整条真实路线仍待实机验收。
- 模拟宇宙外部引擎使用 Windows 输入、桌面截图和本地窗口检测，尚未适配，不在当前可执行菜单中冒充可用。
- 抽卡记录目前需要迁移本地缓存取链接方案。自动剧情使用单线程轮询及云游戏截图/输入，不依赖 Qt；按 Ctrl+C 停止。
- Windows 本地游戏专属的帧率解锁、触屏进程修改、启动器更新/预下载不属于本版本。
- 单账户，Intel Mac 暂不支持。正常模式仅在需要登录时临时显示浏览器，确认后自动返回后台。

## 开发检查

```sh
python -m pip install pytest
python -m pytest tests/test_terminal_menu.py tests/test_terminal_quality.py tests/test_terminal_update_menu.py tests/test_terminal_fight.py tests/test_module/test_terminal_runner.py tests/test_utils/test_terminal_qr.py
```

测试使用模拟任务和进程，不会登录账号或消耗云游戏时长。实际云会话的登录、渲染、识别与任务结果需要单独验收。

基于 [moesnow/March7thAssistant](https://github.com/moesnow/March7thAssistant)，遵循仓库 GPL-3.0 许可证。
