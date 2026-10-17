"""Tests for :mod:`utils.terminal_qr`.

Each test exercises a single guarantee from the renderer:

* the half-block ANSI output round-trips back to the original module matrix,
* the ASCII fallback (non-TTY / over-width / forced) also round-trips,
* the recovered matrix actually decodes as a real QR (via OpenCV), and
* the various width / failure / contract cases are honoured.
"""
from __future__ import annotations

import io
import os
import shutil

import numpy as np
import pytest

from utils.terminal_qr import (
    DEFAULT_MIN_WIDTH,
    QUIET_ZONE_MODULES,
    TerminalTooNarrowError,
    _BG_WHITE,
    _FG_BLACK,
    _FG_DEFAULT,
    _FULL_BLOCK,
    _LOWER_BLOCK,
    _RESET,
    _UPPER_BLOCK,
    _make_matrix,
    decode_ansi_render,
    decode_ascii_render,
    has_explicit_white_background,
    matrix_to_image_bytes,
    render_qr_terminal,
    write_qr_terminal,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _StringIOTTY(io.StringIO):
    """A StringIO that reports itself as a TTY or not based on ``tty``."""

    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # type: ignore[override]
        return self._tty


# A handful of realistic payloads (long enough to force a non-trivial QR, but
# short enough that ERROR_CORRECT_L yields a manageable matrix).
_LOGIN_URL = (
    "https://user.miyoushe.com/qr-login?token=ABCDEFGHIJ1234567890"
    "&scene=assistant&t=1700000000"
)


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

class TestRenderRoundTripAnsi:
    """Render to ANSI half-block, then recover the module matrix."""

    def test_rendered_matrix_matches_qrcode_matrix(self):
        text, n = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        expected = _make_matrix(_LOGIN_URL)
        recovered = decode_ansi_render(text, expected_modules=n)
        assert len(recovered) == n
        assert recovered == expected

    def test_every_line_carries_explicit_white_background(self):
        text, _ = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        assert has_explicit_white_background(text)
        # Each line must set the white BG, then reset at the end.
        for line in text.splitlines():
            assert line.startswith(_BG_WHITE), line[:10]
            assert line.endswith(_RESET), line[-10:]
        # The default FG must appear at least once for the white modules.
        assert _FG_DEFAULT in text
        # Black FG must appear at least once for the black modules.
        assert _FG_BLACK in text

    def test_quiet_zone_is_visible_in_ansi_output(self):
        # The first and last few characters of every row must be the quiet
        # zone: explicit white BG + space (no FG black on those columns).
        text, _ = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        for line in text.splitlines():
            # Strip ANSI and look at the visible characters.
            visible = line
            for esc in (_BG_WHITE, _FG_BLACK, _FG_DEFAULT, _RESET):
                visible = visible.replace(esc, "")
            assert visible[:QUIET_ZONE_MODULES] == " " * QUIET_ZONE_MODULES
            assert visible[-QUIET_ZONE_MODULES:] == " " * QUIET_ZONE_MODULES

    def test_two_modules_per_terminal_line(self):
        """Half-block packing: each terminal row covers two QR rows."""
        text, n = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        # n + 2 * quiet_zone = total; round up to the nearest even (terminal row).
        total = n + 2 * QUIET_ZONE_MODULES
        expected_lines = (total + 1) // 2  # ceil(total / 2)
        assert text.count("\n") + 1 == expected_lines

    def test_block_glyph_distribution_matches_matrix(self):
        """Each half-block glyph should reflect the two underlying modules."""
        text, n = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        expected = _make_matrix(_LOGIN_URL)
        # The recovered matrix must agree with the source on every cell.
        recovered = decode_ansi_render(text, expected_modules=n)
        for r in range(n):
            for c in range(n):
                assert recovered[r][c] == expected[r][c]


class TestRenderRoundTripAscii:
    """Render to ASCII (#/space) and recover the matrix."""

    def test_ascii_round_trip(self):
        text, n = render_qr_terminal(_LOGIN_URL, force_ascii=True)
        recovered = decode_ascii_render(text)
        assert recovered == _make_matrix(_LOGIN_URL)

    def test_ascii_has_no_ansi(self):
        text, _ = render_qr_terminal(_LOGIN_URL, force_ascii=True)
        assert "\x1b" not in text

    def test_ascii_has_quiet_zone_padding(self):
        text, n = render_qr_terminal(_LOGIN_URL, force_ascii=True)
        for line in text.splitlines():
            assert line.startswith(" " * QUIET_ZONE_MODULES)
            assert line.endswith(" " * QUIET_ZONE_MODULES)

    def test_ascii_width_matches_modules_plus_quiet_zone(self):
        text, n = render_qr_terminal(_LOGIN_URL, force_ascii=True)
        expected_width = n + 2 * QUIET_ZONE_MODULES
        for line in text.splitlines():
            assert len(line) == expected_width


# ---------------------------------------------------------------------------
# End-to-end: actually decode the rendered QR
# ---------------------------------------------------------------------------

class TestRenderedMatrixIsARealQr:
    """The rendered half-block output should decode back to the source text."""

    @pytest.fixture
    def cv2(self):
        cv2 = pytest.importorskip("cv2")
        return cv2

    def test_recovered_matrix_decodes_via_opencv(self, cv2):
        text, n = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        matrix = decode_ansi_render(text, expected_modules=n)
        png = matrix_to_image_bytes(matrix, scale=10)
        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        assert img is not None and img.size > 0, "rendered QR produced empty image"
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        assert data == _LOGIN_URL

    def test_ascii_matrix_decodes_via_opencv(self, cv2):
        text, n = render_qr_terminal(_LOGIN_URL, force_ascii=True)
        matrix = decode_ascii_render(text)
        png = matrix_to_image_bytes(matrix, scale=10)
        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        assert data == _LOGIN_URL


# ---------------------------------------------------------------------------
# TTY detection / non-TTY fallback
# ---------------------------------------------------------------------------

class TestTtyDetection:
    def test_non_tty_stream_produces_ascii(self):
        stream = _StringIOTTY(tty=False)
        text, n = render_qr_terminal(_LOGIN_URL, stream=stream)
        assert "\x1b" not in text
        # The ASCII matrix should still round-trip.
        assert decode_ascii_render(text) == _make_matrix(_LOGIN_URL)

    def test_is_tty_false_argument_produces_ascii(self):
        text, _ = render_qr_terminal(_LOGIN_URL, is_tty=False)
        assert "\x1b" not in text

    def test_is_tty_true_with_wide_terminal_keeps_ansi(self):
        text, _ = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        assert "\x1b" in text
        assert has_explicit_white_background(text)

    def test_force_ascii_overrides_tty(self):
        # Even when forcing TTY, force_ascii wins.
        text, _ = render_qr_terminal(_LOGIN_URL, is_tty=True, force_ascii=True)
        assert "\x1b" not in text


# ---------------------------------------------------------------------------
# Terminal-width handling
# ---------------------------------------------------------------------------

class TestTerminalWidth:
    def test_max_width_too_small_raises_terminal_too_narrow(self):
        # A TTY stream plus a max_width that cannot fit the QR body must
        # raise — producing a wrapped / unreadable QR is worse than failing
        # loudly so the caller can nudge the user to widen their window.
        with pytest.raises(TerminalTooNarrowError) as exc_info:
            render_qr_terminal(_LOGIN_URL, max_width=5, is_tty=True)
        assert "5" in str(exc_info.value)
        assert "widen" in str(exc_info.value).lower()

    def test_max_width_exactly_fits(self):
        text, n = render_qr_terminal(_LOGIN_URL, max_width=80, is_tty=True)
        needed = n + 2 * QUIET_ZONE_MODULES
        # A max_width at the exact body size must still render ANSI.
        text_exact, _ = render_qr_terminal(_LOGIN_URL, max_width=needed, is_tty=True)
        assert "\x1b" in text_exact
        # One cell too small → refuse with TerminalTooNarrowError.
        with pytest.raises(TerminalTooNarrowError):
            render_qr_terminal(_LOGIN_URL, max_width=needed - 1, is_tty=True)

    def test_force_ascii_overrides_width_check(self):
        # An explicit force_ascii request must NOT raise on narrow width;
        # the caller has opted in to ASCII for whatever sink it uses.
        text, _ = render_qr_terminal(
            _LOGIN_URL, max_width=5, is_tty=True, force_ascii=True
        )
        assert "\x1b" not in text
        assert decode_ascii_render(text) == _make_matrix(_LOGIN_URL)

    def test_non_tty_ignores_width_check(self):
        # Piped / file output: width is the consumer's problem, so the
        # renderer must not raise; it produces ASCII regardless of width.
        text, _ = render_qr_terminal(
            _LOGIN_URL, stream=_StringIOTTY(tty=False), max_width=5
        )
        assert "\x1b" not in text

    def test_min_width_clamp(self, monkeypatch):
        # Force the terminal to claim 1 column; the detector must still be
        # able to honour ``min_width``.
        def fake_terminal_size(fallback=(80, 24)):
            return os.terminal_size((1, 24))

        monkeypatch.setattr(shutil, "get_terminal_size", fake_terminal_size)
        text, _ = render_qr_terminal("hi", is_tty=True)
        # The renderer picked the larger of min_width and the (1-column)
        # detected width, so ANSI is produced.
        assert "\x1b" in text

    def test_min_width_can_be_lower_and_still_refuse(self, monkeypatch):
        # Lowering min_width lets the renderer pick a narrower max_width,
        # but if even that smaller cap can't fit the QR the renderer
        # must still refuse instead of producing a wrapped output.
        def fake_terminal_size(fallback=(80, 24)):
            return os.terminal_size((1, 24))

        monkeypatch.setattr(shutil, "get_terminal_size", fake_terminal_size)
        with pytest.raises(TerminalTooNarrowError):
            render_qr_terminal(_LOGIN_URL, min_width=1, is_tty=True)


# ---------------------------------------------------------------------------
# Failure / edge cases
# ---------------------------------------------------------------------------

class TestFailureModes:
    def test_empty_data_raises(self):
        with pytest.raises(ValueError):
            render_qr_terminal("")

    def test_non_string_data_raises(self):
        with pytest.raises(ValueError):
            render_qr_terminal(123)  # type: ignore[arg-type]

    def test_negative_quiet_zone_raises(self):
        with pytest.raises(ValueError):
            render_qr_terminal(_LOGIN_URL, quiet_zone=-1)

    def test_decode_ansi_render_rejects_negative_quiet_zone(self):
        text, _ = render_qr_terminal("hi", is_tty=True, max_width=80)
        with pytest.raises(ValueError):
            decode_ansi_render(text, quiet_zone=-1)

    def test_decode_ascii_render_rejects_too_small_output(self):
        with pytest.raises(ValueError):
            decode_ascii_render("\n\n")

    def test_render_returns_modules_per_side(self):
        _, n = render_qr_terminal(_LOGIN_URL, force_ascii=True)
        assert n == len(_make_matrix(_LOGIN_URL))


# ---------------------------------------------------------------------------
# write_qr_terminal integration
# ---------------------------------------------------------------------------

class TestWriteQrTerminal:
    def test_writes_to_stream(self):
        buf = _StringIOTTY(tty=True)
        n, total = write_qr_terminal(_LOGIN_URL, stream=buf, max_width=80)
        assert n > 0
        assert total == n * n
        assert buf.getvalue().startswith(_BG_WHITE)
        assert buf.getvalue().rstrip("\n").endswith(_RESET)

    def test_prefix_and_suffix(self):
        buf = _StringIOTTY(tty=True)
        write_qr_terminal(_LOGIN_URL, stream=buf, prefix="PRE", suffix="POST", max_width=80)
        out = buf.getvalue()
        assert out.startswith("PRE" + _BG_WHITE)
        assert out.rstrip("\n").endswith(_RESET + "POST")


# ---------------------------------------------------------------------------
# Sanity checks for the exposed constants and helpers
# ---------------------------------------------------------------------------

class TestContract:
    def test_quiet_zone_default_is_iso_minimum(self):
        assert QUIET_ZONE_MODULES == 4

    def test_default_min_width_is_positive(self):
        assert DEFAULT_MIN_WIDTH > 0

    def test_block_glyphs_are_distinct(self):
        assert len({_UPPER_BLOCK, _LOWER_BLOCK, _FULL_BLOCK}) == 3

    def test_make_matrix_includes_position_detection_patterns(self):
        # The QR matrix must contain the three position-detection patterns
        # (top-left, top-right, bottom-left). Each finder pattern is a
        # 7-module ring: solid black outer border, white middle ring,
        # 3x3 black core. Spot-check the corner cells.
        matrix = _make_matrix(_LOGIN_URL)
        n = len(matrix)
        # Outer ring of the top-left finder pattern is solid black.
        assert all(matrix[r][0] for r in range(7))
        assert all(matrix[0][c] for c in range(7))
        assert all(matrix[r][6] for r in range(7))
        assert all(matrix[6][c] for c in range(7))
        # Inner ring (1 module in) is white.
        assert not any(matrix[r][1] for r in range(1, 6))
        assert not any(matrix[1][c] for c in range(1, 6))
        # Core (3x3) is black.
        assert all(matrix[r][c] for r in range(2, 5) for c in range(2, 5))
