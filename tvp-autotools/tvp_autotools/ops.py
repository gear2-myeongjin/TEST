"""Crop / 불필요 프레임 삭제 작업 흐름.

TVPaint 호출은 전부 Backend 인터페이스를 통해서만 하므로, 테스트에서는 가짜 백엔드로 교체할 수 있다.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from tvp_autotools.core import (
    Instance,
    analyze_image,
    crop_image,
    AtlasLayout,
    choose_atlas_layout,
    compose_atlas,
    group_runs,
    plan_frames,
    union_bbox,
)

Progress = Callable[[str, float], None]  # (메시지, 0.0~1.0)


class ToolError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류."""


@dataclass(frozen=True)
class LayerRef:
    id: int
    name: str
    is_anim: bool
    is_locked: bool
    clip_name: str
    project_name: str


@dataclass(frozen=True)
class CropSpec:
    source: LayerRef
    width: int
    height: int
    offset: tuple[int, int]  # 원본 캔버스에서 잘라낸 영역의 좌상단
    instances: list[Instance]
    images: list[Path]  # 인스턴스 순서대로 크롭된 PNG
    source_opacity: float | None = None  # 작업 시작 전에 읽어 둔 원본 레이어 불투명도 (TVPaint 단위)


class Backend(Protocol):
    def current_layer(self) -> LayerRef: ...
    def layer_range(self, layer: LayerRef) -> tuple[int, int]: ...
    def render_frames(self, layer: LayerRef, frames: list[int], out_dir: Path) -> dict[int, Path]: ...
    def undo_group(self, name: str) -> AbstractContextManager[None]: ...
    def replace_layer_frames(self, layer: LayerRef, images: list[Path], work: Path) -> int: ...
    def build_cropped_project(self, spec: CropSpec, work: Path) -> str: ...
    def build_atlas_project(self, source: LayerRef, atlas_png: Path, layout: AtlasLayout, work: Path) -> str: ...


def _make_workdir() -> Path:
    """임시 작업 폴더. TVPaint 가 한글/공백이 섞인 경로에 저장하지 못할 수 있어 영문 경로를 우선 고른다."""
    import os

    candidates = [tempfile.gettempdir(), os.environ.get("ProgramData", ""), os.environ.get("PUBLIC", "")]
    for base in candidates:
        if base and base.isascii() and " " not in base and os.path.isdir(base):
            try:
                root = Path(base) / "tvpaam_tmp"
                root.mkdir(exist_ok=True)
                return Path(tempfile.mkdtemp(prefix="w_", dir=root))
            except OSError:
                continue
    return Path(tempfile.mkdtemp(prefix="tvpaam_"))


def _check_layer(layer: LayerRef, expected_id: int | None) -> None:
    if expected_id is not None and layer.id != expected_id:
        raise ToolError(
            "확인창을 띄운 뒤 선택 레이어가 바뀌었습니다.\n"
            f"지금 선택된 레이어: {layer.name}\n다시 실행해 주세요."
        )
    if not layer.is_anim:
        raise ToolError(f"'{layer.name}'은(는) 애니메이션 레이어가 아닙니다.")


