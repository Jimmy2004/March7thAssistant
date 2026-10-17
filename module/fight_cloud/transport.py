"""Strict cloud input and relative camera movement for the route subprocess."""
from __future__ import annotations

import base64
import math
import time

POINTER_SCRIPT = r"""
(() => {
    if (window.__m7FightPointer) return;
    let locked = null;
    let pending = null;
    const original = Object.getOwnPropertyDescriptor(Document.prototype, 'pointerLockElement');
    const exit = document.exitPointerLock.bind(document);
    const request = Element.prototype.requestPointerLock;
    Object.defineProperty(document, 'pointerLockElement', {
        configurable: true, get: () => locked || (original && original.get.call(document)) || null
    });
    Element.prototype.requestPointerLock = function() {
        locked = this;
        document.dispatchEvent(new Event('pointerlockchange'));
        return Promise.resolve();
    };
    document.exitPointerLock = function() {
        locked = null;
        exit();
        document.dispatchEvent(new Event('pointerlockchange'));
    };
    const patch = event => {
        if (!pending) return;
        const delta = pending;
        if (event.type === 'mousemove') pending = null;
        Object.defineProperties(event, {
            movementX: {configurable: true, value: delta[0]},
            movementY: {configurable: true, value: delta[1]}
        });
    };
    window.addEventListener('pointermove', patch, true);
    window.addEventListener('mousemove', patch, true);
    window.__m7FightPointer = {
        arm(dx, dy) {
            const target = document.querySelector('.game-player__video');
            if (!target) throw new Error('未找到云游戏画面');
            if (!locked) target.requestPointerLock();
            pending = [dx, dy];
        },
        clear() {
            pending = null;
            locked = null;
            Element.prototype.requestPointerLock = request;
            document.exitPointerLock = exit;
            delete document.pointerLockElement;
            window.removeEventListener('pointermove', patch, true);
            window.removeEventListener('mousemove', patch, true);
            delete window.__m7FightPointer;
            document.dispatchEvent(new Event('pointerlockchange'));
        }
    };
})();
"""


class CloudTransport:
    def __init__(self, controller, logger):
        from module.automation.cdp_input import CdpInput
        self.controller = controller
        self.input = CdpInput(controller, logger)
        self.input.SPECIAL_KEY_MAP = dict(self.input.SPECIAL_KEY_MAP, caps={
            "key": "CapsLock", "code": "CapsLock", "vk": 20})
        self.keys = set()
        self.mouse_held = False
        self.position = (960, 540)
        self.flip = False
        self.script_id = None

    def enable(self):
        self.controller._confirm_viewport_resolution()
        self.script_id = self.controller.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": POINTER_SCRIPT,
        }).get("identifier")
        self.controller.driver.execute_script(POINTER_SCRIPT)

    def _key(self, key, down):
        key = str(key).lower()
        if self.input._get_key_payload(key) is None:
            raise ValueError(f"不支持的锄大地按键: {key}")
        if down:
            self.keys.add(key)
        self.input._dispatch_key_event(key, "keyDown" if down else "keyUp")
        if not down:
            self.keys.discard(key)

    def _mouse(self, event, x=None, y=None):
        if x is not None:
            x, y = float(x), float(y)
            if not (math.isfinite(x) and math.isfinite(y) and 0 <= x < 1920 and 0 <= y < 1080):
                raise ValueError("锄大地鼠标坐标超出游戏画面")
            self.position = (x, y)
        if event == "mousePressed":
            self.mouse_held = True
        button = "left" if self.mouse_held or event == "mouseReleased" else "none"
        self.controller.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": event, "x": self.position[0], "y": self.position[1],
            "button": button, "buttons": 1 if self.mouse_held and event != "mouseReleased" else 0,
            "clickCount": 0 if event == "mouseMoved" else 1,
            "pointerType": "mouse", "modifiers": self.input.active_modifiers,
        })
        if event == "mouseReleased":
            self.mouse_held = False

    @staticmethod
    def _delay(value):
        seconds = float(value)
        if not math.isfinite(seconds) or not 0 <= seconds <= 60:
            raise ValueError("锄大地按键时长无效")
        return seconds

    def click(self, x, y, delay=0.05):
        delay = self._delay(delay)
        self._mouse("mouseMoved", x, y)
        try:
            self._mouse("mousePressed")
            time.sleep(delay)
        finally:
            self._mouse("mouseReleased")

    def dispatch(self, op, args):
        if op == "check":
            self.controller.check_cloud_game_interruptions()
        elif op == "screenshot":
            self.controller.check_cloud_game_interruptions()
            image = self.controller.take_screenshot()
            if isinstance(image, tuple):
                image = image[0]
            if not isinstance(image, bytes) or not image:
                raise RuntimeError("云游戏截图失败")
            return base64.b64encode(image).decode("ascii")
        elif op in {"key_down", "key_up", "key_press"}:
            key = args["key"]
            if op == "key_press":
                delay = self._delay(args.get("delay", 0))
                try:
                    self._key(key, True)
                    time.sleep(delay)
                finally:
                    self._key(key, False)
            else:
                self._key(key, op == "key_down")
        elif op == "click":
            self.click(args["x"], args["y"], args.get("delay", 0.05))
        elif op == "click_cursor":
            clicks = int(args.get("clicks", 1))
            if not 1 <= clicks <= 100:
                raise ValueError("锄大地点击次数无效")
            for _ in range(clicks):
                self.click(*self.position, args.get("delay", 0.05))
        elif op == "drag":
            duration = self._delay(args.get("press_time", 0))
            self._mouse("mouseMoved", args["x"], args["y"])
            try:
                self._mouse("mousePressed")
                x, y = self.position
                for step in range(1, 13):
                    self._mouse("mouseMoved", x + (args["end_x"] - x) * step / 12,
                                y + (args["end_y"] - y) * step / 12)
                    time.sleep(0.2 / 12)
                time.sleep(duration)
            finally:
                self._mouse("mouseReleased")
        elif op == "scroll":
            clicks = float(args["clicks"])
            if not math.isfinite(clicks) or abs(clicks) > 100:
                raise ValueError("锄大地滚轮值无效")
            self.controller.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": self.position[0], "y": self.position[1],
                "deltaX": 0, "deltaY": -120 * clicks,
                "modifiers": self.input.active_modifiers,
            })
        elif op == "relative_move":
            dx, dy = float(args["dx"]), float(args.get("dy", 0))
            if not math.isfinite(dx) or not math.isfinite(dy) or max(abs(dx), abs(dy)) > 20000:
                raise ValueError("锄大地视角移动值无效")
            self.controller.driver.execute_script(
                "window.__m7FightPointer.arm(arguments[0], arguments[1]);", dx, dy)
            # Real CDP events remain trusted; only their relative deltas are replaced.
            self.flip = not self.flip
            self._mouse("mouseMoved", 960 + int(self.flip), 540)
        else:
            raise ValueError(f"未知锄大地操作: {op}")

    def close(self):
        errors = []
        for key in list(self.keys):
            try:
                self._key(key, False)
            except Exception as exc:
                errors.append(exc)
        if self.mouse_held:
            try:
                self._mouse("mouseReleased")
            except Exception as exc:
                errors.append(exc)
        try:
            self.controller.driver.execute_script("if(window.__m7FightPointer) window.__m7FightPointer.clear();")
            if self.script_id:
                self.controller.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self.script_id})
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise RuntimeError("锄大地输入清理失败") from errors[0]
