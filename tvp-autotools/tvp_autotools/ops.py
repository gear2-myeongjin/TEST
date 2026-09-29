"""Crop / 불필요 프레임 삭제 작업 흐름.

TVPaint 호출은 전부 Backend 인터페이스를 통해서만 하므로, 테스트에서는 가짜 백엔드로 교체할 수 있다.
"""

from __future__ import annotations

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
    plan_compaction,
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


class Backend(Protocol):
    def current_layer(self) -> LayerRef: ...
    def instances(self, layer: LayerRef) -> list[Instance]: ...
    def render_instances(self, layer: LayerRef, starts: list[int], out_dir: Path) -> dict[int, Path]: ...
    def undo_group(self, name: str) -> AbstractContextManager[None]: ...
    def delete_instance(self, layer: LayerRef, start: int, length: int) -> None: ...
    def set_instance_length(self, layer: LayerRef, start: int, length: int) -> None: ...
    def build_cropped_project(self, spec: CropSpec) -> str: ...


def _make_workdir() -> Path:
    """임시 작업 폴더. TVPaint 가 한글/공백이 섞인 경로에 저장하지 못할 수 있어 영문 경로를 우선 고른다."""
    import os

    candidates = [tempfile.gettempdir(), os.environ.get("ProgramData", ""), os.environ.get("PUBLIC", "")]
    for base in candidates:
        if base and base.isascii() and " " not in base and os.path.isdir(base):
            try:
                root = Path(base) / "tvp_autocrop_tmp"
                root.mkdir(exist_ok=True)
                return Path(tempfile.mkdtemp(prefix="w_", dir=root))
            except OSError:
                continue
    return Path(tempfile.mkdtemp(prefix="tvp_autocrop_"))


def _check_layer(layer: LayerRef, expected_id: int | None) -> None:
    if expected_id is not None and layer.id != expected_id:
        raise ToolError(
            "확인창을 띄운 뒤 선택 레이어가 바뀌었습니다.\n"
            f"지금 선택된 레이어: {layer.name}\n다시 실행해 주세요."
        )
    if not layer.is_anim:
        raise ToolError(f"'{layer.name}'은(는) 애니메이션 레이어가 아닙니다.")


def _render_and_analyze(backend: Backend, layer: LayerRef, work: Path, progress: Progress):
    progress("인스턴스 구조 읽는 중", 0.05)
    instances = backend.instances(layer)
    if not instances:
        raise ToolError("레이어에 프레임이 없습니다.")

    progress(f"인스턴스 {len(instances)}개 렌더링 중", 0.15)
    files = backend.render_instances(layer, [i.start for i in instances], work)
    missing = [i.start for i in instances if i.start not in files]
    if missing:
        raise ToolError(f"렌더링 결과를 찾지 못한 프레임이 있습니다: {missing[:10]}")

    infos = []
    for n, inst in enumerate(instances):
        infos.append(analyze_image(files[inst.start]))
        progress("픽셀 분석 중", 0.45 + 0.25 * (n + 1) / len(instances))
    return instances, files, infos


def run_compact(backend: Backend, expected_layer_id: int | None, progress: Progress) -> str:
    layer = backend.current_layer()
    _check_layer(layer, expected_layer_id)
    if layer.is_locked:
        raise ToolError(f"'{layer.name}' 레이어가 잠겨 있습니다. 잠금을 풀고 다시 실행해 주세요.")

    work = _make_workdir()
    try:
        instances, _files, infos = _render_and_analyze(backend, layer, work, progress)
        plan = plan_compaction(instances, infos)

        if plan.kept == 0:
            raise ToolError("레이어 전체가 빈 프레임입니다. 삭제하지 않았습니다.")
        if plan.is_noop:
            progress("완료", 1.0)
            return f"'{layer.name}': 정리할 프레임이 없습니다. (이미 {plan.kept}장 1콤마)"

        progress("프레임 정리 중", 0.72)
        with backend.undo_group("AutoCrop_CleanFrames"):
            total = len(plan.actions)
            for n, action in enumerate(plan.actions):
                if action.kind == "delete":
                    backend.delete_instance(layer, action.start, action.length)
                else:
                    backend.set_instance_length(layer, action.start, 1)
                progress("프레임 정리 중", 0.72 + 0.23 * (n + 1) / total)

        progress("결과 검증 중", 0.97)
        after = backend.instances(layer)
        ok = len(after) == plan.kept and all(i.length == 1 for i in after)

        lines = [
            f"'{layer.name}' 정리 완료",
            f"인스턴스 {len(instances)}개 → {plan.kept}장 (1콤마)",
            f"빈 프레임 {plan.removed_empty}개, 중복 {plan.removed_duplicate}개 삭제, 콤마 {plan.shortened}개 축소",
            "TVPaint에서 실행취소(Ctrl+Z) 한 번으로 되돌릴 수 있습니다.",
        ]
        if not ok:
            lines.append(
                f"\n⚠ 검증 불일치: 예상 {plan.kept}장, 실제 {len(after)}개 인스턴스. "
                "결과를 확인하고 문제가 있으면 Ctrl+Z로 되돌려 주세요."
            )
        progress("완료", 1.0)
        return "\n".join(lines)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_crop(backend: Backend, expected_layer_id: int | None, progress: Progress) -> str:
    layer = backend.current_layer()
    _check_layer(layer, expected_layer_id)

    work = _make_workdir()
    try:
        instances, files, infos = _render_and_analyze(backend, layer, work, progress)
        bbox = union_bbox([i.bbox for i in infos])
        if bbox is None:
            raise ToolError("레이어 전체가 빈 프레임이라 크롭할 영역이 없습니다.")

        progress("크롭 이미지 만드는 중", 0.75)
        cropped_dir = work / "cropped"
        cropped_dir.mkdir()
        cropped: list[Path] = []
        for n, inst in enumerate(instances):
            dst = cropped_dir / f"c{n:05d}.png"
            crop_image(files[inst.start], dst, bbox)
            cropped.append(dst)

        left, top, right, bottom = bbox
        spec = CropSpec(
            source=layer,
            width=right - left,
            height=bottom - top,
            offset=(left, top),
            instances=instances,
            images=cropped,
        )
        progress("새 프로젝트 생성 중", 0.85)
        project_name = backend.build_cropped_project(spec)
        progress("완료", 1.0)
        return (
            f"'{layer.name}' 크롭 완료\n"
            f"새 프로젝트: {project_name}\n"
            f"크기 {spec.width}×{spec.height} (원본 캔버스 기준 X {left}, Y {top})\n"
            f"인스턴스 {len(instances)}개, 콤마 구조 유지, 원본 프로젝트는 변경되지 않았습니다.\n"
            "새 프로젝트는 아직 저장되지 않았습니다."
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
