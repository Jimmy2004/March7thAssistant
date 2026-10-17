"""Tests for :mod:`module.update.sync_policy`."""

from __future__ import annotations

import pytest

from module.update.sync_policy import is_managed_path as managed


# ---------------------------------------------------------------------------
# Hard rejections — applied to every input, regardless of allow-list.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "",
    " ",
    "/etc/passwd",
    "/main.py",
    "C:/main.py",
    "C:foo",
    "../main.py",
    "module/../main.py",
    "tasks/base/../../main.py",
    "utils\\admin.py",
    "module\\automation\\foo.py",
    ".git/config",
    ".github/workflows/ci.yml",
    ".venv/bin/python",
    "module/.hidden/foo.py",
    "tasks/.cache/x.py",
    "\0bad",
    "module/automation/\0bad.py",
    "main.py\0",
    "\\0leet.py",
])
def test_rejects_unsafe_inputs(path):
    assert managed(path) is False


# ---------------------------------------------------------------------------
# Platform denylist — Windows-only artefacts, GUI leftovers, repo metadata.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "tools/launcher.exe",
    "module/automation/hook.dll",
    "assets/scripts/setup.bat",
    "utils/command/legacy.cmd",
    "tasks/build.ps1",
    "desktop.lnk",
    "build/March7thAssistant.spec",
    "module/update/extra.spec",
    "app/main.py",
    "app/__init__.py",
    "3rdparty/foo/bar.py",
    "README.md",
    "README_MACOS.md",
    "README_EN.md",
    "requirements.txt",
    "requirements-macos.txt",
    "pyproject.toml",
    "uv.lock",
    ".github/ISSUE_TEMPLATE/bug.md",
    "module/update/sync_policy.py",
    "module/update/version_check.py",
    "terminal.py",
    "config.yaml",
    "settings/user.yaml",
    "logs/2026-10-17.log",
    "tasks/base/genshin_starRail_fps_unlocker.py",
    "tasks/base/pythonchecker.py",
    "tasks/game/starrailcontroller.py",
    "tasks/tool/screenshot.py",
    "tasks/tool/__init__.py",
    "tasks/tool/autoplot/_engine.py",
    "tasks/base/tasks.py",
    "utils/admin.py",
    "utils/autostart.py",
    "utils/schedule.py",
    "utils/desktop.py",
    "utils/terminal_qr.py",
    "utils/registry/__init__.py",
    "utils/registry/game_auto_hdr.py",
    "assets/config/upstream-sync.json",
])
def test_platform_denylist(path):
    assert managed(path) is False


# ---------------------------------------------------------------------------
# Directory traversal & look-alike-prefix attempts.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    # Looks like an allow-listed module tree but escapes via ``..``.
    "module/automation/../update/version_check.py",
    # Prefix confusion: ``module/automation2`` is not the allow-listed
    # ``module/automation``.
    "module/automation2/foo.py",
    "module/automatic/foo.py",
    # The ``utils`` denylist is exact-match per file / subpackage, so
    # siblings whose names merely share a prefix are NOT excluded. We
    # therefore do NOT add look-alike filenames here — they pass — but
    # we DO still reject the exact excluded directory ``utils/registry``.
    "utils/registry/sub/foo.py",
    "utils/registry/x.py",
    # ``assets`` look-alikes that must not slip into the images allow-list
    # (which is exact-prefix against ``share`` / ``zh_CN`` / ``autoplot``).
    "assets/images/zh_CNs/foo.png",
    "assets/images/zh_CN.png",
    "assets/images/auto_plot/foo.png",
    "assets/images/sharemore/foo.png",
    # ``assets/config`` vs. ``assets/configuration``.
    "assets/configuration/foo.json",
    # ``assets/models`` vs. an unrelated subtree.
    "assets/modelzoo/foo.onnx",
    # Bare Windows drive style sneaking past the absolute-path check.
    "D:module/automation/foo.py",
    # Try to walk out of an allowed root to land on a denied file.
    "tasks/base/../tool/screenshot.py",
    "tasks/../terminal.py",
    "module/automation/../../main.py",
])
def test_prefix_and_traversal_attempts(path):
    assert managed(path) is False


# ---------------------------------------------------------------------------
# Positive cases — legitimate cloud-game payload entries.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    # Entry point.
    "main.py",
    # ``module/`` allow-list.
    "module/automation/foo/bar.py",
    "module/automation/__init__.py",
    "module/game/cloud.py",
    "module/game/base.py",
    "module/ocr/ocr.py",
    "module/screen/screen.py",
    "module/workflow/run.py",
    "module/localization/i18n.py",
    "module/notification/notify.py",
    "module/telemetry/metrics.py",
    "module/logger/log.py",
    "module/config/config.py",
    # ``tasks/`` tree in full.
    "tasks/__init__.py",
    "tasks/base/base.py",
    "tasks/base/download.py",
    "tasks/daily/foo.py",
    "tasks/game/__init__.py",
    "tasks/tool/autoplot/_config.py",
    # ``utils/`` excluding the denylist.
    "utils/__init__.py",
    "utils/clipboard.py",
    "utils/color.py",
    "utils/command.py",
    "utils/console.py",
    "utils/date.py",
    "utils/dpi.py",
    "utils/encryption.py",
    "utils/image_utils.py",
    "utils/pause.py",
    "utils/progress.py",
    "utils/screenshot_util.py",
    "utils/singleton.py",
    "utils/tasks.py",
    "utils/version.py",
    "utils/logger/__init__.py",
    # Near-prefix files — ``utils`` denylist is exact-match, so these pass.
    "utils/registryish/foo.py",
    "utils/admin_extra.py",
    "utils/desktop_lock.py",
    "utils/admin.py.bak",
    # ``assets/config/`` minus ``upstream-sync.json``.
    "assets/config/character_names.json",
    "assets/config/config.example.yaml",
    "assets/config/instance_drops.json",
    "assets/config/workflow.jsonc",
    "assets/config/workflows/abc.json",
    # ``assets/images/`` allow-list.
    "assets/images/share/2025-01-01/foo.png",
    "assets/images/zh_CN/daily/monday.png",
    "assets/images/autoplot/legend.png",
    # ``assets/models/``.
    "assets/models/divergent.onnx",
    "assets/models/skip.onnx",
    "assets/models/sub/group/model.onnx",
])
def test_accepts_managed_payload(path):
    assert managed(path) is True


@pytest.mark.parametrize("path", ["tasks/tool/autoplot/terminal.py", "tasks/tool/autoplot/__init__.py", "module/game/cloud.py/nested.py"])
def test_preserves_mac_specific_modules(path):
    assert not managed(path)


def test_real_model_directory_is_managed():
    assert managed("assets/model/divergent.onnx")


def test_cloud_quality_compatibility_helper_is_protected():
    assert not managed("utils/cloud_quality.py")
