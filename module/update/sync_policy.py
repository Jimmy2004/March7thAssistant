"""Selective upstream-sync policy for the macOS CN cloud-game build.

Pure function :func:`is_managed_path` decides whether a relative repository
path is allowed to participate in cloud-game upstream sync. The rules below
are intentionally explicit and conservative — anything not covered by the
allow-list is rejected.

Allow-list (relative repo paths):

* ``main.py``
* ``module/`` subtrees: ``automation``, ``game/cloud.py``, ``game/base.py``,
  ``ocr``, ``screen``, ``workflow``, ``localization``, ``notification``,
  ``telemetry``, ``logger``, ``config/config.py``
* ``tasks/`` tree in full, excluding ``tasks/base/genshin_starRail_fps_unlocker.py``,
  ``tasks/base/pythonchecker.py`` and ``tasks/game/starrailcontroller.py``
* ``utils/`` tree in full, excluding the ``registry/`` subpackage and the
  files ``admin.py``, ``autostart.py``, ``schedule.py``, ``desktop.py``,
  ``terminal_qr.py``
* ``assets/images/share/``, ``assets/images/zh_CN/``, ``assets/images/autoplot/``
* ``assets/config/`` in full except ``assets/config/upstream-sync.json``
* ``assets/models/`` in full

Platform denylist (Windows-only artefacts, GUI leftovers, run-time state):

* File suffixes: ``.exe``, ``.dll``, ``.bat``, ``.cmd``, ``.ps1``, ``.lnk``,
  ``.spec``
* Top-level pieces: ``app/``, ``3rdparty/``, ``README*``, ``requirements*``,
  ``pyproject.toml``, ``uv.lock``, ``.github/``, ``module/update/``,
  ``terminal.py``, ``config.yaml``, ``settings/``, ``logs/``
* Deleted GUI code that must not come back: ``tasks/tool/screenshot.py``,
  ``tasks/tool/__init__.py``, ``tasks/tool/autoplot/_engine.py``,
  ``tasks/base/tasks.py``

Hard rejections on every input (security & correctness):

* Empty path, absolute paths, ``..`` segments, backslashes, hidden
  directories (``.*`` component) and ``\\0`` are always rejected.
"""

from __future__ import annotations

import os
from typing import Final

# ---------------------------------------------------------------------------
# Hard rejections — applied before anything else.
# ---------------------------------------------------------------------------

_WINDOWS_SUFFIXES: Final[tuple[str, ...]] = (
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".lnk", ".spec",
)

# Top-level entries that are not part of the managed cloud-game payload.
_TOP_LEVEL_DENY: Final[frozenset[str]] = frozenset({
    "app",
    "3rdparty",
    ".github",
    "settings",
    "logs",
})

# Bare filenames at the repo root that must never be synced.
_ROOT_FILE_DENY: Final[frozenset[str]] = frozenset({
    "terminal.py",
    "config.yaml",
    "pyproject.toml",
    "uv.lock",
})

# ``tasks/`` files removed from the managed set.
_TASKS_DENY: Final[frozenset[str]] = frozenset({
    "tasks/base/genshin_starRail_fps_unlocker.py",
    "tasks/base/pythonchecker.py",
    "tasks/game/starrailcontroller.py",
    # GUI leftovers — server must never re-create these.
    "tasks/tool/screenshot.py",
    "tasks/tool/__init__.py",
    "tasks/tool/autoplot/_engine.py",
    "tasks/base/tasks.py",
    "tasks/tool/autoplot/terminal.py",
    "tasks/tool/autoplot/__init__.py",
})

# ``utils/`` entries excluded from the managed set.
_UTILS_DENY_DIRS: Final[frozenset[str]] = frozenset({
    "utils/registry",
})
_UTILS_DENY_FILES: Final[frozenset[str]] = frozenset({
    "utils/admin.py",
    "utils/autostart.py",
    "utils/schedule.py",
    "utils/desktop.py",
    "utils/terminal_qr.py",
})

# ``assets/config/`` exclusion.
_ASSETS_CONFIG_DENY: Final[frozenset[str]] = frozenset({
    "assets/config/upstream-sync.json",
})

