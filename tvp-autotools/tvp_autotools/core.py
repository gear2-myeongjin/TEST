"""TVPaint와 무관한 순수 로직. 여기 있는 함수는 TVPaint 없이 테스트할 수 있다."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class Instance:
    """레이어 인스턴스(그림 1장 + 콤마 길이)."""

    start: int
    length: int


@dataclass(frozen=True)
class ImageInfo:
    """렌더된 인스턴스 이미지 분석 결과."""

    empty: bool  # 알파가 전부 0이면 True (흰 배경은 불투명이므로 빈 것이 아님)
    signature: str  # 픽셀 동일성 판정용 해시
    bbox: tuple[int, int, int, int] | None  # (left, top, right, bottom), right/bottom 은 exclusive


def analyze_image(path: Path) -> ImageInfo:
    """PNG 한 장을 분석한다.

    - 빈 프레임: 알파 채널 전체가 0
    - 동일성: 완전히 투명한 픽셀의 RGB 는 무시하고(렌더러마다 값이 다를 수 있음) 나머지 RGBA 가 완전히 같으면 같은 그림
    """
    with Image.open(path) as src:
        img = src.convert("RGBA")
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return ImageInfo(empty=True, signature="EMPTY", bbox=None)

    mask = alpha.point(lambda a: 255 if a else 0)
    canonical = Image.composite(img, Image.new("RGBA", img.size, (0, 0, 0, 0)), mask)
    digest = hashlib.blake2b(canonical.tobytes(), digest_size=20)
    digest.update(f"{img.size}".encode())
    return ImageInfo(empty=False, signature=digest.hexdigest(), bbox=bbox)


def union_bbox(boxes: list[tuple[int, int, int, int] | None]) -> tuple[int, int, int, int] | None:
    valid = [b for b in boxes if b is not None]
    if not valid:
        return None
    return (
        min(b[0] for b in valid),
        min(b[1] for b in valid),
        max(b[2] for b in valid),
        max(b[3] for b in valid),
    )


def crop_image(src: Path, dst: Path, bbox: tuple[int, int, int, int]) -> None:
    with Image.open(src) as im:
        im.convert("RGBA").crop(bbox).save(dst, format="PNG")


@dataclass(frozen=True)
class CompactAction:
    kind: str  # "delete" | "shorten"
    start: int
    length: int
    reason: str  # "empty" | "duplicate" | "exposure"


@dataclass(frozen=True)
class CompactPlan:
    actions: list[CompactAction]  # 반드시 뒤에서 앞 순서 (앞쪽 프레임 번호가 밀리지 않도록)
    kept: int
    removed_empty: int
    removed_duplicate: int
    shortened: int

    @property
    def is_noop(self) -> bool:
        return not self.actions


def plan_compaction(instances: list[Instance], infos: list[ImageInfo]) -> CompactPlan:
    """빈 프레임 제거 + 연속 중복 그림 제거 + 전부 1콤마.

    예: A A - - B B B - C C  ->  A B C
    중복 판정은 '직전에 남긴 그림'과 비교한다. 따라서 A - A 처럼 빈 프레임을 사이에 둔
    같은 그림도 하나로 합쳐지고, A B A 처럼 다른 그림을 사이에 둔 경우는 둘 다 남는다.
    """
    if len(instances) != len(infos):
        raise ValueError("인스턴스 수와 분석 결과 수가 다릅니다.")

    forward: list[CompactAction] = []
    prev_sig: str | None = None
    kept = removed_empty = removed_dup = shortened = 0

    for inst, info in zip(instances, infos):
        if info.empty:
            forward.append(CompactAction("delete", inst.start, inst.length, "empty"))
            removed_empty += 1
            continue
        if info.signature == prev_sig:
            forward.append(CompactAction("delete", inst.start, inst.length, "duplicate"))
            removed_dup += 1
            continue
        prev_sig = info.signature
        kept += 1
        if inst.length > 1:
            forward.append(CompactAction("shorten", inst.start, 1, "exposure"))
            shortened += 1

    return CompactPlan(
        actions=list(reversed(forward)),
        kept=kept,
        removed_empty=removed_empty,
        removed_duplicate=removed_dup,
        shortened=shortened,
    )


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip(" .")
    return cleaned or "layer"


# ---------------- 프레임 단위 계획 ----------------
# TVPaint 의 인스턴스 탐색(tv_ExposureNext)은 빈 이미지를 건너뛰므로 인스턴스 구조를 믿지 않고,
# 레이어 구간의 모든 프레임을 렌더해서 실제 픽셀만으로 판단한다.


@dataclass(frozen=True)
class FramePlan:
    keep: list[int]  # 남길 프레임 번호 (순서대로, 결과는 각 1콤마)
    total: int
    removed_empty: int  # 빈 프레임 수
    removed_repeat: int  # 직전 그림과 같은 프레임 수 (콤마로 늘어난 것 + 복사본)

    @property
    def is_noop(self) -> bool:
        return len(self.keep) == self.total


def plan_frames(frames: list[int], infos: list[ImageInfo]) -> FramePlan:
    """빈 프레임 제거 + 직전에 남긴 그림과 같은 프레임 제거. 결과는 남은 그림이 1장씩."""
    keep: list[int] = []
    prev: str | None = None
    empty = repeat = 0
    for frame, info in zip(frames, infos):
        if info.empty:
            empty += 1
            continue
        if info.signature == prev:
            repeat += 1
            continue
        prev = info.signature
        keep.append(frame)
    return FramePlan(keep=keep, total=len(frames), removed_empty=empty, removed_repeat=repeat)


def group_runs(frames: list[int], infos: list[ImageInfo]) -> list[tuple[int, int]]:
    """연속으로 같은 그림(빈 프레임 포함)인 구간을 (시작 프레임, 길이)로 묶는다. Crop 의 콤마 복원용."""
    runs: list[tuple[int, int]] = []
    prev: str | None = None
    for frame, info in zip(frames, infos):
        if runs and info.signature == prev:
            start, length = runs[-1]
            runs[-1] = (start, length + 1)
        else:
            runs.append((frame, 1))
            prev = info.signature
    return runs


# ---------------- Atlas ----------------
# 기존 Photoshop Atlas Builder v3.1 규칙을 그대로 옮긴 것. 임의로 바꾸지 말 것.

TIE_EPSILON = 1e-9  # squarePenalty 동률 판정 (부동소수점 오차 수준만 동률로 본다)


@dataclass(frozen=True)
class AtlasLayout:
    cols: int
    rows: int
    cell_w: int
    cell_h: int
    padding: int
    width: int
    height: int
    count: int

    def cell_origin(self, index: int) -> tuple[int, int]:
        """프레임 index 의 셀 좌상단. 원본 크롭 (0,0) 이 이 좌표에 그대로 놓인다 (중앙정렬 없음)."""
        col = index % self.cols
        row = index // self.cols
        return col * (self.cell_w + self.padding), row * (self.cell_h + self.padding)


def choose_atlas_layout(count: int, cell_w: int, cell_h: int, padding: int = 0) -> AtlasLayout:
    """모든 열 수 후보를 검사해 실제 픽셀 비율이 가장 정사각형에 가까운 구성을 고른다.

    점수: abs(log(atlasW / atlasH)). 동률이면 1) 빈 셀이 적은 쪽 2) 면적이 작은 쪽.
    """
    import math

    if count < 1 or cell_w < 1 or cell_h < 1:
        raise ValueError("프레임 수와 셀 크기는 1 이상이어야 합니다.")

    best: AtlasLayout | None = None
    best_key: tuple[float, int, int] | None = None
    for cols in range(1, count + 1):
        rows = math.ceil(count / cols)
        width = cols * cell_w + (cols - 1) * padding
        height = rows * cell_h + (rows - 1) * padding
        penalty = abs(math.log(width / height))
        waste = cols * rows - count
        area = width * height
        candidate = AtlasLayout(cols, rows, cell_w, cell_h, padding, width, height, count)
        if best_key is None or penalty < best_key[0] - TIE_EPSILON:
            best, best_key = candidate, (penalty, waste, area)
        elif abs(penalty - best_key[0]) <= TIE_EPSILON and (waste, area) < (best_key[1], best_key[2]):
            best, best_key = candidate, (min(penalty, best_key[0]), waste, area)
    assert best is not None
    return best


def compose_atlas(images: list[Path], layout: AtlasLayout, dst: Path) -> None:
    """셀 크기와 정확히 같은 이미지들을 셀 좌상단에 1:1 로 붙인다. 빈 셀은 완전 투명."""
    atlas = Image.new("RGBA", (layout.width, layout.height), (0, 0, 0, 0))
    for index, path in enumerate(images):
        with Image.open(path) as src:
            im = src.convert("RGBA")
        if im.size != (layout.cell_w, layout.cell_h):
            raise ValueError(f"{index + 1}번째 프레임 크기 {im.size}가 셀 크기와 다릅니다.")
        atlas.paste(im, layout.cell_origin(index))  # 마스크 없이 붙여 알파까지 그대로 복사
    atlas.save(dst, format="PNG")
