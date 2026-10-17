"""Render QR codes into terminal-friendly text.

This module wraps :mod:`qrcode` to build a QR matrix from arbitrary text and
turn it into a string suitable for direct ``print``/``stdout.write`` output.
The renderer:

- draws black modules on top of an *explicit* white ANSI background, so the
  result is legible regardless of the user's terminal theme;
- always reserves a quiet zone (default: 4 modules per side, the ISO/IEC 18004
  minimum) to keep scanners happy;
- packs two module rows per terminal line by using the Unicode half-block
  characters ``▀``/``▄`` (and ``█`` when both halves are black);
- falls back to plain ASCII (``#`` / space) when the destination stream is not
  a TTY, or when the requested QR is wider than the configured maximum;
- never logs the source text.

The CLI integration lives in :mod:`module.game.cloud`; this module only knows
about the QR encoding and the visual rendering.

Raises
------
TerminalTooNarrowError
    When the renderer is asked for an ANSI output (TTY) but the QR
    matrix would not fit in the configured ``max_width``. The caller
    is expected to surface a friendly "widen your terminal" message;
    the renderer deliberately refuses to emit a wrapped / unreadable
    QR in that case.
"""
from __future__ import annotations

import io
import shutil
import sys
from typing import IO, Any, List, Optional, Sequence, Tuple

import qrcode
from qrcode.constants import (
    ERROR_CORRECT_H,
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
)


# --- ANSI SGR fragments ---------------------------------------------------
# Explicitly set both background (white) and foreground (black) so the
# rendering looks the same no matter what the terminal's default theme is.
_RESET = "\x1b[0m"
_BG_WHITE = "\x1b[47m"
_FG_BLACK = "\x1b[30m"
_FG_DEFAULT = "\x1b[39m"  # paired with _BG_WHITE for the "white module" case


# --- QR geometry ---------------------------------------------------------
# ISO/IEC 18004 mandates a 4-module quiet zone. Lowering it is allowed for
# aesthetic reasons but breaks some scanners.
QUIET_ZONE_MODULES = 4
DEFAULT_MIN_WIDTH = 40  # never request a smaller terminal than this


# --- Half-block characters -----------------------------------------------
_UPPER_BLOCK = "▀"   # top half coloured, bottom half = background
_LOWER_BLOCK = "▄"   # bottom half coloured, top half = background
_FULL_BLOCK = "█"    # both halves coloured
_WHITE_BLOCK = " "   # explicit white background module


class TerminalTooNarrowError(RuntimeError):
    """Raised when the QR matrix is wider than the requested terminal width.

    ASCII fallback for ``force_ascii=True`` and non-TTY streams is
    still permitted (those destinations are not column-limited), but
    ANSI fallback would produce a wrapped / broken QR. Callers should
    catch this and surface a friendly "widen your terminal" message
    instead.
    """


# --- Error correction map (string → qrcode constant) ----------------------
_ERROR_LEVELS = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}


def _make_matrix(data: str, error_correction: int = ERROR_CORRECT_L) -> List[List[bool]]:
    """Build the QR module matrix for ``data`` (without quiet zone)."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=error_correction,
        box_size=1,
        border=0,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return [[bool(cell) for cell in row] for row in qr.modules]


def _matrix_size(matrix: Sequence[Sequence[bool]]) -> int:
    return len(matrix)


def _module_at(matrix: Sequence[Sequence[bool]], quiet: int, r: int, c: int) -> bool:
    """Return the module value at absolute ``(r, c)`` (including the quiet zone).

    Positions outside the QR matrix are treated as white (the quiet zone).
    """
    n = len(matrix)
    if r < quiet or r >= quiet + n:
        return False
    if c < quiet or c >= quiet + n:
        return False
    return bool(matrix[r - quiet][c - quiet])


def _terminal_width(fallback: int = 80) -> int:
    """Return the destination terminal's column count, with a sane fallback."""
    try:
        cols = shutil.get_terminal_size(fallback=(fallback, 24)).columns
    except (OSError, ValueError):
        cols = fallback
    return max(1, int(cols))


