"""Cloud route transport, timeouts, verified assets and native engine probes."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from module.fight_cloud import resources
from module.fight_cloud.runner import build_options, serve_worker, write_engine_config
from module.fight_cloud.transport import CloudTransport


def cfg(**values):
    return SimpleNamespace(get_value=lambda key, default=None: values.get(key, default))


def test_options_map_legacy_unconfigured_version():
    assert build_options(cfg(fight_map_version="不配置"))["fight_map_version"] == "default"


@pytest.mark.parametrize("hours", [0, -1, 25, float("nan"), float("inf")])
def test_invalid_timeout_rejected(hours):
    with pytest.raises(ValueError):
        build_options(cfg(fight_timeout=hours))


def test_config_keeps_calibration_and_disables_shutdown_and_repeat(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"angle": "1.2", "angle_set": True, "auto_shutdown": 1,
                                "allow_run_again": True, "allow_map_buy": True}))
    write_engine_config(path, cfg(), build_options(cfg()))
    data = json.loads(path.read_text())
    assert data["angle"] == "1.2" and data["angle_set"]
    assert not data["auto_shutdown"] and not data["allow_run_again"]
    assert not data["allow_map_buy"]


def test_partial_resource_download_never_installs(tmp_path):
    archive = tmp_path / "invalid.tar.gz"
    archive.write_bytes(b"truncated")
    destination = tmp_path / "installed"
    with pytest.raises(ValueError, match="校验失败"):
        resources.install_archive(archive, destination)
    assert not destination.exists()


def test_allowlist_drops_windows_launchers_and_ui():
    from pathlib import PurePosixPath as P
    for name in ("fhoe.py", "点这里啦.exe", "utils/drivers/window.py", "utils/ui/pause.py", "picture/a.exe"):
        assert not resources._keep(P(name))
    for name in ("map/default/map_1-1_0.json", "picture/transfer.webp", "utils/flows/combat.py", "LICENSE"):
        assert resources._keep(P(name))


def test_upstream_updates_do_not_overwrite_cloud_engine_or_dispatch():
    from module.update.sync_policy import is_managed_path
    assert not is_managed_path("tasks/daily/fight.py")
    assert not is_managed_path("module/fight_cloud/worker.py")
    assert not is_managed_path("module/fight_cloud/engine.json")


def transport():
    controller = Mock()
    return CloudTransport(controller, Mock()), controller


def test_key_press_releases_after_sleep_failure(monkeypatch):
    bridge, controller = transport()
    monkeypatch.setattr("module.fight_cloud.transport.time.sleep", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        bridge.dispatch("key_press", {"key": "shift", "delay": 0.1})
    payloads = [call.args[1] for call in controller.execute_cdp_cmd.call_args_list]
    assert [data["type"] for data in payloads] == ["keyDown", "keyUp"]
    assert payloads[-1]["modifiers"] == 0
    assert not bridge.keys


def test_mouse_error_releases_button(monkeypatch):
    bridge, controller = transport()
    monkeypatch.setattr("module.fight_cloud.transport.time.sleep", Mock(side_effect=RuntimeError("fail")))
    with pytest.raises(RuntimeError):
        bridge.click(200, 300)
    last = controller.execute_cdp_cmd.call_args.args[1]
    assert last["type"] == "mouseReleased" and last["buttons"] == 0


def test_relative_movement_arms_then_uses_trusted_cdp():
    bridge, controller = transport()
    bridge.dispatch("relative_move", {"dx": 495, "dy": 0})
    assert controller.driver.execute_script.call_args.args[1:] == (495, 0)
    command, args = controller.execute_cdp_cmd.call_args.args
    assert command == "Input.dispatchMouseEvent" and args["type"] == "mouseMoved"
    assert 0 <= args["x"] < 1920


@pytest.mark.parametrize("op,args", [("click", {"x": -1, "y": 100}),
    ("key_down", {"key": "unsupported"}), ("scroll", {"clicks": float("nan")}),
    ("relative_move", {"dx": float("inf")}), ("shell", {})])
def test_unknown_or_invalid_inputs_fail(op, args):
    bridge, _ = transport()
    with pytest.raises(ValueError):
        bridge.dispatch(op, args)


def test_close_releases_every_held_key_and_clears_script():
    bridge, controller = transport()
    bridge.dispatch("key_down", {"key": "w"})
    bridge.dispatch("key_down", {"key": "alt"})
    bridge.close()
    assert not bridge.keys
    assert "clear()" in controller.driver.execute_script.call_args.args[0]


def test_worker_timeout_even_without_requests():
    parent, child = socket.socketpair()
    try:
        with pytest.raises(TimeoutError):
            serve_worker(Mock(poll=lambda: None), parent, Mock(), 0.02)
    finally:
        parent.close()
        child.close()


def test_worker_failure_is_not_success():
    parent, child = socket.socketpair()
    try:
        child.sendall(json.dumps({"op": "failed", "args": {"error": "传送失败"}}).encode() + b"\n")
        with pytest.raises(RuntimeError, match="传送失败"):
            serve_worker(Mock(), parent, Mock(), 1)
    finally:
        parent.close()
        child.close()


def test_worker_success_requires_done_and_clean_exit():
    parent, child = socket.socketpair()
    try:
        child.sendall(json.dumps({"op": "done", "args": {"summary": {"completed": 1, "skipped": 0}}}).encode() + b"\n")
        assert serve_worker(Mock(wait=lambda timeout: 0), parent, Mock(), 1)["completed"] == 1
    finally:
        parent.close()
        child.close()


def load_adapters():
    path = resources.PROJECT_ROOT / "module/fight_cloud/adapters.py"
    spec = importlib.util.spec_from_file_location("fight_adapters_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_screenshot_uses_game_frame_bgr_and_offsets():
    adapter = load_adapters()
    buffer = io.BytesIO()
    Image.new("RGB", (1920, 1080), (255, 0, 0)).save(buffer, format="PNG")
    adapter._rpc = Mock(call=Mock(return_value=base64.b64encode(buffer.getvalue()).decode()))
    source = adapter.ScreenSource()
    frame, *rect = source.take_screenshot((10, 20, -30, -40))
    assert rect == [10, 20, 1890, 1040]
    assert frame.shape == (1020, 1880, 3)
    assert tuple(frame[0, 0]) == (0, 0, 255)
    assert source.has_fresh_frame()


@pytest.mark.skipif(os.environ.get("MARCH7TH_FHOE_TESTS") != "1", reason="opt-in downloaded real engine probe")
@pytest.mark.parametrize("version", resources.MANIFEST["route_versions"])
def test_real_engine_all_versions_import_without_desktop(version, tmp_path):
    root = resources.prepare_resources()
    config = tmp_path / "config.json"
    options = build_options(cfg(fight_map_version=version))
    write_engine_config(config, cfg(), options)
    option_file = tmp_path / "options.json"
    option_file.write_text(json.dumps(options))
    result = subprocess.run([sys.executable, str(resources.PROJECT_ROOT / "module/fight_cloud/worker.py"),
        "--root", str(root), "--config", str(config), "--options", str(option_file), "--probe"],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["version"] == version and data["maps"] > 0
    assert data["desktop_modules"] == []


@pytest.mark.skipif(os.environ.get("MARCH7TH_FHOE_TESTS") != "1", reason="opt-in real engine RPC route replay")
def test_actual_engine_replays_route_through_socket_without_desktop(tmp_path):
    root = resources.prepare_resources()
    fixture = tmp_path / "engine"
    fixture.mkdir()
    for name in ("utils", "picture"):
        (fixture / name).symlink_to(root / name, target_is_directory=True)
    maps = fixture / "map/default"
    maps.mkdir(parents=True)
    (maps / "map_1-1_0.json").write_text(json.dumps({
        "name": "Fixture-1", "author": "test",
        "start": [{"normal_run": 1}],
        "map": [{"space": 0.01}, {"caps": 0.01}, {"mouse_move": 1}, {"scroll": 1}, {"w": 0.05}],
    }))
    config = tmp_path / "config.json"
    options = build_options(cfg(fight_single_map=True))
    write_engine_config(config, cfg(), options)
    data = json.loads(config.read_text())
    data.update(angle="1.2", angle_set=True, auto_run_in_map=False)
    config.write_text(json.dumps(data))
    script = tmp_path / "replay.py"
    script.write_text("\n".join([
        "import sys,socket,json",
        f"sys.path.insert(0,{str(resources.PROJECT_ROOT / 'module/fight_cloud')!r})",
        "from worker import load_engine,run_routes",
        "from adapters import RpcClient,install_adapters",
        f"from pathlib import Path; engine=load_engine(Path({str(fixture)!r}),Path({str(config)!r}))",
        "rpc=RpcClient(socket.socket(fileno=int(sys.argv[1])));install_adapters(rpc)",
        "from utils.flows.monthly_pass import MonthlyPass",
        "MonthlyPass.monthly_pass_check=lambda self:None",
        f"summary=run_routes(engine,json.loads({json.dumps(options)!r}),rpc)",
        "rpc.call('done',summary=summary)",
    ]))
    parent, child = socket.socketpair()
    events = []
    buffer = io.BytesIO()
    Image.new("RGB", (1920, 1080), (255, 0, 0)).save(buffer, format="PNG")
    image = base64.b64encode(buffer.getvalue()).decode()

    def dispatch(op, args):
        events.append((op, args))
        return image if op == "screenshot" else None

    proc = subprocess.Popen([sys.executable, str(script), str(child.fileno())],
        pass_fds=(child.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.close()
    try:
        summary = serve_worker(proc, parent, SimpleNamespace(dispatch=dispatch), 15)
        assert summary["completed"] == 1 and summary["skipped"] == 0
        assert any(op == "relative_move" for op, _ in events)
        assert any(op == "key_press" and args["key"] == "caps" for op, args in events)
        assert any(op == "key_up" and args["key"] == "w" for op, args in events)
        assert any(op == "scroll" for op, _ in events)
    finally:
        parent.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
