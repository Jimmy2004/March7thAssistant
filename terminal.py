"""云游戏终端前端（Mac Apple Silicon，国服官服）。

设计目标：
    * 不依赖 Qt，仅使用 Python 标准库；第三方依赖（ruamel.yaml、
      module.terminal_runner）按需延迟导入，避免在 ``--help``/``--list``
      等纯展示路径上付出加载成本。
    * 首次启动时通过多轮问答生成默认配置；用户在任何一级输入 ``0``
      或 ``b`` 可以回退到上一级；在第一页输入 ``0`` 表示放弃首次配置，
      取消时不得写入任何配置。
    * 配置拆分到两个文件：
          - ``config.yaml``：使用 ``ruamel.yaml`` 读取/写入，从
            ``assets/config/config.example.yaml`` 复制或保留现有键值，
            并强制覆盖若干云游戏相关字段。
          - ``settings/terminal.json``：仅保存终端层面的偏好（计划列表、
            调试开关等），使用临时文件 + ``os.replace`` 原子写入，且不
            写入任何凭证。

入口：
    ``python terminal.py`` 进入交互菜单；
    ``python terminal.py --help`` / ``--list`` 打印帮助或任务列表；
    ``python terminal.py --task <name> [--debug] [--schedule HH:MM,...]``
    以非交互方式执行一次任务或挂载一个每日计划。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

# 顶层只允许标准库；下文使用 ruamel.yaml / module.terminal_runner 时按需延迟导入。


# ─────────────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────────────

APP_ROOT = Path(__file__).resolve().parent
EXAMPLE_CONFIG = APP_ROOT / "assets" / "config" / "config.example.yaml"
CONFIG_PATH = APP_ROOT / "config.yaml"
SETTINGS_DIR = APP_ROOT / "settings"
TERMINAL_SETTINGS_PATH = SETTINGS_DIR / "terminal.json"

# 已接入终端运行器的云游戏任务。
TASK_CHOICES: tuple[str, ...] = (
    "main",
    "routine",
    "daily",
    "power",
    "currencywars",
    "divergent",
    "forgottenhall",
    "purefiction",
    "apocalyptic",
    "redemption",
    "screen_test",
    "autoplot",
    "screenshot",
    "fight",
    "divergentloop",
)

# 强制写入 config.yaml 的云游戏相关字段，确保终端配置始终可用。
#
# 注意：``fight_enable`` 不在此处强制覆盖——它的初值由
# ``assets/config/config.example.yaml``（默认 ``false``）提供；用户在
# 「运行设置 → 锄大地」里手动开启后，``write_config_yaml`` 的 answers
# 会把对应的答案写回 config.yaml，从而保留用户的「开启」决策。
CLOUD_GAME_DEFAULTS: dict[str, Any] = {
    "cloud_game_enable": True,
    "browser_headless_enable": True,  # 由 debug 标志在写入时取反
    "browser_headless_restart_on_not_logged_in": True,
    "browser_persistent_enable": True,
    "locales": "zh_CN",
    "after_finish": "Exit",
    "pause_after_success": False,
    "exit_after_failure": True,
    "check_update": False,
    "auto_update": False,
    "universe_enable": False,
    "telemetry_enable": False,
}

# 与资源校验清单共用同一个固定版本。
FIGHT_RESOURCE_VERSION = json.loads(
    (APP_ROOT / "module/fight_cloud/engine.json").read_text(encoding="utf-8")
)["revision"][:12]

# 锄大地设置项 → ``write_config_yaml(answers=...)`` 允许出现的 key 白名单。
#
# 只有下列 key 才会被「锄大地设置」子菜单写入 config.yaml，避免 UI 误
# 改其他无关字段。``fight_enable`` 必须出现——「完整运行」会按其值决定
# 是否把锄大地纳入主流程。``FIGHT_ANSWER_KEYS`` 与下方的
# ``FIGHT_ADVANCED_ANSWER_KEYS`` 合并构成「锄大地 answers」完整白名
# 单，``write_config_yaml`` 通过 ``_FIGHT_ANSWER_WRITERS`` 统一管控。
FIGHT_ANSWER_KEYS: frozenset[str] = frozenset({
    "fight_enable",
    "fight_map_version",
    "fight_main_map",
    "fight_timeout",
    "fight_team_enable",
    "fight_team_number",
    "fight_reward_enable",
    "fight_allow_map_buy",
    "fight_allow_snack_buy",
    "fight_allow_memory_token",
})

# 云游戏路线的高级设置。
FIGHT_ADVANCED_ANSWER_KEYS: frozenset[str] = frozenset({
    "fight_start_map",
    "fight_single_map",
})


def fight_answer_allowlist() -> frozenset[str]:
    """返回「锄大地」answers 允许出现的所有 key 并集。

    测试与外部调用方可使用本函数断言 ``write_config_yaml`` 不会让非
    白名单键穿透到 config.yaml。
    """
    return FIGHT_ANSWER_KEYS | FIGHT_ADVANCED_ANSWER_KEYS

# 锄大地地图版本：菜单编号 → 存储键。
# 5 档分别对应 Fhoe-Rail 的不同执行策略，存储时直接落 raw key，UI 文
# 案与示例配置中的 ``不配置`` 不重叠——菜单内不提供「不配置」档，因为
# 是否启用由 ``fight_enable`` 控制。
_FIGHT_MAP_VERSION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1", "default"),       # 默认（疾跑）
    ("2", "default_lite"),  # 默认（轻量）
    ("3", "HuangQuan"),     # 黄泉专用
    ("4", "technique"),     # 秘技路线
    ("5", "reward"),        # 获得特殊物品
)

_FIGHT_MAP_LABELS = {
    "default": "默认路线", "default_lite": "精简路线", "HuangQuan": "黄泉专用",
    "technique": "秘技路线", "reward": "特殊物品领取",
}

# 锄大地优先星球：菜单编号 → 存储值（字符串数字）。
# 存储值 0 自动选择；1..6 对应资源中的六个区域。
_FIGHT_MAIN_MAP_OPTIONS: tuple[tuple[str, str, str], ...] = (
    # (菜单编号, 存储值, 中文标签)
    ("1", "0", "不配置"),
    ("2", "1", "空间站"),
    ("3", "2", "雅利洛"),
    ("4", "3", "仙舟"),
    ("5", "4", "匹诺康尼"),
    ("6", "5", "翁法罗斯"),
    ("7", "6", "第六区域"),
)

# 退出码常量；便于脚本化调用方做分支处理。
EXIT_OK = 0
EXIT_RUNTIME = 1          # 运行时错误（云任务执行失败等）
EXIT_PLATFORM = 2         # 平台/架构不满足要求
EXIT_BAD_ARGS = 3         # 参数错误（--schedule 格式错、--task 不存在等）
EXIT_NOT_INITIALIZED = 4  # 非交互模式下用户尚未运行过首次配置问答


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────


def is_supported_platform() -> bool:
    """运行前检查：仅允许 macOS + Apple Silicon (arm64) 真正执行。

    ``--help`` / ``--list`` 等展示类指令在任何平台都可以使用，因此调用方
    应先自行判断是否需要调用该函数，而不是依赖本函数返回 ``False`` 时直接
    退出。
    """
    return sys.platform == "darwin" and platform.machine() == "arm64"


def task_choices() -> tuple[str, ...]:
    """对外暴露任务列表，便于 ``--list`` 与菜单共用同一数据源。"""
    return TASK_CHOICES


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """将 ``payload`` 以 UTF-8 编码原子写入 ``path``。

    使用 ``tempfile.NamedTemporaryFile`` 在同一目录下生成临时文件，写入
    完成后调用 ``os.replace`` 原子替换目标。这样即使在写入过程中断电或
    进程被强杀，目标文件要么保持旧版本，要么是完全写入的新版本，不会
    出现半截内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # 清理残留临时文件，避免下次启动时误读。
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_terminal_settings() -> dict[str, Any]:
    """读取终端偏好；不存在或损坏时返回空字典。"""
    if not TERMINAL_SETTINGS_PATH.exists():
        return {}
    try:
        with TERMINAL_SETTINGS_PATH.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_terminal_settings(payload: dict[str, Any]) -> None:
    """原子写入终端偏好；显式排除任何可能携带凭证的字段。

    ``terminal.json`` 仅承载无敏感信息的终端层偏好：是否初始化、调试
    开关、失败重试次数、计划列表。账号、Cookie、登录态等永远不会落到
    这个文件中。
    """
    safe_keys = {"initialized", "debug", "retries", "schedule"}
    filtered = {k: v for k, v in payload.items() if k in safe_keys}
    _atomic_write_json(TERMINAL_SETTINGS_PATH, filtered)


