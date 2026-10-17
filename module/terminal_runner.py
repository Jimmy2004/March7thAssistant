"""终端模式的任务运行器。

提供两个入口：
- ``run_task``：在终端中以子进程方式运行一次 ``main.py <task>``，支持有限重试。
- ``run_schedule``：按 ``HH:MM`` 时刻表每天定时运行任务，等待期间不启动任何进程。

设计约束：
- 顶层只导入标准库；``psutil`` 属于第三方库，在清理函数内部延迟导入。
- 子进程直接继承本进程的 stdout/stderr（不使用 PIPE），保持终端滚动输出。
- 通过项目级 ``fcntl`` 文件锁保证同一时刻只有一个实例在运行。
- 每次运行结束（成功 / 失败 / Ctrl+C）都清理本项目独占的浏览器进程
  （cmdline 中带 ``--march-7th-assistant-sr-cloud-game`` 标记的进程）。
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import re
import subprocess
import sys
import time

try:  # pragma: no cover - Windows 上没有 fcntl，降级为无锁运行
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_SCRIPT = os.path.join(PROJECT_ROOT, "main.py")
EXECUTABLE_NAME = "March7th Assistant.exe"
LOCK_FILE = os.path.join(PROJECT_ROOT, ".terminal_runner.lock")

# 本项目独占浏览器的 cmdline 标记，见 module/game/cloud.py
BROWSER_TAG = "--march-7th-assistant-sr-cloud-game"

RETRY_INTERVAL_SECONDS = 5
TERMINATE_TIMEOUT_SECONDS = 10
SCHEDULE_TICK_SECONDS = 60

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 与 GUI 启动方式保持一致，移除 Qt 相关变量避免子进程初始化 Qt 失败
_QT_ENV_KEYS = (
    "QML2_IMPORT_PATH",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_FONTDIR",
)


def _build_command(task: str) -> list[str]:
    """构造启动命令：打包后用同目录 exe，否则用 python 运行 main.py。"""
    if getattr(sys, "frozen", False):
        return [os.path.join(PROJECT_ROOT, EXECUTABLE_NAME), task]
    return [sys.executable, MAIN_SCRIPT, task]


def _build_env(debug: bool) -> dict[str, str]:
    """构造子进程环境变量；debug 决定浏览器是否以无头模式运行。"""
    env = dict(os.environ)
    for key in _QT_ENV_KEYS:
        env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["MARCH7TH_TERMINAL"] = "1"
    env["MARCH7TH_CLOUD_GAME_ENABLE"] = "true"
    env["MARCH7TH_BROWSER_HEADLESS_ENABLE"] = "false" if debug else "true"
    env["MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN"] = "true"
    env.pop("MARCH7TH_GUI_STARTED", None)
    env["MARCH7TH_AFTER_FINISH"] = "Exit"
    return env


def _local_now() -> datetime.datetime:
    """当前本地时间（可被测试 mock）。"""
    return datetime.datetime.now()


def _stop_process(proc: subprocess.Popen) -> None:
    """终止子进程：先 terminate，超时后 kill，最后 wait 收尸。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        with contextlib.suppress(OSError):
            proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)


def _cleanup_browsers() -> bool:
    """清理本项目独占的浏览器进程。

    只结束 cmdline 中精确包含 ``BROWSER_TAG`` 参数的进程，不触碰其他浏览器。
    返回是否全部清理成功。
    """
    import psutil

    tagged: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid"]):
        try:
            cmdline = proc.cmdline()
        except psutil.Error:
            continue
        if BROWSER_TAG not in cmdline:
            continue
        tagged.append(proc)

    ok = True
    for proc in tagged:
        try:
            proc.terminate()
        except psutil.Error:
            ok = False

    _, alive = psutil.wait_procs(tagged, timeout=TERMINATE_TIMEOUT_SECONDS)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            ok = False
    if alive:
        _, alive = psutil.wait_procs(alive, timeout=TERMINATE_TIMEOUT_SECONDS)
    return ok and not alive


