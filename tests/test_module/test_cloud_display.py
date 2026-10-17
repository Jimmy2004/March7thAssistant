"""Viewport invariants and cloud quality persistence before app initialization."""
import base64
import io
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from module.game.cloud import CloudGameController


def controller(quality='2'):
    instance = CloudGameController.__new__(CloudGameController)
    instance.driver = Mock()
    instance.cfg = SimpleNamespace(get_value=lambda key, default=None: quality)
    instance.logger = Mock()
    return instance


def test_geometry_ignores_host_scale_and_dimensions():
    c = controller()
    c._confirm_viewport_resolution()
    commands = c.driver.execute_cdp_cmd.call_args_list
    assert commands[0].args == ('Emulation.setDeviceMetricsOverride', {
        'width': 1920, 'height': 1080, 'screenWidth': 1920, 'screenHeight': 1080,
        'deviceScaleFactor': 1, 'mobile': False})
    assert commands[1].args == ('Emulation.setPageScaleFactor', {'pageScaleFactor': 1})


@pytest.mark.parametrize('quality', ['0', '1', '2', '3'])
def test_quality_registered_for_next_page_and_read_back(quality):
    c = controller(quality)
    c.driver.execute_cdp_cmd.return_value = {'identifier': 'quality-script'}
    c.driver.execute_script.side_effect = [True, quality]
    assert c._apply_cloud_game_settings() is True
    command, args = c.driver.execute_cdp_cmd.call_args.args
    assert command == 'Page.addScriptToEvaluateOnNewDocument'
    assert 'config.speedLimitGearId = quality' in args['source']
    assert 'https://sr.mihoyo.com' in args['source']
    assert c._quality_script_id == 'quality-script'


def test_invalid_quality_does_not_change_browser():
    c = controller('unexpected')
    with pytest.raises(ValueError):
        c._apply_cloud_game_settings()
    c.driver.execute_cdp_cmd.assert_not_called()


def test_unapplied_quality_is_not_reported_as_success():
    c = controller('2')
    c.driver.execute_cdp_cmd.return_value = {'identifier': 'quality-script'}
    c.driver.execute_script.side_effect = [True, '0']
    with pytest.raises(RuntimeError, match='未保存'):
        c._apply_cloud_game_settings()


def test_refresh_script_is_replaced_instead_of_accumulated():
    c = controller('1')
    c._quality_script_id = 'previous-script'
    c.driver.execute_cdp_cmd.return_value = {'identifier': 'new-script'}
    c.driver.execute_script.side_effect = [False, '1']
    assert c._apply_cloud_game_settings() is False
    assert c.driver.execute_cdp_cmd.call_args_list[0].args == (
        'Page.removeScriptToEvaluateOnNewDocument', {'identifier': 'previous-script'})


def test_geometry_set_under_screenshot_http_timeout():
    c = controller()
    config = SimpleNamespace(timeout=120)
    c.driver.command_executor._client_config = config
    c._ensure_window_not_minimized_for_frame_capture = Mock()
    def command(name, args):
        assert config.timeout == 15
        return {'data': base64.b64encode(b'screenshot').decode()} if name == 'Page.captureScreenshot' else {}
    c.driver.execute_cdp_cmd.side_effect = command
    assert c._take_browser_screenshot() == b'screenshot'
    assert config.timeout == 120


@pytest.mark.skipif(os.environ.get('MARCH7TH_BROWSER_TESTS') != '1', reason='opt-in real headless Chrome smoke')
@pytest.mark.parametrize('desktop_scale', [1, 2])
def test_real_chrome_virtual_viewport_and_screenshot_remain_1080p(desktop_scale):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--window-size=800,600')
    options.add_argument(f'--force-device-scale-factor={desktop_scale}')
    driver = webdriver.Chrome(options=options)
    c = controller()
    c.driver = driver
    try:
        driver.get('data:text/html,<style>html,body{margin:0;overflow:hidden}</style><div>viewport test</div>')
        c._confirm_viewport_resolution()
        driver.set_window_size(640, 480)
        c._confirm_viewport_resolution()
        assert driver.execute_script('return [innerWidth,innerHeight,devicePixelRatio,visualViewport.scale]') == [1920,1080,1,1]
        with Image.open(io.BytesIO(driver.get_screenshot_as_png())) as image:
            assert image.size == (1920,1080)
    finally:
        driver.quit()


def test_browser_arguments_ignore_custom_geometry_but_preserve_other_flags():
    c = controller()
    c.cfg = SimpleNamespace(browser_scale_factor='invalid', browser_persistent_enable=False,
                            cloud_game_fullscreen_enable=False,
                            browser_launch_argument=['--window-size=640,480', '--force-device-scale-factor=3', '--disable-gpu'])
    args = c._get_browser_arguments(headless=True)
    assert [x for x in args if x.startswith('--window-size=')] == ['--window-size=1920,1080']
    assert [x for x in args if x.startswith('--force-device-scale-factor=')] == ['--force-device-scale-factor=1']
    assert '--disable-gpu' in args