def _load_yaml(path: Path) -> Any:
    """延迟导入 ruamel.yaml 并加载已有 YAML；文件不存在时返回 ``None``。"""
    if not path.exists():
        return None
    from ruamel.yaml import YAML  # 延迟导入，避免 --help 时付出成本

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    with path.open("r", encoding="utf-8") as fp:
        return yaml.load(fp)


def _dump_yaml(path: Path, data: Any) -> None:
    """延迟导入 ruamel.yaml 并写回 YAML。"""
    from ruamel.yaml import YAML  # 延迟导入

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    with path.open("w", encoding="utf-8") as fp:
        yaml.dump(data, fp)


def write_config_yaml(debug: bool = False, answers: dict[str, Any] | None = None) -> None:
    """写入 ``config.yaml``。

    逻辑：
        1. 读取 ``config.example.yaml`` 得到默认结构；
        2. 若 ``config.yaml`` 已存在则保留其中所有键值（用户的本地化
           副本不会被覆盖）；
        3. 把 ``CLOUD_GAME_DEFAULTS``（其中 ``browser_headless_enable``
           取反于 ``debug``）强制写入，确保云游戏相关字段始终符合终端
           前端的预期；
        4. 把首配问答里的云游戏偏好（付费时长、排队超时、登录超时）写
           入对应的字段。
    """
    from ruamel.yaml import YAML  # 延迟导入

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True

    # 1. 复制示例配置作为基准。
    with EXAMPLE_CONFIG.open("r", encoding="utf-8") as fp:
        base = yaml.load(fp)
    if base is None:
        base = {}

    # 2. 叠加已有用户配置（保留现有值）。
    existing = _load_yaml(CONFIG_PATH)
    if existing is not None:
        _merge_yaml(base, existing)

    # 3. 强制覆盖云游戏相关字段。
    overrides = dict(CLOUD_GAME_DEFAULTS)
    overrides["browser_headless_enable"] = not debug
    for key, value in overrides.items():
        base[key] = value

    # 4. 把首配问答答案落到 config.yaml。
    if answers:
        if "use_paid_time" in answers:
            base["cloud_game_use_paid_time"] = bool(answers["use_paid_time"])
        if "max_queue_minutes" in answers:
            base["cloud_game_max_queue_time"] = int(answers["max_queue_minutes"])
        if "login_timeout_minutes" in answers:
            base["cloud_game_login_timeout"] = int(answers["login_timeout_minutes"])
        # 云游戏画质：仅当调用方显式提供 video_quality 时才覆盖；
        # 不提供则保留现有 config.yaml 里的画质值。
        if "video_quality" in answers:
            from utils.cloud_quality import normalize_quality

            base["cloud_game_video_quality"] = normalize_quality(answers["video_quality"])

        # 锄大地设置项：只有白名单内的 key 才会被写入；其它字段（如
        # 调用方误传的 ``account``）一律忽略，避免污染 config.yaml。
        _apply_fight_answers(base, answers)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            yaml.dump(base, fp)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temporary, CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _merge_yaml(base: Any, overlay: Any) -> None:
    """将 ``overlay`` 中的标量键合并进 ``base``；``base`` 必须是 mapping。"""
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge_yaml(base[key], value)
        else:
            base[key] = value


# 「锄大地」答案键的写入映射。所有 UI 输入都必须先经过此函数归一化
# 后再写入 config.yaml；不在白名单内的 key 一律忽略，避免误改其它字段。
_FIGHT_ANSWER_WRITERS: dict[str, Callable[[Any], Any]] = {
    "fight_enable": bool,
    "fight_map_version": str,
    "fight_main_map": lambda v: str(int(v)),
    "fight_timeout": lambda v: max(1, min(24, int(v))),
    "fight_team_enable": bool,
    "fight_team_number": lambda v: str(max(1, min(10, int(v)))),
    "fight_reward_enable": bool,
    "fight_allow_map_buy": bool,
    "fight_allow_snack_buy": bool,
    "fight_allow_memory_token": bool,
    "fight_start_map": str,
    "fight_single_map": bool,
}


def _apply_fight_answers(base: dict[str, Any], answers: dict[str, Any]) -> None:
    """把锄大地白名单内的 answer 写入 ``base``；其它 key 忽略。"""
    if not isinstance(base, dict) or not isinstance(answers, dict):
        return
    for key, value in answers.items():
        if key not in _FIGHT_ANSWER_WRITERS:
            continue
        try:
            base[key] = _FIGHT_ANSWER_WRITERS[key](value)
        except (TypeError, ValueError):
            # 调用方传入了无法规约的值——静默跳过而非崩溃，让写盘动作仍能完成。
            continue


# ─────────────────────────────────────────────────────────────────────────────
# 输入辅助
# ─────────────────────────────────────────────────────────────────────────────


class _BackSignal(Exception):
    """内部信号：用户输入 ``b``/``0`` 触发回退。"""


class _CancelledSignal(Exception):
    """内部信号：首次配置在最顶层被取消。"""


def _prompt(text: str, *, default: str | None = None) -> str:
    """读取用户输入，``EOFError`` 视为取消首配，``KeyboardInterrupt`` 回退。"""
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(f"{text}{suffix}: ").strip()
    except EOFError as exc:
        raise _CancelledSignal() from exc
    except KeyboardInterrupt as exc:
        raise _BackSignal() from exc
    return value if value or default is None else default


