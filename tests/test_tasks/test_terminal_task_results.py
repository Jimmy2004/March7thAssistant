"""Exercise dispatcher semantics without importing OCR or starting a browser."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def dispatcher(action, result):
    source = ast.parse(Path('main.py').read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'run_sub_task')
    code = compile(ast.Module(body=[function], type_ignores=[]), 'main.py', 'exec')
    engine = Mock()
    engine.start.return_value = result
    game = Mock()
    env = {
        'cfg': SimpleNamespace(cloud_game_enable=True),
        'game': game, 'cloud_game': Mock(),
        'CurrencyWars': lambda: engine, 'DivergentUniverse': lambda: engine,
        'Daily': Mock(), 'Power': Mock(), 'Universe': Mock(), 'Fight': Mock(),
        'Redemption': Mock(), 'screen_test': Mock(), 'reward': Mock(),
        'challenge': Mock(), 'telemetry': Mock(), 'time': SimpleNamespace(time=lambda: 1),
    }
    exec(code, env)
    return lambda: env['run_sub_task'](action), game, env


def _loop_dispatcher(action, side_effects):
    """为 ``divergentloop`` / ``currencywarsloop`` 组装独立调度器。

    与 ``dispatcher`` 的差异：内层引擎用 ``side_effect`` 列表控制逐轮返回值，
    便于验证「成功后继续无限循环」「False 后跳出」「Ctrl+C 透传」三种行为。
    """
    source = ast.parse(Path('main.py').read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'run_sub_task')
    code = compile(ast.Module(body=[function], type_ignores=[]), 'main.py', 'exec')
    engine = Mock()
    engine.start.side_effect = side_effects
    game = Mock()
    env = {
        'cfg': SimpleNamespace(cloud_game_enable=True),
        'game': game, 'cloud_game': Mock(),
        'CurrencyWars': lambda: engine, 'DivergentUniverse': lambda: engine,
        'Daily': Mock(), 'Power': Mock(), 'Universe': Mock(), 'Fight': Mock(),
        'Redemption': Mock(), 'screen_test': Mock(), 'reward': Mock(),
        'challenge': Mock(), 'telemetry': Mock(), 'time': SimpleNamespace(time=lambda: 1),
    }
    exec(code, env)
    return lambda: env['run_sub_task'](action), game, env, engine


@pytest.mark.parametrize('task', ['currencywars', 'divergent'])
def test_false_task_result_is_failure_before_successful_exit(task):
    run, game, env = dispatcher(task, False)
    with pytest.raises(RuntimeError, match='任务未完成'):
        run()
    game.stop.assert_not_called()
    env['telemetry'].track_task_complete.assert_called_once_with(task, False, 0)


@pytest.mark.parametrize('task', ['currencywars', 'divergent'])
def test_true_result_completes_then_closes_game(task):
    run, game, env = dispatcher(task, True)
    run()
    game.stop.assert_called_once_with(False)
    env['telemetry'].track_task_complete.assert_called_once_with(task, True, 0)


def test_failed_daily_does_not_claim_reward_success():
    run, game, env = dispatcher('daily', True)
    env['Daily'].run.return_value = False
    with pytest.raises(RuntimeError):
        run()
    env['reward'].start.assert_not_called()
    game.stop.assert_not_called()


def test_divergentloop_sequence_true_continues_indefinitely():
    """成功后继续无限循环：前 3 轮 True，模拟一次 Ctrl+C 终止外层调用。"""
    side_effects = [True, True, True, KeyboardInterrupt()]
    run, game, env, engine = _loop_dispatcher('divergentloop', side_effects)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert engine.start.call_count == 4
    # 3 个成功轮次均有成功上报；KeyboardInterrupt 是 BaseException，
    # 不会进入 except Exception 分支，因此没有对应的 track_task_complete 调用。
    completed = env['telemetry'].track_task_complete.call_args_list
    successes = [c.args[1] for c in completed]
    assert successes == [True, True, True]
    # 循环任一成功轮都不应关闭游戏（控制权仍在外层 runner / Ctrl+C 处理）。
    assert not game.stop.called


def test_divergentloop_false_stops_with_runtime_error_for_retry_cleanup():
    """返回 ``False`` 时上报失败并抛 RuntimeError，外层有限重试可继续。"""
    run, game, env, engine = _loop_dispatcher('divergentloop', [False])
    with pytest.raises(RuntimeError, match=r'任务未完成: divergentloop'):
        run()
    assert engine.start.call_count == 1
    env['telemetry'].track_task_complete.assert_called_once_with('divergentloop', False, 0)
    game.stop.assert_not_called()


def test_divergentloop_keyboard_interrupt_propagates():
    """Ctrl+C 立即透传给外层 runner，不被改写成任务失败。

    KeyboardInterrupt 是 BaseException 实例，不被 ``except Exception`` 拦
    截；本测试验证它原样抛出，让 ``module.terminal_runner`` 的 SIGINT 处
    理路径接管浏览器清理。
    """
    run, game, env, engine = _loop_dispatcher('divergentloop', [KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        run()
    assert engine.start.call_count == 1
    # KeyboardInterrupt 跳过 except Exception 分支；不应有失败上报。
    env['telemetry'].track_task_complete.assert_not_called()
    game.stop.assert_not_called()


def test_terminal_task_choice_exposes_divergentloop():
    """终端 CLI 的 ``--task`` 选项必须显式接受 ``divergentloop``。"""
    import terminal  # 导入受控，pytest 在仓库根目录运行

    assert 'divergentloop' in terminal.task_choices()
    assert 'divergentloop' in terminal.TASK_CHOICES
    # 仅追加、不改写既有数值顺序：保留向后兼容。
    assert terminal.TASK_CHOICES[-1] == 'divergentloop'
