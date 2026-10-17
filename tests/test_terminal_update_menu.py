"""``terminal.check_updates`` 启动期更新检查的单元测试。

设计要点：
    * **直接走真实 wrapper** ``terminal.check_updates``：不替换顶层
      函数，只向 ``sys.modules["module.update.upstream_sync"]`` 注入 stub
      模块（因为 ``module.update.upstream_sync`` 暂未在仓库中存在，由后端
      负责人实现）。这样能验证前端封装的完整分支：
      ``importlib.import_module`` → ``check_for_updates`` → ``apply_update``。
    * **不依赖也不污染** ``tests/test_terminal_menu.py`` 的 ``isolated_paths``
      fixture；本文件自带最小隔离夹具（只需把 I/O 重定向到 ``tmp_path``，
      不替换 ``check_updates``）。
    * **不联网**：所有 backend 调用由 stub 提供；stub 抛出网络异常时
      wrapper 应静默处理并返回 ``False``。

测试覆盖：
    1. main 启动时先调用 ``check_updates``（Y 路径才会进入 main_loop 之后
       才是「退出」）—— 真实 wrapper + stub backend。
    2. 已是最新：不下载、不应用，返回 ``False``。
    3. 有更新 + 用户答 Y：调用 ``apply_update``，返回 ``True``，提示重启。
    4. 有更新 + 用户答 N：不调用 ``apply_update``，返回 ``False``。
    5. 有更新 + 用户在提示处输入 ``0``/``b``：视为取消，不下载，返回 ``False``。
    6. 网络失败（``check_for_updates`` 抛异常）：打印错误但不崩溃，
       返回 ``False``，后续任务可以继续（验证主流程因此不被阻塞）。
    7. 非交互 + 有更新：仅提示命令字，不下载、不退出，返回 ``False``。
    8. 主菜单 7 选项：可独立触发检查；成功应用时 ``action_check_updates``
       返回 ``EXIT_OK``，但调用 ``main_loop`` 真正的判断逻辑需要
       ``check_updates`` 返回 ``True``（通过 stub 验证 main_loop 行为）。
    9. ``--check-update`` CLI：独立模式不进入 main_loop，按 stub 表现退出。
    10. ``--list`` 输出包含「7  检查更新」入口。
"""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import terminal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 测试装置：复制必要的最小夹具，但不修改 check_updates。
# ─────────────────────────────────────────────────────────────────────────────


class _Input:
    """最小 ``input`` 回放器。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._iter = iter(responses or [])

    def __call__(self, prompt: str = "") -> str:  # noqa: D401 - mock 接口
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise EOFError from exc


@pytest.fixture
def isolated_root(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """仅隔离 I/O，不触碰 ``check_updates``。"""
    example_src = PROJECT_ROOT / "assets" / "config" / "config.example.yaml"
    example_dst = tmp_path / "assets" / "config" / "config.example.yaml"
    example_dst.parent.mkdir(parents=True, exist_ok=True)
    example_dst.write_text(example_src.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(terminal, "APP_ROOT", tmp_path)
    monkeypatch.setattr(terminal, "EXAMPLE_CONFIG", example_dst)
    monkeypatch.setattr(terminal, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(terminal, "SETTINGS_DIR", tmp_path / "settings")
    monkeypatch.setattr(
        terminal, "TERMINAL_SETTINGS_PATH", tmp_path / "settings" / "terminal.json"
    )

    return {
        "root": tmp_path,
        "config": tmp_path / "config.yaml",
        "settings": tmp_path / "settings" / "terminal.json",
    }


class _SyncStub:
    """``module.update.upstream_sync`` 内存替身。

    字段：
        * ``info_to_return``：``check_for_updates`` 返回的 ``UpdateInfo``。
        * ``raise_on_check``：让 ``check_for_updates`` 抛异常（模拟网络失败）。
        * ``apply_calls``：每次 ``apply_update`` 调用的入参。
        * ``apply_return``：``apply_update`` 的返回值 ``UpdateResult``。
        * ``apply_raises``：让 ``apply_update`` 抛异常（模拟合并失败）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.info_to_return: Any = None
        self.raise_on_check: Exception | None = None
        self.apply_calls: list[tuple[Any, Path]] = []
        self.apply_return: Any = None
        self.apply_raises: Exception | None = None

    def check_for_updates(self, root: Path):
        self.calls.append(("check_for_updates", (root,)))
        if self.raise_on_check is not None:
            raise self.raise_on_check
        return self.info_to_return

    def apply_update(self, info, root: Path):
        self.calls.append(("apply_update", (info, root)))
        self.apply_calls.append((info, root))
        if self.apply_raises is not None:
            raise self.apply_raises
        return self.apply_return


