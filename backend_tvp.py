"""PyTVPaint(tvpaint-rpc 플러그인)를 통한 실제 TVPaint 백엔드.

주의: 모든 메서드는 반드시 같은 워커 스레드 하나에서만 호출해야 한다 (RPC 클라이언트가 스레드 안전하지 않음).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator

# pytvpaint 는 import 순간 TVPaint 접속을 최대 60초 기다린다. UI 가 멈추지 않도록 끄고 직접 접속한다.
os.environ.setdefault("PYTVPAINT_WS_STARTUP_CONNECT", "0")
os.environ.setdefault("PYTVPAINT_LOG_LEVEL", "WARNING")

from fileseq import FileSequence, FrameSet  # noqa: E402
from pytvpaint import george  # noqa: E402
from pytvpaint.george.client import rpc_client  # noqa: E402
from pytvpaint.layer import Layer, LayerInstance  # noqa: E402
from pytvpaint.project import Project  # noqa: E402

from tvp_autotools.core import Instance, safe_filename  # noqa: E402
from tvp_autotools.ops import CropSpec, LayerRef, ToolError  # noqa: E402


class TVPaintBackend:
    def __init__(self) -> None:
        self._layer_cache: dict[int, Layer] = {}

    # ---------- 연결 ----------
    def connect(self) -> None:
        if not rpc_client.is_connected:
            rpc_client.connect()

    @property
    def connected(self) -> bool:
        try:
            return rpc_client.is_connected
        except Exception:
            return False

    # ---------- 조회 ----------
    def current_layer(self) -> LayerRef:
        layer = Layer.current_layer()
        self._layer_cache = {layer.id: layer}
        return LayerRef(
            id=layer.id,
            name=layer.name,
            is_anim=layer.is_anim_layer,
            is_locked=layer.is_locked,
            clip_name=layer.clip.name,
            project_name=layer.project.name,
        )

    def _layer(self, ref: LayerRef) -> Layer:
        layer = self._layer_cache.get(ref.id)
        if layer is None:
            layer = Layer(layer_id=ref.id)
            self._layer_cache[ref.id] = layer
        layer.make_current()
        return layer

    def instances(self, ref: LayerRef) -> list[Instance]:
        layer = self._layer(ref)
        layer.refresh()
        starts: list[int] = []
        current = LayerInstance(layer, layer.start)
        while True:
            starts.append(current.start)
            nxt = current.next
            if nxt is None:
                break
            current = nxt
        end = layer.end
        result = []
        for i, s in enumerate(starts):
            stop = starts[i + 1] - 1 if i + 1 < len(starts) else end
            result.append(Instance(start=s, length=stop - s + 1))
        return result

    # ---------- 렌더 ----------
    def render_instances(self, ref: LayerRef, starts: list[int], out_dir: Path) -> dict[int, Path]:
        """레이어만 단독으로, 투명 배경·비프리멀티플라이 PNG 로 렌더한다.

        불투명도/블렌딩 모드는 렌더 중에만 100%/Color 로 바꿨다가 반드시 원래대로 되돌린다.
        (불투명도가 픽셀에 구워지면 크롭 후 불투명도가 이중 적용되기 때문)
        """
        layer = self._layer(ref)
        clip = layer.clip
        visibility = [(lyr, lyr.is_visible) for lyr in clip.layers]
        opacity = layer.opacity
        blending = layer.blending_mode
        # PyTVPaint 렌더 도중 오류가 나면 배경/저장 설정이 복구되지 않으므로 직접 저장해 두었다가 되돌린다
        background = george.tv_background_get()
        alpha_save = george.tv_alpha_save_mode_get()
        save_mode = george.tv_save_mode_get()
        frame_set = FrameSet(sorted(starts))
        try:
            layer.opacity = 100
            layer.blending_mode = george.BlendingMode.COLOR
            pattern = str(out_dir / "inst.#.png")
            try:
                seq = clip.render(
                    pattern,
                    frame_set=frame_set,
                    layer_selection=[layer],
                    alpha_mode=george.AlphaSaveMode.NO_PREMULTIPLY,
                    background_mode=george.BackgroundMode.NONE,
                )
            except george.GeorgeError as exc:
                raise ToolError(self._render_diagnostics(layer, frame_set, out_dir, exc)) from exc
        finally:
            with contextlib.suppress(Exception):
                george.tv_background_set(background[0], background[1])
            with contextlib.suppress(Exception):
                george.tv_alpha_save_mode_set(alpha_save)
            with contextlib.suppress(Exception):
                george.tv_save_mode_set(save_mode[0], *save_mode[1])
            with contextlib.suppress(Exception):
                layer.opacity = opacity
            with contextlib.suppress(Exception):
                layer.blending_mode = blending
            for lyr, was_visible in visibility:
                with contextlib.suppress(Exception):
                    if lyr.is_visible != was_visible:
                        lyr.is_visible = was_visible
            with contextlib.suppress(Exception):
                layer.make_current()

        if not isinstance(seq, FileSequence):
            seq = FileSequence(str(seq))
        return {s: Path(seq.frame(s)) for s in starts if Path(seq.frame(s)).exists()}

    @staticmethod
    def _render_diagnostics(layer: Layer, frame_set: FrameSet, out_dir: Path, exc: Exception) -> str:
        """렌더 실패 시 원인 파악용 정보를 모은다."""
        info = [f"TVPaint가 이미지 저장을 거부했습니다 ({exc}).", "", "--- 진단 정보 (이 내용을 그대로 전달해 주세요) ---"]
        probes = {
            "렌더 요청 프레임": lambda: str(frame_set),
            "임시 폴더": lambda: str(out_dir),
            "레이어 start/end": lambda: f"{layer.start} / {layer.end}",
            "클립 start/end": lambda: f"{layer.clip.start} / {layer.clip.end}",
            "클립 timeline_start": lambda: str(layer.clip.timeline_start),
            "클립 mark in/out": lambda: f"{layer.clip.mark_in} / {layer.clip.mark_out}",
            "프로젝트 start_frame": lambda: str(layer.project.start_frame),
            "프로젝트 경로": lambda: str(layer.project.path),
        }
        for label, probe in probes.items():
            try:
                info.append(f"{label}: {probe()}")
            except Exception as e:  # noqa: BLE001
                info.append(f"{label}: (읽기 실패: {e})")
        return "\n".join(info)

    # ---------- 편집 ----------
    @contextlib.contextmanager
    def undo_group(self, name: str) -> Iterator[None]:
        george.tv_update_undo()
        george.tv_undo_open_stack()
        try:
            yield
        finally:
            # 중간에 오류가 나도 스택은 반드시 닫는다 (열린 채로 남으면 TVPaint 실행취소가 꼬임)
            george.tv_undo_close_stack(name)

    def delete_instance(self, ref: LayerRef, start: int, length: int) -> None:
        layer = self._layer(ref)
        layer.select_frames(start, start + length - 1)
        selected = layer.selected_frames
        if len(selected) != length:
            layer.clear_selection()
            raise ToolError(f"프레임 {start} 선택 실패 (요청 {length}, 실제 {len(selected)}). 작업을 중단했습니다.")
        george.tv_layer_cut()
        layer.clear_selection()

    def set_instance_length(self, ref: LayerRef, start: int, length: int) -> None:
        layer = self._layer(ref)
        LayerInstance(layer, start).length = length

    # ---------- 크롭 프로젝트 ----------
    def build_cropped_project(self, spec: CropSpec) -> str:
        src_layer = self._layer(spec.source)
        src_project = src_layer.project
        src_start = src_layer.start

        fps = src_project.fps
        par = src_project.pixel_aspect_ratio
        field = src_project.field_order
        start_frame = src_project.start_frame
        opacity = src_layer.opacity
        blending = src_layer.blending_mode
        pre_behavior = src_layer.pre_behavior
        post_behavior = src_layer.post_behavior

        base_dir = src_project.path.parent if str(src_project.path) not in ("", ".") else Path.home()
        stem = src_project.path.stem or "project"
        new_path = base_dir / f"{safe_filename(stem)}_{safe_filename(spec.source.name)}_crop.tvpp"

        project = Project.new(new_path, spec.width, spec.height, par, fps, field, start_frame)
        project.make_current()
        clip = project.current_clip
        defaults = list(clip.layers)

        layer = Layer.new_anim_layer(spec.source.name, clip)
        for lyr in defaults:
            if lyr.id != layer.id:
                george.tv_layer_kill(lyr.id)
        layer.make_current()

        base = layer.start
        clip.current_frame = base
        for n, png in enumerate(spec.images):
            if n > 0:
                george.tv_layer_insert_image(count=1, direction=george.InsertDirection.AFTER)
            george.tv_load_image(png)

        # 콤마 복원: 뒤에서부터 늘려야 앞 인스턴스 위치가 밀리지 않는다
        for n in reversed(range(len(spec.instances))):
            length = spec.instances[n].length
            if length > 1:
                LayerInstance(layer, base + n).length = length

        if src_start != layer.start:
            layer.shift(src_start)

        for attr, value in (
            ("opacity", opacity),
            ("blending_mode", blending),
            ("pre_behavior", pre_behavior),
            ("post_behavior", post_behavior),
        ):
            with contextlib.suppress(Exception):
                setattr(layer, attr, value)

        clip.current_frame = layer.start

        # 구조 검증
        built = self.instances(
            LayerRef(layer.id, layer.name, True, False, clip.name, project.name)
        )
        expected = [i.length for i in spec.instances]
        actual = [i.length for i in built]
        if expected != actual:
            raise ToolError(
                "새 프로젝트의 콤마 구조가 원본과 다릅니다.\n"
                f"원본 {len(expected)}개 / 결과 {len(actual)}개 인스턴스.\n"
                "새 프로젝트는 열린 채로 두었으니 확인해 주세요. 원본은 변경되지 않았습니다."
            )
        return project.name