def _analyze_all(paths: list[Path]):
    """PNG 분석을 CPU 코어 수만큼 병렬로 처리한다."""
    import os
    from concurrent.futures import ThreadPoolExecutor

    workers = max(1, min(8, (os.cpu_count() or 2)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(analyze_image, paths))


def _render_and_analyze(backend: Backend, layer: LayerRef, work: Path, progress: Progress):
    progress("레이어 구간 읽는 중", 0.03)
    start, end = backend.layer_range(layer)
    frames = list(range(start, end + 1))
    if not frames:
        raise ToolError("레이어에 프레임이 없습니다.")

    progress(f"프레임 {len(frames)}개 렌더링 중", 0.08)
    files = backend.render_frames(layer, frames, work)
    missing = [f for f in frames if f not in files]
    if missing:
        raise ToolError(f"렌더링 결과를 찾지 못한 프레임이 있습니다: {missing[:10]}")

    progress("픽셀 분석 중", 0.5)
    infos = _analyze_all([files[f] for f in frames])
    return frames, files, infos


def _method_note(backend: Backend) -> str:
    lines = []
    method = getattr(backend, "render_method", None)
    if method:
        lines.append(f"이미지 저장 방식: {method}")
    mode = getattr(backend, "opacity_mode", None)
    if mode:
        lines.append(f"불투명도 처리: {mode}")
    cal = getattr(backend, "_calibration", None)
    if cal and cal.get("log") and cal.get("reader") is None:  # 안전 모드일 때만 원인 파악용 기록을 보여준다
        lines.append("--- 불투명도 시험 기록 ---")
        lines += cal["log"]
    return ("\n\n" + "\n".join(lines)) if lines else ""


def run_compact(backend: Backend, expected_layer_id: int | None, progress: Progress) -> str:
    with contextlib.suppress(Exception):
        backend.operation_label = "불필요 프레임 삭제"
    layer = backend.current_layer()
    _check_layer(layer, expected_layer_id)
    if layer.is_locked:
        raise ToolError(f"'{layer.name}' 레이어가 잠겨 있습니다. 잠금을 풀고 다시 실행해 주세요.")

    work = _make_workdir()
    try:
        frames, files, infos = _render_and_analyze(backend, layer, work, progress)
        plan = plan_frames(frames, infos)
        if not plan.keep:
            raise ToolError("레이어 전체가 빈 프레임입니다. 삭제하지 않았습니다.")
        if plan.is_noop:
            progress("완료", 1.0)
            return f"'{layer.name}': 정리할 프레임이 없습니다. (이미 {len(plan.keep)}장 1콤마)" + _method_note(backend)

        progress("레이어 다시 구성 중", 0.75)
        with backend.undo_group("TvpAAM_CleanFrames"):
            result_count = backend.replace_layer_frames(layer, [files[f] for f in plan.keep], work)

        lines = [
            f"'{layer.name}' 정리 완료",
            f"프레임 {plan.total}개 → {len(plan.keep)}장 (1콤마)",
            f"빈 프레임 {plan.removed_empty}개, 반복 그림(콤마·복사본) {plan.removed_repeat}개 정리",
            "TVPaint에서 실행취소(Ctrl+Z) 한 번으로 되돌릴 수 있습니다.",
        ]
        if result_count != len(plan.keep):
            lines.append(
                f"\n⚠ 검증 불일치: 예상 {len(plan.keep)}프레임, 실제 {result_count}프레임. "
                "결과를 확인하고 문제가 있으면 Ctrl+Z로 되돌려 주세요."
            )
        progress("완료", 1.0)
        return "\n".join(lines) + _method_note(backend)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_crop(backend: Backend, expected_layer_id: int | None, progress: Progress) -> str:
    with contextlib.suppress(Exception):
        backend.operation_label = "Crop"
    layer = backend.current_layer()
    _check_layer(layer, expected_layer_id)

    source_opacity = None
    reader = getattr(backend, "read_layer_opacity", None)
    if reader is not None:
        source_opacity = reader(layer)  # 렌더링 등 어떤 작업보다 먼저 읽는다

    work = _make_workdir()
    try:
        frames, files, infos = _render_and_analyze(backend, layer, work, progress)
        bbox = union_bbox([i.bbox for i in infos])
        if bbox is None:
            raise ToolError("레이어 전체가 빈 프레임이라 크롭할 영역이 없습니다.")

        # 같은 그림이 이어지는 구간은 한 장 + 콤마로 되살린다 (빈 프레임 구간도 그대로 유지)
        runs = group_runs(frames, infos)
        progress("크롭 이미지 만드는 중", 0.7)
        cropped_dir = work / "cropped"
        cropped_dir.mkdir()
        cropped: list[Path] = []
        for n, (start, _length) in enumerate(runs):
            dst = cropped_dir / f"crop_{n:05d}.png"
            crop_image(files[start], dst, bbox)
            cropped.append(dst)

        left, top, right, bottom = bbox
        spec = CropSpec(
            source=layer,
            width=right - left,
            height=bottom - top,
            offset=(left, top),
            instances=[Instance(start, length) for start, length in runs],
            images=cropped,
            source_opacity=source_opacity,
        )
        progress("새 프로젝트 생성 중", 0.82)
        project_name = backend.build_cropped_project(spec, work)
        progress("완료", 1.0)
        check = getattr(backend, "opacity_check", None)
        warning = f"\n\n⚠ 불투명도 확인 실패 (이 내용을 그대로 전달해 주세요)\n{check}" if check else ""
        return (
            f"'{layer.name}' 크롭 완료\n"
            f"새 프로젝트: {project_name}\n"
            f"크기 {spec.width}×{spec.height} (원본 캔버스 기준 X {left}, Y {top})\n"
            f"프레임 {len(frames)}개, 그림 {len(runs)}장 + 콤마 구조 유지, 원본 프로젝트는 변경되지 않았습니다.\n"
            "새 프로젝트는 아직 저장되지 않았습니다."
            + _method_note(backend)
            + warning
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------- Atlas ----------------


@dataclass
class AtlasPlan:
    """분석 결과. 확인창에 보여준 뒤 build_atlas 로 넘긴다 (임시 폴더는 build/discard 에서 지운다)."""

    layer: LayerRef
    layout: AtlasLayout
    work: Path
    cells: list[Path]

    def discard(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)


def analyze_atlas(backend: Backend, expected_layer_id: int | None, progress: Progress) -> AtlasPlan:
    """현재 레이어의 타임라인 프레임을 있는 그대로, 공통 union bounds 크기의 셀로 만든다."""
    with contextlib.suppress(Exception):
        backend.operation_label = "Atlas 생성"
    layer = backend.current_layer()
    _check_layer(layer, expected_layer_id)

    work = _make_workdir()
    try:
        frames, files, infos = _render_and_analyze(backend, layer, work, progress)
        bbox = union_bbox([i.bbox for i in infos])
        if bbox is None:
            raise ToolError("레이어 전체가 빈 프레임이라 Atlas를 만들 수 없습니다.")
        progress("셀 이미지 만드는 중", 0.75)
        cell_dir = work / "cells"
        cell_dir.mkdir()
        cells: list[Path] = []
        for n, frame in enumerate(frames):
            dst = cell_dir / f"cell_{n:05d}.png"
            crop_image(files[frame], dst, bbox)  # 모든 프레임을 같은 bbox 로: 프레임 내부 좌표 보존
            cells.append(dst)
        left, top, right, bottom = bbox
        layout = choose_atlas_layout(len(cells), right - left, bottom - top)
        progress("분석 완료", 1.0)
        return AtlasPlan(layer=layer, layout=layout, work=work, cells=cells)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def build_atlas(backend: Backend, plan: AtlasPlan, progress: Progress) -> str:
    try:
        current = backend.current_layer()
        _check_layer(current, plan.layer.id)
        progress("Atlas 합성 중", 0.3)
        atlas_png = plan.work / "atlas.png"
        compose_atlas(plan.cells, plan.layout, atlas_png)
        progress("새 프로젝트 생성 중", 0.6)
        name = backend.build_atlas_project(plan.layer, atlas_png, plan.layout, plan.work)
        progress("완료", 1.0)
        L = plan.layout
        return (
            f"'{plan.layer.name}' Atlas 생성 완료\n"
            f"새 프로젝트: {name}\n"
            f"프레임 {L.count}개, 셀 {L.cell_w}×{L.cell_h}, {L.cols}열 × {L.rows}행\n"
            f"Atlas {L.width}×{L.height}, 원본 프로젝트는 변경되지 않았습니다.\n"
            "새 프로젝트는 아직 저장되지 않았습니다."
            + _method_note(backend)
        )
    finally:
        plan.discard()