def _make_info(*, baseline: str, target: str, available: bool):
    """构造 ``UpdateInfo`` 兼容对象（``SimpleNamespace`` 即可，wrapper 只读属性）。"""
    return SimpleNamespace(baseline=baseline, target=target, available=available)


def _make_result(*, changed: int, ignored: int, backup_dir: str):
    return SimpleNamespace(changed=changed, ignored=ignored, backup_dir=backup_dir)


@pytest.fixture
def sync_stub(monkeypatch) -> _SyncStub:
    """向 ``sys.modules`` 注入 ``module.update.upstream_sync`` stub。

    直接把 ``module.update`` 整体替换为一个新的 ``ModuleType``（避免对
    真实 ``module.update`` 包做 ``setattr`` —— 它的 ``__init__`` 是空
    的，但对象本身可能不支持属性添加）。然后用 stub 作为其
    ``upstream_sync`` 子模块。
    """
    stub = _SyncStub()
    sync_mod = ModuleType("module.update.upstream_sync")
    sync_mod.check_for_updates = stub.check_for_updates  # type: ignore[attr-defined]
    sync_mod.apply_update = stub.apply_update  # type: ignore[attr-defined]
    sync_mod.UpdateInfo = SimpleNamespace  # type: ignore[attr-defined]
    sync_mod.UpdateResult = SimpleNamespace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.update.upstream_sync", sync_mod)

    # 把 ``module.update`` 整体替换为轻量占位包，仅暴露 ``upstream_sync``。
    update_pkg = ModuleType("module.update")
    update_pkg.upstream_sync = sync_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.update", update_pkg)

    return stub


# ─────────────────────────────────────────────────────────────────────────────
# 直接调用 wrapper：单元层
# ─────────────────────────────────────────────────────────────────────────────


def test_check_updates_already_latest_returns_false(
    capsys, isolated_root, sync_stub, monkeypatch
):
    """已是最新：不调用 apply_update，返回 False。"""
    sync_stub.info_to_return = _make_info(baseline="abc123", target="abc123", available=False)
    monkeypatch.setattr(builtins, "input", _Input([]))

    rc = terminal.check_updates(interactive=True)

    assert rc is False
    assert any(name == "check_for_updates" for name, _ in sync_stub.calls)
    assert not sync_stub.apply_calls
    out = capsys.readouterr().out
    assert "已是最新" in out


def test_check_updates_available_user_says_yes_applies(
    capsys, isolated_root, sync_stub, monkeypatch
):
    """有更新 + 用户答 Y → 调用 apply_update 并返回 True。"""
    sync_stub.info_to_return = _make_info(baseline="abc123", target="def456", available=True)
    sync_stub.apply_return = _make_result(changed=3, ignored=1, backup_dir="/tmp/bak")
    monkeypatch.setattr(builtins, "input", _Input(["y"]))

    rc = terminal.check_updates(interactive=True)

    assert rc is True
    assert len(sync_stub.apply_calls) == 1
    info_arg, root_arg = sync_stub.apply_calls[0]
    assert info_arg is sync_stub.info_to_return
    assert root_arg == isolated_root["root"]
    out = capsys.readouterr().out
    assert "def456" in out  # local → target 信息
    assert "合并 3" in out
    assert "忽略 1" in out
    assert "/tmp/bak" in out
    assert "重启" in out


