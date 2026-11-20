"""PNG 尺寸纯函数探针（docs/01 §4 竖屏前置，H3）。

零 IO、零三方：只解析 PNG 首块 IHDR 的宽高（字节 16–23，大端 uint32）。
devicectl 截图产物是 PNG；竖屏判定 ``height > width``，方向证据来自图片
本身而非按压次数（docs/archive/04（EVD-20261113-02） E27：iPhone 不渲染 portraitUpsideDown）。
"""

from __future__ import annotations

import struct

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngProbeError(ValueError):
    """字节流不是合法 PNG 或 IHDR 形态异常。"""


def png_dimensions(data: bytes) -> tuple[int, int]:
    """返回 (width, height)；非 PNG/截断/IHDR 长度异常一律 fail-loud。"""
    if len(data) < 24:
        raise PngProbeError("字节流短于 PNG IHDR 最小长度 24")
    if data[:8] != _PNG_SIGNATURE:
        raise PngProbeError("缺少 PNG 签名，不是 devicectl PNG 产物")
    # 8 字节签名 + 4 字节长度 + 4 字节类型；IHDR 长度必须为 13
    length, chunk_type = struct.unpack(">I4s", data[8:16])
    if chunk_type != b"IHDR" or length != 13:
        raise PngProbeError(
            f"首块不是 IHDR 或长度非 13（type={chunk_type!r}, len={length}）")
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise PngProbeError(f"IHDR 宽高非法：{width}x{height}")
    return width, height


def is_portrait_png(data: bytes) -> bool:
    """竖屏：高大于宽；正方形按非竖屏处理（方向不明，调用方应 fail-loud）。"""
    width, height = png_dimensions(data)
    return height > width
