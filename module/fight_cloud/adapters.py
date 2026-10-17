"""Fhoe driver API implemented through a private socket to our cloud controller.

Loaded only by worker.py. No desktop, keyboard hook, Tk or Windows imports.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import socket
import sys
import threading
import time
import types

_rpc = None


class RpcClient:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.stream = sock.makefile("rwb")
        self.lock = threading.Lock()

    def call(self, op, **args):
        # Fhoe sprint detection uses a second thread; requests must not interleave.
        with self.lock:
            self.stream.write(json.dumps({"op": op, "args": args}).encode() + b"\n")
            self.stream.flush()
            line = self.stream.readline(32 * 1024 * 1024)
            if not line or not line.endswith(b"\n"):
                raise RuntimeError("云游戏连接已关闭")
            response = json.loads(line)
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "云游戏操作失败"))
            return response.get("result")


class Window:
    hwnd = 1
    title = "云星穹铁道（无窗口）"
    client = "云游戏"
    winrect = (0, 0, 1920, 1080)

    def __init__(self, **kwargs):
        pass

    def get_rect(self):
        return self.winrect

    def check_window_visibility(self):
        return True

    def switch_window(self):
        _rpc.call("check")
        return True


class ScreenSource:
    def __init__(self, window=None):
        self.window = window or Window()
        self.temp_screenshot = (0, 0, 0, 0, 0)

    def cal_screenshot(self):
        return (0, 0, 1920, 1080)

    @staticmethod
    def capture_window_background(hwnd, region, crop_region=None):
        from PIL import Image
        image = Image.open(io.BytesIO(base64.b64decode(_rpc.call("screenshot")))).convert("RGB")
        return image.crop(crop_region) if crop_region else image

    def take_screenshot(self, offset=(0, 0, 0, 0), max_retries=50, retry_interval=2):
        import numpy as np
        picture = self.capture_window_background(1, (0, 0, 1920, 1080))
        if picture.size != (1920, 1080):
            raise RuntimeError(f"锄大地截图尺寸错误: {picture.size}")
        left, top, right, bottom = offset[0], offset[1], 1920 + offset[2], 1080 + offset[3]
        if not (0 <= left < right <= 1920 and 0 <= top < bottom <= 1080):
            raise ValueError(f"锄大地截图范围错误: {offset}")
        frame = np.array(picture.crop((left, top, right, bottom)))[:, :, ::-1].copy()
        self.temp_screenshot = (frame, left, top, right, bottom)
        return self.temp_screenshot

    def has_fresh_frame(self):
        import numpy as np
        return isinstance(self.temp_screenshot[0], np.ndarray)


class KeyboardEvent:
    @staticmethod
    def translate_key(key_name):
        return {"alt_l": "alt", "alt_r": "alt", "shift_l": "shift", "shift_r": "shift",
                "ctrl_l": "ctrl", "ctrl_r": "ctrl", "caps_lock": "caps"}.get(key_name, key_name)

    @staticmethod
    def press_key(key_name):
        _rpc.call("key_down", key=KeyboardEvent.translate_key(key_name))

    @staticmethod
    def release_key(key_name):
        _rpc.call("key_up", key=KeyboardEvent.translate_key(key_name))

    @staticmethod
    def keyboard_press(key_name, delay=0):
        # A single RPC keeps short movement timing independent of socket latency.
        _rpc.call("key_press", key=KeyboardEvent.translate_key(key_name), delay=delay)

    tap_escape = staticmethod(lambda hold: KeyboardEvent.keyboard_press("esc", hold))


class MouseEvent:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "cfg"):
            return
        from utils.config.config import ConfigurationManager
        self.cfg = ConfigurationManager()
        self.window = Window()
        self.last_search_allow_retry = False
        self.multi_num = 1
        self.scale = 1

    def click(self, points, slot=0.0, clicks=1, delay=0.05):
        for _ in range(clicks):
            self.mouse_press(int(points[0]), int(points[1]), delay)

    def mouse_press(self, x, y, delay=0.05):
        _rpc.call("click", x=x, y=y, delay=delay)

    def mouse_drag(self, x, y, end_x, end_y, press_time=0):
        _rpc.call("drag", x=x, y=y, end_x=end_x, end_y=end_y, press_time=press_time)
        time.sleep(1)

    def mouse_press_alt(self, x, y, delay=0.4):
        KeyboardEvent.press_key("alt")
        try:
            self.mouse_press(x, y, delay)
        finally:
            KeyboardEvent.release_key("alt")

    def relative_click(self, points):
        self.mouse_press_alt(int(19.2 * points[0]), int(10.8 * points[1]))

    def scroll(self, clicks):
        _rpc.call("scroll", clicks=clicks)

    def click_at_cursor(self, clicks=1, delay=0.05):
        _rpc.call("click_cursor", clicks=clicks, delay=delay)

    def click_center(self):
        self.mouse_press(960, 540)

    def click_target_with_alt(self, matcher, target_path, threshold, flag=True, clicks=1):
        KeyboardEvent.press_key("alt")
        try:
            time.sleep(0.15)
            matcher.click_target(target_path, threshold, flag, clicks=clicks)
        finally:
            KeyboardEvent.release_key("alt")
            time.sleep(0.1)

    def get_multi_num(self):
        self.multi_num = float(self.cfg.config_file.get("angle", 1))
        return self.multi_num

    def mouse_move(self, x, fine=1, align=False):
        chunk = max(1, 30 // max(1, fine))
        multiplier = 1 if align else self.get_multi_num()
        remaining = float(x)
        while abs(remaining) > 1e-8:
            part = max(-chunk, min(chunk, remaining))
            _rpc.call("relative_move", dx=round(16.5 * part * multiplier), dy=0)
            remaining -= part
            time.sleep(0.2 * fine)


class Pause:
    """Terminal cancellation is owned by the parent, not global desktop hotkeys."""
    def __init__(self, dev=False):
        self.pending_key = None

    def check_pause(self, dev=False, last_point=""):
        _rpc.call("check")
        return False

    def wait_if_paused(self):
        _rpc.call("check")
        return 0.0


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module


def install_adapters(rpc):
    global _rpc
    _rpc = rpc
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | 锄大地 | %(message)s")
    logger = logging.getLogger("fhoe-cloud")
    _module("utils.core.log", log=logger, webhook_and_log=logger.info)
    _module("utils.drivers.window", Window=Window)
    _module("utils.drivers.screen", ScreenSource=ScreenSource, BASE_WIDTH=1920, BASE_HEIGHT=1080)
    _module("utils.drivers.keyboard_event", KeyboardEvent=KeyboardEvent)
    _module("utils.drivers.mouse_event", MouseEvent=MouseEvent)
    _module("utils.ui.pause", Pause=Pause)
    _module("utils.ui.text_window", show_text=lambda *a, **k: None,
            start_tkinter_thread=lambda *a, **k: None, TEXT_WINDOWS={})
    # The pinned config only uses byte dumps/loads; no native orjson dependency.
    _module("orjson", loads=json.loads,
            dumps=lambda obj: json.dumps(obj, ensure_ascii=False).encode("utf-8"))
