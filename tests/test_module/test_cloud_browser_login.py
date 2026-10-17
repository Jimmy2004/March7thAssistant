"""Temporary visible authentication, followed by verified background resume."""
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from module.game.cloud import CloudGameController, CloudGameLoginTimeoutError


def controller(headless=True, restart=True, persistent=True):
    c = CloudGameController.__new__(CloudGameController)
    c.cfg = SimpleNamespace(browser_headless_enable=headless,
        browser_headless_restart_on_not_logged_in=restart,
        browser_persistent_enable=persistent,
        get_value=lambda key, default=None: default)
    c.logger = Mock()
    c._restart_browser = Mock()
    c._show_browser_qr_login = Mock()
    c._run_qr_login_flow = Mock()
    c.stop_game = Mock()
    return c


def test_already_logged_in_keeps_browser_in_background():
    c = controller()
    c._check_login = Mock(return_value=True)
    c._ensure_cloud_login()
    c._restart_browser.assert_not_called()
    c._show_browser_qr_login.assert_not_called()


def test_window_stays_until_phone_confirmation_then_background_verified(monkeypatch):
    monkeypatch.setattr("module.game.cloud.time.sleep", lambda seconds: None)
    c = controller()
    checks = []

    def check(timeout=5):
        checks.append(c._restart_browser.call_args_list.copy())
        return len(checks) >= 3

    c._check_login = Mock(side_effect=check)
    c._ensure_cloud_login()
    assert checks[1] == [call(headless=False)]
    assert checks[2] == [call(headless=False)]
    assert checks[3] == [call(headless=False), call(headless=True)]
    c._show_browser_qr_login.assert_called_once()
    c._run_qr_login_flow.assert_not_called()
    c._check_login.assert_called_with(timeout=15)


def test_terminal_migrates_old_disabled_popup_preference(monkeypatch):
    monkeypatch.setenv("MARCH7TH_TERMINAL", "1")
    c = controller(restart=False)
    c._check_login = Mock(side_effect=[False, True, True])
    c._ensure_cloud_login()
    assert c._restart_browser.call_args_list == [call(headless=False), call(headless=True)]
    c._run_qr_login_flow.assert_not_called()


def test_lost_login_after_relaunch_fails_without_popup_loop():
    c = controller()
    c._check_login = Mock(side_effect=[False, True, False])
    with pytest.raises(RuntimeError, match="登录状态未能保留"):
        c._ensure_cloud_login()
    assert c._restart_browser.call_count == 2
    c.stop_game.assert_called_once()


def test_login_timeout_closes_visible_browser():
    c = controller()
    c._check_login = Mock(return_value=False)
    c._check_login_timeout = Mock(side_effect=CloudGameLoginTimeoutError("超时"))
    with pytest.raises(CloudGameLoginTimeoutError):
        c._ensure_cloud_login()
    c.stop_game.assert_called_once()
    assert c._restart_browser.call_args_list == [call(headless=False)]


def test_cancel_during_login_closes_visible_browser():
    c = controller()
    c._check_login = Mock(return_value=False)
    c._show_browser_qr_login.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        c._ensure_cloud_login()
    c.stop_game.assert_called_once()


def test_debug_mode_keeps_visible_browser_after_login():
    c = controller(headless=False)
    c._check_login = Mock(side_effect=[False, True])
    c._ensure_cloud_login()
    c._show_browser_qr_login.assert_called_once()
    c._restart_browser.assert_not_called()


def test_disabled_persistence_fails_before_window_is_created():
    c = controller(persistent=False)
    c._check_login = Mock(return_value=False)
    with pytest.raises(RuntimeError, match="登录状态保存"):
        c._ensure_cloud_login()
    c._restart_browser.assert_not_called()
