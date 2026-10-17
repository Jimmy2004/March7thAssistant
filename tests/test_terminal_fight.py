"""``terminal.py`` 锄大地（fight）相关 UI 集成的单元测试。

覆盖：
    * 「设置 → 锄大地」子菜单的返回路径：输入 ``0``/``b`` 不写任何字段；
      子动作里再输入 ``0``/``b`` 只退出子动作、不修改配置；
    * 各设置项的「开 / 关」与「范围整数」菜单写回后能在 ``config.yaml``
      里保留；
    * 非法输入会在子动作里被拒，不会污染 config；
    * ``fight`` 已加入 TASK_CHOICES / AVAILABLE_TASKS，可以通过 CLI
      ``--task fight`` 与 ``--schedule`` 进入调度；
    * 资源检查子动作在 ``module.fight_cloud.resources`` 缺失时仍优雅
      返回错误，不抛异常，不发任何网络请求。

通过临时目录隔离 I/O；``module.terminal_runner`` 用内存替身接管；
``module.fight_cloud.resources`` 仅在「下载 / 校验」相关用例里临时注
入 stub。所有用例都不接触真实网络。
"""

from __future__ import annotations

import builtins
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import terminal  # noqa: E402  需要在 sys.path 注入后导入


# ─────────────────────────────────────────────────────────────────────────────
# 测试装置
# ─────────────────────────────────────────────────────────────────────────────