def _prompt_choice(
    text: str,
    options: Sequence[str],
    *,
    allow_back: bool = True,
    allow_zero: bool = True,
    default: str | None = None,
) -> str:
    """带 ``b``/``0`` 回退的菜单选项提示。

    返回值要么是 ``options`` 中的一个字符串，要么是 ``"b"`` 表示回退、
    ``"0"`` 表示退出当前页。
    """
    hint = "（输入选项）"
    if allow_back:
        hint += "，输入 b 返回上一级"
    if allow_zero:
        hint += "，输入 0 退出"
    while True:
        raw = _prompt(f"{text} {hint}", default=default)
        lowered = raw.lower()
        if allow_back and lowered in {"b", "back"}:
            return "b"
        if allow_zero and raw == "0":
            return "0"
        if raw in options:
            return raw
        print(f"  ! 无效输入：{raw!r}。请从 {options} 中选择。")


def _prompt_yes_no(text: str, *, default: bool = False, allow_back: bool = True) -> bool:
    """是/否提示。``b``/``0`` 走 ``_BackSignal``。"""
    suffix = "[y/N]" if not default else "[Y/n]"
    while True:
        raw = _prompt(f"{text} {suffix}", default=("Y" if default else "N"))
        if allow_back and raw.lower() in {"b", "back", "0"}:
            raise _BackSignal()
        if raw == "":
            return default
        if raw.lower() in {"y", "yes", "是"}:
            return True
        if raw.lower() in {"n", "no", "否"}:
            return False
        print("  ! 请输入 y 或 n。")


def _prompt_int(
    text: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
    allow_back: bool = True,
) -> int:
    """整数提示，限定范围。"""
    while True:
        raw = _prompt(f"{text}（{minimum}-{maximum}，默认 {default}）", default=str(default))
        if allow_back and (raw.lower() in {"b", "back"} or (raw == "0" and minimum > 0)):
            raise _BackSignal()
        try:
            value = int(raw)
        except ValueError:
            print("  ! 请输入整数。")
            continue
        if value < minimum or value > maximum:
            print(f"  ! 取值需在 {minimum}-{maximum} 之间。")
            continue
        return value


# ─────────────────────────────────────────────────────────────────────────────
# 首次配置问答
# ─────────────────────────────────────────────────────────────────────────────


def first_time_setup() -> dict[str, Any]:
    """首次启动多轮问答；返回用户答案。

    五道题的顺序与默认值（与需求保持一致）：
        1. 是否允许使用付费时长（默认 ``False``）
        2. 最大排队等待分钟数（默认 ``60``）
        3. 等待登录超时分钟数（默认 ``20``）
        4. 失败重试次数 0-5（默认 ``2``）
        5. 是否以调试模式（显示浏览器窗口）运行（默认 ``False``）

    任何一级输入 ``b``/``0`` 会抛 ``_BackSignal``，状态机回到上一题；
    在第 1 题回退视为放弃首配（抛 ``_CancelledSignal``）。
    """
    answers: dict[str, Any] = {
        "use_paid_time": False,
        "max_queue_minutes": 60,
        "login_timeout_minutes": 20,
        "retries": 2,
        "debug": False,
    }

    # 顺序问题函数列表，便于在收到 _BackSignal 时重新调用对应问题。
    questions: tuple[Callable[[], None], ...] = (
        lambda: answers.__setitem__(
            "use_paid_time",
            _prompt_yes_no("是否允许使用云游戏付费时长", default=False),
        ),
        lambda: answers.__setitem__(
            "max_queue_minutes",
            _prompt_int(
                "云游戏最大排队等待时间（分钟）",
                minimum=1,
                maximum=240,
                default=60,
            ),
        ),
        lambda: answers.__setitem__(
            "login_timeout_minutes",
            _prompt_int(
                "等待登录超时时间（分钟，超时后终止运行）",
                minimum=1,
                maximum=120,
                default=20,
            ),
        ),
        lambda: answers.__setitem__(
            "retries",
            _prompt_int(
                "失败重试次数", minimum=0, maximum=5, default=2
            ),
        ),
        lambda: answers.__setitem__(
            "debug",
            _prompt_yes_no(
                "调试模式：是否显示浏览器窗口（仅本次生效）",
                default=False,
            ),
        ),
    )

    index = 0
    while index < len(questions):
        try:
            questions[index]()
            index += 1
        except _BackSignal:
            if index == 0:
                # 第 1 题没有上一题，视为放弃首配。
                raise _CancelledSignal() from None
            print("  已返回上一题。")
            index -= 1
    return answers


# ─────────────────────────────────────────────────────────────────────────────
# 计划管理
# ─────────────────────────────────────────────────────────────────────────────


def _parse_schedule(raw: str) -> list[str]:
    """解析 ``HH:MM[,HH:MM...]``，返回标准化的时间列表。"""
    if not raw:
        raise ValueError("计划时间不能为空")
    times: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            raise ValueError("时间格式错误：不能包含空时间")
        parts = token.split(":")
        if len(parts) != 2:
            raise ValueError(f"时间格式错误：{token!r}，应为 HH:MM")
        hour, minute = parts
        if not (hour.isdigit() and minute.isdigit()):
            raise ValueError(f"时间格式错误：{token!r}，应为 HH:MM")
        h, m = int(hour), int(minute)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"时间越界：{token!r}")
        times.append(f"{h:02d}:{m:02d}")
    if not times:
        raise ValueError("计划时间不能为空")
    return sorted(set(times))


def load_schedule() -> list[dict[str, Any]]:
    """读取终端偏好中的计划列表。"""
    settings = _load_terminal_settings()
    raw = settings.get("schedule", [])
    return [item for item in raw if isinstance(item, dict)]


def save_schedule(entries: Iterable[dict[str, Any]]) -> None:
    """保存计划列表到终端偏好。"""
    payload = _load_terminal_settings()
    payload["schedule"] = list(entries)
    _save_terminal_settings(payload)


# ─────────────────────────────────────────────────────────────────────────────
# 顶层菜单循环
# ─────────────────────────────────────────────────────────────────────────────


MENU_HEADER = "====== 云游戏终端（Mac Apple Silicon · 国服官服） ======"
MENU_ITEMS: tuple[tuple[str, str], ...] = (
    ("1", "执行一次任务"),
    ("2", "每天指定时间执行（计划任务）"),
    ("3", "运行设置"),
    ("4", "调试运行（仅本次显示浏览器窗口）"),
    ("5", "显示当前配置和计划"),
    ("6", "退出"),
    ("7", "检查更新"),
)


# ─────────────────────────────────────────────────────────────────────────────
# 更新检查（lazy import module.update.upstream_sync）
# ─────────────────────────────────────────────────────────────────────────────


class UpdateStartupBlocked(RuntimeError):
    """An incomplete update needs recovery before tasks can run."""