def _is_tty(stream: Optional[IO[str]]) -> bool:
    """Return whether ``stream`` (defaults to stdout) is a real TTY."""
    if stream is None:
        stream = sys.stdout
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):
        return False


def render_qr_terminal(
    data: str,
    *,
    stream: Optional[IO[str]] = None,
    max_width: Optional[int] = None,
    min_width: int = DEFAULT_MIN_WIDTH,
    quiet_zone: int = QUIET_ZONE_MODULES,
    error_correction: int = ERROR_CORRECT_L,
    is_tty: Optional[bool] = None,
    force_ascii: bool = False,
) -> Tuple[str, int]:
    """Render a QR code for ``data`` as a terminal-friendly string.

    Parameters
    ----------
    data:
        Text to encode (e.g., the decoded cloud-login URL).
    stream:
        Stream used to detect TTY status. ``None`` defaults to ``sys.stdout``.
    max_width:
        Maximum terminal cells the QR may occupy (quiet zone included).
        ``None`` auto-detects via :func:`shutil.get_terminal_size`.
    min_width:
        Lower bound for the detected ``max_width``; never returns less than
        this many columns when auto-detecting.
    quiet_zone:
        Modules of white padding on each side.
    error_correction:
        ``qrcode`` error-correction constant. ``L`` keeps the matrix small.
    is_tty:
        Force the TTY decision. ``None`` autodetects from ``stream``.
    force_ascii:
        When ``True`` the renderer always produces plain ``#``/space output,
        ignoring TTY detection and width checks. Useful for log files and
        for unit tests.

    Returns
    -------
    (text, modules_per_side)
        The fully-rendered multi-line string and the QR matrix size, so
        callers can either ``print()`` it directly or assert sizes in tests.
    """
    if not isinstance(data, str) or not data:
        raise ValueError("data must be a non-empty string")
    if quiet_zone < 0:
        raise ValueError("quiet_zone must be non-negative")

    matrix = _make_matrix(data, error_correction=error_correction)
    n = _matrix_size(matrix)
    total_cells = n + 2 * quiet_zone

    if max_width is None:
        max_width = max(min_width, _terminal_width())
    max_width = max(1, int(max_width))

    width_ok = total_cells <= max_width
    detect_tty = not force_ascii
    if detect_tty and is_tty is None:
        is_tty = _is_tty(stream)

    if force_ascii:
        # Explicit ASCII request from the caller; column count is
        # irrelevant because the destination is either a file or
        # explicitly opt-ed-in to non-ANSI output.
        text = _render_ascii(matrix, quiet_zone)
    elif not is_tty:
        # Piped / file output: ASCII is the only safe choice; the
        # consumer decides how to wrap or display it.
        text = _render_ascii(matrix, quiet_zone)
    elif not width_ok:
        # TTY but too narrow: producing ANSI would wrap the QR into
        # unreadable strips. Refuse loudly so the caller can nudge the
        # user to widen the window instead.
        needed = total_cells
        raise TerminalTooNarrowError(
            f"QR width {needed} cells exceeds max_width {max_width}; "
            "widen the terminal before retrying."
        )
    else:
        text = _render_ansi(matrix, quiet_zone)

    return text, n


def _render_ansi(matrix: Sequence[Sequence[bool]], quiet: int) -> str:
    """Render the matrix using half-block unicode on an explicit white BG."""
    n = len(matrix)
    full = n + 2 * quiet
    out_lines: List[str] = []
    for r0 in range(0, full, 2):
        r1 = r0 + 1
        parts: List[str] = [_BG_WHITE]  # whole line sits on white
        cur_fg = ""  # "" = default FG (used with _BG_WHITE for white modules)
        for c in range(full):
            top = _module_at(matrix, quiet, r0, c)
            bot = _module_at(matrix, quiet, r1, c)
            if top and bot:
                if cur_fg != "B":
                    parts.append(_FG_BLACK)
                    cur_fg = "B"
                parts.append(_FULL_BLOCK)
            elif top and not bot:
                if cur_fg != "B":
                    parts.append(_FG_BLACK)
                    cur_fg = "B"
                parts.append(_UPPER_BLOCK)
            elif not top and bot:
                if cur_fg != "B":
                    parts.append(_FG_BLACK)
                    cur_fg = "B"
                parts.append(_LOWER_BLOCK)
            else:
                if cur_fg != "":
                    parts.append(_FG_DEFAULT)
                    cur_fg = ""
                parts.append(_WHITE_BLOCK)
        parts.append(_RESET)
        out_lines.append("".join(parts))
    return "\n".join(out_lines)


