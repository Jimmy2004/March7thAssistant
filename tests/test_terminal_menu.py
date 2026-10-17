"""``terminal.py`` 的单元测试。

覆盖范围：
    * 首次配置问答：在首题取消（``EOFError`` / ``_CancelledSignal``）时，
      不写入 ``config.yaml`` 也不写入 ``settings/terminal.json``。
    * 问答完成后：``config.yaml`` 含强制覆盖字段；
      ``settings/terminal.json`` 仅含非敏感字段，并以原子方式保存。
    * 主菜单行为：执行一次、调试运行、查看配置、退出；Ctrl+C 在运行
      任务时回到菜单；子动作中 ``0``/``b`` 返回上一级。
    * CLI：``--help``/``--list`` 不需要 Darwin+arm64；``--task`` 未初始
      化时报 ``EXIT_NOT_INITIALIZED``；``--schedule`` 格式错误报
      ``EXIT_BAD_ARGS``。
    * 平台检查：在 Linux 上跑 ``--task`` 应该报 ``EXIT_PLATFORM``。

通过 ``sys.modules['module.terminal_runner']`` 注入伪 ``run_task`` /
``run_schedule``，避免任何真实浏览器或游戏逻辑参与测试。
"""

from __future__ import annotations

import builtins
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import terminal  # noqa: E402  需要在 conftest 配置 sys.path 之后导入


# ─────────────────────────────────────────────────────────────────────────────
# 测试装置
# ─────────────────────────────────────────────────────────────────────────────


class _Input:
    """模拟 ``input`` 的回放器。

    支持 ``side_effect`` 风格的输入序列：用 ``send`` / ``raise_keyboard``
    / ``raise_eof`` 控制每次 ``input`` 的返回值或异常，便于表达 EOF /
    Ctrl+C 等场景。
    """

    def __init__(self, responses: list[str] | None = None) -> None:
        self._iter = iter(responses or [])
        self._raise_eof = False
        self._raise_kb = False

    def __call__(self, prompt: str = "") -> str:  # noqa: D401 - mock 接口
        if self._raise_eof:
            raise EOFError
        if self._raise_kb:
            raise KeyboardInterrupt
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise EOFError from exc

    def raise_eof_next(self) -> None:
        """下一次 ``input`` 抛 ``EOFError``，然后回到正常迭代。"""
        self._raise_eof = True

    def raise_kb_next(self) -> None:
        """下一次 ``input`` 抛 ``KeyboardInterrupt``，然后回到正常迭代。"""
        self._raise_kb = True


