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
