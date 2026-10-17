"""``terminal.py`` 终端画质菜单的单元测试。

覆盖：
    * 三档画质映射：菜单 1/2/3 分别对应 ``cloud_game_video_quality``
      的规范键 ``'high'/'traffic_saving'/'extreme_saving'``。
    * 旧四档值迁移：``'0'/'1'`` 归一为 ``high``，``'2'`` 归一为
      ``traffic_saving``，``'3'`` 归一为 ``extreme_saving``，且
      ``write_config_yaml`` 在显式提供 ``video_quality`` 时归一化。
    * 返回不保存：画质子菜单输入 ``0``/``b`` 不写 ``config.yaml``。
    * 常规设置返回：设置主菜单输入 ``0`` 返回时不触发
      ``first_time_setup``，也不写 ``config.yaml``。
    * ``action_show_state`` 中 ``cloud_game_video_quality`` 显示当前中文标签。

通过临时目录隔离 I/O，复用 ``tests/test_terminal_menu.py`` 中的 fixtures
不需要 -- 直接重复最小 fixture，避免改动已有测试。
"""

from __future__ import annotations

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
# 测试装置
# ─────────────────────────────────────────────────────────────────────────────


class _Input:
    """顺序回放 ``input``；遇到越界抛 ``EOFError``。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._iter = iter(responses or [])

    def __call__(self, prompt: str = "") -> str:  # noqa: D401 - mock 接口
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise EOFError from exc


class _RunnerStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def run_task(self, task, retries=0, debug=False):
        self.calls.append(("run_task", (task,), {"retries": retries, "debug": debug}))
        return True

    def run_schedule(self, task, times, retries=0, debug=False):
        self.calls.append(
            ("run_schedule", (task, list(times)), {"retries": retries, "debug": debug})
        )
        return True


@pytest.fixture
def fake_runner(monkeypatch):
    stub = _RunnerStub()
    module = ModuleType("module.terminal_runner")
    module.run_task = stub.run_task  # type: ignore[attr-defined]
    module.run_schedule = stub.run_schedule  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.terminal_runner", module)
    monkeypatch.setitem(sys.modules, "module", SimpleNamespace(terminal_runner=module))
    yield stub


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """把所有 I/O 隔离到临时目录。

    同时把 ``terminal.check_updates`` 替换为无害 stub，避免任何测试触发
    真实网络或意外的 backend 加载。专门验证更新路径的测试不应复用此
    fixture（应自行接管 ``check_updates``）。
    """
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
            import builtins

            monkeypatch.setattr(builtins, "input", fake_input)
        monkeypatch.setattr(sys, "argv", ["terminal.py", *argv])
        return terminal.main(argv)

    return _run


def _seed_initialized(tmp_path: Path) -> Path:
    """让 ``terminal.json`` 显示为已配置，跳过首配。"""
    settings_path = tmp_path / "settings" / "terminal.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"initialized": True, "debug": False, "retries": 2}),
        encoding="utf-8",
    )
    return settings_path


def _load_config_value(path: Path, key: str):
    """读取 config.yaml 中的标量字段；缺失时返回 None。"""
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.load(fp)
    if data is None:
        return None
    return data.get(key)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 三档画质映射
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "menu_choice,expected",
    [
        ("1", "high"),           # 高画质
        ("2", "traffic_saving"), # 省流画质
        ("3", "extreme_saving"), # 极致省流
    ],
)
def test_cloud_game_video_quality_mapping(
    monkeypatch,
    capsys,
    run_in_isolated_root,
    tmp_path,
    isolated_paths,
    fake_runner,
    menu_choice,
    expected,
):
    """设置 → 云游戏画质 → 选 1/2/3 后，写入 ``utils.cloud_quality`` 的规范键。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",       # 主菜单：进入「设置」
        "2",       # 设置子菜单：云游戏画质
        menu_choice,  # 画质档次
        "0",       # 返回设置子菜单
        "0",       # 返回主菜单
        "6",       # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    assert isolated_paths["config"].exists()
    quality = _load_config_value(isolated_paths["config"], "cloud_game_video_quality")
    assert quality == expected


# ─────────────────────────────────────────────────────────────────────────────
# 2. 返回不保存
# ─────────────────────────────────────────────────────────────────────────────