def _render_ascii(matrix: Sequence[Sequence[bool]], quiet: int) -> str:
    """Plain ASCII rendering (used for non-TTY output and over-width fallback)."""
    n = len(matrix)
    full = n + 2 * quiet
    lines: List[str] = []
    for r in range(full):
        row = []
        for c in range(full):
            mod = _module_at(matrix, quiet, r, c)
            row.append("#" if mod else " ")
        lines.append("".join(row))
    return "\n".join(lines)


def write_qr_terminal(
    data: str,
    *,
    stream: Optional[IO[str]] = None,
    max_width: Optional[int] = None,
    quiet_zone: int = QUIET_ZONE_MODULES,
    error_correction: int = ERROR_CORRECT_L,
    is_tty: Optional[bool] = None,
    force_ascii: bool = False,
    prefix: str = "",
    suffix: str = "",
) -> Tuple[int, int]:
    """Render ``data`` and write it directly to ``stream``.

    Returns ``(modules_per_side, total_modules)``.
    """
    if stream is None:
        stream = sys.stdout
    text, n = render_qr_terminal(
        data,
        stream=stream,
        max_width=max_width,
        quiet_zone=quiet_zone,
        error_correction=error_correction,
        is_tty=is_tty,
        force_ascii=force_ascii,
    )
    if prefix:
        stream.write(prefix)
    stream.write(text)
    if suffix:
        stream.write(suffix)
    stream.flush()
    return n, n * n


def decode_ansi_render(
    rendered: str,
    *,
    expected_modules: Optional[int] = None,
    quiet_zone: int = QUIET_ZONE_MODULES,
    width_scale: int = 1,
) -> List[List[bool]]:
    """Recover the module matrix from a half-block ANSI rendering.

    Useful for round-tripping tests: render a QR with :func:`render_qr_terminal`,
    then run this parser to recover the matrix and compare against
    :func:`_make_matrix`.

    The renderer packs two module rows per terminal line and uses one
    character cell per QR module (so ``width_scale`` is normally ``1``).
    ANSI escape sequences are stripped before the modules are read; the
    background is verified to be the explicit white ``\\x1b[47m`` escape.
    """
    if quiet_zone < 0:
        raise ValueError("quiet_zone must be non-negative")
    if width_scale < 1:
        raise ValueError("width_scale must be >= 1")

    lines = rendered.splitlines()
    # Strip the trailing reset, the leading white BG, and any per-cell FG escapes.
    cleaned: List[str] = []
    for raw in lines:
        # Remove all SGR fragments so we are left with the visible glyphs only.
        line = raw
        for esc in (_RESET, _BG_WHITE, _FG_BLACK, _FG_DEFAULT):
            line = line.replace(esc, "")
        cleaned.append(line)

    # Each terminal row encodes two QR rows (top/bottom halves).
    qr_rows = len(cleaned) * 2
    qr_cols = max(len(c) for c in cleaned) if cleaned else 0

    if expected_modules is not None:
        # quiet zone on each side + QR body
        body_modules = expected_modules
    else:
        body_modules = qr_cols - 2 * quiet_zone

    if body_modules <= 0:
        raise ValueError("rendered output too small to contain a QR body")

    out = [[False] * body_modules for _ in range(body_modules)]

    for term_r, line in enumerate(cleaned):
        top_qr_row = (term_r * 2) - quiet_zone
        bot_qr_row = top_qr_row + 1
        for term_c, ch in enumerate(line):
            qr_c = term_c - quiet_zone
            if qr_c < 0 or qr_c >= body_modules:
                continue
            if ch == _UPPER_BLOCK:
                top_mod = True
                bot_mod = False
            elif ch == _LOWER_BLOCK:
                top_mod = False
                bot_mod = True
            elif ch == _FULL_BLOCK:
                top_mod = True
                bot_mod = True
            elif ch == _WHITE_BLOCK:
                top_mod = False
                bot_mod = False
            else:
                # Unknown glyph; treat the whole cell as white so a regression
                # never silently produces a black module.
                top_mod = False
                bot_mod = False

            if 0 <= top_qr_row < body_modules:
                out[top_qr_row][qr_c] = out[top_qr_row][qr_c] or top_mod
            if 0 <= bot_qr_row < body_modules:
                out[bot_qr_row][qr_c] = out[bot_qr_row][qr_c] or bot_mod

    return out


