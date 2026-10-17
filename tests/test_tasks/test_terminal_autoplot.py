"""Cloud dialogue behavior and cancellation, without desktop input or Qt."""
import subprocess
import sys
from unittest.mock import Mock

import pytest

from tasks.tool.autoplot import terminal


@pytest.fixture
def engine(monkeypatch):
    auto = Mock()
    auto.click_element.return_value = False
    detector = Mock()
    detector.has_dialog_options.return_value = False
    detector.detect_skip_button.return_value = False
    detector.is_auto_battle_off.return_value = False
    monkeypatch.setattr(terminal, 'auto', auto)
    monkeypatch.setattr(terminal, 'SceneDetector', detector)
    monkeypatch.setattr(terminal.time, 'sleep', lambda _: None)
    return terminal.TerminalAutoplot('cloud'), auto, detector


def test_click_disabled_never_selects_even_if_detection_misses_options(engine):
    e, auto, detector = engine
    e.click_enable = False
    detector.has_dialog_options.return_value = False
    e._dialog_loop()
    detector.click_dialog_option.assert_not_called()


def test_click_disabled_with_options_only_waits(engine):
    e, auto, detector = engine
    e.click_enable = False
    detector.has_dialog_options.return_value = True
    e._dialog_loop()
    detector.detect_skip_button.assert_not_called()
    auto.click_element.assert_not_called()
    detector.click_dialog_option.assert_not_called()


def test_continue_releases_mouse_on_interrupt(engine, monkeypatch):
    e, auto, detector = engine
    auto.click_element.return_value = True
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(terminal.time, 'sleep', interrupt)
    with pytest.raises(KeyboardInterrupt):
        e._dialog_loop()
    auto.mouse_up.assert_called_once()


def test_battle_cooldown(engine, monkeypatch):
    e, auto, detector = engine
    detector.is_auto_battle_off.return_value = True
    clock = iter([100, 105, 111])
    monkeypatch.setattr(terminal.time, 'monotonic', lambda: next(clock))
    for _ in range(3):
        e._handle_non_dialog_actions()
    assert auto.press_key.call_count == 2


def test_hero_reader_exclusively_owns_actions_while_active(engine, monkeypatch):
    e, auto, detector = engine
    e.hero_chronicle_enable = True
    e._hero_reader = Mock(completed=False, finished=False)
    e._hero_reader.tick.return_value = True
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(terminal.time, 'sleep', interrupt)
    with pytest.raises(KeyboardInterrupt):
        e.run()
    detector.is_dialog_scene.assert_not_called()


def test_completed_reader_returns_true(engine):
    e, auto, detector = engine
    e.hero_chronicle_enable = True
    e._hero_reader = Mock(completed=True, finished=True)
    assert e.run() is True


def test_import_does_not_load_qt():
    result = subprocess.run([sys.executable, '-c',
        'from tasks.tool.autoplot.terminal import TerminalAutoplot; '
        'import sys; assert not any(x.startswith(("PySide6", "PyQt")) for x in sys.modules)'],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