def _load_upstream_sync() -> Any:
    """延迟导入 ``module.update.upstream_sync``。

    后台模块与具体实现由主代理负责；本前端只做薄包装。模块暂时不存在时
    返回 ``None``，调用方会把它当作「未启用」并静默跳过。
    """
    try:
        return importlib.import_module("module.update.upstream_sync")
    except Exception as exc:
        print(f"检查更新模块无法加载：{exc}", file=sys.stderr)
        return None


def check_updates(interactive: bool = True) -> bool:
    """启动时检查上游更新。

    行为契约：
        * 已最新或有新上游：提示 ``local=X / target=Y``（或「已是最新」）；
          在交互式下询问是否下载并合并（``Y/N``，默认 ``N``，``0``/``b``
          视为取消），只有 ``y``/``yes`` 才调用 ``apply_update``。
        * 非交互式 + 有更新：只提示 ``python terminal.py --check-update``，
          不自动下载、不退出，调用方可继续正常流程。
        * 网络/导入失败：打印错误，**不抛异常** 返回 ``False``。
        * 成功应用：打印合并/忽略文件数、备份目录，提示用户重启终端
          以避免同进程混入新旧代码，**返回 ``True``**（让上层决定退出）。
        * 未应用（已是最新 / 用户拒绝 / 非交互）：**返回 ``False``**。

    测试可通过 ``monkeypatch.setattr(terminal, "check_updates", ...)``
    整体替换；也可通过向 ``sys.modules["module.update.upstream_sync"]``
    注入 stub 来验证内部逻辑分支。
    """
    sync_mod = _load_upstream_sync()
    if sync_mod is None or not hasattr(sync_mod, "check_for_updates"):
        # 后台模块未就绪 → 不阻塞启动。
        return False

    print("正在检查上游源码更新...")
    try:
        info = sync_mod.check_for_updates(APP_ROOT)
    except KeyboardInterrupt:
        print("已取消更新检查。")
        return False
    except Exception as exc:  # 网络或解析错误
        print(f"  ! 检查更新失败：{exc}", file=sys.stderr)
        if isinstance(exc, getattr(sync_mod, "UpdateRecoveryError", ())):
            raise UpdateStartupBlocked(str(exc)) from exc
        return False

    available = bool(getattr(info, "available", False))
    baseline = str(getattr(info, "baseline", ""))
    target = str(getattr(info, "target", ""))

    if not available:
        print(
            f"  已是最新（local={baseline or '?'}，target={target or '?'}）。"
            if baseline or target
            else "  已是最新。"
        )
        return False

    print(f"  发现上游新提交：local={baseline} → target={target}。")

    if not interactive:
        # 非交互：仅提示，不下载也不退出。
        print(
            "  检测到非交互模式，自动跳过下载。\n"
            "  请手动执行：python terminal.py --check-update"
        )
        return False

    while True:
        try:
            answer = _prompt(
                "是否下载并合并上游更新？(Y/N，默认 N，0/b 返回)", default="N"
            ).lower()
        except (_BackSignal, _CancelledSignal, KeyboardInterrupt):
            print("已取消更新检查。")
            return False
        if answer in {"n", "no", "0", "b", "back"}:
            print("已跳过更新。")
            return False
        if answer in {"y", "yes"}:
            break
        print("请输入 Y 或 N（0/b 返回）。")

    try:
        result = sync_mod.apply_update(info, APP_ROOT)
    except Exception as exc:
        print(f"  ! 应用更新失败：{exc}", file=sys.stderr)
        if isinstance(exc, getattr(sync_mod, "UpdateRecoveryError", ())):
            raise UpdateStartupBlocked(str(exc)) from exc
        return False
    except KeyboardInterrupt:
        print("更新已取消，项目已恢复。")
        return False

    changed = int(getattr(result, "changed", 0))
    ignored = int(getattr(result, "ignored", 0))
    backup = str(getattr(result, "backup_dir", ""))
    print(
        f"  更新完成：合并 {changed} 个文件，忽略 {ignored} 个。"
        f"\n  备份目录：{backup}"
        f"\n  请重启终端前端以加载新代码（本进程即将退出）。"
    )
    return True





def print_main_menu() -> None:
    print(MENU_HEADER)
    for key, desc in MENU_ITEMS:
        print(f"  {key}. {desc}")


def _run_once(task: str, retries: int, debug: bool) -> int:
    """调用 ``module.terminal_runner.run_task``，并把异常映射为退出码。"""
    from module.terminal_runner import run_task  # 延迟导入

    try:
        return EXIT_OK if run_task(task, retries=retries, debug=debug) else EXIT_RUNTIME
    except KeyboardInterrupt:
        print("已停止任务并关闭云游戏。")
        return 130
    except Exception as exc:  # pragma: no cover - 真正运行才会触发
        print(f"  ! 执行任务 {task!r} 时出错：{exc}", file=sys.stderr)
        return EXIT_RUNTIME


def _run_schedule(task: str, times: Sequence[str], retries: int, debug: bool) -> int:
    from module.terminal_runner import run_schedule  # 延迟导入

    try:
        return EXIT_OK if run_schedule(task, times=list(times), retries=retries, debug=debug) else EXIT_RUNTIME
    except Exception as exc:  # pragma: no cover
        print(f"  ! 执行计划任务 {task!r} 时出错：{exc}", file=sys.stderr)
        return EXIT_RUNTIME


def action_run_once(debug: bool, retries: int) -> int:
    """菜单 1：执行一次任务。"""
    task = _choose_task("请选择要执行的任务（输入 0 返回主菜单）")
    if task == "0":
        return EXIT_OK
    rc = _run_once(task, retries=retries, debug=debug)
    print(f"  任务 {task} 返回码 {rc}")
    return rc


def action_schedule(debug: bool, retries: int) -> int:
    """Configure and start a daily schedule; keep a saved plan for later runs."""
    while True:
        saved = load_schedule()
        if saved:
            print("1. 启动上次计划  2. 重新设定计划  0. 返回")
            choice = _prompt("请选择", default="1").lower()
            if choice in {"0", "b", "back"}:
                return EXIT_OK
            if choice == "1":
                entry = saved[-1]
                return _run_schedule(entry["task"], entry["times"], retries, debug)
            if choice != "2":
                continue
        task = _choose_task("请选择定时执行的任务（0 或 b 返回）")
        if task == "0":
            return EXIT_OK
        while True:
            raw = _prompt("每天执行时间（HH:MM，逗号分隔；0 或 b 返回选任务）")
            if raw.lower() in {"0", "b", "back"}:
                break
            try:
                times = _parse_schedule(raw)
            except ValueError as exc:
                print(exc)
                continue
            save_schedule([{"task": task, "times": times, "retries": retries, "debug": debug}])
            print("计划已保存，开始等待；Ctrl+C 返回菜单。")
            return _run_schedule(task, times, retries, debug)


# 云游戏画质选项：菜单编号 → utils.cloud_quality 中的规范 key。
# 三档：1=高画质、2=省流画质、3=极致省流。
_CLOUD_GAME_VIDEO_QUALITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1", "high"),
    ("2", "traffic_saving"),
    ("3", "extreme_saving"),
)


