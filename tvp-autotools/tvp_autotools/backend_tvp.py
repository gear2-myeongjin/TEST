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

from pytvpaint import george  # noqa: E402
from pytvpaint.george.client import rpc_client  # noqa: E402
from pytvpaint.layer import Layer, LayerInstance  # noqa: E402
from pytvpaint.project import Project  # noqa: E402

from tvp_autotools.core import Instance, safe_filename  # noqa: E402
from tvp_autotools.ops import CropSpec, LayerRef, ToolError  # noqa: E402


class TVPaintBackend:
    def __init__(self) -> None:
        self._layer_cache: dict[int, Layer] = {}
        self._strategy: str | None = None  # 이 TVPaint 에서 동작이 확인된 저장 방식

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

    def layer_signature(self) -> tuple[int, str]:
        """실시간 표시용 가벼운 조회 (RPC 2회). 바뀌었을 때만 current_layer() 로 전체 정보를 읽는다."""
        layer_id = george.tv_layer_current_id()
        return layer_id, george.tv_layer_info(layer_id).name

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
    # TVPaint 환경에 따라 특정 저장 명령이 -1 을 돌려주는 경우가 있어, 여러 방식을 첫 프레임에 시험해 보고
    # 제대로 된 PNG(캔버스 크기)를 만드는 첫 번째 방식을 이후 프레임에 사용한다.
    STRATEGIES = ("ProjectSaveSequence", "SaveSequence", "SaveImage", "SaveDisplay")

    def render_instances(self, ref: LayerRef, starts: list[int], out_dir: Path) -> dict[int, Path]:
        layer = self._layer(ref)
        clip = layer.clip
        project = layer.project
        size = (project.width, project.height)
        start_frame = project.start_frame

        restore = self._snapshot_render_state(layer, clip)
        attempts: list[str] = []
        try:
            self._isolate_layer(layer, clip)
            strategy = self._pick_strategy(layer, clip, starts[0], start_frame, size, out_dir, attempts)
            if strategy is None:
                raise ToolError(self._render_diagnostics(layer, starts, out_dir, attempts))
            result: dict[int, Path] = {}
            for frame in starts:
                try:
                    result[frame] = self._render_one(strategy, layer, clip, frame, start_frame, size, out_dir)
                except Exception as exc:  # noqa: BLE001
                    attempts.append(f"{strategy} 프레임 {frame}: {exc}")
                    raise ToolError(self._render_diagnostics(layer, starts, out_dir, attempts)) from exc
            return result
        finally:
            restore()

    @property
    def render_method(self) -> str | None:
        return self._strategy

    def _snapshot_render_state(self, layer: Layer, clip):
        """렌더 전에 바꾸는 모든 설정을 저장하고, 되돌리는 함수를 돌려준다 (오류가 나도 반드시 호출됨)."""

        def safe(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return None

        visibility = [(lyr, safe(lambda l=lyr: l.is_visible)) for lyr in clip.layers]
        opacity = safe(lambda: layer.opacity)
        blending = safe(lambda: layer.blending_mode)
        background = safe(george.tv_background_get)
        alpha_save = safe(george.tv_alpha_save_mode_get)
        save_mode = safe(george.tv_save_mode_get)
        frame = safe(lambda: clip.current_frame)

        def restore() -> None:
            steps = []
            if background is not None:
                steps.append(lambda: george.tv_background_set(background[0], background[1]))
            if alpha_save is not None:
                steps.append(lambda: george.tv_alpha_save_mode_set(alpha_save))
            if save_mode is not None:
                steps.append(lambda: george.tv_save_mode_set(save_mode[0], *save_mode[1]))
            if opacity is not None:
                steps.append(lambda: setattr(layer, "opacity", opacity))
            if blending is not None:
                steps.append(lambda: setattr(layer, "blending_mode", blending))
            for lyr, was_visible in visibility:
                if was_visible is not None:
                    steps.append(lambda l=lyr, v=was_visible: l.is_visible != v and setattr(l, "is_visible", v))
            if frame is not None:
                steps.append(lambda: setattr(clip, "current_frame", frame))
            steps.append(layer.make_current)
            for step in steps:
                with contextlib.suppress(Exception):
                    step()

        return restore

    @staticmethod
    def _isolate_layer(layer: Layer, clip) -> None:
        layer.opacity = 100
        layer.blending_mode = george.BlendingMode.COLOR
        for lyr in clip.layers:
            want = lyr.id == layer.id
            if lyr.is_visible != want:
                lyr.is_visible = want
        george.tv_save_mode_set(george.SaveFormat.PNG)
        george.tv_alpha_save_mode_set(george.AlphaSaveMode.NO_PREMULTIPLY)
        george.tv_background_set(george.BackgroundMode.NONE)
        layer.make_current()

    def _pick_strategy(self, layer, clip, frame, start_frame, size, out_dir, attempts) -> str | None:
        if self._strategy is not None:
            return self._strategy
        for name in self.STRATEGIES:
            try:
                self._render_one(name, layer, clip, frame, start_frame, size, out_dir / f"probe_{name}")
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{name}: 실패 ({exc})")
                continue
            attempts.append(f"{name}: 성공")
            self._strategy = name
            return name
        return None

    @staticmethod
    def _render_one(name, layer, clip, frame, start_frame, size, out_dir: Path) -> Path:
        """프레임 하나를 전용 빈 폴더에 저장하고, 생긴 PNG 한 장을 검증해서 돌려준다."""
        from PIL import Image

        target = out_dir / f"f{frame:06d}"
        target.mkdir(parents=True, exist_ok=True)
        path = target / "img.png"
        real = frame - start_frame  # TVPaint 내부 프레임 번호 (0 부터)

        clip.current_frame = frame
        layer.make_current()
        if name == "ProjectSaveSequence":
            george.tv_project_save_sequence(path, start=real, end=real)
        elif name == "SaveSequence":
            george.tv_save_sequence(path, real, real)
        elif name == "SaveImage":
            george.tv_save_image(path)
        elif name == "SaveDisplay":
            george.tv_save_display(path)
        else:
            raise ValueError(name)

        files = sorted(target.glob("*.png"))
        if len(files) != 1:
            raise RuntimeError(f"PNG {len(files)}개 생성됨")
        with Image.open(files[0]) as im:
            if im.size != size:
                raise RuntimeError(f"크기 {im.size[0]}x{im.size[1]} (캔버스 {size[0]}x{size[1]})")
            if im.mode != "RGBA":  # 팔레트/흑백/알파 없음은 픽셀 손실 가능성이 있어 쓰지 않는다
                raise RuntimeError(f"RGBA 아님 (mode={im.mode})")
        return files[0]

    @staticmethod
    def _render_diagnostics(layer: Layer, starts: list[int], out_dir: Path, attempts: list[str]) -> str:
        info = ["TVPaint에서 레이어 이미지를 저장하지 못했습니다.", "", "--- 진단 정보 (이 내용을 그대로 전달해 주세요) ---"]
        info += attempts
        probes = {
            "요청 프레임": lambda: f"{starts[:8]}{' ...' if len(starts) > 8 else ''}",
            "임시 폴더": lambda: str(out_dir),
            "레이어 start/end": lambda: f"{layer.start} / {layer.end}",
            "클립 start/end": lambda: f"{layer.clip.start} / {layer.clip.end}",
            "프로젝트 start_frame": lambda: str(layer.project.start_frame),
            "캔버스": lambda: f"{layer.project.width}x{layer.project.height}",
            "프로젝트 경로(원문)": lambda: repr(george.tv_get_project_name()),
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
