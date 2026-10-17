"""Run native Fhoe logic without importing its Windows environment in this app."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from module.fight_cloud.resources import MANIFEST, PROJECT_ROOT, prepare_resources
from module.fight_cloud.transport import CloudTransport


def build_options(cfg):
    get = cfg.get_value
    options = {key: get(key, default) for key, default in {
        "fight_map_version": "default", "fight_main_map": "0", "fight_start_map": "",
        "fight_single_map": False, "fight_timeout": 12, "fight_map_timeout": 900,
    }.items()}
    if options["fight_map_version"] == "不配置":
        options["fight_map_version"] = "default"
    if options["fight_map_version"] not in MANIFEST["route_versions"]:
        raise ValueError("锄大地路线版本无效，请在运行设置中重新选择")
    hours = float(options["fight_timeout"])
    if not math.isfinite(hours) or not 0 < hours <= 24:
        raise ValueError("锄大地超时必须大于 0 且不超过 24 小时")
    return options


def write_engine_config(path, cfg, options):
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update({
        "real_width": 1920, "real_height": 1080,
        "map_version": options["fight_map_version"], "main_map": str(options["fight_main_map"]),
        "auto_shutdown": 0, "taskkill_name": "", "allow_run_again": False,
        "allow_run_next_day": False, "map_debug": False, "script_debug": False,
        "webhook_url": "", "notify_enabled": False,
        "refresh_hour": int(cfg.get_value("refresh_hour", 4)),
    })
    for key in ("allow_map_buy", "allow_snack_buy", "allow_memory_token"):
        value = cfg.get_value("fight_" + key, "不配置")
        if not cfg.get_value("fight_reward_enable", False):
            data[key] = False
        elif isinstance(value, bool):
            data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def serve_worker(proc, sock, transport, timeout):
    """Bound the whole worker lifetime, including loops with no RPC traffic."""
    deadline = time.monotonic() + timeout
    sock.setblocking(False)
    pending = bytearray()
    summary = None
    with selectors.DefaultSelector() as selector:
        selector.register(sock, selectors.EVENT_READ)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("锄大地达到运行时限")
            if not selector.select(min(0.5, max(0, deadline - time.monotonic()))):
                if proc.poll() is not None:
                    raise RuntimeError(f"锄大地引擎意外退出: {proc.returncode}")
                continue
            block = sock.recv(65536)
            if not block:
                break
            pending.extend(block)
            if len(pending) > 1024 * 1024:
                raise ValueError("锄大地请求超出长度限制")
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                request = json.loads(line)
                op, args = request["op"], request.get("args", {})
                if op == "failed":
                    raise RuntimeError(args.get("error", "锄大地未完成"))
                if op == "done":
                    summary = args["summary"]
                    result = None
                else:
                    result = transport.dispatch(op, args)
                reply = json.dumps({"ok": True, "result": result}).encode() + b"\n"
                sock.settimeout(max(0.1, min(30, deadline - time.monotonic())))
                sock.sendall(reply)
                sock.setblocking(False)
                if summary is not None:
                    code = proc.wait(timeout=max(0.1, min(10, deadline - time.monotonic())))
                    if code != 0:
                        raise RuntimeError(f"锄大地引擎退出码: {code}")
                    return summary
    raise RuntimeError("锄大地引擎未报告完成")


def run_cloud_fight(cfg, controller, logger):
    options = build_options(cfg)
    resources = prepare_resources()
    config_path = PROJECT_ROOT / "settings" / "fhoe-cloud.json"
    write_engine_config(config_path, cfg, options)
    if cfg.get_value("fight_team_enable", False):
        from tasks.base.team import Team
        Team.change_to(cfg.get_value("fight_team_number", "7"))
    from module.screen import screen
    screen.change_to("main")
    transport = CloudTransport(controller, logger)
    proc = None
    parent, child = socket.socketpair()
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, terminate)
    try:
        transport.enable()
        with tempfile.TemporaryDirectory(prefix="fhoe-run-", dir=PROJECT_ROOT / "settings") as tmp:
            option_path = Path(tmp) / "options.json"
            option_path.write_text(json.dumps(options), encoding="utf-8")
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            env.pop("PYTHONPATH", None)
            command = [sys.executable, str(Path(__file__).with_name("worker.py")),
                       "--root", str(resources), "--config", str(config_path),
                       "--options", str(option_path), "--fd", str(child.fileno())]
            logger.info(f"开始云端锄大地，路线：{options['fight_map_version']}；Ctrl+C 可停止")
            proc = subprocess.Popen(command, pass_fds=(child.fileno(),), env=env,
                                    stdin=subprocess.DEVNULL, cwd=resources)
            child.close()
            result = serve_worker(proc, parent, transport, float(options["fight_timeout"]) * 3600)
            logger.info(f"锄大地完成：执行 {result['completed']} 张，按设置跳过 {result['skipped']} 张")
            return True
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        parent.close()
        child.close()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        transport.close()