@contextlib.contextmanager
def _project_lock():
    """项目级文件锁；yield 是否成功拿到锁。拿不到锁时不执行任何任务。"""
    acquired = False
    handle = None
    try:
        try:
            handle = open(LOCK_FILE, "w")
        except OSError as exc:
            logger.error("无法打开锁文件 %s: %s", LOCK_FILE, exc)
            yield False
            return
        if fcntl is None:  # pragma: no cover - 非 POSIX 平台
            acquired = True
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        yield acquired
    finally:
        if handle is not None:
            if acquired and fcntl is not None:  # pragma: no cover - 依赖平台
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def run_task(task: str, retries: int = 2, debug: bool = False) -> bool:
    """在终端中运行一次任务，返回是否成功。

    - 通过文件锁防止与另一个实例同时启动。
    - 最多尝试 ``retries + 1`` 次，两次运行之间间隔 5 秒。
    - 每次尝试结束后（无论成败）都清理独占浏览器；
      清理失败时立即停止，不再开始下一次尝试。
    - Ctrl+C 时终止子进程、清理浏览器并返回 False。
    """
    if not isinstance(task, str) or not task:
        raise ValueError("task 必须是非空字符串")
    if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 5:
        raise ValueError("retries 必须是 0-5 的整数")

    command = _build_command(task)
    env = _build_env(debug)

    with _project_lock() as acquired:
        if not acquired:
            logger.error("已有实例正在运行（锁文件: %s），本次退出", LOCK_FILE)
            return False

        attempts = retries + 1
        for attempt in range(attempts):
            if attempt:
                time.sleep(RETRY_INTERVAL_SECONDS)
            logger.info("运行任务 %s（第 %d/%d 次）", task, attempt + 1, attempts)
            proc = None
            returncode = None
            cleanup_ok = False
            try:
                proc = subprocess.Popen(command, env=env, cwd=PROJECT_ROOT)
                returncode = proc.wait()
            except OSError as exc:
                logger.error("启动子进程失败: %s", exc)
            finally:
                # Also runs on Ctrl+C, unexpected errors, and failed process creation.
                try:
                    if proc is not None:
                        _stop_process(proc)
                finally:
                    cleanup_ok = _cleanup_browsers()
            if not cleanup_ok:
                logger.error("浏览器清理失败，停止剩余重试")
                return False
            if returncode == 0:
                return True
            logger.warning("任务 %s 返回码 %s", task, returncode)
        return False


def _validate_times(times) -> list[str]:
    """严格校验 ``HH:MM`` 格式（两位小时 00-23、两位分钟 00-59），去重并排序。"""
    if not isinstance(times, (list, tuple)) or not times:
        raise ValueError("times 必须是非空列表")
    validated: list[str] = []
    for item in times:
        if not isinstance(item, str) or not _TIME_PATTERN.match(item):
            raise ValueError(f"非法的时刻格式: {item!r}，要求 HH:MM（如 08:30）")
        if item not in validated:
            validated.append(item)
    return sorted(validated)


def _next_run_time(times: list[str], now: datetime.datetime | None = None) -> datetime.datetime:
    """返回下一个即将到来的运行时刻；今天已过的时刻顺延到明天，不补跑。"""
    if now is None:
        now = _local_now()
    for hhmm in times:
        hour, minute = int(hhmm[:2]), int(hhmm[3:])
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
    first_hour, first_minute = int(times[0][:2]), int(times[0][3:])
    tomorrow = now + datetime.timedelta(days=1)
    return tomorrow.replace(hour=first_hour, minute=first_minute, second=0, microsecond=0)


def run_schedule(
    task: str,
    times: list[str],
    retries: int = 2,
    debug: bool = False,
) -> bool:
    """按每天重复的 ``HH:MM`` 时刻表定时运行任务。

    - 时刻表在开始时一次性严格校验，非法格式抛 ``ValueError``。
    - 只运行下一个即将到来的时刻；错过的多个时刻不会补跑。
    - 等待期间不启动任何进程（不打开浏览器）。
    - Ctrl+C 时立即返回，返回值表示此前所有已执行运行是否全部成功。
    """
    schedule = _validate_times(times)
    all_ok = True
    try:
        while True:
            target = _next_run_time(schedule)
            while True:
                delta = (target - _local_now()).total_seconds()
                if delta <= 0:
                    break
                time.sleep(min(delta, SCHEDULE_TICK_SECONDS))
            all_ok = run_task(task, retries=retries, debug=debug) and all_ok
    except KeyboardInterrupt:
        logger.warning("收到 Ctrl+C，退出定时运行")
        return all_ok