class _RunnerStub:
    """``module.terminal_runner`` 的内存替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def run_task(self, task, retries=0, debug=False):
        self.calls.append(("run_task", (task,), {"retries": retries, "debug": debug}))
        return True

    def run_schedule(self, task, times, retries=0, debug=False):
        self.calls.append(
            (
                "run_schedule",
                (task, list(times)),
                {"retries": retries, "debug": debug},
            )
        )
        return True


@pytest.fixture
def fake_runner(monkeypatch):
    """向 ``sys.modules`` 注入 ``module.terminal_runner`` 替身。"""
    stub = _RunnerStub()
    module = ModuleType("module.terminal_runner")
    module.run_task = stub.run_task  # type: ignore[attr-defined]
    module.run_schedule = stub.run_schedule  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.terminal_runner", module)
    monkeypatch.setitem(sys.modules, "module", SimpleNamespace(terminal_runner=module))
    yield stub


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """把所有 I/O 重定向到临时目录。

    * ``APP_ROOT`` → ``tmp_path``
    * ``EXAMPLE_CONFIG`` 拷贝一份 ``config.example.yaml`` 进去
    * 清空全局状态，避免测试间互相污染
    * 默认禁用 ``check_updates``，防止真实网络请求或意外的 backend 加载。
      专门验证更新路径的测试不应依赖此 fixture，应自行接管 ``check_updates``
      或向 ``sys.modules`` 注入 ``module.update.upstream_sync`` stub。
    """
    example_src = PROJECT_ROOT / "assets" / "config" / "config.example.yaml"
    example_dst = tmp_path / "assets" / "config" / "config.example.yaml"
    example_dst.parent.mkdir(parents=True, exist_ok=True)
    example_dst.write_text(example_src.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(terminal, "APP_ROOT", tmp_path)
    monkeypatch.setattr(terminal, "EXAMPLE_CONFIG", example_dst)
    monkeypatch.setattr(terminal, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(terminal, "SETTINGS_DIR", tmp_path / "settings")
    monkeypatch.setattr(terminal, "TERMINAL_SETTINGS_PATH", tmp_path / "settings" / "terminal.json")
    # 防止任何测试意外触发真实 upstream 检查；返回 False 视作「无更新、不阻塞」。
    monkeypatch.setattr(terminal, "check_updates", lambda interactive=True: False)

    return {
        "root": tmp_path,
        "config": tmp_path / "config.yaml",
        "settings": tmp_path / "settings" / "terminal.json",
    }


@pytest.fixture
def run_in_isolated_root(monkeypatch):
    """封装 ``main(argv)``，把 CWD 切到 ``tmp_path`` 并把 ``sys.argv`` 清掉。"""

    def _run(argv, *, tmp_path, fake_input=None):
        monkeypatch.chdir(tmp_path)
        if fake_input is not None:
            monkeypatch.setattr(builtins, "input", fake_input)
        monkeypatch.setattr(sys, "argv", ["terminal.py", *argv])
        return terminal.main(argv)

    return _run


# ─────────────────────────────────────────────────────────────────────────────
# 1. CLI 展示类指令
# ─────────────────────────────────────────────────────────────────────────────


def test_help_works_on_any_platform(monkeypatch, capsys, run_in_isolated_root, tmp_path):
    """``--help`` 在非 Darwin+arm64 平台也能用，且退出码为 0。"""
    monkeypatch.setattr(terminal, "is_supported_platform", lambda: False)
    fake = _Input()
    rc = run_in_isolated_root(["--help"], tmp_path=tmp_path, fake_input=fake)
    out = capsys.readouterr().out
    assert rc == terminal.EXIT_OK
    assert "云游戏终端" in out
    assert "内置任务" in out


def test_list_works_on_any_platform(monkeypatch, capsys, run_in_isolated_root, tmp_path):
    """``--list`` 不依赖 Darwin+arm64。"""
    monkeypatch.setattr(terminal, "is_supported_platform", lambda: False)
    rc = run_in_isolated_root(["--list"], tmp_path=tmp_path, fake_input=_Input())
    out = capsys.readouterr().out
    assert rc == terminal.EXIT_OK
    for name in terminal.TASK_CHOICES:
        assert f"- {name}" in out


# ─────────────────────────────────────────────────────────────────────────────
# 2. 平台检查
# ─────────────────────────────────────────────────────────────────────────────


def test_task_on_unsupported_platform_returns_exit_platform(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """在非 Darwin+arm64 上执行 ``--task`` 必须报 ``EXIT_PLATFORM``。"""
    monkeypatch.setattr(terminal, "is_supported_platform", lambda: False)
    (isolated_paths["settings"]).parent.mkdir(parents=True, exist_ok=True)
    isolated_paths["settings"].write_text(
        json.dumps({"initialized": True}), encoding="utf-8"
    )
    rc = run_in_isolated_root(["--task", "main"], tmp_path=tmp_path, fake_input=_Input())
    err = capsys.readouterr().err
    assert rc == terminal.EXIT_PLATFORM
    assert "macOS" in err
    # runner 不应被调用。
    assert fake_runner.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. 首次配置问答
# ─────────────────────────────────────────────────────────────────────────────


def test_first_setup_cancelled_does_not_write_anything(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """首题 EOF → 取消首配，不写 config.yaml 也不写 terminal.json。"""

    class CancelFirstQ(_Input):
        def __init__(self):
            super().__init__(responses=["n"])  # 永远不会用到

        def __call__(self, prompt: str = "") -> str:
            raise EOFError

    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=CancelFirstQ())
    captured = capsys.readouterr()
    assert rc == terminal.EXIT_OK
    assert "配置未保存" in captured.out
    assert not isolated_paths["config"].exists()
    assert not isolated_paths["settings"].exists()


def test_first_setup_completed_writes_config_and_settings(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """首配完成 → 写入两个文件，强制覆盖字段就位，terminal.json 无凭证字段。"""
    responses = [
        # use_paid_time (default N → "n")
        "n",
        # max_queue_minutes
        "30",
        # login_timeout_minutes
        "15",
        # retries (0-5)
        "3",
        # debug (default N → "n")
        "n",
        # main_loop 主菜单 → 6 退出
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    out = capsys.readouterr().out
    assert rc == terminal.EXIT_OK
    assert "已写入" in out

    assert isolated_paths["config"].exists()
    assert isolated_paths["settings"].exists()

    settings = json.loads(isolated_paths["settings"].read_text(encoding="utf-8"))
    # 仅允许的字段，且不含任何凭证。
    assert set(settings.keys()) <= {"initialized", "debug", "retries", "schedule"}
    assert settings["initialized"] is True
    assert settings["retries"] == 3
    assert "account" not in settings
    assert "token" not in settings
    assert "cookie" not in settings

    # config.yaml 含强制覆盖字段。
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    with isolated_paths["config"].open("r", encoding="utf-8") as fp:
        cfg = yaml.load(fp)
    assert cfg["cloud_game_enable"] is True
    assert cfg["browser_headless_enable"] is True  # debug=False → headless=True
    assert cfg["browser_headless_restart_on_not_logged_in"] is True
    assert cfg["browser_persistent_enable"] is True
    assert cfg["locales"] == "zh_CN"
    assert cfg["after_finish"] == "Exit"
    assert cfg["pause_after_success"] is False
    assert cfg["exit_after_failure"] is True
    assert cfg["check_update"] is False
    assert cfg["auto_update"] is False
    assert cfg["universe_enable"] is False
    assert cfg["fight_enable"] is False
    assert cfg["telemetry_enable"] is False
    # 问答答案也写入 config.yaml 对应字段。
    assert cfg["cloud_game_use_paid_time"] is False
    assert cfg["cloud_game_max_queue_time"] == 30
    assert cfg["cloud_game_login_timeout"] == 15


def test_first_setup_back_to_first_question_then_finish(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """第 2 题输入 ``b`` 应回退到第 1 题；最终仍能完成问答并写入。"""
    responses = [
        "n",   # 1. use_paid_time
        "b",   # 2. max_queue_minutes → 回到第 1 题
        "y",   # 1. use_paid_time（重新回答）
        "45",  # 2. max_queue_minutes
        "20",  # 3. login_timeout_minutes
        "2",   # 4. retries
        "n",   # 5. debug
        "6",   # 退出主菜单
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    with isolated_paths["config"].open("r", encoding="utf-8") as fp:
        cfg = yaml.load(fp)
    assert cfg["cloud_game_use_paid_time"] is True
    assert cfg["cloud_game_max_queue_time"] == 45


# ─────────────────────────────────────────────────────────────────────────────
# 4. 主菜单行为
# ─────────────────────────────────────────────────────────────────────────────


def _seed_initialized(monkeypatch, tmp_path: Path, debug: bool = False, retries: int = 2) -> Path:
    """把 ``terminal.json`` 初始化为已配置状态，跳过首配问答。"""
    settings_path = tmp_path / "settings" / "terminal.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"initialized": True, "debug": debug, "retries": retries}),
        encoding="utf-8",
    )
    return settings_path


def test_menu_run_once_calls_runner(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """菜单 1：选择任务 → 调用 ``run_task``；不在 CLI 路径上出错。"""
    _seed_initialized(monkeypatch, tmp_path)
    responses = [
        "1",        # 主菜单：执行一次
        "1",        # 子菜单：选择第 1 个任务（main）
        "6",        # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    assert any(name == "run_task" for name, _, _ in fake_runner.calls)
    task_name, _, kwargs = fake_runner.calls[0]
    assert task_name == "run_task"
    assert kwargs == {"retries": 2, "debug": False}


def test_menu_debug_run_forces_debug_true(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """菜单 4：调试运行，即使 ``terminal.json`` 中 debug=False 也要传 True。"""
    _seed_initialized(monkeypatch, tmp_path, debug=False)
    responses = [
        "4",   # 调试运行
        "3",   # 子菜单：选择第 3 个任务（daily）
        "6",   # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    assert fake_runner.calls
    _, _, kwargs = fake_runner.calls[0]
    assert kwargs["debug"] is True


def test_menu_schedule_appends_entry(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """菜单 2：选任务 + 时间 → 计划列表新增一项。"""
    _seed_initialized(monkeypatch, tmp_path)
    responses = [
        "2",                # 计划任务
        "2",                # 子菜单选 routine
        "06:30,18:45",      # 时间
        "n",                # 不再添加
        "6",                # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    schedule = terminal.load_schedule()
    assert schedule == [
        {"task": "routine", "times": ["06:30", "18:45"], "retries": 2, "debug": False}
    ]


def test_menu_show_state_prints_config_and_schedule(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """菜单 5：打印当前配置和计划；不应改变状态。"""
    _seed_initialized(monkeypatch, tmp_path)
    # 预先写一份 schedule 让展示内容可断言。
    terminal.save_schedule(
        [{"task": "daily", "times": ["12:00"], "retries": 2, "debug": False}]
    )
    responses = ["5", "6"]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    out = capsys.readouterr().out
    assert "计划列表" in out
    assert "daily" in out
    assert "12:00" in out
    # schedule 仍然存在。
    assert terminal.load_schedule()


def test_menu_zero_returns_to_main_loop(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """主菜单输入 ``0`` 等同于退出，退出码为 0。"""
    _seed_initialized(monkeypatch, tmp_path)
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(["0"]))
    assert rc == terminal.EXIT_OK


# ─────────────────────────────────────────────────────────────────────────────
# 5. 原子保存
# ─────────────────────────────────────────────────────────────────────────────


def test_terminal_settings_are_written_atomically(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``terminal.json`` 不存在中间态；写入后只允许 ``safe_keys`` 内的字段。"""
    # 触发一次首配以写入 terminal.json。
    responses = [
        "n",   # 付费时长
        "30",  # 排队
        "20",  # 登录超时
        "2",   # 重试
        "n",   # debug
        "6",   # 退出
    ]
    run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))

    # 再次启动，模拟一次原子写入（菜单 2 → schedule）。
    _seed_initialized(monkeypatch, tmp_path)
    responses2 = [
        "2",
        "4",               # power
        "06:00",
        "n",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses2))
    assert rc == terminal.EXIT_OK

    payload = json.loads(isolated_paths["settings"].read_text(encoding="utf-8"))
    assert set(payload.keys()) <= {"initialized", "debug", "retries", "schedule"}
    assert payload["initialized"] is True
    assert isinstance(payload["schedule"], list)
    assert payload["schedule"][0]["task"] == "power"


