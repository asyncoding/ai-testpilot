"""ffmpeg 录屏抽帧命令拼装纯函数（docs/08 FR-EX-005 AC2，S3.3）。

UI 失败录屏（xcresult 附件 .mp4）需要抽帧做人肉/视觉取证，docs/03 §3.5 给的
原型是 ``ffmpeg -i <video>.mp4 -vf fps=2 frame_%03d.png``。本模块零 IO：只拼
argv 并做参数白名单校验，真实 ffmpeg 仅手动探针调用，不进自动门禁。

白名单纪律：除输入/输出路径与 fps 外一切参数固定（``-y`` 幂等覆盖、固定
``fps=`` 滤镜、固定 ``frame_%03d.png`` 命名），调用方无法借参数拼入额外滤镜
或输出目标。
"""

from __future__ import annotations

import posixpath

# fps 合法区间：1 帧/秒起步（再低取证密度不够），30 封顶（防手滑把长录屏
# 抽成海量帧撑爆制品根）；默认 2 与 docs/03 原型一致
MIN_FPS = 1
MAX_FPS = 30
DEFAULT_FPS = 2
FRAME_PATTERN = "frame_%03d.png"


def _require_path(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 为空")
    return value.strip()


def build_frame_extract_argv(
    *,
    video_path: str,
    frames_dir: str,
    fps: int = DEFAULT_FPS,
) -> list[str]:
    """拼 ``ffmpeg -y -i <video> -vf fps=<n> <dir>/frame_%03d.png`` argv。"""
    video_path = _require_path(video_path, "video_path")
    frames_dir = _require_path(frames_dir, "frames_dir")
    # bool 是 int 子类，显式拒绝；fps 只接受白名单区间内整数
    if isinstance(fps, bool) or not isinstance(fps, int):
        raise ValueError(f"fps 必须为整数，实际: {fps!r}")
    if not MIN_FPS <= fps <= MAX_FPS:
        raise ValueError(f"fps 越界（{MIN_FPS}..{MAX_FPS}）: {fps}")
    output_pattern = posixpath.join(frames_dir, FRAME_PATTERN)
    return [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"fps={fps}",
        output_pattern,
    ]