def test_check_updates_available_user_says_no_does_not_apply(
    capsys, isolated_root, sync_stub, monkeypatch
):
    """有更新 + 用户答 N → 不调用 apply_update，返回 False。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)
    monkeypatch.setattr(builtins, "input", _Input(["n"]))

    rc = terminal.check_updates(interactive=True)

    assert rc is False
    assert not sync_stub.apply_calls
    out = capsys.readouterr().out
    assert "跳过更新" in out


def test_check_updates_available_user_cancel_via_b(
    capsys, isolated_root, sync_stub, monkeypatch
):
    """有更新 + 用户输入 ``b`` → 视为取消，不下载，返回 False。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)
    # ``_BackSignal`` 由 ``_prompt`` 在收到 b/0 时抛出；
    # 这里直接验证 wrapper 在``_BackSignal``下干净退出。
    from terminal import _BackSignal  # noqa: WPS433 - 测试内部符号

    def fake_prompt(text, *, default=None):
        raise _BackSignal()

    monkeypatch.setattr(terminal, "_prompt", fake_prompt)

    rc = terminal.check_updates(interactive=True)

    assert rc is False
    assert not sync_stub.apply_calls
    out = capsys.readouterr().out
    assert "取消更新检查" in out


def test_check_updates_network_failure_continues(
    capsys, isolated_root, sync_stub, monkeypatch
):
    """网络失败：打印错误但不崩溃，返回 False。"""
    sync_stub.raise_on_check = RuntimeError("网络不可达")
    monkeypatch.setattr(builtins, "input", _Input([]))

    rc = terminal.check_updates(interactive=True)

    assert rc is False
    assert not sync_stub.apply_calls
    err = capsys.readouterr().err
    assert "检查更新失败" in err
    assert "网络不可达" in err


def test_check_updates_apply_failure_does_not_crash(
    capsys, isolated_root, sync_stub, monkeypatch
):
    """合并失败：返回 False，不让 wrapper 把异常抛给调用方。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)
    sync_stub.apply_raises = RuntimeError("合并冲突")
    monkeypatch.setattr(builtins, "input", _Input(["y"]))

    rc = terminal.check_updates(interactive=True)

    assert rc is False
    assert sync_stub.apply_calls  # 确实尝试过
    err = capsys.readouterr().err
    assert "应用更新失败" in err
    assert "合并冲突" in err


def test_check_updates_noninteractive_with_available_skips(
    capsys, isolated_root, sync_stub, monkeypatch
):
    """非交互 + 有更新：仅提示命令字，不下载、不退出，返回 False。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)
    # ``input`` 不应被调用；如果被调用也是个小隐患，验证没读任何东西。
    monkeypatch.setattr(builtins, "input", _Input([]))

    rc = terminal.check_updates(interactive=False)

    assert rc is False
    assert not sync_stub.apply_calls
    out = capsys.readouterr().out
    assert "--check-update" in out


def test_check_updates_no_backend_module_is_noop(capsys, isolated_root, monkeypatch):
    """后端模块未提供时：wrapper 静默返回 False，不阻塞。"""
    # 删除模块注入并阻止重新导入。
    monkeypatch.delitem(sys.modules, "module.update.upstream_sync", raising=False)

    def fake_import(name, *args, **kwargs):
        if name == "module.update.upstream_sync":
            raise ImportError("not implemented yet")
        return original_import(name, *args, **kwargs)

    original_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", fake_import)

    rc = terminal.check_updates(interactive=True)
    assert rc is False


# ─────────────────────────────────────────────────────────────────────────────
# 集成层：CLI / main_loop 行为
# ─────────────────────────────────────────────────────────────────────────────


def _seed_initialized(tmp_path: Path, *, debug: bool = False, retries: int = 2) -> Path:
    """跳过首配，把 terminal.json 标记为已配置。"""
    settings_path = tmp_path / "settings" / "terminal.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"initialized": True, "debug": debug, "retries": retries}),
        encoding="utf-8",
    )
    return settings_path


def _run_with_args(
    monkeypatch,
    argv: list[str],
    *,
    tmp_path: Path,
    fake_input: _Input | None = None,
):
    """封装 ``terminal.main(argv)``：chdir + 替换 input + 清空 sys.argv。"""
    monkeypatch.chdir(tmp_path)
    if fake_input is not None:
        monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(sys, "argv", ["terminal.py", *argv])
    return terminal.main(argv)