def _action_cloud_game_video_quality() -> int:
    """「设置 → 云游戏画质」子菜单。

    显示 ``config.yaml`` 中当前的 ``cloud_game_video_quality``，让用户从
    三档中选择；选择后调用 ``write_config_yaml(answers={"video_quality": key})``
    写回。``0``/``b`` 仅返回，不写入。
    """
    # 读取当前画质作为默认值（显示当前档时使用相同的规范 key）。
    current: str | None = None
    data = _load_yaml(CONFIG_PATH)
    if isinstance(data, dict):
        raw = data.get("cloud_game_video_quality")
        if raw is not None:
            try:
                from utils.cloud_quality import normalize_quality as _normalize_quality

                current = _normalize_quality(raw)
            except Exception:
                current = None

    # 延迟导入，保证 --help / --list 不付成本。
    from utils.cloud_quality import quality_label

    print("── 云游戏画质 ──")
    for number, key in _CLOUD_GAME_VIDEO_QUALITY_OPTIONS:
        marker = "（当前）" if key == current else ""
        print(f"  {number}. {quality_label(key)} {marker}".rstrip())
    print("  0/b. 返回")

    raw = _prompt("请选择画质（0 或 b 返回）", default="0")
    if raw.lower() in {"0", "b", "back"}:
        return EXIT_OK
    for number, key in _CLOUD_GAME_VIDEO_QUALITY_OPTIONS:
        if raw == number:
            settings = _load_terminal_settings()
            debug = bool(settings.get("debug", False))
            write_config_yaml(debug=debug, answers={"video_quality": key})
            print(f"  已将云游戏画质设置为：{quality_label(key)}（下次启动自动应用）")
            return EXIT_OK
    print(f"  ! 无效输入：{raw!r}")
    return EXIT_OK


# ─────────────────────────────────────────────────────────────────────────────
# 「设置 → 锄大地」子菜单
# ─────────────────────────────────────────────────────────────────────────────


# 「锄大地」设置项的菜单编号 → 中文标题 / 简短提示。
# 真实配置键由 ``write_config_yaml(answers=...)`` 写入；这里只负责
# 收集 UI 答案并按白名单落盘。
_FIGHT_MENU_ITEMS: tuple[tuple[str, str, str], ...] = (
    # (菜单编号, 中文标题, 简介)
    ("1", "启用锄大地（影响「完整运行」）", "总开关：决定「完整运行」是否包含锄大地流程"),
    ("2", "地图版本", "选择默认、精简、黄泉专用、秘技或特殊物品路线"),
    ("3", "优先星球", "自动选择或指定已解锁区域"),
    ("4", "超时时间（小时）", "1..24 小时，到点强制结束"),
    ("5", "启用自动切换队伍", "开始锄大地前是否自动切换队伍"),
    ("6", "自动切换队伍编号", "1..10，对应游戏内队伍槽位"),
    ("7", "启用「特殊物品领取」", "开启后执行所选的购买和领取路线"),
    ("8", "每周购买代币与过期邮包", "需要同时开启特殊物品领取"),
    ("9", "每日购买秘技零食", "需要同时开启特殊物品领取"),
    ("10", "每周获得翁法罗斯记忆代币", "需要同时开启特殊物品领取"),
    ("11", "检查 / 下载 Fhoe 引擎资源", "检查路线与图片资源，缺失时联网下载"),
    ("12", "高级：起始地图", "例如 1-1_0；留空自动选择"),
    ("13", "高级：单地图模式", "开启后只执行起始地图，适合首次检查"),
)


def _read_current_fight_value(key: str) -> Any:
    """从 config.yaml 读取指定 key 的当前值；不存在时返回 ``None``。"""
    data = _load_yaml(CONFIG_PATH)
    if not isinstance(data, dict):
        return None
    return data.get(key)


def _format_fight_bool_for_display(key: str) -> str:
    """把布尔值翻译成中文「开 / 关」标签，便于在菜单标题中显示。"""
    raw = _read_current_fight_value(key)
    if raw is None:
        return "（未设置）"
    return "开" if raw is True else ("未配置" if raw == "不配置" else "关")


def _format_fight_int_for_display(key: str) -> str:
    raw = _read_current_fight_value(key)
    if raw is None:
        return "（未设置）"
    return str(raw)


def _format_fight_map_version_for_display(key: str) -> str:
    raw = _read_current_fight_value(key)
    if raw is None:
        return "（未设置）"
    for number, option in _FIGHT_MAP_VERSION_OPTIONS:
        if option == raw:
            return _FIGHT_MAP_LABELS[option]
    return str(raw)


def _format_fight_main_map_for_display(key: str) -> str:
    raw = _read_current_fight_value(key)
    if raw is None:
        return "（未设置）"
    for number, value, label in _FIGHT_MAIN_MAP_OPTIONS:
        if value == str(raw):
            return f"{label}（{value}）"
    return str(raw)


def _write_fight_answer(answer_key: str, value: Any) -> None:
    """把单条锄大地答案写回 config.yaml（原子写入）。"""
    settings = _load_terminal_settings()
    debug = bool(settings.get("debug", False))
    write_config_yaml(debug=debug, answers={answer_key: value})


def _action_fight_enable() -> int:
    """菜单 1：启用 / 关闭锄大地。"""
    current = bool(_read_current_fight_value("fight_enable"))
    print(f"  当前：{'开' if current else '关'}。")
    return _prompt_and_write_bool(
        "fight_enable",
        "是否启用锄大地（影响「完整运行」流程）",
        default=current,
    )


def _action_fight_map_version() -> int:
    """菜单 2：选择地图版本。"""
    print("── 地图版本 ──")
    for number, key in _FIGHT_MAP_VERSION_OPTIONS:
        marker = "（当前）" if key == _read_current_fight_value("fight_map_version") else ""
        print(f"  {number}. {_FIGHT_MAP_LABELS[key]} {marker}".rstrip())
    print("  0/b. 返回")

    raw = _prompt("请选择地图版本（0 或 b 返回）", default="0")
    if raw.lower() in {"0", "b", "back"}:
        return EXIT_OK
    for number, key in _FIGHT_MAP_VERSION_OPTIONS:
        if raw == number:
            _write_fight_answer("fight_map_version", key)
            print(f"  已将地图版本设置为：{_FIGHT_MAP_LABELS[key]}（下次启动自动应用）")
            return EXIT_OK
    print(f"  ! 无效输入：{raw!r}")
    return EXIT_OK


