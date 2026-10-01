"""TVPaint 없이 로직을 검증하는 테스트. 가짜 백엔드는 TVPaint 애니메이션 레이어처럼
'인스턴스를 지우면 뒤 프레임이 당겨지는' 구조를 흉내 낸다."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tvp_autotools.ops import CropSpec, LayerRef, ToolError, run_compact, run_crop  # noqa: E402

W, H = 64, 48


def blank():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def drawing(x, y, color=(255, 0, 0, 255), size=4):
    im = blank()
    for i in range(size):
        for j in range(size):
            im.putpixel((x + i, y + j), color)
    return im


class FakeTVP:
    """TVPaint 레이어 흉내: frames 는 (이미지, 콤마 길이) 목록, 내부적으로 프레임마다 이미지를 펼쳐 둔다."""

    def __init__(self, frames, start=1, name="Fire_Effect_01"):
        self.per_frame = [img for img, length in frames for _ in range(length)]
        self.start = start
        self.ref = LayerRef(7, name, True, False, "Clip 1", "scene")
        self.undo_open = 0
        self.undo_closed = 0
        self.fail_replace = False
        self.built: CropSpec | None = None
        self.snapshot = None

    def current_layer(self):
        return self.ref

    def layer_range(self, layer):
        return self.start, self.start + len(self.per_frame) - 1

    def render_frames(self, layer, frames, out_dir):
        result = {}
        for i, img in enumerate(self.per_frame):
            f = self.start + i
            if f in frames:
                p = out_dir / f"f.{f:05d}.png"
                img.save(p)
                result[f] = p
        return result

    @contextlib.contextmanager
    def undo_group(self, name):
        self.undo_open += 1
        self.snapshot = list(self.per_frame)
        try:
            yield
        finally:
            self.undo_closed += 1

    def undo(self):
        self.per_frame = self.snapshot

    def replace_layer_frames(self, layer, images, work):
        if self.fail_replace:
            raise RuntimeError("simulated failure")
        self.per_frame = [Image.open(p).copy() for p in images]
        return len(self.per_frame)

    def build_cropped_project(self, spec, work):
        self.built = spec
        self.built_images = [Image.open(p).copy() for p in spec.images]
        return "scene_Fire_Effect_01_crop"


def noop(msg, value):
    assert 0.0 <= value <= 1.0


A = drawing(5, 5)
A_copy = drawing(5, 5)  # 따로 복사한 같은 그림
B = drawing(20, 10, (0, 255, 0, 255))
C = drawing(40, 30, (0, 0, 255, 255))


def same(im1, im2):
    return im1.tobytes() == im2.tobytes()


def result_is(tvp, expected):
    assert len(tvp.per_frame) == len(expected)
    assert all(same(a, b) for a, b in zip(tvp.per_frame, expected))


def test_spec_example_A_A_blank_B_blank_C():
    # A A - - B B B - C C  (첫 A는 콤마 2, 두 번째 A는 복사본)
    tvp = FakeTVP([(A, 2), (A_copy, 1), (blank(), 2), (B, 3), (blank(), 1), (C, 2)])
    msg = run_compact(tvp, 7, noop)
    result_is(tvp, [A, B, C])
    assert tvp.undo_open == tvp.undo_closed == 1
    assert "3장" in msg and "빈 프레임 3개" in msg


def test_empty_frames_are_removed():
    tvp = FakeTVP([(blank(), 1), (A, 1), (blank(), 4), (B, 1), (blank(), 2)])
    run_compact(tvp, 7, noop)
    result_is(tvp, [A, B])


def test_consecutive_copies_removed_not_cleared():
    tvp = FakeTVP([(A, 1), (A_copy, 1), (A_copy, 1), (B, 1)])
    run_compact(tvp, 7, noop)
    result_is(tvp, [A, B])  # 프레임 자체가 사라져야 하고 빈 칸이 남으면 안 된다


def test_undo_restores_everything():
    tvp = FakeTVP([(A, 2), (blank(), 2), (B, 3)])
    run_compact(tvp, 7, noop)
    tvp.undo()
    assert len(tvp.per_frame) == 7


def test_white_background_is_not_empty():
    white = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    tvp = FakeTVP([(A, 1), (white, 2), (B, 1)])
    run_compact(tvp, 7, noop)
    result_is(tvp, [A, white, B])


def test_A_B_A_keeps_all():
    tvp = FakeTVP([(A, 2), (B, 2), (A_copy, 2)])
    run_compact(tvp, 7, noop)
    result_is(tvp, [A, B, A])


def test_A_blank_A_merges():
    tvp = FakeTVP([(A, 1), (blank(), 1), (A_copy, 1), (B, 1)])
    run_compact(tvp, 7, noop)
    result_is(tvp, [A, B])


def test_invisible_pixel_rgb_ignored():
    ghost = drawing(5, 5)
    ghost.putpixel((50, 40), (123, 45, 67, 0))
    tvp = FakeTVP([(A, 1), (ghost, 1)])
    run_compact(tvp, 7, noop)
    assert len(tvp.per_frame) == 1


def test_one_pixel_difference_is_different_drawing():
    near = drawing(5, 5)
    near.putpixel((5, 5), (254, 0, 0, 255))
    tvp = FakeTVP([(A, 1), (near, 1)])
    run_compact(tvp, 7, noop)
    assert len(tvp.per_frame) == 2


def test_all_empty_refuses():
    tvp = FakeTVP([(blank(), 3), (blank(), 1)])
    with pytest.raises(ToolError):
        run_compact(tvp, 7, noop)
    assert len(tvp.per_frame) == 4
    assert tvp.undo_open == 0


def test_already_clean_is_noop():
    tvp = FakeTVP([(A, 1), (B, 1)])
    msg = run_compact(tvp, 7, noop)
    assert "정리할 프레임이 없습니다" in msg
    assert tvp.undo_open == 0


def test_layer_changed_after_confirm():
    tvp = FakeTVP([(A, 1)])
    with pytest.raises(ToolError, match="바뀌었습니다"):
        run_compact(tvp, 999, noop)


def test_locked_layer_refused():
    tvp = FakeTVP([(A, 2)])
    tvp.ref = LayerRef(7, "L", True, True, "c", "p")
    with pytest.raises(ToolError, match="잠겨"):
        run_compact(tvp, 7, noop)


def test_undo_stack_closed_on_failure():
    tvp = FakeTVP([(A, 1), (blank(), 1), (B, 1)])
    tvp.fail_replace = True
    with pytest.raises(RuntimeError):
        run_compact(tvp, 7, noop)
    assert tvp.undo_open == tvp.undo_closed == 1


def test_layer_not_starting_at_1():
    tvp = FakeTVP([(A, 2), (blank(), 1), (B, 2)], start=25)
    run_compact(tvp, 7, noop)
    result_is(tvp, [A, B])


def test_crop_union_bbox_and_structure():
    tvp = FakeTVP([(A, 2), (blank(), 1), (B, 3), (C, 1)], start=10)
    msg = run_crop(tvp, 7, noop)
    spec = tvp.built
    assert spec.offset == (5, 5)
    assert (spec.width, spec.height) == (39, 29)
    assert [(i.start, i.length) for i in spec.instances] == [(10, 2), (12, 1), (13, 3), (16, 1)]
    assert all(im.size == (39, 29) for im in tvp.built_images)
    assert tvp.built_images[0].getpixel((0, 0)) == (255, 0, 0, 255)
    assert tvp.built_images[3].getpixel((38, 28)) == (0, 0, 255, 255)
    assert tvp.built_images[1].getchannel("A").getbbox() is None  # 빈 프레임 구간 유지
    assert len(tvp.per_frame) == 7  # 원본 불변
    assert "39×29" in msg


def test_crop_keeps_empty_gaps_instead_of_holding_previous_drawing():
    # 빈 프레임이 앞 그림의 콤마로 흡수되면 안 된다
    tvp = FakeTVP([(A, 1), (blank(), 3), (B, 1)])
    run_crop(tvp, 7, noop)
    assert [i.length for i in tvp.built.instances] == [1, 3, 1]


def test_crop_all_empty_refuses():
    tvp = FakeTVP([(blank(), 2)])
    with pytest.raises(ToolError):
        run_crop(tvp, 7, noop)


def test_non_anim_layer_refused():
    tvp = FakeTVP([(A, 1)])
    tvp.ref = LayerRef(7, "BG", False, False, "c", "p")
    with pytest.raises(ToolError, match="애니메이션 레이어가 아닙니다"):
        run_crop(tvp, 7, noop)


def test_worker_delivers_results_to_matching_callbacks():
    import time

    from tvp_autotools.worker import Worker

    w = Worker()
    got = []
    w.submit(lambda: "poll", lambda v: got.append(("poll", v)), lambda e, t: got.append(("poll_err", e)))
    w.submit(lambda: "run", lambda v: got.append(("run", v)), lambda e, t: got.append(("run_err", e)))
    time.sleep(0.3)
    while not w.results.empty():
        w.results.get_nowait()()
    assert got == [("poll", "poll"), ("run", "run")]