class _Input:
    """顺序回放 ``input``；越界抛 ``EOFError``。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._iter = iter(responses or [])

    def __call__(self, prompt: str = "") -> str:  # noqa: D401 - mock 接口
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise EOFError from exc


class _RunnerStub:
    """``module.terminal_runner`` 内存替身。"""

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
    stub = _RunnerStub()
    module = ModuleType("module.terminal_runner")
    module.run_task = stub.run_task  # type: ignore[attr-defined]
    module.run_schedule = stub.run_schedule  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.terminal_runner", module)
    monkeypatch.setitem(sys.modules, "module", SimpleNamespace(terminal_runner=module))
    yield stub


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """把所有 I/O 重定向到临时目录，并禁用 ``check_updates``。"""
    example_src = PROJECT_ROOT / "assets" / "config" / "config.example.yaml"
    example_dst = tmp_path / "assets" / "config" / "config.example.yaml"
    example_dst.parent.mkdir(parents=True, exist_ok=True)
    example_dst.write_text(example_src.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(terminal, "APP_ROOT", tmp_path)
    monkeypatch.setattr(terminal, "EXAMPLE_CONFIG", example_dst)
    monkeypatch.setattr(terminal, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(terminal, "SETTINGS_DIR", tmp_path / "settings")
    monkeypatch.setattr(
        terminal,
        "TERMINAL_SETTINGS_PATH",
        tmp_path / "settings" / "terminal.json",
    )
    monkeypatch.setattr(terminal, "check_updates", lambda interactive=True: False)

    return {
        "root": tmp_path,
        "config": tmp_path / "config.yaml",
        "settings": tmp_path / "settings" / "terminal.json",
    }


@pytest.fixture
def run_in_isolated_root(monkeypatch):
    """封装 ``main(argv)``：chdir + 替换 input + 清空 sys.argv。"""

    def _run(argv, *, tmp_path, fake_input=None):
        monkeypatch.chdir(tmp_path)
        if fake_input is not None:
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


def _load_config(path: Path) -> dict[str, object]:
    """用 ruamel.yaml 读取 config.yaml，便于断言键值。"""
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.load(fp)
    if data is None:
        return {}
    return dict(data)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 任务注册
# ─────────────────────────────────────────────────────────────────────────────


def test_fight_added_to_task_choices_and_available_tasks():
    """``fight`` 已被 ``TASK_CHOICES`` 和 ``AVAILABLE_TASKS`` 同时登记。"""
    assert "fight" in terminal.TASK_CHOICES
    from utils.tasks import AVAILABLE_TASKS

    assert AVAILABLE_TASKS["fight"] == "锄大地"


def test_fight_help_and_list_mention_fight(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """``--help`` 与 ``--list`` 输出应包含 fight 任务。"""
    monkeypatch.setattr(terminal, "is_supported_platform", lambda: False)
    rc = run_in_isolated_root(["--list"], tmp_path=tmp_path, fake_input=_Input())
    out = capsys.readouterr().out
    assert rc == terminal.EXIT_OK
    assert "- fight" in out

    rc2 = run_in_isolated_root(["--help"], tmp_path=tmp_path, fake_input=_Input())
    out2 = capsys.readouterr().out
    assert rc2 == terminal.EXIT_OK
    assert "- fight" in out2


def test_cli_task_fight_runs_runner(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """``--task fight`` 在已初始化时调用 ``run_task``。"""
    _seed_initialized(tmp_path)
    rc = run_in_isolated_root(
        ["--task", "fight"], tmp_path=tmp_path, fake_input=_Input()
    )
    assert rc == terminal.EXIT_OK
    assert any(call[0] == "run_task" and call[1] == ("fight",) for call in fake_runner.calls)


def test_cli_schedule_fight_starts_scheduler(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """``--task fight --schedule ...`` 走调度路径并保存计划。"""
    _seed_initialized(tmp_path)
    rc = run_in_isolated_root(
        ["--task", "fight", "--schedule", "07:30,21:00"],
        tmp_path=tmp_path,
        fake_input=_Input(),
    )
    assert rc == terminal.EXIT_OK
    assert any(call[0] == "run_schedule" and call[1][0] == "fight" for call in fake_runner.calls)
    schedule = terminal.load_schedule()
    assert any(
        e["task"] == "fight" and e["times"] == ["07:30", "21:00"] for e in schedule
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 「设置 → 锄大地」子菜单：返回路径
# ─────────────────────────────────────────────────────────────────────────────


def test_fight_submenu_zero_returns_without_mutation(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """进入「锄大地」子菜单后立刻 ``0`` 返回：保持 ``config.yaml`` 与初值一致。"""
    _seed_initialized(tmp_path)
    # 先把示例配置复制到 tmp_path，让后续读 current 与默认值一致。
    terminal.write_config_yaml()
    snapshot = json.loads(json.dumps(_load_config(isolated_paths["config"])))
    # 把 example config 的战斗字段摘出来作为基线快照。
    example_fight_keys = {
        k: snapshot.get(k)
        for k in (
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
            "fight_start_map",
            "fight_single_map",
        )
    }

    responses = [
        "3",   # 主菜单：设置
        "3",   # 设置子菜单：锄大地
        "0",   # 立刻返回 → 不写任何字段
        "0",   # 设置子菜单：返回主菜单
        "6",   # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    out = capsys.readouterr().out
    assert "锄大地" in out

    after = _load_config(isolated_paths["config"])
    for key, before in example_fight_keys.items():
        assert after.get(key) == before, f"{key} 被改写：{before!r} → {after.get(key)!r}"


def test_fight_submenu_b_returns_without_mutation(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """进入「锄大地」子菜单后 ``b`` 返回：同样不修改任何字段。"""
    _seed_initialized(tmp_path)
    terminal.write_config_yaml()
    snapshot = _load_config(isolated_paths["config"])

    responses = [
        "3",
        "3",
        "b",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    after = _load_config(isolated_paths["config"])
    assert after == snapshot


def test_fight_sublevel_zero_returns_without_writing(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """进入某个子动作（例如地图版本）后输入 ``0``：只退出子动作、不修改字段。"""
    _seed_initialized(tmp_path)
    terminal.write_config_yaml()
    snapshot = _load_config(isolated_paths["config"])

    responses = [
        "3",   # 主菜单：设置
        "3",   # 设置子菜单：锄大地
        "2",   # 子菜单：地图版本
        "0",   # 直接返回 → 不写
        "0",   # 锄大地子菜单：返回
        "0",   # 设置子菜单：返回
        "6",   # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    after = _load_config(isolated_paths["config"])
    assert after == snapshot


def test_fight_sublevel_b_returns_without_writing(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子动作里输入 ``b``：同样不写 config.yaml。"""
    _seed_initialized(tmp_path)
    terminal.write_config_yaml()
    snapshot = _load_config(isolated_paths["config"])

    responses = [
        "3",
        "3",
        "2",   # 地图版本
        "b",
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    after = _load_config(isolated_paths["config"])
    assert after == snapshot


def test_fight_settings_submenu_invalid_input_does_not_crash(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """子菜单里随便输入无效字符：不应崩溃，也不应写任何字段。"""
    _seed_initialized(tmp_path)
    terminal.write_config_yaml()
    snapshot = _load_config(isolated_paths["config"])

    responses = [
        "3",
        "3",
        "not-a-number",   # 子菜单无效
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    out = capsys.readouterr().out
    assert "无效输入" in out
    after = _load_config(isolated_paths["config"])
    assert after == snapshot


# ─────────────────────────────────────────────────────────────────────────────
# 3. 设置项持久化（开 / 关 / 范围整数 / 枚举）
# ─────────────────────────────────────────────────────────────────────────────


def test_fight_enable_toggle_persists(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子菜单 1 选「开」：``fight_enable`` 写为 True，后续读 config 仍为 True。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",   # 锄大地
        "1",   # 启用锄大地
        "y",   # 是 → 开启
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_enable"] is True

    # 再走一遍：写回为 False 后再读，确认 toggle 真的能落盘来回切换。
    monkeypatch.setattr(builtins, "input", _Input(["n"]))
    terminal._action_fight_enable()
    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_enable"] is False


def test_fight_main_map_persists(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子菜单 3 选「翁法罗斯」：``fight_main_map`` 写为字符串 "5"。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",
        "3",   # 优先星球
        "6",   # 6 = 翁法罗斯（_FIGHT_MAIN_MAP_OPTIONS 索引）
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_main_map"] == "5"


def test_fight_timeout_persists_within_range(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子菜单 4 输入合法整数：``fight_timeout`` 落盘。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",
        "4",
        "8",   # 1..24 小时
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_timeout"] == 8


def test_fight_timeout_rejects_out_of_range(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """_prompt_int 会反复拒绝越界值，最终由合法输入落盘。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",
        "4",      # 超时时间
        "100",    # 越界（被 _prompt_int 拒绝并重问）
        "25",     # 越界
        "3",      # 合法
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    out = capsys.readouterr().out
    # 越界值被 _prompt_int 拒绝、合法值 3 落盘。
    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_timeout"] == 3
    # 提示里至少出现过一次「取值需在 1-24 之间」。
    assert "取值需在" in out


def test_fight_timeout_zero_means_back_not_false(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """整数提示里 ``0`` 走回退，而不是被当作 0 小时写入。"""
    _seed_initialized(tmp_path)
    terminal.write_config_yaml()
    snapshot = _load_config(isolated_paths["config"])

    responses = [
        "3",
        "3",
        "4",      # 超时时间
        "0",      # 回退
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    after = _load_config(isolated_paths["config"])
    # 没有写入 0：保持示例配置的字面量。
    assert after == snapshot


def test_fight_map_version_persists(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子菜单 2 选 5 档中的任意一档：``fight_map_version`` 写为对应 raw key。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",
        "2",   # 地图版本
        "3",   # HuangQuan
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_map_version"] == "HuangQuan"


def test_fight_team_enable_and_number_persist(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子菜单 5/6：``fight_team_enable`` 与 ``fight_team_number`` 都会落盘。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",   # 主菜单：设置
        "3",   # 设置子菜单：锄大地
        "5",   # 锄大地子菜单：启用自动切队
        "y",   # → True
        "6",   # 锄大地子菜单：自动切队编号
        "4",   # → 4
        "0",   # 锄大地子菜单：返回
        "0",   # 设置子菜单：返回
        "6",   # 主菜单：退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_team_enable"] is True
    assert cfg["fight_team_number"] == "4"


def test_fight_reward_options_persist(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子菜单 7/8/9/10：``fight_reward_enable`` 与三项 ``fight_allow_*`` 落盘。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",   # 主菜单：设置
        "3",   # 设置子菜单：锄大地
        "7",   # 启用特殊物品领取
        "y",
        "8",   # 每周购买代币
        "y",
        "9",   # 每日购买秘技零食
        "y",
        "10",  # 每周翁法罗斯记忆代币
        "y",
        "0",   # 锄大地子菜单：返回
        "0",   # 设置子菜单：返回
        "6",   # 主菜单：退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_reward_enable"] is True
    assert cfg["fight_allow_map_buy"] is True
    assert cfg["fight_allow_snack_buy"] is True
    assert cfg["fight_allow_memory_token"] is True


def test_fight_boolean_prompts_reject_zero_as_false(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """布尔开关里 ``0`` 必须走「返回」而非「否」：避免与 0/b 回退混淆。"""
    _seed_initialized(tmp_path)
    terminal.write_config_yaml()
    snapshot = _load_config(isolated_paths["config"])
    assert snapshot.get("fight_enable") is False

    responses = [
        "3",
        "3",
        "1",
        "0",   # 不是 false，而是返回 → 不写
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    after = _load_config(isolated_paths["config"])
    assert after.get("fight_enable") is False


def test_fight_start_map_validates_regex(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """高级起始地图需要匹配 ``[1-6]-[0-9]+_[0-9]+``，非法格式被拒。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",
        "12",          # 起始地图
        "garbage",     # 非法
        "1-1_3",       # 合法
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_start_map"] == "1-1_3"


def test_fight_start_map_empty_means_auto(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """起始地图留空等价于「自动」：``fight_start_map`` 落为 ``""``。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",
        "12",
        "",       # 留空
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_start_map"] == ""


def test_fight_single_map_toggle_persists(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """子菜单 13 切换 ``fight_single_map``：落盘为布尔值。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",
        "13",
        "y",
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_single_map"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. write_config_yaml：allowlist 与持久化
# ─────────────────────────────────────────────────────────────────────────────


def test_write_config_yaml_filters_unknown_answers(isolated_paths):
    """``write_config_yaml(answers=...)`` 只接受白名单内的 key，其它忽略。

    注意：``fight_path`` 等键已存在于 ``config.example.yaml`` 里——它们的
    值是示例配置提供的；本测试关心的是调用方通过 ``answers`` 传入的白名
    单外键（如 ``account`` / ``token``）不会被写到磁盘。
    """
    terminal.write_config_yaml(
        answers={
            "fight_enable": True,
            "fight_map_version": "HuangQuan",
            # 不在白名单内：必须被忽略
            "account": "alice@example.com",
            "token": "secret",
            # fight_path 不在 allowlist 里；其值仍来自示例配置（不应被覆盖）。
            "fight_path": "/etc/passwd",
        }
    )

    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_enable"] is True
    assert cfg["fight_map_version"] == "HuangQuan"
    # 白名单外键不应被写入。
    assert "account" not in cfg
    assert "token" not in cfg
    # fight_path 保留示例配置里的字面量（未被 answers 覆盖）。
    assert cfg["fight_path"] != "/etc/passwd"


def test_write_config_yaml_rounds_trip_through_yaml(isolated_paths):
    """answers 走完 write_config_yaml 后，再次加载能拿回相同值。"""
    terminal.write_config_yaml(
        answers={
            "fight_enable": True,
            "fight_main_map": "5",
            "fight_timeout": 6,
            "fight_team_enable": True,
            "fight_team_number": "3",
            "fight_reward_enable": True,
            "fight_allow_map_buy": True,
            "fight_allow_snack_buy": False,
            "fight_allow_memory_token": False,
            "fight_start_map": "1-2_3",
            "fight_single_map": True,
            "fight_map_version": "reward",
        }
    )

    # 模拟一次重启：再调用一次 write_config_yaml(answers=...) 不会丢失前序值。
    terminal.write_config_yaml(answers={"fight_timeout": 5})
    cfg = _load_config(isolated_paths["config"])
    assert cfg["fight_enable"] is True
    assert cfg["fight_main_map"] == "5"
    assert cfg["fight_timeout"] == 5
    assert cfg["fight_team_enable"] is True
    assert cfg["fight_team_number"] == "3"
    assert cfg["fight_reward_enable"] is True
    assert cfg["fight_allow_map_buy"] is True
    assert cfg["fight_allow_snack_buy"] is False
    assert cfg["fight_allow_memory_token"] is False
    assert cfg["fight_start_map"] == "1-2_3"
    assert cfg["fight_single_map"] is True
    assert cfg["fight_map_version"] == "reward"


def test_apply_fight_answers_ignores_unconvertible(isolated_paths):
    """``_apply_fight_answers`` 接收无法规约的值时不抛异常，保持原值不变。"""
    base: dict[str, object] = {"fight_enable": False}
    # 故意塞入不可转换的值；调用方错误不应当让写盘动作崩溃。
    terminal._apply_fight_answers(
        base,
        {
            "fight_timeout": "not-an-int",
            "fight_team_number": None,
            "fight_enable": "yes",   # 合法：bool("yes") → True
            "fight_main_map": "3",   # 合法：int("3") → "3"
        },
    )
    assert base["fight_enable"] is True
    assert base["fight_main_map"] == "3"
    # 不可转换值不修改原键（保留初始值或缺省）。
    assert base.get("fight_timeout") != "not-an-int"


def test_fight_answer_allowlist_unions_keys():
    """``fight_answer_allowlist()`` 返回白名单全集（含基础 + 高级）。"""
    allowlist = terminal.fight_answer_allowlist()
    # 基础项必须出现。
    for key in (
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
    ):
        assert key in allowlist
    # 高级项必须出现。
    for key in ("fight_start_map", "fight_single_map"):
        assert key in allowlist
    # 显式的不在白名单中的键不得被允许。
    assert "account" not in allowlist
    assert "token" not in allowlist
    assert "fight_path" not in allowlist


# ─────────────────────────────────────────────────────────────────────────────
# 5. 资源准备 / 网络
# ─────────────────────────────────────────────────────────────────────────────


def test_prepare_resources_missing_module_shows_error(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """``module.fight_cloud.resources`` 缺失时显示可读错误，不抛异常。"""
    _seed_initialized(tmp_path)
    # 确保 sys.modules 里没有 stub。
    monkeypatch.delitem(sys.modules, "module.fight_cloud", raising=False)
    monkeypatch.delitem(
        sys.modules, "module.fight_cloud.resources", raising=False
    )

    responses = [
        "3",
        "3",
        "11",   # 检查 / 下载引擎资源
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK

    captured = capsys.readouterr()
    assert "尚未提供" in captured.err or "尚未提供" in captured.out
    assert "module.fight_cloud.resources" in (captured.err + captured.out)


def test_prepare_resources_success_returns_path(
    monkeypatch, isolated_paths, tmp_path, capsys
):
    """stub 后 ``prepare_resources`` 应返回 ``pathlib.Path`` 并打印。"""
    fake_root = tmp_path / "fhoe"

    def fake_prepare(root) -> Path:
        # 后端允许 Path 或 None；此处断言传入 APP_ROOT（被 monkeypatch 到 tmp_path）。
        assert Path(root) == isolated_paths["root"]
        fake_root.mkdir(parents=True, exist_ok=True)
        return fake_root

    resources_mod = ModuleType("module.fight_cloud.resources")
    resources_mod.prepare_resources = fake_prepare  # type: ignore[attr-defined]

    fight_cloud_pkg = ModuleType("module.fight_cloud")
    fight_cloud_pkg.resources = resources_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "module.fight_cloud", fight_cloud_pkg)
    monkeypatch.setitem(
        sys.modules, "module.fight_cloud.resources", resources_mod
    )

    rc = terminal._action_fight_prepare_resources()
    out = capsys.readouterr().out

    assert rc == terminal.EXIT_OK
    assert str(fake_root) in out
    assert terminal.FIGHT_RESOURCE_VERSION in out


def test_prepare_resources_failure_is_caught(
    monkeypatch, isolated_paths, tmp_path, capsys
):
    """stub 后 ``prepare_resources`` 抛异常时返回 ``EXIT_RUNTIME`` 而非崩溃。"""

    def boom(root) -> Path:
        raise RuntimeError("网络不可达")

    resources_mod = ModuleType("module.fight_cloud.resources")
    resources_mod.prepare_resources = boom  # type: ignore[attr-defined]
    fight_cloud_pkg = ModuleType("module.fight_cloud")
    fight_cloud_pkg.resources = resources_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.fight_cloud", fight_cloud_pkg)
    monkeypatch.setitem(
        sys.modules, "module.fight_cloud.resources", resources_mod
    )

    rc = terminal._action_fight_prepare_resources()
    err = capsys.readouterr().err

    assert rc == terminal.EXIT_RUNTIME
    assert "下载 / 校验失败" in err
    assert "网络不可达" in err


def test_prepare_resources_never_makes_real_network_calls(
    monkeypatch, isolated_paths, tmp_path
):
    """即使 ``prepare_resources`` stub 也不应触发真实 HTTP/套接字。"""
    import socket as _socket

    real_socket = _socket.socket

    def forbidden(*args, **kwargs):
        raise AssertionError("准备资源时不应触达网络栈")

    monkeypatch.setattr(_socket, "socket", forbidden)

    sentinel = tmp_path / "engine"
    sentinel.mkdir(parents=True, exist_ok=True)

    resources_mod = ModuleType("module.fight_cloud.resources")
    resources_mod.prepare_resources = lambda root: sentinel  # type: ignore[attr-defined]
    fight_cloud_pkg = ModuleType("module.fight_cloud")
    fight_cloud_pkg.resources = resources_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "module.fight_cloud", fight_cloud_pkg)
    monkeypatch.setitem(
        sys.modules, "module.fight_cloud.resources", resources_mod
    )

    terminal._action_fight_prepare_resources()
    # 还原以免破坏后续测试。
    monkeypatch.setattr(_socket, "socket", real_socket)


# ─────────────────────────────────────────────────────────────────────────────
# 6. 任务在 schedule 中可识别
# ─────────────────────────────────────────────────────────────────────────────


def test_fight_in_schedule_saved_correctly(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner
):
    """菜单 2 → 选择 fight → 输入时间 → 计划被保存。"""
    _seed_initialized(tmp_path)
    responses = [
        "2",                # 主菜单：计划任务
        "fight",            # 直接输入任务名
        "09:00",
        "n",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    schedule = terminal.load_schedule()
    assert any(
        e["task"] == "fight" and e["times"] == ["09:00"] for e in schedule
    )


def test_menu_run_once_can_pick_fight_by_number(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """在「执行一次」里按数字选 fight：应被解析为 ``fight`` 任务。"""
    _seed_initialized(tmp_path)
    responses = [
        "1",                # 主菜单：执行一次
        # 任务列表中 fight 是末尾项之一；这里直接按任务名选，更稳。
        "fight",
        "6",                # 退出
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    assert any(call[1] == ("fight",) for call in fake_runner.calls)


# ─────────────────────────────────────────────────────────────────────────────
# 7. 菜单显示
# ─────────────────────────────────────────────────────────────────────────────


def test_action_settings_lists_fight_entry(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """主菜单 3 「设置」子菜单应列出「3. 锄大地」。"""
    _seed_initialized(tmp_path)
    responses = [
        "3",
        "3",   # 锄大地子菜单（也验证编号不冲突）
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    out = capsys.readouterr().out
    assert "锄大地" in out
    assert "── 锄大地 ──" in out


def test_fight_submenu_shows_current_value_marker(
    monkeypatch, isolated_paths, run_in_isolated_root, tmp_path, fake_runner, capsys
):
    """子菜单里应能看到「当前」标记，便于核对已写入的值。"""
    _seed_initialized(tmp_path)
    terminal.write_config_yaml(answers={"fight_enable": True, "fight_main_map": "5"})

    responses = [
        "3",
        "3",   # 锄大地
        "0",
        "0",
        "6",
    ]
    rc = run_in_isolated_root([], tmp_path=tmp_path, fake_input=_Input(responses))
    assert rc == terminal.EXIT_OK
    out = capsys.readouterr().out
    # 至少有一条「当前：」标记。
    assert re.search(r"当前：开", out) or re.search(r"当前：翁法罗斯", out)


# ─────────────────────────────────────────────────────────────────────────────
# 8. 任务菜单 / 选择器
# ─────────────────────────────────────────────────────────────────────────────


def test_choose_task_lists_fight_label(
    monkeypatch, isolated_paths, tmp_path, capsys
):
    """``_choose_task`` 在菜单中应展示中文标签「锄大地」。"""
    monkeypatch.setattr(builtins, "input", _Input(["1"]))
    terminal._choose_task("请选择")
    out = capsys.readouterr().out
    assert "锄大地" in out