# ---------------------------------------------------------------------------
# Allow-list — only these roots / subtrees may participate in sync.
# ---------------------------------------------------------------------------

_ALLOWED_ROOTS: Final[tuple[str, ...]] = (
    "main.py",
    "module",
    "tasks",
    "utils",
    "assets",
)

_MODULE_ALLOWED: Final[frozenset[str]] = frozenset({
    "module/automation",
    "module/game/cloud.py",
    "module/game/base.py",
    "module/ocr",
    "module/screen",
    "module/workflow",
    "module/localization",
    "module/notification",
    "module/telemetry",
    "module/logger",
    "module/config/config.py",
    # ``module/update/`` is intentionally absent — that is the sync engine
    # itself and lives outside the cloud-game payload.
})

_ASSETS_IMAGES_ALLOWED: Final[frozenset[str]] = frozenset({
    "assets/images/share",
    "assets/images/zh_CN",
    "assets/images/autoplot",
})

# README*/requirements* match by prefix, not exact folder name.
_README_PREFIX: Final[str] = "README"
_REQUIREMENTS_PREFIX: Final[str] = "requirements"


def _normalize(path: str) -> str | None:
    """Return a forward-slash, collapsed form of ``path`` or ``None`` if it
    is structurally unsafe and must be rejected outright."""
    if not isinstance(path, str) or not path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        return None
    if "\\0" in path or "\0" in path:
        return None
    if "\\" in path:
        return None
    if path != path.strip():
        return None
    if os.path.isabs(path):
        return None
    # Drive-letter style like ``C:foo`` is not absolute but is meaningless on
    # this repo layout — reject it.
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return None

    collapsed = path.replace("\\", "/")
    while "//" in collapsed:
        collapsed = collapsed.replace("//", "/")
    collapsed = collapsed.strip("/")
    if not collapsed:
        return None

    parts = collapsed.split("/")
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            return None
        # Hidden component (``.github``, ``.git``, ``.venv`` …) never syncs.
        if part.startswith("."):
            return None
    return collapsed


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _is_allowed_module(path: str) -> bool:
    for entry in _MODULE_ALLOWED:
        if path == entry or (not entry.endswith(".py") and _is_under(path, entry)):
            return True
    return False


def _is_allowed_assets(path: str) -> bool:
    if _is_under(path, "assets/models") or _is_under(path, "assets/model"):
        return True
    if _is_under(path, "assets/config"):
        return True
    if any(_is_under(path, entry) for entry in _ASSETS_IMAGES_ALLOWED):
        return True
    return False


def is_managed_path(path: str) -> bool:
    """Return ``True`` iff ``path`` (a forward-slash repo-relative path) is
    permitted to participate in the macOS CN cloud-game upstream sync."""
    normalized = _normalize(path)
    if normalized is None:
        return False

    # Platform denylist — file extensions.
    lowered = normalized.lower()
    for suffix in _WINDOWS_SUFFIXES:
        if lowered.endswith(suffix):
            return False

    top = normalized.split("/", 1)[0]

    # README* / requirements* at any depth — they are docs, not payload.
    last = normalized.rsplit("/", 1)[-1]
    if last.startswith(_README_PREFIX) or last.startswith(_REQUIREMENTS_PREFIX):
        return False

    # Root-level file denylist.
    if top in _ROOT_FILE_DENY:
        return False
    if top in _TOP_LEVEL_DENY:
        return False

    # ``module/update/`` never syncs (it is the engine itself).
    if _is_under(normalized, "module/update"):
        return False

    # Allow-list evaluation.
    if normalized == "main.py":
        return True

    if top == "module":
        return _is_allowed_module(normalized)

    if top == "tasks":
        if normalized in _TASKS_DENY:
            return False
        return True  # entire ``tasks/`` tree is otherwise accepted

    if top == "utils":
        if normalized in _UTILS_DENY_FILES:
            return False
        for denied_dir in _UTILS_DENY_DIRS:
            if _is_under(normalized, denied_dir):
                return False
        return True

    if top == "assets":
        if normalized in _ASSETS_CONFIG_DENY:
            return False
        return _is_allowed_assets(normalized)

    return False
