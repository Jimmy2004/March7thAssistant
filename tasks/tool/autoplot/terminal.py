"""自动对话模块 — 纯命令行 / 云游戏版本。

``TerminalAutoplot`` 是 ``AutoPlot`` 的无 Qt 等价实现，沿用同一组
``_config`` 常量与 ``SceneDetector`` 静态接口，专门服务于 ``terminal.py``
以及其它不带 Qt 事件循环的入口。

设计目标：
    * 单线程轮询（``time.sleep``），不依赖 ``QTimer``；调用方在自己的
      线程中通过 ``run()`` 阻塞驱动，直到自然结束或上层中断。
    * 截图、模板匹配、点击、键盘输入均复用 ``module.automation.auto``；
      该实例在云游戏环境下走 CDP 输入，本地环境下走本地输入，因此同一
      份代码同时兼容两种形态。
    * 配置项 ``autoplot_skip_enable`` / ``click_enable`` /
      ``battle_detect_enable`` / ``phone_detect_enable`` /
      ``hero_chronicle_enable`` 直接对位 GUI 端的同名开关。
    * ``run() -> bool`` 返回 ``True`` 表示引擎自认正常结束（例如英雄
      史记阅读全部完成），返回 ``False`` 表示在主循环中收到了退出条件
      而非被中断终止。
    * ``Ctrl+C`` 一律不捕获：``run()`` 内不做 ``except KeyboardInterrupt``
      处理，由调用方（``terminal.py`` 的 ``main``）自行清理资源。
    * 主循环中任何意外异常一律向上抛出，调用方负责有限重试。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from module.automation import auto
from module.config import cfg
from module.logger import log

from ._config import (
    Crop,
    Defaults,
    EngineState,
    Img,
    Threshold,
)
from ._detector import SceneDetector


# ``HeroChronicleReader`` 在初始化时会枚举 ``assets/images/autoplot/hero_chronicle``
# 下的模板；在没有这些资源的测试 / 纯 CLI 路径里也不应让导入失败，因此
# 将其延迟到第一次真正启用该功能时再加载。
def _build_hero_reader(click: Callable[[tuple[int, int]], None], title: str):
    from ._hero_chronicle import HeroChronicleReader
    return HeroChronicleReader(click, title)


class TerminalAutoplot:
    """无 Qt 的自动对话 / 自动剧情引擎。

    使用方式::

        engine = TerminalAutoplot(
            game_title_name=cfg.game_title_name,
            autoplot_skip_enable=True,
            click_enable=True,
            battle_detect_enable=True,
            phone_detect_enable=True,
            hero_chronicle_enable=False,
        )
        engine.run()

    ``run()`` 在以下两种情况下返回：
        * 返回 ``True`` — 引擎自认正常结束（例如英雄史记已经全部读完）。
        * 返回 ``False`` — 状态机自行请求退出（例如 HeroChronicle 因
          长时间停滞而放弃）。
    ``KeyboardInterrupt`` 和其它未处理异常均直接抛出。
    """

    def __init__(
        self,
        game_title_name: str,
        *,
        autoplot_skip_enable: bool = Defaults.AUTO_SKIP,
        click_enable: bool = Defaults.AUTO_CLICK,
        battle_detect_enable: bool = Defaults.AUTO_BATTLE_DETECT,
        phone_detect_enable: bool = Defaults.AUTO_PHONE_DETECT,
        hero_chronicle_enable: bool = False,
    ) -> None:
        self._game_title_name: str = game_title_name

        # 行为开关：与 AutoPlot 的同名语义保持一致。
        self.autoplot_skip_enable: bool = bool(autoplot_skip_enable)
        self.click_enable: bool = bool(click_enable)
        self.battle_detect_enable: bool = bool(battle_detect_enable)
        self.phone_detect_enable: bool = bool(phone_detect_enable)
        self.hero_chronicle_enable: bool = bool(hero_chronicle_enable)

        # 状态机：保留 ``EngineState`` 以便和 GUI 版本保持日志口径一致。
        self._state: EngineState = EngineState.IDLE

        # 自动战斗检测冷却（monotonic 秒），避免每帧重复按热键。
        self._last_auto_battle_check: float = 0.0

        # HeroChronicleReader 仅在需要时才创建；构造时需要 ``click`` 回调。
        self._hero_reader = None

    # ------------------------------------------------------------------
    # 配置热更新（CLI 端目前主要在构造时传入，这里保留方法以便未来扩展）
    # ------------------------------------------------------------------

    def update_options(self, options: dict) -> None:
        """原地更新行为开关，未在 ``options`` 中出现的键保持原值。"""
        if 'autoplot_skip_enable' in options:
            self.autoplot_skip_enable = bool(options['autoplot_skip_enable'])
        if 'click_enable' in options:
            self.click_enable = bool(options['click_enable'])
        if 'battle_detect_enable' in options:
            self.battle_detect_enable = bool(options['battle_detect_enable'])
        if 'phone_detect_enable' in options:
            self.phone_detect_enable = bool(options['phone_detect_enable'])
        if 'hero_chronicle_enable' in options:
            enabled = bool(options['hero_chronicle_enable'])
            if enabled != self.hero_chronicle_enable and self._hero_reader is not None:
                self._hero_reader.reset()
            self.hero_chronicle_enable = enabled

    # ------------------------------------------------------------------
    # HeroChronicle 的 click 回调：把 1920x1080 基准坐标转换成实际像素
    # ------------------------------------------------------------------

    def _click_hero_book(self, point) -> None:
        """供 ``HeroChronicleReader`` 调用的点击回调。

        ``point`` 是基于 1920x1080 的坐标，需要结合 reader 在每次动作
        前重新捕获并写入的 ``region`` 缩放到当前游戏窗口的实际像素。
        """
        reader = self._hero_reader
        if reader is None:
            return
        left, top, width, height = reader.region
        x = left + point[0] * width / 1920
        y = top + point[1] * height / 1080
        auto.click_element_with_pos(((x, y), (x, y)))

    # ------------------------------------------------------------------
    # 行为策略
    # ------------------------------------------------------------------

    def _tick_hero_chronicle(self) -> Optional[bool]:
        """执行一次 ``HeroChronicleReader.tick()``。

        返回:
            ``None`` — 未启用或 reader 暂时等待（继续主循环）。
            ``True`` — 任务自然结束（全部英雄已读完），调用方应返回 ``True``。
            ``False`` — reader 已放弃（停滞/卡片错误），调用方应返回 ``False``。
        """
        if not self.hero_chronicle_enable:
            return None
        if self._hero_reader is None:
            self._hero_reader = _build_hero_reader(self._click_hero_book, self._game_title_name)
            self._hero_reader.reset()
        handled = self._hero_reader.tick()
        # tick() 内部检测到自然完成会置位 ``completed``；
        # 长时间停滞则会置位 ``finished`` 但 ``completed`` 为 False，
        # 这两类都需要让 ``run()`` 退出循环。
        if self._hero_reader.completed:
            self._state = EngineState.IDLE
            return True
        if self._hero_reader.finished:
            self._state = EngineState.IDLE
            return False
        self._hero_handled = handled
        if handled:
            # 处于英雄史记页面内：暂停主循环的对话检测，让读者推进。
            self._state = EngineState.MONITORING
        return None

    def _handle_non_dialog_actions(self) -> None:
        """非对话场景下的辅助操作，按 AUTO_BATTLE_COOLDOWN 控制自动战斗键。"""
        if self.battle_detect_enable and SceneDetector.is_auto_battle_off():
            now = time.monotonic()
            if now - self._last_auto_battle_check >= Threshold.AUTO_BATTLE_COOLDOWN:
                log.info("尝试开启自动战斗")
                auto.press_key(cfg.get_value("hotkey_auto_battle", "v"))
                self._last_auto_battle_check = now

        if self.phone_detect_enable:
            SceneDetector.handle_phone_dialogs()

        SceneDetector.click_close()

    def _dialog_loop(self) -> None:
        """单次对话循环：对应 ``AutoPlot._dialog_loop`` 的无 Qt 版本。

        顺序与原 GUI 版本保持一致：
            1. ``click_enable`` 关闭且存在选项 → 仅等待，不点击。
            2. 自动跳过 + 确认。
            3. 选项推进（优先 SELECT，不存在则 EXIT）。
            4. continue 箭头推进。
        """
        # 1. 对话选项检测：在 ``click_enable=False`` 时只等待用户手动选择。
        if not self.click_enable:
            if SceneDetector.has_dialog_options():
                self._state = EngineState.WAITING_OPTION
                return
            # 没有选项时恢复正常对话态。
            if self._state == EngineState.WAITING_OPTION:
                self._state = EngineState.DIALOG_ACTIVE

        # 2. 自动跳过按钮：成功跳过后会弹出确认框。
        if self.autoplot_skip_enable and SceneDetector.detect_skip_button():
            if not SceneDetector.click_confirm():
                SceneDetector.move_mouse_to_click_pos()
            return

        # 3. 对话选项推进（无论 click_enable 是否为 True，在这里都尝试）。
        # 注意：原 GUI 版本在此步骤总会调用 click_dialog_option；同样地，
        # 当 ``click_enable`` 关闭时上面已经发现选项并直接 ``return`` 了。
        if self.click_enable:
            SceneDetector.click_dialog_option()

        # 4. 检测 continue 箭头：使用 ``action="down"`` 后短暂延时再 ``mouse_up``，
        # 与原 GUI 引擎实现一致。
        if auto.click_element(
            Img.CONTINUE, "image", Threshold.CONTINUE_MATCH,
            crop=Crop.DIALOG_CONTINUE,
            action="down",
        ):
            try:
                time.sleep(0.2)
            finally:
                auto.mouse_up()
            log.debug("检测到 continue 箭头，点击推进对话")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self) -> bool:
        """单线程驱动引擎，直到自然结束或上层抛中断。

        返回:
            ``True``  引擎自认正常结束（例如英雄史记已全部读完）。
            ``False`` 引擎自认应中止（例如 HeroChronicle 长时间停滞）。

        抛出:
            ``KeyboardInterrupt`` — 不捕获，直接交给 ``main`` 清理。
            ``Exception``         — 主循环内任意未处理异常原样上抛，
                                    由调用方决定是否进行有限重试。
        """
        self._state = EngineState.MONITORING
        log.info("TerminalAutoplot 已启动")

        try:
            while True:
                # 1. HeroChronicle 优先级最高：它在启用时会独占屏幕，不需要
                #    同时执行对话检测；返回非 None 表示需要终止主循环。
                hero_outcome = self._tick_hero_chronicle()
                if hero_outcome is not None:
                    log.info(f"自动剧情结束，英雄纪阅读结果：{hero_outcome}")
                    return hero_outcome

                if getattr(self, "_hero_handled", False):
                    time.sleep(Threshold.MONITOR_INTERVAL / 1000.0)
                    continue

                # 2. 对话场景检测 → 进入对话循环；否则执行非对话场景辅助。
                if SceneDetector.is_dialog_scene():
                    self._state = EngineState.DIALOG_ACTIVE
                    self._dialog_loop()
                else:
                    self._state = EngineState.MONITORING
                    self._handle_non_dialog_actions()

                # 3. 在每轮之间留出 ``MONITOR_INTERVAL`` 的固定步进；
                #    同时为 Ctrl+C 提供响应窗口。
                time.sleep(Threshold.MONITOR_INTERVAL / 1000.0)
        finally:
            # ``KeyboardInterrupt`` 会在外层 ``main`` 中处理，这里仅做
            # 状态复位和必要的轻量清理；异常会沿调用栈继续向上传播。
            self._state = EngineState.IDLE
            log.info("TerminalAutoplot 已停止")


def run() -> bool:
    return TerminalAutoplot(
        cfg.game_title_name,
        autoplot_skip_enable=cfg.get_value("autoplot_skip_enable", True),
        click_enable=cfg.get_value("autoplot_click_enable", True),
        battle_detect_enable=cfg.get_value("autoplot_battle_detect_enable", True),
        phone_detect_enable=cfg.get_value("autoplot_phone_detect_enable", True),
        hero_chronicle_enable=cfg.get_value("autoplot_hero_chronicle_enable", False),
    ).run()