def test_main_runs_check_update_before_first_qa(
    monkeypatch, isolated_root, sync_stub, capsys
):
    """``main`` 在平台检查通过后、首次问答之前调用 ``check_updates``。

    stub 返回「已是最新」，则正常进入首配问答并完成。
    """
    sync_stub.info_to_return = _make_info(baseline="x", target="x", available=False)

    # 把 stdin 标记为 TTY（interactive=True），但实际用 stub 接管 input。
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    responses = [
        # 首次问答
        "n",
        "30",
        "15",
        "2",
        "n",
        # main_loop
        "6",
    ]
    rc = _run_with_args(
        monkeypatch,
        [],
        tmp_path=isolated_root["root"],
        fake_input=_Input(responses),
    )
    assert rc == terminal.EXIT_OK
    # wrapper 真的被调用了
    assert any(name == "check_for_updates" for name, _ in sync_stub.calls)
    # 未尝试应用更新
    assert not sync_stub.apply_calls
    # 首配正常写入
    assert isolated_root["config"].exists()
    assert isolated_root["settings"].exists()


def test_main_noninteractive_with_update_suggests_command(
    monkeypatch, isolated_root, sync_stub, capsys
):
    """非交互 stdin 下：检测到更新只打印命令，不下载，正常进首配。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)

    # 假装 stdin 不是 TTY。
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    # 提供完整首配 + 主菜单 6 退出。
    responses = [
        "n", "30", "15", "2", "n",  # 首配
        "6",                          # 主菜单退出
    ]
    rc = _run_with_args(
        monkeypatch,
        [],
        tmp_path=isolated_root["root"],
        fake_input=_Input(responses),
    )
    assert rc == terminal.EXIT_OK
    assert any(name == "check_for_updates" for name, _ in sync_stub.calls)
    assert not sync_stub.apply_calls  # 没自动下载
    out = capsys.readouterr().out
    assert "--check-update" in out  # 提示用户手动跑


def test_menu_seven_check_updates_triggers_apply(
    monkeypatch, isolated_root, sync_stub, capsys
):
    """主菜单 7：选 7 → 询问 → 答 Y → 返回 True → main_loop 退出 0。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)
    sync_stub.apply_return = _make_result(changed=2, ignored=0, backup_dir="/tmp/bak7")
    _seed_initialized(isolated_root["root"])

    responses = [
        "7",  # 主菜单：检查更新
        "y",  # 确认下载并合并
    ]
    rc = _run_with_args(
        monkeypatch,
        [],
        tmp_path=isolated_root["root"],
        fake_input=_Input(responses),
    )
    assert rc == terminal.EXIT_OK
    assert sync_stub.apply_calls
    out = capsys.readouterr().out
    assert "合并 2" in out
    assert "重启" in out


