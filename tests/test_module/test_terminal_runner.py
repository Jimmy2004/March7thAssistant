"""module/terminal_runner.py 的单元测试。

全部通过 mock 隔离真实子进程、psutil 与文件锁，不启动任何真实游戏或浏览器。
"""

import datetime
import subprocess
import sys
import types

import pytest

from module import terminal_runner
from module.terminal_runner import (
    BROWSER_TAG,
    RETRY_INTERVAL_SECONDS,
    TERMINATE_TIMEOUT_SECONDS,
    _build_command,
    _build_env,
    _cleanup_browsers,
    _next_run_time,
    _stop_process,
    _validate_times,
    run_schedule,
    run_task,
)


class FakePopen:
    """子进程替身：可配置 wait() 的返回码或抛出的异常。"""

    def __init__(self, returncode=0, wait_error=None):
        self.returncode = returncode
        self.wait_error = wait_error
        self.terminated = False
        self.killed = False
        self.exited = False
        self.wait_timeouts = []
        self.command = None
        self.env = None
        self.cwd = None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        self.exited = True
        return self.returncode

    def poll(self):
        return self.returncode if self.exited else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


@pytest.fixture
def no_sleep(monkeypatch):
    """让 sleep 立即返回并记录调用参数。"""
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(terminal_runner.time, "sleep", fake_sleep)
    return calls


@pytest.fixture
def lock_free(monkeypatch, tmp_path):
    """把锁文件指向临时目录，避免污染项目根目录。"""
    monkeypatch.setattr(terminal_runner, "LOCK_FILE", str(tmp_path / "test.lock"))
    return tmp_path


def _stub_cleanup(monkeypatch, results):
    """按顺序返回清理结果（bool），并记录调用次数。"""
    calls = []
    queue = list(results)

    def fake_cleanup():
        calls.append(True)
        return queue.pop(0) if queue else True

    monkeypatch.setattr(terminal_runner, "_cleanup_browsers", fake_cleanup)
    return calls


def _stub_popen(monkeypatch, procs):
    """按顺序返回假子进程；procs 中的异常项会被 Popen 抛出。"""
    spawned = []
    queue = list(procs)

    def fake_popen(command, env=None, cwd=None):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        item.command = command
        item.env = env
        item.cwd = cwd
        spawned.append(item)
        return item

    monkeypatch.setattr(terminal_runner.subprocess, "Popen", fake_popen)
    return spawned


# ---------------------------------------------------------------------------
# _build_env / _build_command
# ---------------------------------------------------------------------------


def test_build_env_sets_required_variables(monkeypatch):
    monkeypatch.setenv("MARCH7TH_BROWSER_HEADLESS_ENABLE", "stale")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", "/should/be/removed")
    env = _build_env(debug=False)
    assert env["MARCH7TH_TERMINAL"] == "1"
    assert env["MARCH7TH_CLOUD_GAME_ENABLE"] == "true"
    assert env["MARCH7TH_BROWSER_HEADLESS_ENABLE"] == "true"
    assert env["MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN"] == "true"
    assert "MARCH7TH_GUI_STARTED" not in env
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in env


def test_build_env_headless_follows_debug():
    assert _build_env(debug=True)["MARCH7TH_BROWSER_HEADLESS_ENABLE"] == "false"
    assert _build_env(debug=False)["MARCH7TH_BROWSER_HEADLESS_ENABLE"] == "true"