def _action_fight_main_map() -> int:
    """菜单 3：选择优先星球。"""
    print("── 优先星球 ──")
    current_raw = _read_current_fight_value("fight_main_map")
    current_str = None if current_raw is None else str(current_raw)
    for number, value, label in _FIGHT_MAIN_MAP_OPTIONS:
        marker = "（当前）" if value == current_str else ""
        print(f"  {number}. {label}（{value}） {marker}".rstrip())
    print("  0/b. 返回")

    raw = _prompt("请选择优先星球（0 或 b 返回）", default="0")
    if raw.lower() in {"0", "b", "back"}:
        return EXIT_OK
    for number, value, _label in _FIGHT_MAIN_MAP_OPTIONS:
        if raw == number:
            _write_fight_answer("fight_main_map", value)
            print(f"  已将优先星球设置为：{value}（下次启动自动应用）")
            return EXIT_OK
    print(f"  ! 无效输入：{raw!r}")
    return EXIT_OK


def _action_fight_timeout() -> int:
    """菜单 4：超时时间（小时）。"""
    current = _read_current_fight_value("fight_timeout")
    default = int(current) if isinstance(current, (int, str)) and str(current).isdigit() else 12
    return _prompt_and_write_int(
        "fight_timeout",
        "锄大地超时时间（小时，1-24）",
        minimum=1,
        maximum=24,
        default=default,
    )


def _action_fight_team_enable() -> int:
    """菜单 5：是否在锄大地前自动切队。"""
    current = bool(_read_current_fight_value("fight_team_enable"))
    return _prompt_and_write_bool(
        "fight_team_enable",
        "是否在锄大地前自动切换队伍",
        default=current,
    )


def _action_fight_team_number() -> int:
    """菜单 6：自动切队编号。"""
    current = _read_current_fight_value("fight_team_number")
    default = int(current) if isinstance(current, (int, str)) and str(current).isdigit() else 1
    return _prompt_and_write_int(
        "fight_team_number",
        "自动切换队伍编号（1-10）",
        minimum=1,
        maximum=10,
        default=default,
    )


def _action_fight_reward_enable() -> int:
    """菜单 7：是否向锄大地写入「特殊物品领取」三项。"""
    current = bool(_read_current_fight_value("fight_reward_enable"))
    return _prompt_and_write_bool(
        "fight_reward_enable",
        "启用「特殊物品领取」",
        default=current,
    )


def _action_fight_allow_map_buy() -> int:
    current = _read_current_fight_value("fight_allow_map_buy") is True
    return _prompt_and_write_bool(
        "fight_allow_map_buy",
        "每周购买代币与过期邮包",
        default=current,
    )


def _action_fight_allow_snack_buy() -> int:
    current = _read_current_fight_value("fight_allow_snack_buy") is True
    return _prompt_and_write_bool(
        "fight_allow_snack_buy",
        "每日购买秘技零食",
        default=current,
    )


def _action_fight_allow_memory_token() -> int:
    current = _read_current_fight_value("fight_allow_memory_token") is True
    return _prompt_and_write_bool(
        "fight_allow_memory_token",
        "每周获得翁法罗斯记忆代币",
        default=current,
    )