def test_terminal_settings_strips_unknown_keys(
    monkeypatch, isolated_paths
):
    """写入时即使调用方塞了 ``account`` 等字段也会被丢弃。"""
    terminal._save_terminal_settings(
        {
            "initialized": True,
            "debug": False,
            "retries": 2,
            "account": "alice@example.com",  # 应被丢弃
            "token": "secret",                # 应被丢弃
            "cookie": "k=v",                  # 应被丢弃
        }
    )
    payload = json.loads(isolated_paths["settings"].read_text(encoding="utf-8"))
    assert set(payload.keys()) == {"initialized", "debug", "retries"}


# ─────────────────────────────────────────────────────────────────────────────
# 6. Ctrl+C / EOF
# ─────────────────────────────────────────────────────────────────────────────


def test_keyboard_interrupt_during_first_setup_returns_to_previous_question(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """问答中 Ctrl+C 视为「返回上一层」，最终仍能完成。"""

    class KBThenNormal(_Input):
        def __init__(self, normal):
            super().__init__(responses=normal)
            self._kbi_done = False

        def __call__(self, prompt: str = "") -> str:
            if not self._kbi_done:
                self._kbi_done = True
                raise KeyboardInterrupt
            return super().__call__(prompt)

    responses = [
        "y",   # 1. use_paid_time
        "30",  # 2. max_queue_minutes
        "15",  # 3. login_timeout_minutes
        "3",   # 4. retries
        "n",   # 5. debug
        "6",   # 退出
    ]
    rc = run_in_isolated_root(
        [], tmp_path=tmp_path, fake_input=KBThenNormal(responses)
    )
    assert rc == terminal.EXIT_OK


def test_keyboard_interrupt_at_first_question_cancels_setup(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """首题 Ctrl+C 等同于放弃首配：不写任何文件，退出码 0。"""

    class KBCancel(_Input):
        cancelled = False
        def __call__(self, prompt: str = "") -> str:
            if not self.cancelled:
                self.cancelled = True
                raise KeyboardInterrupt
            return "6"

    rc = run_in_isolated_root(
        [], tmp_path=tmp_path, fake_input=KBCancel()
    )
    out = capsys.readouterr().out
    assert rc == terminal.EXIT_OK
    assert "返回" in out or "放弃" in out
    assert not isolated_paths["config"].exists()
    assert not isolated_paths["settings"].exists()


def test_eof_in_main_menu_exits_gracefully(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """主菜单 EOF → 优雅退出。"""
    _seed_initialized(monkeypatch, tmp_path)

    class EOFAtMenu(_Input):
        def __call__(self, prompt: str = "") -> str:
            raise EOFError

    rc = run_in_isolated_root(
        [], tmp_path=tmp_path, fake_input=EOFAtMenu()
    )
    out = capsys.readouterr().out
    assert rc == terminal.EXIT_OK
    assert "EOF" in out


# ─────────────────────────────────────────────────────────────────────────────
# 7. CLI 非交互参数
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_task_without_init_returns_not_initialized(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``--task`` 在未初始化时退出码 ``EXIT_NOT_INITIALIZED``，且不调 runner。"""
    rc = run_in_isolated_root(["--task", "main"], tmp_path=tmp_path, fake_input=_Input())
    err = capsys.readouterr().err
    assert rc == terminal.EXIT_NOT_INITIALIZED
    assert "尚未运行首次配置" in err
    assert fake_runner.calls == []


def test_cli_task_after_init_runs_runner(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``--task main`` 在已初始化时调用 ``run_task``。"""
    _seed_initialized(monkeypatch, tmp_path, debug=False, retries=1)
    rc = run_in_isolated_root(
        ["--task", "main"], tmp_path=tmp_path, fake_input=_Input()
    )
    assert rc == terminal.EXIT_OK
    assert fake_runner.calls
    name, args, kwargs = fake_runner.calls[0]
    assert name == "run_task"
    assert args == ("main",)
    assert kwargs == {"retries": 1, "debug": False}


def test_cli_task_with_debug_flag_passes_debug_true(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``--task main --debug`` 强制 debug=True。"""
    _seed_initialized(monkeypatch, tmp_path, debug=False, retries=2)
    rc = run_in_isolated_root(
        ["--task", "main", "--debug"], tmp_path=tmp_path, fake_input=_Input()
    )
    assert rc == terminal.EXIT_OK
    assert fake_runner.calls[0][2]["debug"] is True


def test_cli_schedule_saves_entry_and_starts_waiting(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``--task X --schedule HH:MM,...`` 保存计划并启动等待，不立即调用 ``run_task``。"""
    _seed_initialized(monkeypatch, tmp_path, debug=False, retries=2)
    rc = run_in_isolated_root(
        ["--task", "daily", "--schedule", "09:00,21:30"],
        tmp_path=tmp_path,
        fake_input=_Input(),
    )
    assert rc == terminal.EXIT_OK
    # 进入调度等待，不直接启动任务。
    assert fake_runner.calls[0][0] == "run_schedule"
    schedule = terminal.load_schedule()
    assert any(
        e["task"] == "daily" and e["times"] == ["09:00", "21:30"] for e in schedule
    )


def test_cli_schedule_invalid_format_returns_bad_args(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``--schedule`` 解析失败 → ``EXIT_BAD_ARGS``。"""
    _seed_initialized(monkeypatch, tmp_path)
    rc = run_in_isolated_root(
        ["--task", "daily", "--schedule", "not-a-time"],
        tmp_path=tmp_path,
        fake_input=_Input(),
    )
    err = capsys.readouterr().err
    assert rc == terminal.EXIT_BAD_ARGS
    assert "时间格式错误" in err
    assert fake_runner.calls == []


def test_cli_schedule_without_task_returns_bad_args(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``--schedule`` 单独使用 → ``EXIT_BAD_ARGS``。"""
    _seed_initialized(monkeypatch, tmp_path)
    rc = run_in_isolated_root(
        ["--schedule", "09:00"], tmp_path=tmp_path, fake_input=_Input()
    )
    assert rc == terminal.EXIT_BAD_ARGS


def test_cli_help_and_list_dont_need_runner(
    monkeypatch, capsys, run_in_isolated_root, tmp_path, isolated_paths, fake_runner
):
    """``--help``/``--list`` 不依赖 ``module.terminal_runner``。

    即使 ``module.terminal_runner`` 不在 ``sys.modules`` 中也不应失败。
    """
    monkeypatch.delitem(sys.modules, "module.terminal_runner", raising=False)
    monkeypatch.delitem(sys.modules, "module", raising=False)
    rc = run_in_isolated_root(
        ["--list"], tmp_path=tmp_path, fake_input=_Input()
    )
    assert rc == terminal.EXIT_OK


# ─────────────────────────────────────────────────────────────────────────────
# 8. 解析器细节
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_schedule_normalises_zero_padding():
    assert terminal._parse_schedule("6:30,18:5") == ["06:30", "18:05"]


def test_parse_schedule_rejects_out_of_range():
    with pytest.raises(ValueError):
        terminal._parse_schedule("24:00")
    with pytest.raises(ValueError):
        terminal._parse_schedule("9:60")


def test_parse_schedule_rejects_empty():
    with pytest.raises(ValueError):
        terminal._parse_schedule("")


def test_is_supported_platform_reads_machine_attr(monkeypatch):
    """``is_supported_platform`` 同时检查 ``sys.platform`` 和 ``platform.machine``。"""
    monkeypatch.setattr(terminal.sys, "platform", "darwin")
    monkeypatch.setattr(terminal.platform, "machine", lambda: "arm64")
    assert terminal.is_supported_platform() is True

    monkeypatch.setattr(terminal.platform, "machine", lambda: "x86_64")
    assert terminal.is_supported_platform() is False

    monkeypatch.setattr(terminal.sys, "platform", "linux")
    monkeypatch.setattr(terminal.platform, "machine", lambda: "arm64")
    assert terminal.is_supported_platform() is False


def test_runner_success_boolean_maps_to_exit_zero(monkeypatch, fake_runner):
    monkeypatch.setattr(sys.modules['module.terminal_runner'], 'run_task', lambda *a, **k: True)
    assert terminal._run_once('daily', 0, False) == 0


def test_runner_failure_boolean_maps_to_nonzero(monkeypatch, fake_runner):
    monkeypatch.setattr(sys.modules['module.terminal_runner'], 'run_task', lambda *a, **k: False)
    assert terminal._run_once('daily', 0, False) == 1


def test_zero_retries_can_be_selected(monkeypatch):
    monkeypatch.setattr(builtins, 'input', lambda _: '0')
    assert terminal._prompt_int('重试次数', minimum=0, maximum=5, default=2) == 0


def test_default_numeric_answer_works(monkeypatch):
    monkeypatch.setattr(builtins, 'input', lambda _: '')
    assert terminal._prompt_int('等待分钟', minimum=1, maximum=120, default=20) == 20


def test_settings_do_not_make_normal_runs_visible(monkeypatch, tmp_path, isolated_paths, fake_runner):
    _seed_initialized(monkeypatch, tmp_path, debug=True, retries=0)
    assert terminal.main(['--task', 'daily']) == 0
    assert fake_runner.calls[-1][2]['debug'] is False