def test_video_quality_submenu_back_does_not_write(
    monkeypatch,
    capsys,
    run_in_isolated_root,
    tmp_path,
    isolated_paths,
    fake_runner,
):
    """画质子菜单输入 ``0`` 返回：不触发 ``write_config_yaml``，
    ``cloud_game_video_quality`` 保持示例配置里的注释状态（即未写入）。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",   # 主菜单：设置
        "2",   # 设置子菜单：画质
        "0",   # 直接返回 → 不保存
        "0",   # 返回主菜单
        "6",   # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    # settings 子动作不应写 config.yaml：主菜单里没有任何「执行一次/计划/
    # 调试运行」动作，config.yaml 应保持空（示例配置的全部都是被强制覆盖
    # 才会写，第一题未回答，所以不会写任何字段）。
    assert not isolated_paths["config"].exists()


def test_video_quality_submenu_b_does_not_write(
    monkeypatch,
    capsys,
    run_in_isolated_root,
    tmp_path,
    isolated_paths,
    fake_runner,
):
    """画质子菜单输入 ``b`` 返回：同样不写 config.yaml。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "2",
        "b",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    assert not isolated_paths["config"].exists()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 常规设置返回
# ─────────────────────────────────────────────────────────────────────────────


def test_settings_menu_zero_returns_without_first_setup(
    monkeypatch,
    capsys,
    run_in_isolated_root,
    tmp_path,
    isolated_paths,
    fake_runner,
):
    """设置子菜单输入 ``0`` 直接返回：不进入 first_time_setup，
    不写任何文件。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",   # 主菜单：设置
        "0",   # 直接返回 → 不应触发首配问答
        "6",   # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    out = capsys.readouterr().out
    assert rc == terminal.EXIT_OK
    # 屏幕里出现「设置」菜单头。
    assert "── 运行设置 ──" in out
    # 不应触发首配问答，所以不应看到「已写入」。
    assert "已写入" not in out
    # 配置文件状态不变：terminal.json 仍由 _seed_initialized 提供。
    payload = json.loads(isolated_paths["settings"].read_text(encoding="utf-8"))
    assert payload["initialized"] is True
    # 没有触发首配问答，未写过 config.yaml。
    assert not isolated_paths["config"].exists()


def test_settings_menu_b_returns_without_first_setup(
    monkeypatch,
    capsys,
    run_in_isolated_root,
    tmp_path,
    isolated_paths,
    fake_runner,
):
    """设置子菜单输入 ``b`` 直接返回：不触发首配问答。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "b",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    assert not isolated_paths["config"].exists()


# ─────────────────────────────────────────────────────────────────────────────
# 4. write_config_yaml 直接行为
# ─────────────────────────────────────────────────────────────────────────────


def test_write_config_yaml_without_video_quality_preserves_field(
    isolated_paths,
):
    """``write_config_yaml`` 不提供 ``video_quality`` 时不应触碰
    ``cloud_game_video_quality``；再次调用不带视频参数时该值保持不变。"""
    # 第一次写入：设置画质为旧码 '2'，应被 ``normalize_quality`` 归一为
    # ``traffic_saving``。
    terminal.write_config_yaml(answers={"video_quality": "2"})
    first = _load_config_value(isolated_paths["config"], "cloud_game_video_quality")
    assert first == "traffic_saving"

    # 第二次写入：完全不传 answers，不应改画质。
    terminal.write_config_yaml()
    second = _load_config_value(isolated_paths["config"], "cloud_game_video_quality")
    assert second == "traffic_saving"


@pytest.mark.parametrize(
    "legacy,expected",
    [
        ("0", "high"),
        ("1", "high"),
        ("2", "traffic_saving"),
        ("3", "extreme_saving"),
    ],
)
def test_write_config_yaml_normalizes_legacy_quality(
    isolated_paths, legacy, expected
):
    """旧四档 ``'0'/'1'/'2'/'3'`` 在 ``write_config_yaml`` 中仍被识别，
    并归一为三档规范键。"""
    terminal.write_config_yaml(answers={"video_quality": legacy})
    quality = _load_config_value(isolated_paths["config"], "cloud_game_video_quality")
    assert quality == expected


# ─────────────────────────────────────────────────────────────────────────────
# 5. action_show_state 中画质显示
# ─────────────────────────────────────────────────────────────────────────────


def test_show_state_displays_screen_and_video_quality(
    monkeypatch,
    capsys,
    run_in_isolated_root,
    tmp_path,
    isolated_paths,
    fake_runner,
):
    """菜单 5：在 ``show state`` 输出中应包含 ``screen: 1920×1080``
    与 ``cloud_game_video_quality``（中文标签）。"""
    _seed_initialized(tmp_path)
    # 预先写一份带画质值的 config.yaml（旧码 '1' → high）。
    terminal.write_config_yaml(answers={"video_quality": "1"})

    responses = ["5", "6"]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    out = capsys.readouterr().out
    assert "1920×1080" in out
    assert "cloud_game_video_quality" in out
    assert "高画质" in out