def test_build_command_uses_main_py(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    cmd = _build_command("daily")
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("main.py")
    assert cmd[2] == "daily"


# ---------------------------------------------------------------------------
# _validate_times / _next_run_time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("good", [["08:00"], ["00:00", "23:59"], ["09:05", "21:30"]])
def test_validate_times_accepts_strict_hh_mm(good):
    assert _validate_times(good) == sorted(set(good))


@pytest.mark.parametrize(
    "bad",
    [
        [],
        ["8:00"],       # 小时缺少前导零
        ["08:0"],       # 分钟缺少前导零
        ["24:00"],      # 小时越界
        ["12:60"],      # 分钟越界
        ["08-00"],      # 非法分隔符
        ["08:00:00"],   # 多余部分
        ["abc"],
        [800],          # 非字符串
        None,
    ],
)
def test_validate_times_rejects_invalid(bad):
    with pytest.raises(ValueError):
        _validate_times(bad)


def test_validate_times_sorts_and_dedupes():
    assert _validate_times(["21:30", "08:00", "08:00"]) == ["08:00", "21:30"]


def test_next_run_time_same_day():
    now = datetime.datetime(2026, 10, 17, 7, 0)
    assert _next_run_time(["08:00", "21:00"], now) == datetime.datetime(2026, 10, 17, 8, 0)


def test_next_run_time_skips_missed_times_no_catch_up():
    """已过时刻顺延到明天，不补跑今天错过的多个时刻。"""
    now = datetime.datetime(2026, 10, 17, 13, 0)
    assert _next_run_time(["08:00", "12:00"], now) == datetime.datetime(2026, 10, 18, 8, 0)


def test_next_run_time_exact_now_is_treated_as_missed():
    now = datetime.datetime(2026, 10, 17, 8, 0)
    assert _next_run_time(["08:00"], now) == datetime.datetime(2026, 10, 18, 8, 0)


# ---------------------------------------------------------------------------
# run_task
# ---------------------------------------------------------------------------


def test_run_task_success_first_try(lock_free, no_sleep, monkeypatch):
    spawned = _stub_popen(monkeypatch, [FakePopen(returncode=0)])
    cleanups = _stub_cleanup(monkeypatch, [True])

    assert run_task("daily", retries=2) is True
    assert len(spawned) == 1
    proc = spawned[0]
    assert proc.command[2] == "daily"
    assert proc.cwd == terminal_runner.PROJECT_ROOT
    assert proc.env["MARCH7TH_TERMINAL"] == "1"
    assert proc.env["MARCH7TH_CLOUD_GAME_ENABLE"] == "true"
    assert "MARCH7TH_GUI_STARTED" not in proc.env
    assert len(cleanups) == 1
    assert no_sleep == []  # 首次即成功，无重试间隔


def test_run_task_retries_with_five_second_interval(lock_free, no_sleep, monkeypatch):
    spawned = _stub_popen(
        monkeypatch, [FakePopen(1), FakePopen(1), FakePopen(0)]
    )
    cleanups = _stub_cleanup(monkeypatch, [True, True, True])

    assert run_task("daily", retries=2) is True
    assert len(spawned) == 3  # 两次失败 + 第三次成功
    assert no_sleep == [RETRY_INTERVAL_SECONDS, RETRY_INTERVAL_SECONDS]
    assert RETRY_INTERVAL_SECONDS == 5
    assert len(cleanups) == 3  # 每次尝试后都清理


def test_run_task_fails_after_exhausting_retries(lock_free, no_sleep, monkeypatch):
    spawned = _stub_popen(monkeypatch, [FakePopen(1), FakePopen(1)])
    _stub_cleanup(monkeypatch, [True, True])

    assert run_task("daily", retries=1) is False
    assert len(spawned) == 2  # retries=1 -> 共 2 次尝试


def test_run_task_stops_when_cleanup_fails(lock_free, no_sleep, monkeypatch):
    """清理失败后不得开始下一次运行。"""
    spawned = _stub_popen(monkeypatch, [FakePopen(1), FakePopen(0)])
    cleanups = _stub_cleanup(monkeypatch, [False])

    assert run_task("daily", retries=2) is False
    assert len(spawned) == 1  # 只尝试了一次，后续重试被取消
    assert len(cleanups) == 1


def test_run_task_continues_when_spawn_fails(lock_free, no_sleep, monkeypatch):
    """子进程启动失败按一次失败处理，清理失败会阻止后续重试。"""
    spawned = _stub_popen(monkeypatch, [OSError("boom"), FakePopen(0)])
    _stub_cleanup(monkeypatch, [True, True])

    assert run_task("daily", retries=1) is True
    assert len(spawned) == 1  # 第一次未能 spawn，第二次成功


def test_run_task_ctrl_c_terminates_child_and_cleans_up(lock_free, no_sleep, monkeypatch):
    spawned = _stub_popen(
        monkeypatch, [FakePopen(wait_error=KeyboardInterrupt())]
    )
    cleanups = _stub_cleanup(monkeypatch, [True])

    with pytest.raises(KeyboardInterrupt):
        run_task("daily", retries=2)
    assert spawned[0].terminated is True
    assert len(cleanups) == 1  # Ctrl+C 后也清理了浏览器


def test_run_task_blocked_by_second_instance(lock_free, no_sleep, monkeypatch):
    """文件锁被占用时不得启动子进程。"""
    fcntl = pytest.importorskip("fcntl")
    calls = []
    monkeypatch.setattr(terminal_runner.subprocess, "Popen", lambda *a, **k: calls.append(1))

    lock_handle = open(terminal_runner.LOCK_FILE, "w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_task("daily") is False
        assert calls == []
    finally:
        lock_handle.close()


def test_run_task_rejects_invalid_args(lock_free):
    with pytest.raises(ValueError):
        run_task("")
    with pytest.raises(ValueError):
        run_task(123)
    with pytest.raises(ValueError):
        run_task("daily", retries=-1)


def test_debug_run_uses_visible_browser_env(lock_free, no_sleep, monkeypatch):
    spawned = _stub_popen(monkeypatch, [FakePopen(0)])
    _stub_cleanup(monkeypatch, [True])

    assert run_task("daily", debug=True) is True
    assert spawned[0].env["MARCH7TH_BROWSER_HEADLESS_ENABLE"] == "false"


# ---------------------------------------------------------------------------
# _stop_process
# ---------------------------------------------------------------------------


def test_stop_process_terminate_then_wait():
    proc = FakePopen(0)  # poll() 返回 None，进程仍在运行
    _stop_process(proc)
    assert proc.terminated is True
    assert proc.killed is False
    assert proc.wait_timeouts == [TERMINATE_TIMEOUT_SECONDS]


def test_stop_process_kills_after_timeout():
    proc = FakePopen(0)
    results = iter([subprocess.TimeoutExpired(cmd="x", timeout=1), 0])

    def wait(timeout=None):
        proc.wait_timeouts.append(timeout)
        value = next(results)
        if isinstance(value, BaseException):
            raise value
        proc.exited = True
        return value

    proc.wait = wait
    _stop_process(proc)
    assert proc.terminated is True
    assert proc.killed is True
    assert len(proc.wait_timeouts) == 2  # terminate 后超时，再 kill 后 wait


def test_stop_process_noop_when_already_exited():
    proc = FakePopen(0)
    proc.exited = True  # poll() 返回 0，表示已退出
    _stop_process(proc)
    assert proc.terminated is False
    assert proc.wait_timeouts == []


# ---------------------------------------------------------------------------
# _cleanup_browsers
# ---------------------------------------------------------------------------


class FakePsutilProcess:
    def __init__(self, cmdline, die_on_terminate=False):
        self._cmdline = cmdline
        self.terminated = False
        self.killed = False
        self.die_on_terminate = die_on_terminate

    def cmdline(self):
        return self._cmdline

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _install_fake_psutil(monkeypatch, procs):
    fake = types.ModuleType("psutil")

    class FakePsutilError(Exception):
        pass

    fake.Error = FakePsutilError
    fake.Process = FakePsutilProcess
    fake.process_iter = lambda attrs=None: list(procs)

    def wait_procs(candidates, timeout=None):
        gone = [p for p in candidates if p.die_on_terminate]
        alive = [p for p in candidates if not p.die_on_terminate]
        return gone, alive

    fake.wait_procs = wait_procs
    monkeypatch.setitem(sys.modules, "psutil", fake)
    return fake


def test_cleanup_only_kills_tagged_processes(monkeypatch):
    tagged = FakePsutilProcess(["/path/chrome", BROWSER_TAG, "--remote-debugging-port=9222"])
    other_browser = FakePsutilProcess(["/path/chrome", "--remote-debugging-port=9222"])
    unrelated = FakePsutilProcess(["/usr/bin/vim"])
    tagged.die_on_terminate = True

    _install_fake_psutil(monkeypatch, [tagged, other_browser, unrelated])

    assert _cleanup_browsers() is True
    assert tagged.terminated is True
    assert other_browser.terminated is False
    assert unrelated.terminated is False


def test_cleanup_reports_failure_when_kill_also_fails(monkeypatch):
    survivor = FakePsutilProcess(["chrome", BROWSER_TAG])  # terminate 后仍存活 -> noexcept kill
    _install_fake_psutil(monkeypatch, [survivor])

    assert _cleanup_browsers() is False
    assert survivor.terminated is True
    assert survivor.killed is True


def test_cleanup_noop_when_no_tagged_process(monkeypatch):
    _install_fake_psutil(monkeypatch, [FakePsutilProcess(["chrome", "--other-tag"])])
    assert _cleanup_browsers() is True


def test_cleanup_ignores_processes_that_raise_on_cmdline(monkeypatch):
    fake = _install_fake_psutil(monkeypatch, [])

    class BadProcess:
        def cmdline(self):
            raise fake.Error("access denied")

    fake.process_iter = lambda attrs=None: [BadProcess()]

    assert _cleanup_browsers() is True


# ---------------------------------------------------------------------------
# run_schedule
# ---------------------------------------------------------------------------


class Clock:
    """可推进的假时钟，sleep 时前进。"""

    def __init__(self, start):
        self.now = start
        self.sleeps = []

    def local_now(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += datetime.timedelta(seconds=seconds)

    def interrupt_on_nth_sleep(self, n):
        original = self.sleep

        def wrapped(seconds):
            original(seconds)
            if len(self.sleeps) >= n:
                raise KeyboardInterrupt

        return wrapped


def test_run_schedule_runs_once_at_target_then_ctrl_c(lock_free, monkeypatch):
    clock = Clock(datetime.datetime(2026, 10, 17, 7, 59, 59))
    monkeypatch.setattr(terminal_runner, "_local_now", clock.local_now)
    monkeypatch.setattr(terminal_runner.time, "sleep", clock.sleep)

    ran = []

    def fake_run_task(task, retries=2, debug=False):
        ran.append((task, retries, debug))
        # 运行完后，等待下一次定时点时被 Ctrl+C 打断
        monkeypatch.setattr(terminal_runner.time, "sleep", clock.interrupt_on_nth_sleep(1))
        return True

    monkeypatch.setattr(terminal_runner, "run_task", fake_run_task)

    assert run_schedule("daily", ["08:00"], retries=1, debug=True) is True
    assert ran == [("daily", 1, True)]  # 到达目标时刻后才运行
    assert clock.sleeps  # 全程通过 sleep 等待，未启动任何其他进程


def test_run_schedule_ctrl_c_before_any_run_returns_true(lock_free, monkeypatch):
    """还没运行任何任务就被 Ctrl+C -> 返回 True。"""
    clock = Clock(datetime.datetime(2026, 10, 17, 7, 0))
    monkeypatch.setattr(terminal_runner, "_local_now", clock.local_now)
    monkeypatch.setattr(terminal_runner.time, "sleep", clock.interrupt_on_nth_sleep(1))

    calls = []
    monkeypatch.setattr(terminal_runner, "run_task", lambda *a, **k: calls.append(1) or True)

    assert run_schedule("daily", ["08:00"]) is True
    assert calls == []


def test_run_schedule_returns_false_when_a_run_failed(lock_free, monkeypatch):
    clock = Clock(datetime.datetime(2026, 10, 17, 7, 59, 59))
    monkeypatch.setattr(terminal_runner, "_local_now", clock.local_now)
    monkeypatch.setattr(terminal_runner.time, "sleep", clock.sleep)

    def failing_run(task, retries=2, debug=False):
        monkeypatch.setattr(terminal_runner.time, "sleep", clock.interrupt_on_nth_sleep(1))
        return False

    monkeypatch.setattr(terminal_runner, "run_task", failing_run)

    assert run_schedule("daily", ["08:00"]) is False


def test_run_schedule_waits_until_target_without_running(lock_free, no_sleep, monkeypatch):
    """目标时刻未到时只 sleep，不调用 run_task。"""
    ticks = {"n": 0}

    def advancing_now():
        ticks["n"] += 1
        if ticks["n"] <= 2:
            return datetime.datetime(2026, 10, 17, 7, 0)
        raise KeyboardInterrupt

    monkeypatch.setattr(terminal_runner, "_local_now", advancing_now)
    calls = []
    monkeypatch.setattr(terminal_runner, "run_task", lambda *a, **k: calls.append(1) or True)

    assert run_schedule("daily", ["08:00"]) is True
    assert calls == []
    assert no_sleep  # 等待通过 sleep 完成


def test_run_schedule_propagates_invalid_times(lock_free):
    with pytest.raises(ValueError):
        run_schedule("daily", ["25:00"])
    with pytest.raises(ValueError):
        run_schedule("daily", [])
