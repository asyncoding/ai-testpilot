"""S3.3 FR-EX-005 AC2 ffmpeg 抽帧 argv 纯函数测试（core 零 IO，白名单守卫）。"""

from __future__ import annotations

import pytest

from testpilot_core.ffmpeg_commands import (
    DEFAULT_FPS,
    FRAME_PATTERN,
    MAX_FPS,
    MIN_FPS,
    build_frame_extract_argv,
)


def test_default_argv_matches_docs_prototype():
    argv = build_frame_extract_argv(
        video_path="store/artifacts/rec.mp4",
        frames_dir="store/frames/run-1",
    )
    assert argv == [
        "ffmpeg", "-y",
        "-i", "store/artifacts/rec.mp4",
        "-vf", f"fps={DEFAULT_FPS}",
        f"store/frames/run-1/{FRAME_PATTERN}",
    ]


def test_custom_fps_within_whitelist():
    argv = build_frame_extract_argv(
        video_path="v.mp4", frames_dir="d", fps=MAX_FPS
    )
    assert argv[-2] == f"fps={MAX_FPS}"
    assert argv[-1] == f"d/{FRAME_PATTERN}"
    assert build_frame_extract_argv(video_path="v.mp4", frames_dir="d",
                                    fps=MIN_FPS)[-2] == f"fps={MIN_FPS}"


@pytest.mark.parametrize("bad_fps", [0, MAX_FPS + 1, -1, 2.0, "2", True, False])
def test_fps_outside_whitelist_or_wrong_type_rejected(bad_fps):
    with pytest.raises(ValueError):
        build_frame_extract_argv(video_path="v.mp4", frames_dir="d",
                                 fps=bad_fps)


@pytest.mark.parametrize(
    "video_path,frames_dir",
    [("", "d"), ("   ", "d"), ("v.mp4", ""), ("v.mp4", "  ")],
)
def test_blank_paths_rejected(video_path, frames_dir):
    with pytest.raises(ValueError):
        build_frame_extract_argv(video_path=video_path, frames_dir=frames_dir)


def test_no_extra_filter_surface_only_whitelisted_tokens():
    argv = build_frame_extract_argv(video_path="v.mp4", frames_dir="d")
    # 除 -vf fps 外不允许出现其他滤镜/编码器入口
    assert argv.count("-vf") == 1
    assert all(not token.startswith("-filter") for token in argv)