def _action_fight_prepare_resources() -> int:
    """菜单 11：检查 / 下载引擎资源。

    真实下载/校验由 ``module.fight_cloud.resources.prepare_resources`` 完
    成；前端只把安装根目录（``APP_ROOT``）交给后端，并显示其返回的
    ``pathlib.Path``。后端模块未提供或调用失败时打印可读错误并返回
    ``EXIT_RUNTIME``，不抛异常，不触发额外的网络代码（网络访问全部由
    后端模块自行管理）。
    """
    print(f"  目标引擎版本：{FIGHT_RESOURCE_VERSION}")
    print("  正在按需下载 / 校验 Fhoe 引擎资源（可能需要联网）...")
    try:
        from module.fight_cloud.resources import prepare_resources  # 延迟导入
    except ImportError as exc:
        print(
            f"  ! 引擎资源模块尚未提供（module.fight_cloud.resources）：{exc}",
            file=sys.stderr,
        )
        print(
            "  ! 云游戏适配模块缺失，请更新本分支代码后重试。",
            file=sys.stderr,
        )
        return EXIT_RUNTIME
    try:
        target: Path = prepare_resources(APP_ROOT)
    except Exception as exc:
        print(f"  ! 下载 / 校验失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME
    print(f"  资源已就绪：{target}")
    return EXIT_OK


def _action_fight_start_map() -> int:
    """菜单 12：高级 — 起始地图字符串（正则 [1-6]-[0-9]+_[0-9]+）。"""
    import re as _re

    current = _read_current_fight_value("fight_start_map")
    default = "" if not current else str(current)
    while True:
        raw = _prompt(
            "起始地图（格式 [1-6]-[行]_[列]，如 1-1_3；留空交给引擎自动）",
            default="",
        )
        if raw.lower() in {"b", "back"}:
            return EXIT_OK
        if raw == "0":
            return EXIT_OK
        if raw == "":
            _write_fight_answer("fight_start_map", "")
            print("  已留空：起始地图由引擎自动决定。")
            return EXIT_OK
        if _re.fullmatch(r"[1-6]-[0-9]+_[0-9]+", raw):
            _write_fight_answer("fight_start_map", raw)
            print(f"  已将起始地图设置为：{raw}（下次启动自动应用）")
            return EXIT_OK
        print(f"  ! 格式不正确：{raw!r}，应为 [1-6]-[行]_[列]，例如 1-1_3。")


def _action_fight_single_map() -> int:
    """菜单 13：高级 — 是否只刷一张地图。"""
    current = bool(_read_current_fight_value("fight_single_map"))
    return _prompt_and_write_bool(
        "fight_single_map",
        "是否只刷一张地图（true=只刷指定起始地图）",
        default=current,
    )


def _prompt_and_write_bool(
    config_key: str,
    prompt_text: str,
    *,
    default: bool,
) -> int:
    """通用的「开关 / 否」设置子流程：返回 EXIT_OK。"""
    try:
        value = _prompt_yes_no(prompt_text, default=default)
    except _BackSignal:
        return EXIT_OK
    _write_fight_answer(config_key, value)
    state = "开" if value else "关"
    print(f"  已将「{prompt_text}」设置为：{state}（下次启动自动应用）")
    return EXIT_OK


def _prompt_and_write_int(
    config_key: str,
    prompt_text: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    """通用的「范围整数」设置子流程：返回 EXIT_OK。"""
    try:
        value = _prompt_int(prompt_text, minimum=minimum, maximum=maximum, default=default)
    except _BackSignal:
        return EXIT_OK
    _write_fight_answer(config_key, value)
    print(f"  已将「{prompt_text}」设置为：{value}（下次启动自动应用）")
    return EXIT_OK


# 「设置 → 锄大地」内部子动作派发表。``None`` 哨兵留给未来扩展。
_FIGHT_SUB_ACTIONS: dict[str, Callable[[], int]] = {
    "1": _action_fight_enable,
    "2": _action_fight_map_version,
    "3": _action_fight_main_map,
    "4": _action_fight_timeout,
    "5": _action_fight_team_enable,
    "6": _action_fight_team_number,
    "7": _action_fight_reward_enable,
    "8": _action_fight_allow_map_buy,
    "9": _action_fight_allow_snack_buy,
    "10": _action_fight_allow_memory_token,
    "11": _action_fight_prepare_resources,
    "12": _action_fight_start_map,
    "13": _action_fight_single_map,
}


def _render_fight_submenu() -> None:
    """把锄大地子菜单打印到 stdout。"""
    print("── 锄大地 ──")
    for number, title, desc in _FIGHT_MENU_ITEMS:
        suffix = ""
        if number == "1":
            suffix = f"（当前：{'开' if bool(_read_current_fight_value('fight_enable')) else '关'}）"
        elif number == "2":
            suffix = f"（当前：{_format_fight_map_version_for_display('fight_map_version')}）"
        elif number == "3":
            suffix = f"（当前：{_format_fight_main_map_for_display('fight_main_map')}）"
        elif number == "4":
            suffix = f"（当前：{_format_fight_int_for_display('fight_timeout')} 小时）"
        elif number == "5":
            suffix = f"（当前：{_format_fight_bool_for_display('fight_team_enable')}）"
        elif number == "6":
            suffix = f"（当前：{_format_fight_int_for_display('fight_team_number')}）"
        elif number == "7":
            suffix = f"（当前：{_format_fight_bool_for_display('fight_reward_enable')}）"
        elif number == "8":
            suffix = f"（当前：{_format_fight_bool_for_display('fight_allow_map_buy')}）"
        elif number == "9":
            suffix = f"（当前：{_format_fight_bool_for_display('fight_allow_snack_buy')}）"
        elif number == "10":
            suffix = f"（当前：{_format_fight_bool_for_display('fight_allow_memory_token')}）"
        elif number == "12":
            suffix = f"（当前：{_read_current_fight_value('fight_start_map') or '自动'}）"
        elif number == "13":
            suffix = f"（当前：{_format_fight_bool_for_display('fight_single_map')}）"
        line = f"  {number}. {title}"
        if suffix:
            line += f" {suffix}"
        if desc:
            line += f"\n      {desc}"
        print(line)
    print("  0. 返回")


def action_fight_settings() -> int:
    """「设置 → 锄大地」子菜单：循环展示 13 个独立设置项，``0``/``b`` 返回。

    每一项都是一个独立子动作：用户在子动作中再次输入 ``0``/``b`` 只退出
    当前子动作回到本菜单，不会写入任何 config.yaml。本菜单本身则需要
    显式输入 ``0``（或 ``b``）才退出，避免与子动作里的「布尔开关为假」
    混淆。
    """
    while True:
        try:
            _render_fight_submenu()
            raw = _prompt("请选择（0 或 b 返回设置主菜单）", default="0")
        except _BackSignal:
            return EXIT_OK
        if raw.lower() in {"0", "b", "back"}:
            return EXIT_OK
        action = _FIGHT_SUB_ACTIONS.get(raw)
        if action is None:
            print(f"  ! 无效输入：{raw!r}")
            continue
        action()
        # 子动作通过 _BackSignal / 显式判断已处理「返回」；不修改任何
        # 全局状态，自然落回循环顶部继续显示本菜单。


def action_general_settings() -> int:
    """常规设置：重新运行首次配置问答。"""
    try:
        new_state = first_time_setup()
    except _CancelledSignal:
        print("  ! 已取消首次配置，未写入任何文件。")
        return EXIT_OK
    debug = bool(new_state.get("debug", False))
    retries = int(new_state.get("retries", 2))
    write_config_yaml(debug=debug, answers=new_state)
    settings = _load_terminal_settings()
    settings["initialized"] = True
    settings["debug"] = debug
    settings["retries"] = retries
    _save_terminal_settings(settings)
    print("  配置已写入 config.yaml 与 settings/terminal.json")
    return EXIT_OK


def action_settings() -> int:
    """菜单 3：设置子菜单（循环），内含常规设置、云游戏画质与锄大地三项。"""
    while True:
        print("── 运行设置 ──")
        print("  1. 重新配置常规设置")
        print("  2. 云游戏画质")
        print("  3. 锄大地")
        print("  0. 返回")
        raw = _prompt("请选择（0 返回主菜单）", default="0")
        if raw.lower() in {"0", "b", "back"}:
            return EXIT_OK
        if raw == "1":
            action_general_settings()
        elif raw == "2":
            _action_cloud_game_video_quality()
        elif raw == "3":
            action_fight_settings()
        else:
            print(f"  ! 无效输入：{raw!r}")


def action_debug_run(retries: int) -> int:
    """菜单 4：调试运行（仅本次有窗口）。"""
    task = _choose_task("请选择要调试运行的任务（输入 0 返回主菜单）")
    if task == "0":
        return EXIT_OK
    rc = _run_once(task, retries=retries, debug=True)
    print(f"  调试任务 {task} 返回码 {rc}")
    return rc


def action_show_state() -> int:
    """菜单 5：展示当前配置与计划。"""
    settings = _load_terminal_settings()
    debug = bool(settings.get("debug", False))
    initialized = bool(settings.get("initialized", False))

    print("── 终端偏好 ──")
    print(f"  initialized : {initialized}")
    print(f"  debug       : {debug}")

    schedule = load_schedule()
    print(f"── 计划列表（共 {len(schedule)} 项） ──")
    if not schedule:
        print("  （空）")
    else:
        for idx, entry in enumerate(schedule, start=1):
            times = ",".join(entry.get("times", []))
            print(
                f"  {idx}. task={entry.get('task')} "
                f"times=[{times}] retries={entry.get('retries')} "
                f"debug={entry.get('debug')}"
            )

    print("── config.yaml 关键字段 ──")
    data = _load_yaml(CONFIG_PATH)
    if data is None:
        print("  （config.yaml 尚未生成）")
        print("  screen: 1920×1080")
        print("  cloud_game_video_quality: （未设置）")
    else:
        for key in CLOUD_GAME_DEFAULTS:
            print(f"  {key}: {data.get(key)}")
        print("  screen: 1920×1080")
        quality = data.get("cloud_game_video_quality")
        if quality is None:
            quality_label_text = "（未设置）"
        else:
            try:
                from utils.cloud_quality import quality_label

                quality_label_text = quality_label(quality)
            except Exception:
                quality_label_text = str(quality)
        print(f"  cloud_game_video_quality: {quality_label_text}")
    return EXIT_OK


def _choose_task(prompt: str) -> str:
    """列出任务并要求用户选择。"""
    print("可用任务：")
    for idx, name in enumerate(TASK_CHOICES, start=1):
        from utils.tasks import AVAILABLE_TASKS
        print(f"  {idx}. {AVAILABLE_TASKS.get(name, name)}")
    while True:
        raw = _prompt(prompt, default="1")
        if raw == "0":
            return "0"
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(TASK_CHOICES):
                return TASK_CHOICES[n - 1]
        if raw in TASK_CHOICES:
            return raw
        print(f"  ! 无效输入：{raw!r}")


def main_loop(debug: bool, retries: int) -> int:
    """交互式主循环。

    * ``EOFError`` / ``_CancelledSignal``（Ctrl+D）：优雅退出（``EXIT_OK``）。
    * ``KeyboardInterrupt`` / ``_BackSignal``：Ctrl+C 在菜单运行时回到
      主菜单；在问答中由 ``first_time_setup`` 解释为「返回上一层」。
    """
    while True:
        print()
        print_main_menu()
        try:
            choice = _prompt("请选择菜单项（输入 0 退出）", default="1")
        except (_CancelledSignal, EOFError):
            print("\n  ! 收到 EOF，退出终端。")
            return EXIT_OK
        except (_BackSignal, KeyboardInterrupt):
            print("\n  ! 收到 Ctrl+C，返回主菜单。")
            continue

        try:
            current_settings = _load_terminal_settings()
            retries = int(current_settings.get("retries", retries))
            debug = False
            if choice in {"1", "2", "4"} and not current_settings.get("initialized"):
                print("请先选择 3 完成首次设置。")
                continue
            if choice == "0" or choice == "6":
                return EXIT_OK
            if choice == "1":
                action_run_once(debug, retries)
            elif choice == "2":
                action_schedule(debug, retries)
            elif choice == "3":
                action_settings()
            elif choice == "4":
                action_debug_run(retries)
            elif choice == "5":
                action_show_state()
            elif choice == "7":
                # 菜单 7：手动触发更新检查。成功应用 → EXIT_OK，提示重启。
                if check_updates(interactive=True):
                    return EXIT_OK
            else:
                print(f"  ! 未知选项：{choice!r}")
        except UpdateStartupBlocked:
            print("未完成更新尚未恢复，停止运行。", file=sys.stderr)
            return EXIT_RUNTIME
        except (_CancelledSignal, EOFError):
            return EXIT_OK
        except KeyboardInterrupt:
            print("已停止并返回主菜单。")
            continue
        except _BackSignal:
            # 子动作里把 Ctrl+C 翻译成回退；这里吞掉并回到主菜单。
            print("\n  ! 已返回主菜单。")
            continue


# ─────────────────────────────────────────────────────────────────────────────
# 命令行解析 / 入口
# ─────────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terminal.py",
        description="云游戏终端前端（Mac Apple Silicon · 国服官服）。",
    )
    parser.add_argument("--list", action="store_true", help="列出所有可用任务并退出")
    parser.add_argument("--task", choices=TASK_CHOICES, help="非交互：执行指定任务")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="非交互：本次运行显示浏览器窗口（仅本次生效）",
    )
    parser.add_argument(
        "--schedule",
        help="非交互：保存并启动每日计划，格式 HH:MM[,HH:MM...]，需配合 --task 使用",
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="独立模式：检查上游更新；可交互询问是否应用，成功应用后退出。",
    )
    return parser


