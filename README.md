# 三月七小助手：Apple Silicon 终端版

目标平台为 Apple Silicon Mac、国服官服云·星穹铁道。正常运行不显示浏览器和助手窗口，终端显示菜单、日志和登录二维码。浏览器仅在主动选择调试运行时显示。

本分支正在移植中，尚不等同于 Windows 全功能版。当前入口覆盖原项目内置云游戏任务；外部模拟宇宙、锄大地及抽卡记录仍需继续移植；自动剧情已增加纯终端引擎，尚待实机验收。

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

游戏有效画面始终固定为 **1920×1080，缩放比例 1**，不随 Mac 屏幕、Retina 缩放或调试窗口大小改变。菜单 **3 运行设置 → 2 云游戏画质** 可选择超高清、高清、标清、低清；每次启动都会加载所选流量/码率档位，保留登录资料和其它网页偏好。

账号未登录时，程序抓取网页二维码并直接在终端绘制；使用米游社 App 扫码并确认。二维码过期会自动刷新。请加宽终端以完整显示二维码，保持合适亮度。登录资料由本项目独立浏览器资料目录保存。

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

`config.yaml` 保存任务参数；`settings/terminal.json` 保存终端偏好和计划。任务菜单还提供自动剧情和保存云游戏截图。需要细调体力、队伍、奖励和推送时，可按 `assets/config/config.example.yaml` 的注释修改 `config.yaml`。默认禁止使用付费时长。

## 范围

- 日常、体力、奖励、货币战争、内置差分宇宙和三种高难挑战复用现有云游戏自动化，仍需逐项实机测试。
- 模拟宇宙外部引擎及锄大地使用 Windows 输入、桌面截图和本地窗口检测，尚未适配，不在当前可执行菜单中冒充可用。
- 抽卡记录目前需要迁移本地缓存取链接方案。自动剧情使用单线程轮询及云游戏截图/输入，不依赖 Qt；按 Ctrl+C 停止。
- Windows 本地游戏专属的帧率解锁、触屏进程修改、启动器更新/预下载不属于本版本。
- 单账户，Intel Mac 暂不支持。正常模式禁止登录失效时切换为有窗口浏览器。

## 开发检查

```sh
python -m pip install pytest
python -m pytest tests/test_terminal_menu.py tests/test_module/test_terminal_runner.py tests/test_utils/test_terminal_qr.py
```

测试使用模拟任务和进程，不会登录账号或消耗云游戏时长。实际云会话的登录、渲染、识别与任务结果需要单独验收。

基于 [moesnow/March7thAssistant](https://github.com/moesnow/March7thAssistant)，遵循仓库 GPL-3.0 许可证。
