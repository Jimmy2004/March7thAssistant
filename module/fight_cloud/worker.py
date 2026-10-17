"""Isolated Fhoe engine entry point. stdout/stderr remain terminal logs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys

from adapters import RpcClient, install_adapters


def load_engine(root: Path, config_path: Path):
    os.chdir(root)
    # Fhoe has its own top-level utils; isolate it from March7th's utils.
    sys.path.insert(0, str(root))
    install_adapters(None)
    from utils.config.config import ConfigurationManager
    ConfigurationManager.CONFIG_FILE_NAME = str(config_path)
    ConfigurationManager.main_start()
    from utils.vision import images
    original_resolve = images.resolve_path
    images.resolve_path = lambda name: original_resolve(str(name).replace("\\", "/"))
    from utils.vision.img import Img
    # Img binds a static alias before later consumers import it.
    Img.resolve_path = staticmethod(images.resolve_path)
    from utils.flows.map_operations import MapOperations
    engine = MapOperations()
    # A route must never power off the user's Mac.
    engine.handle.handle_shutdown = lambda: None
    # Upstream moved camera input out of Handle but its route dispatcher still
    # calls the old method name.
    engine.handle.mouse_move = engine.mouse_event.mouse_move
    original_fight = engine.handle.combat.fight_elapsed

    def checked_fight(*args, **kwargs):
        import time
        started = time.monotonic()
        result = original_fight(*args, **kwargs)
        # The original engine returns True even after its 600-second timeout.
        if time.monotonic() - started >= 600:
            raise TimeoutError("锄大地战斗超时")
        return result

    engine.handle.combat.fight_elapsed = checked_fight
    orientation = engine.handle.orientation
    original_calibrate = orientation.set_angle

    def checked_calibrate(*args, **kwargs):
        from utils.vision import arrow
        angles = []
        original_angle = arrow.cal_ang

        def measured_angle(*a, **k):
            value = original_angle(*a, **k)
            angles.append(value)
            return value

        arrow.cal_ang = measured_angle
        try:
            original_calibrate(*args, **kwargs)
        finally:
            arrow.cal_ang = original_angle
        moved = any(abs((angle - angles[0] + 180) % 360 - 180) >= 5 for angle in angles[1:]) if angles else False
        if not moved:
            engine.cfg.modify_json_file(engine.cfg.CONFIG_FILE_NAME, "angle_set", False)
            raise RuntimeError("未检测到有效视角旋转，停止锄大地；请用调试模式检查画面")

    orientation.set_angle = checked_calibrate
    return engine


def choose_start(engine, options):
    names = engine.map_info.map_list
    requested = options.get("fight_start_map", "")
    if requested:
        if f"map_{requested}.json" not in names:
            raise ValueError(f"当前路线中不存在地图: {requested}")
        return requested, False
    planet = str(options.get("fight_main_map", "0"))
    candidates = [name for name in names if name.startswith(f"map_{planet}-")] if planet != "0" else names
    if not candidates:
        raise ValueError(f"当前路线中不存在优先星球: {planet}")
    return candidates[0][4:-5], planet != "0"


def expected_skip(engine, filename):
    import datetime
    data = engine.cfg.read_json_file(f"map/{engine.map_info.map_version}/{filename}")
    settings = engine.cfg.config_file
    region = data["name"].split("-")[0]
    if region in settings.get("forbid_map", []):
        return True
    if (settings.get("allowlist_mode") or settings.get("allowlist_mode_once") or engine.map.allowlist_mode):
        if region not in settings.get("allowlist_map", []):
            return True
    for step in data["start"]:
        if "check" in step:
            days = step["check"]
            if days != 1 and datetime.datetime.now().weekday() not in (days or []):
                return True
        for flag in ("allow_map_buy", "allow_snack_buy", "allow_memory_token"):
            if "need_" + flag in step and not settings.get(flag, False):
                return True
    return False


def run_routes(engine, options, rpc):
    start, mid = choose_start(engine, options)
    completed = []
    skipped = []
    original = engine.process_single_map
    map_timeout = int(options.get("fight_map_timeout", 900))
    if not 30 <= map_timeout <= 3600:
        raise ValueError("单图超时必须在 30–3600 秒之间")

    def timeout(signum, frame):
        raise TimeoutError("锄大地单图超时，停止本轮")

    signal.signal(signal.SIGALRM, timeout)

    def tracked(index, filename, dev=False):
        signal.setitimer(signal.ITIMER_REAL, map_timeout)
        try:
            permitted_skip = expected_skip(engine, filename)
            engine.mouse_event.last_search_allow_retry = False
            original(index, filename, dev)
            state = engine.map_statu
            if engine.mouse_event.last_search_allow_retry or state.next_map_drag:
                raise RuntimeError(f"地图传送识别失败: {filename}")
            if engine.handle.f_key_error or state.map_f_key_error or state.error_check_point:
                raise RuntimeError(f"地图交互或机关检查失败: {filename}")
            if state.skip_this_map:
                if not permitted_skip:
                    raise RuntimeError(f"地图未完成: {filename}")
                skipped.append(filename)
            else:
                completed.append(filename)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    engine.process_single_map = tracked
    # Calibration runs before process_single_map, so bound it separately too.
    signal.setitimer(signal.ITIMER_REAL, map_timeout)
    try:
        engine.process_map(start, mid, dev=False, single_map=bool(options.get("fight_single_map", False)))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    if not completed and not skipped:
        raise RuntimeError("锄大地没有执行任何路线")
    return {"completed": len(completed), "skipped": len(skipped), "start": start}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--options", required=True, type=Path)
    parser.add_argument("--fd", type=int)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    options = json.loads(args.options.read_text())
    rpc = RpcClient(socket.socket(fileno=args.fd)) if args.fd is not None else None
    engine = None
    try:
        engine = load_engine(args.root.resolve(), args.config.resolve())
        if args.probe:
            start, mid = choose_start(engine, options)
            print(json.dumps({"maps": len(engine.map_info.map_list), "start": start,
                              "version": engine.map_info.map_version, "mid": mid,
                              "desktop_modules": [name for name in sys.modules
                                  if name.split('.')[0] in {"win32gui", "win32api", "pyautogui", "tkinter", "pynput", "keyboard"}]}))
            return 0
        if rpc is None:
            raise ValueError("缺少云游戏连接")
        install_adapters(rpc)
        summary = run_routes(engine, options, rpc)
        rpc.call("done", summary=summary)
        return 0
    except Exception as exc:
        import traceback
        traceback.print_exc()
        if rpc:
            try:
                rpc.call("failed", error=str(exc))
            except Exception:
                pass
        return 1
    finally:
        if engine:
            engine.handle.movement.running = False
        if rpc:
            rpc.sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
