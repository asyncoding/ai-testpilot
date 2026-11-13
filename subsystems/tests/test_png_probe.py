"""png_probe 纯函数 hostless 测试（docs/15 §4，H3）。

红证纪律：合法样本用 struct 现造最小 PNG 字节（签名+IHDR），再逐字节破坏，
确认非 PNG/截断/IHDR 形态异常全部 fail-loud，而不是默默给出方向结论。
"""

from __future__ import annotations

import struct

import pytest

from testpilot_core.png_probe import (
    PngProbeError,
    is_portrait_png,
    png_dimensions,
)

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png(width: int, height: int, *, ihdr_len: int = 13,
         chunk_type: bytes = b"IHDR") -> bytes:
    return (
        _PNG_SIG
        + struct.pack(">I4s", ihdr_len, chunk_type)
        + struct.pack(">II", width, height)
        + b"\x00" * 5  # bit depth/color/compression/filter/interlace
    )


def test_dimensions_roundtrip():
    assert png_dimensions(_png(1206, 2622)) == (1206, 2622)
    assert png_dimensions(_png(2622, 1206)) == (2622, 1206)


def test_portrait_judged_by_pixels_not_press_count():
    # E27：方向只认截图本身；竖屏高>宽，横屏宽>高，正方形按非竖屏 fail-closed
    assert is_portrait_png(_png(1206, 2622)) is True
    assert is_portrait_png(_png(2622, 1206)) is False
    assert is_portrait_png(_png(1000, 1000)) is False


def test_missing_signature_is_loud():
    raw = _png(10, 20)
    with pytest.raises(PngProbeError, match="PNG 签名"):
        png_dimensions(b"not-a-png" + raw[8:])


def test_truncated_stream_is_loud():
    with pytest.raises(PngProbeError, match="短于"):
        png_dimensions(_png(10, 20)[:12])


def test_wrong_first_chunk_or_ihdr_length_is_loud():
    with pytest.raises(PngProbeError, match="IHDR"):
        png_dimensions(_png(10, 20, chunk_type=b"JDHR"))
    with pytest.raises(PngProbeError, match="IHDR"):
        png_dimensions(_png(10, 20, ihdr_len=7))


def test_zero_dimensions_rejected():
    with pytest.raises(PngProbeError, match="宽高非法"):
        png_dimensions(_png(0, 20))