def test_menu_seven_check_updates_skips_on_no(
    monkeypatch, isolated_root, sync_stub, capsys
):
    """主菜单 7 + 用户答 N：不下载，正常回到菜单，最后用 6 退出。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)
    _seed_initialized(isolated_root["root"])

    responses = [
        "7",  # 检查更新
        "n",  # 不下载
        "6",  # 退出
    ]
    rc = _run_with_args(
        monkeypatch,
        [],
        tmp_path=isolated_root["root"],
        fake_input=_Input(responses),
    )
    assert rc == terminal.EXIT_OK
    assert not sync_stub.apply_calls


def test_cli_check_update_does_not_enter_main_loop(
    monkeypatch, isolated_root, sync_stub, capsys
):
    """``--check-update`` 独立模式：直接走 wrapper，不进入 main_loop。"""
    sync_stub.info_to_return = _make_info(baseline="v1", target="v2", available=True)
    sync_stub.apply_return = _make_result(changed=1, ignored=0, backup_dir="/tmp/cli-bak")

    # 把 stdin 标记为 TTY；``--check-update`` 总是 interactive=True。
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    rc = _run_with_args(
        monkeypatch,
        ["--check-update"],
        tmp_path=isolated_root["root"],
        fake_input=_Input(["y"]),
    )
    assert rc == terminal.EXIT_OK
    assert sync_stub.apply_calls  # 走了 apply
    # 首配问答不应该被触发。
    assert not isolated_root["config"].exists()


def test_cli_list_shows_check_update_entry(
    monkeypatch, isolated_root, capsys
):
    """``--list`` 输出应包含「检查更新」入口。"""
    rc = _run_with_args(
        monkeypatch,
        ["--list"],
        tmp_path=isolated_root["root"],
        fake_input=_Input([]),
    )
    assert rc == terminal.EXIT_OK
    out = capsys.readouterr().out
    assert "检查更新" in out
    assert "--check-update" in out


def test_check_updates_help_and_list_skip_backend(
    monkeypatch, isolated_root, capsys
):
    """``--help`` / ``--list`` 不应触达 backend 模块。"""
    sync_mod = ModuleType("module.update.upstream_sync")
    sentinel = {"called": False}

    def must_not_be_called(*a, **k):  # pragma: no cover - 防御
        sentinel["called"] = True
        raise AssertionError("backend 不应在 --help/--list 触发")

    sync_mod.check_for_updates = must_not_be_called  # type: ignore[attr-defined]
    sync_mod.apply_update = must_not_be_called  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.update.upstream_sync", sync_mod)

    for args in (["--help"], ["--list"]):
        rc = _run_with_args(
            monkeypatch,
            args,
            tmp_path=isolated_root["root"],
            fake_input=_Input([]),
        )
        assert rc == terminal.EXIT_OK

    assert not sentinel["called"]


def test_check_updates_failure_does_not_block_main(
    monkeypatch, isolated_root, sync_stub, capsys
):
    """启动期 ``check_updates`` 抛网络异常：``main`` 仍能正常进入首配/任务。"""
    sync_stub.raise_on_check = RuntimeError("dns fail")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    responses = [
        "n", "30", "15", "2", "n",  # 首配
        "6",                          # 退出
    ]
    rc = _run_with_args(
        monkeypatch,
        [],
        tmp_path=isolated_root["root"],
        fake_input=_Input(responses),
    )
    assert rc == terminal.EXIT_OK
    assert not sync_stub.apply_calls
    assert isolated_root["config"].exists()
    err = capsys.readouterr().err
    assert "检查更新失败" in err


def test_failed_recovery_blocks_startup_before_any_task(monkeypatch, tmp_path):
    class RecoveryError(RuntimeError):
        pass
    def fail(root):
        raise RecoveryError('待恢复的更新')
    backend = ModuleType('module.update.upstream_sync')
    backend.UpdateRecoveryError = RecoveryError
    backend.check_for_updates = fail
    monkeypatch.setitem(sys.modules, 'module.update.upstream_sync', backend)
    monkeypatch.setattr(terminal, 'APP_ROOT', tmp_path)
    monkeypatch.setattr(terminal, 'TERMINAL_SETTINGS_PATH', tmp_path / 'terminal.json')
    monkeypatch.setattr(terminal, 'is_supported_platform', lambda: True)
    main_loop = mock.Mock()
    monkeypatch.setattr(terminal, 'main_loop', main_loop)
    assert terminal.main([]) == terminal.EXIT_RUNTIME
    main_loop.assert_not_called()


def test_eof_at_update_confirmation_does_not_download(monkeypatch, tmp_path):
    backend = ModuleType('module.update.upstream_sync')
    backend.check_for_updates = lambda root: SimpleNamespace(available=True, baseline='a'*40, target='b'*40)
    backend.apply_update = mock.Mock()
    monkeypatch.setitem(sys.modules, 'module.update.upstream_sync', backend)
    monkeypatch.setattr(builtins, 'input', _Input([]))
    assert terminal.check_updates(interactive=True) is False
    backend.apply_update.assert_not_called()


def test_invalid_answer_reprompts_without_applying(monkeypatch):
    backend = ModuleType('module.update.upstream_sync')
    backend.check_for_updates = lambda root: SimpleNamespace(available=True, baseline='a'*40, target='b'*40)
    backend.apply_update = mock.Mock()
    monkeypatch.setitem(sys.modules, 'module.update.upstream_sync', backend)
    monkeypatch.setattr(builtins, 'input', _Input(['invalid', 'N']))
    assert terminal.check_updates(interactive=True) is False
    backend.apply_update.assert_not_called()