def _print_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
    print()
    print("内置任务：")
    for name in TASK_CHOICES:
        print(f"  - {name}")
    print()
    print("说明：")
    print("  * 顶层仅依赖标准库；首次执行会进入多轮配置问答。")
    print("  * 任何一级输入 0 或 b 可回退到上一级；首层取消不写入配置。")
    print("  * --help / --list 可在任何平台使用，不依赖 Darwin+arm64。")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    requested = list(sys.argv[1:] if argv is None else argv)
    if "--help" in requested or "-h" in requested:
        _print_help(parser)
        return EXIT_OK
    # argparse 会自己处理 -h/--help 并调用 parser.exit() sys.exit()。
    # 我们要拦截 SystemExit，把退出码映射到常量，并补充任务列表；同时
    # 避免 argparse 直接退出导致测试 / 嵌入式调用拿不到返回值。
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code in (0, None):
            # -h / --help：补一段内置任务列表，再退出。
            _print_help(parser)
            return EXIT_OK
        if isinstance(code, str):
            return EXIT_BAD_ARGS if code else EXIT_OK
        return EXIT_BAD_ARGS

    # --list 在解析后单独处理（不动用平台检查）。
    if args.list:
        # 主菜单里的「7. 检查更新」也属于终端入口，在 --list 中单独列出一行。
        print("- " + "\n- ".join(TASK_CHOICES))
        print("- 7  检查更新（独立命令：python terminal.py --check-update）")
        return EXIT_OK

    # 运行前检查：Darwin + arm64。
    if not is_supported_platform():
        print(
            "  ! 本终端前端仅在 macOS Apple Silicon 上受支持。\n"
            "    当前平台：{plat} / {machine}".format(
                plat=sys.platform, machine=platform.machine()
            ),
            file=sys.stderr,
        )
        return EXIT_PLATFORM

    settings = _load_terminal_settings()

    # Check before configuration questions or launching any game task.
    try:
        if args.check_update:
            check_updates(interactive=True)
            return EXIT_OK
        if check_updates(interactive=bool(sys.stdin.isatty())):
            return EXIT_OK
    except UpdateStartupBlocked:
        print("未完成更新尚未恢复，停止启动。", file=sys.stderr)
        return EXIT_RUNTIME

    # ── 非交互路径 ────────────────────────────────────────────────────────
    if args.task is not None:
        if not settings.get("initialized"):
            print(
                "  ! 检测到尚未运行首次配置问答，请先执行 `python terminal.py` 完成初始化。",
                file=sys.stderr,
            )
            return EXIT_NOT_INITIALIZED

        debug = bool(args.debug)
        retries = int(settings.get("retries", 2))

        if args.schedule:
            try:
                times = _parse_schedule(args.schedule)
            except ValueError as exc:
                print(f"  ! {exc}", file=sys.stderr)
                return EXIT_BAD_ARGS
            entries = [
                {"task": args.task, "times": times, "retries": retries, "debug": debug}
            ]
            save_schedule(entries)
            print(
                f"  已保存计划：{args.task} @ {', '.join(times)} "
                f"（retries={retries}, debug={debug}）"
            )
            return _run_schedule(args.task, times, retries, debug)

        return _run_once(args.task, retries=retries, debug=debug)

    # --schedule 不带 --task 视为参数错误。
    if args.schedule:
        print("  ! --schedule 必须与 --task 同时使用。", file=sys.stderr)
        return EXIT_BAD_ARGS

    # ── 交互路径 ──────────────────────────────────────────────────────────
    if not settings.get("initialized"):
        print("检测到首次启动，进入配置问答（按 Ctrl+C 返回上一层，Ctrl+D 放弃）。")
        try:
            state = first_time_setup()
        except _CancelledSignal:
            print("已返回主菜单，配置未保存。")
            return main_loop(debug=False, retries=2)
        debug = bool(state.get("debug", False))
        retries = int(state.get("retries", 2))
        write_config_yaml(debug=debug, answers=state)
        settings = {"initialized": True, "debug": debug, "retries": retries}
        _save_terminal_settings(settings)
        print("  已写入 config.yaml 与 settings/terminal.json。\n")

    debug = bool(settings.get("debug", False))
    retries = int(settings.get("retries", 2))
    return main_loop(debug=debug, retries=retries)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
