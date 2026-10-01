"""TVPaint 없이 로직을 검증하는 테스트. 가짜 백엔드는 TVPaint 애니메이션 레이어처럼
'인스턴스를 지우면 뒤 프레임이 당겨지는' 구조를 흉내 낸다."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tvp_autotools.core import MODE_CANCEL, MODE_SCALE, choose_atlas_layout, parse_nonneg_int, plan_atlas_output  # noqa: E402
from tvp_autotools.ops import (  # noqa: E402
    CropSpec,
    LayerRef,
    ToolError,
    analyze_atlas,
    analyze_compact,
    analyze_crop,
    apply_compact,
    build_atlas,
    build_crop,
    run_compact,
    run_crop,
)

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

    def build_atlas_project(self, source, atlas_png, layout, work):
        self.atlas = Image.open(atlas_png).copy()
        self.atlas_layout = layout
        return "Atlas_01"

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


# ---------------- Atlas ----------------


def test_layout_square_by_pixels_not_count():
    L = choose_atlas_layout(10, 128, 96)
    assert (L.cols, L.rows, L.width, L.height) == (3, 4, 384, 384)


def test_layout_tall_cells_prefer_wide_grid():
    L = choose_atlas_layout(9, 64, 128)  # 3x3 보다 5x2 가 픽셀상 더 정사각형
    assert (L.cols, L.rows) == (5, 2)


def test_layout_tie_prefers_fewer_waste_then_area():
    import math

    # 동률 후보 중 빈 셀/면적 규칙이 적용되는지 전수 비교로 확인
    for n in range(1, 40):
        for cw, ch in [(10, 10), (30, 10), (10, 30), (7, 5)]:
            L = choose_atlas_layout(n, cw, ch)
            cands = []
            for c in range(1, n + 1):
                r = math.ceil(n / c)
                w, h = c * cw, r * ch
                cands.append((abs(math.log(w / h)), c * r - n, w * h, c))
            best = min(cands, key=lambda t: (round(t[0], 9), t[1], t[2]))
            assert (L.cols,) == (best[3],) or (
                abs(cands[L.cols - 1][0] - best[0]) <= 1e-9 and cands[L.cols - 1][1:3] == best[1:3]
            )


def test_atlas_preserves_in_frame_coordinates_and_order():
    tvp = FakeTVP([(A, 1), (B, 1), (C, 1), (blank(), 1), (A, 1)])
    plan = analyze_atlas(tvp, 7, noop)
    L = plan.output().final
    # union bbox (5,5)-(44,34) -> 셀 39x29, 프레임 5개
    assert (L.cell_w, L.cell_h, L.count) == (39, 29, 5)
    build_atlas(tvp, plan, noop)
    atlas = tvp.atlas
    assert atlas.size == (L.width, L.height)
    frames = [A, B, C, blank(), A]
    for i, src in enumerate(frames):
        x, y = L.cell_origin(i)
        cell = atlas.crop((x, y, x + L.cell_w, y + L.cell_h))
        expected = src.crop((5, 5, 44, 34))  # 모든 프레임 공통 bbox: 내부 좌표 그대로
        assert cell.tobytes() == expected.tobytes(), f"cell {i}"
    # 남는 셀은 완전 투명
    for i in range(L.count, L.cols * L.rows):
        x, y = L.cell_origin(i)
        assert atlas.crop((x, y, x + L.cell_w, y + L.cell_h)).getchannel("A").getbbox() is None
    assert not plan.work.exists()  # 임시 폴더 정리
    assert len(tvp.per_frame) == 5  # 원본 불변


def test_atlas_order_left_to_right_top_to_bottom():
    L = choose_atlas_layout(10, 10, 10)
    assert [L.cell_origin(i) for i in range(5)] == [(0, 0), (10, 0), (20, 0), (30, 0), (0, 10)] or L.cols != 4
    assert L.cell_origin(L.cols) == (0, 10)


def test_atlas_layer_changed_between_confirm_and_build():
    tvp = FakeTVP([(A, 1), (B, 1)])
    plan = analyze_atlas(tvp, 7, noop)
    tvp.ref = LayerRef(8, "other", True, False, "c", "p")
    with pytest.raises(ToolError, match="바뀌었습니다"):
        build_atlas(tvp, plan, noop)
    assert not plan.work.exists()


def test_atlas_all_empty_refuses():
    tvp = FakeTVP([(blank(), 3)])
    with pytest.raises(ToolError):
        analyze_atlas(tvp, 7, noop)


# ---------------- 사양서 13항: Preview / Padding / Max Size ----------------


def test_clean_preview_matches_spec_example_and_actual_result():
    # A A - B B B  ->  total 6, empty 1, duplicate 3, result 2
    tvp = FakeTVP([(A, 1), (A_copy, 1), (blank(), 1), (B, 3)])
    clean = analyze_compact(tvp, 7, noop)
    p = clean.plan
    assert (p.total, p.removed_empty, p.removed_repeat, len(p.keep)) == (6, 1, 3, 2)
    assert len(tvp.per_frame) == 6 and tvp.undo_open == 0  # Preview 단계에서는 바꾸지 않는다
    apply_compact(tvp, clean, noop)
    assert len(tvp.per_frame) == len(p.keep)  # Preview 와 실제 결과가 같다
    assert not clean.work.exists()


def test_clean_refuses_if_layer_changed_after_preview():
    tvp = FakeTVP([(A, 2), (B, 1)])
    clean = analyze_compact(tvp, 7, noop)
    tvp.ref = LayerRef(8, "other", True, False, "c", "p")
    with pytest.raises(ToolError, match="바뀌었습니다"):
        apply_compact(tvp, clean, noop)
    assert len(tvp.per_frame) == 3


def test_clean_refuses_if_frame_range_changed_after_preview():
    tvp = FakeTVP([(A, 2), (B, 1)])
    clean = analyze_compact(tvp, 7, noop)
    tvp.per_frame.append(C)  # 확인창 뒤에 프레임이 추가됨
    with pytest.raises(ToolError, match="프레임 구성이 바뀌었습니다"):
        apply_compact(tvp, clean, noop)


def test_crop_preview_size_equals_actual_crop():
    tvp = FakeTVP([(A, 2), (blank(), 1), (B, 3), (C, 1)], start=10)
    crop = analyze_crop(tvp, 7, noop)
    preview = (crop.spec.width, crop.spec.height)
    assert crop.canvas == (W, H)
    assert len(tvp.per_frame) == 7  # Preview 단계에서는 바꾸지 않는다
    build_crop(tvp, crop, noop)
    assert (tvp.built.width, tvp.built.height) == preview == (39, 29)
    assert all(im.size == preview for im in tvp.built_images)


def test_padding_atlas_size_spec_example():
    from tvp_autotools.core import _with_cell

    base = choose_atlas_layout(12, 100, 100, 2)
    L = _with_cell(type(base)(4, 3, 100, 100, 2, 0, 0, 12), 100, 100)
    assert (L.width, L.height) == (406, 304)
    assert L.cell_origin(1) == (102, 0) and L.cell_origin(4) == (0, 102)


def test_padding_zero_keeps_previous_layout():
    for n in range(1, 30):
        for cw, ch in [(128, 96), (64, 128), (10, 10)]:
            a = choose_atlas_layout(n, cw, ch)
            b = plan_atlas_output(n, cw, ch, 0, 0, MODE_CANCEL).final
            assert (a.cols, a.rows, a.width, a.height) == (b.cols, b.rows, b.width, b.height)


def test_large_padding_reevaluates_layout_by_final_pixel_ratio():
    import math

    n, cw, ch, pad = 6, 10, 10, 50
    L = choose_atlas_layout(n, cw, ch, pad)
    best = min(
        range(1, n + 1),
        key=lambda c: (
            round(abs(math.log((c * cw + (c - 1) * pad) / (math.ceil(n / c) * ch + (math.ceil(n / c) - 1) * pad))), 9),
            c * math.ceil(n / c) - n,
            (c * cw + (c - 1) * pad) * (math.ceil(n / c) * ch + (math.ceil(n / c) - 1) * pad),
        ),
    )
    assert L.cols == best


def test_max_size_cancel():
    out = plan_atlas_output(2, 2048, 2048, 0, 2048, MODE_CANCEL)  # 4096 x 2048 가 되는 구성
    assert (out.base.width, out.base.height) in ((4096, 2048), (2048, 4096))
    assert out.status == "too_big"


def test_max_size_cancel_stops_before_project_creation():
    tvp = FakeTVP([(A, 1), (B, 1), (C, 1)])
    plan = analyze_atlas(tvp, 7, noop)
    out = plan.output(0, 10, MODE_CANCEL)
    with pytest.raises(ToolError, match="초과합니다"):
        build_atlas(tvp, plan, noop, out)
    assert not hasattr(tvp, "atlas")  # 프로젝트를 만들지 않았다


def test_scale_down_spec_example():
    out = plan_atlas_output(16, 512, 512, 4, 2048, MODE_SCALE)
    F = out.final
    assert out.status == "ok" and (out.base.cols, out.base.rows) == (F.cols, F.rows) == (4, 4)
    assert F.width <= 2048 and F.height <= 2048 and F.padding == 4


def test_padding_fixed_after_scale_down():
    out = plan_atlas_output(17, 428, 512, 4, 2048, MODE_SCALE)
    F = out.final
    for i in range(F.count - 1):
        if (i + 1) % F.cols:
            x0, x1 = F.cell_origin(i)[0], F.cell_origin(i + 1)[0]
            assert x1 - (x0 + F.cell_w) == 4
    assert F.cell_origin(F.cols)[1] - F.cell_h == 4


def test_scale_down_same_factor_and_aspect():
    for args in [(17, 428, 512, 4), (10, 128, 96, 0), (30, 300, 70, 8), (5, 999, 333, 2)]:
        out = plan_atlas_output(*args, 1024, MODE_SCALE)
        if out.status != "ok" or not out.scaled:
            continue
        F, B = out.final, out.base
        assert F.width <= 1024 and F.height <= 1024 and out.scale <= 1.0
        # 하나의 비율을 가로·세로에 적용 후 반올림 (보정 포함 1px 이내)
        assert abs(F.cell_w - B.cell_w * out.scale) <= 1.0
        assert abs(F.cell_h - B.cell_h * out.scale) <= 1.0


def test_scale_never_enlarges():
    out = plan_atlas_output(4, 10, 10, 0, 5000, MODE_SCALE)
    assert out.final.cell_w == 10 and not out.scaled


def test_scale_impossible_when_padding_eats_max():
    out = plan_atlas_output(4, 10, 10, 3000, 100, MODE_SCALE)
    assert out.status == "impossible"


def test_scaled_atlas_keeps_relative_positions_and_padding_transparent():
    tvp = FakeTVP([(A, 1), (B, 1), (C, 1), (A_copy, 1)])
    plan = analyze_atlas(tvp, 7, noop)
    out = plan.output(3, 60, MODE_SCALE)
    assert out.status == "ok" and out.scaled
    build_atlas(tvp, plan, noop, out)
    atlas, F = tvp.atlas, out.final
    assert atlas.size == (F.width, F.height)
    # 셀 사이 Padding 은 완전 투명
    x0, _ = F.cell_origin(0)
    gap = atlas.crop((x0 + F.cell_w, 0, x0 + F.cell_w + 3, F.cell_h))
    assert gap.getchannel("A").getbbox() is None
    # 같은 그림(A, A_copy)은 축소 후에도 같은 셀 이미지
    def cell(i):
        x, y = F.cell_origin(i)
        return atlas.crop((x, y, x + F.cell_w, y + F.cell_h)).tobytes()
    assert cell(0) == cell(3)


def test_input_validation():
    assert parse_nonneg_int("", "Padding") == 0
    assert parse_nonneg_int(" 4 ", "Padding") == 4
    for bad in ["-1", "abc", "1.5", "-10"]:
        with pytest.raises(ValueError):
            parse_nonneg_int(bad, "Padding")


def test_premultiplied_resize_has_no_dark_fringe():
    from tvp_autotools.core import _resize_premultiplied

    im = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(10, 30):
        for y in range(10, 30):
            im.putpixel((x, y), (255, 255, 255, 255))
    small = _resize_premultiplied(im, (13, 13))
    pix = small.load()
    for px in (pix[x, y] for x in range(13) for y in range(13)):
        if 0 < px[3] < 255:
            assert px[0] > 200, px  # 반투명 가장자리도 흰색 유지 (검은 테두리 없음)