def decode_ascii_render(
    rendered: str,
    *,
    quiet_zone: int = QUIET_ZONE_MODULES,
) -> List[List[bool]]:
    """Recover the module matrix from the ASCII (``#``/space) fallback output."""
    lines = rendered.splitlines()
    if not lines:
        raise ValueError("rendered ASCII output is empty")
    n_cols = max(len(line) for line in lines)
    # Sanitise ragged lines (should not happen, but be defensive).
    norm = [line.ljust(n_cols, " ") for line in lines]
    body_modules = n_cols - 2 * quiet_zone
    if body_modules <= 0:
        raise ValueError("rendered ASCII output too small to contain a QR body")
    out = [[False] * body_modules for _ in range(body_modules)]
    for r, line in enumerate(norm):
        qr_r = r - quiet_zone
        if qr_r < 0 or qr_r >= body_modules:
            continue
        for c, ch in enumerate(line):
            qr_c = c - quiet_zone
            if qr_c < 0 or qr_c >= body_modules:
                continue
            if ch == "#":
                out[qr_r][qr_c] = True
    return out


def has_explicit_white_background(rendered: str) -> bool:
    """Return ``True`` if ``rendered`` contains the explicit white BG escape.

    Tests use this to assert the renderer honours the "explicit white BG"
    requirement rather than relying on the terminal's default.
    """
    return _BG_WHITE in rendered and _RESET in rendered


def matrix_to_image_bytes(
    matrix: Sequence[Sequence[bool]],
    *,
    scale: int = 8,
    quiet_zone: int = 4,
) -> bytes:
    """Render ``matrix`` as a PNG byte string.

    The output includes a white quiet zone (``quiet_zone`` pixels on every
    side) so OpenCV's :class:`QRCodeDetector` can lock onto the pattern
    even when ``matrix`` was produced by :func:`decode_ansi_render`.

    Used by the round-trip test that decodes a re-rendered QR through a
    real QR detector to confirm the half-block output really is a valid
    QR code.
    """
    import numpy as np  # local import: keeps utils.terminal_qr import-cheap
    if not matrix:
        raise ValueError("matrix must not be empty")
    rows, cols = len(matrix), len(matrix[0])
    arr = np.full((rows, cols), 255, dtype=np.uint8)
    for r, row in enumerate(matrix):
        for c, mod in enumerate(row):
            if mod:
                arr[r, c] = 0
    if quiet_zone > 0:
        arr = np.pad(
            arr,
            quiet_zone,
            mode="constant",
            constant_values=255,
        )
    if scale and scale != 1:
        arr = np.kron(arr, np.ones((scale, scale), dtype=np.uint8))
    try:
        import cv2  # noqa: F401 - availability check
        ok, buf = cv2.imencode(".png", arr)
        if not ok:
            raise RuntimeError("cv2.imencode failed")
        return bytes(buf)
    except ImportError:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(
                "Neither cv2 nor Pillow is available to render the test QR image"
            ) from exc
        img = Image.fromarray(arr, mode="L")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
