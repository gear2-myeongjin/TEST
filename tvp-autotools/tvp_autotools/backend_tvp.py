"""PyTVPaint(tvpaint-rpc 플러그인)를 통한 실제 TVPaint 백엔드.

주의: 모든 메서드는 반드시 같은 워커 스레드 하나에서만 호출해야 한다 (RPC 클라이언트가 스레드 안전하지 않음).
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Iterator

# pytvpaint 는 import 순간 TVPaint 접속을 최대 60초 기다린다. UI 가 멈추지 않도록 끄고 직접 접속한다.
os.environ.setdefault("PYTVPAINT_WS_STARTUP_CONNECT", "0")
os.environ.setdefault("PYTVPAINT_LOG_LEVEL", "WARNING")

from pytvpaint import george  # noqa: E402
from pytvpaint.george.client import rpc_client  # noqa: E402
from pytvpaint.layer import Layer  # noqa: E402
from pytvpaint.project import Project  # noqa: E402

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

    # pytvpaint 의 Layer.opacity 는 '현재 레이어'의 값을 읽는 명령을 써서, 다른 레이어가 현재일 때 엉뚱한 값을
    # 돌려줄 수 있다. 불투명도는 레이어 id 로 정보를 읽고, 쓸 때는 그 레이어를 현재로 만든 뒤 쓴다.
    @staticmethod
    def _get_opacity(layer_id: int) -> int:
        return int(george.tv_layer_info(layer_id).density)

    @staticmethod
    def _set_opacity(layer_id: int, value: int) -> None:
        george.tv_layer_set(layer_id)
        george.tv_layer_density_set(max(0, min(100, int(value))))

    def _layer(self, ref: LayerRef) -> Layer:
        layer = self._layer_cache.get(ref.id)
        if layer is None:
            layer = Layer(layer_id=ref.id)
            self._layer_cache[ref.id] = layer
        layer.make_current()
        return layer

    def layer_range(self, ref: LayerRef) -> tuple[int, int]:
        layer = self._layer(ref)
        layer.refresh()
        return layer.start, layer.end

    # ---------- 렌더 ----------
    # TVPaint 환경에 따라 특정 저장 명령이 -1 을 돌려주는 경우가 있어, 여러 방식을 첫 프레임에 시험해 보고
    # 제대로 된 PNG(캔버스 크기)를 만드는 첫 번째 방식을 이후 프레임에 사용한다.
    STRATEGIES = ("ProjectSaveSequence", "SaveSequence", "SaveImage", "SaveDisplay")

    def render_frames(self, ref: LayerRef, starts: list[int], out_dir: Path) -> dict[int, Path]:
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
            batch = self._render_batch(strategy, layer, clip, starts, start_frame, size, out_dir)
            if batch is not None:
                return batch
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
        opacity = safe(lambda: self._get_opacity(layer.id))
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
                steps.append(lambda: self._set_opacity(layer.id, opacity))
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
            # 불투명도는 되돌린 뒤 실제 값을 다시 읽어 확인하고, 다르면 한 번 더 맞춘다
            if opacity is not None:
                with contextlib.suppress(Exception):
                    if self._get_opacity(layer.id) != opacity:
                        self._set_opacity(layer.id, opacity)
                        layer.make_current()

        return restore

    def _isolate_layer(self, layer: Layer, clip) -> None:
        self._set_opacity(layer.id, 100)
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
    def _render_batch(name, layer, clip, starts, start_frame, size, out_dir: Path) -> dict[int, Path] | None:
        """구간 저장이 되는 방식이면 첫~마지막 인스턴스 구간을 명령 한 번으로 저장한다 (프레임마다 왕복하지 않음).

        파일 수나 크기가 기대와 다르면 None 을 돌려주고, 호출한 쪽은 프레임별 저장으로 돌아간다.
        """
        import re

        from PIL import Image

        if name not in ("ProjectSaveSequence", "SaveSequence"):
            return None
        lo, hi = min(starts), max(starts)
        target = out_dir / "batch"
        target.mkdir(parents=True, exist_ok=True)
        path = target / "img.png"
        try:
            layer.make_current()
            if name == "ProjectSaveSequence":
                george.tv_project_save_sequence(path, start=lo - start_frame, end=hi - start_frame)
            else:
                george.tv_save_sequence(path, lo - start_frame, hi - start_frame)
        except Exception:  # noqa: BLE001
            return None

        def number(p: Path) -> int:
            digits = re.findall(r"\d+", p.stem)
            return int(digits[-1]) if digits else -1

        files = sorted(target.glob("*.png"), key=lambda p: (number(p), p.name))
        if len(files) != hi - lo + 1:
            return None
        by_frame = {lo + i: f for i, f in enumerate(files)}
        try:
            for frame in starts:
                with Image.open(by_frame[frame]) as im:
                    if im.size != size or im.mode != "RGBA":
                        return None
        except Exception:  # noqa: BLE001
            return None
        return {frame: by_frame[frame] for frame in starts}

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

    def _load_images_as_layer(self, clip, images: list[Path], work: Path, tag: str) -> int:
        """PNG 들을 TVPaint 시퀀스 불러오기 한 번으로 새 레이어(각 1콤마)로 만든다. 새 레이어 id 를 돌려준다."""
        seq_dir = work / f"seq_{tag}"
        seq_dir.mkdir(parents=True, exist_ok=True)
        first = None
        for n, src in enumerate(images):
            dst = seq_dir / f"img_{n:05d}.png"
            shutil.copyfile(src, dst)
            first = first or dst
        if first is None:
            raise ToolError("불러올 이미지가 없습니다.")

        before = {lyr.id for lyr in clip.layers}
        # preload: 이미지를 TVPaint 메모리에 올려 임시 파일과의 연결을 끊는다 (작업 후 임시 폴더를 지우므로 필수)
        george.tv_load_sequence(first, preload=True)
        # 반환값이 문서마다 '이미지 수' / '레이어 id' 로 달라서 믿지 않고, 새로 생긴 레이어를 직접 찾는다
        created = [lyr for lyr in clip.layers if lyr.id not in before]
        if len(created) != 1:
            raise ToolError(f"시퀀스 불러오기로 생긴 레이어를 찾지 못했습니다 (새 레이어 {len(created)}개).")
        layer = created[0]
        layer.refresh()
        frames = layer.end - layer.start + 1
        if frames != len(images):
            raise ToolError(f"시퀀스 불러오기 결과가 다릅니다 (요청 {len(images)}장, 결과 {frames}프레임).")
        return layer.id

    def replace_layer_frames(self, ref: LayerRef, images: list[Path], work: Path) -> int:
        """원본 레이어를 '남길 그림만 1장씩' 들어간 새 레이어로 교체한다.

        TVPaint 의 잘라내기/삭제는 환경설정(빈 칸 유지 등)에 따라 프레임을 지우지 않고 내용만 비우는 경우가 있어,
        삭제 대신 새 레이어를 만들어 같은 이름·위치·설정으로 바꿔 끼운다. 전체가 하나의 실행취소 묶음 안에서 실행된다.
        """
        old = self._layer(ref)
        clip = old.clip
        old.refresh()
        props = {
            "name": old.name,
            "start": old.start,
            "position": old.position,
            "opacity": self._get_opacity(old.id),
            "blending_mode": old.blending_mode,
            "pre_behavior": old.pre_behavior,
            "post_behavior": old.post_behavior,
            "is_visible": old.is_visible,
        }
        color_index = None
        with contextlib.suppress(Exception):
            color_index = george.tv_layer_color_get(old.id)

        clip.current_frame = props["start"]
        new_id = self._load_images_as_layer(clip, images, work, "clean")
        new = Layer(layer_id=new_id, clip=clip)

        george.tv_layer_kill(old.id)
        george.tv_layer_rename(new_id, props["name"])
        new.refresh()
        if new.start != props["start"]:
            new.shift(props["start"])
        for attr in ("blending_mode", "pre_behavior", "post_behavior", "is_visible"):
            with contextlib.suppress(Exception):
                setattr(new, attr, props[attr])
        with contextlib.suppress(Exception):
            self._set_opacity(new_id, props["opacity"])
        if color_index is not None:
            with contextlib.suppress(Exception):
                george.tv_layer_color_set(new_id, color_index)
        with contextlib.suppress(Exception):
            new.position = props["position"]
        new.make_current()
        self._layer_cache = {new_id: new}

        new.refresh()
        return new.end - new.start + 1

    # ---------- 크롭 프로젝트 ----------
    def build_cropped_project(self, spec: CropSpec, work: Path) -> str:
        src_layer = self._layer(spec.source)
        src_project = src_layer.project
        src_start = src_layer.start

        fps = src_project.fps
        par = src_project.pixel_aspect_ratio
        field = src_project.field_order
        start_frame = src_project.start_frame
        props = {
            "opacity": self._get_opacity(src_layer.id),
            "blending_mode": src_layer.blending_mode,
            "pre_behavior": src_layer.pre_behavior,
            "post_behavior": src_layer.post_behavior,
        }

        new_path = self._new_project_path(src_project, "Crop")
        src_id = george.tv_project_current_id()

        try:
            project = Project.new(new_path, spec.width, spec.height, par, fps, field, start_frame)
            project.make_current()
            clip = project.current_clip
            defaults = [lyr.id for lyr in clip.layers]

            clip.current_frame = start_frame
            layer_id = self._load_images_as_layer(clip, spec.images, work, "crop")
            for lid in defaults:
                if lid != layer_id:
                    with contextlib.suppress(Exception):
                        george.tv_layer_kill(lid)
            george.tv_layer_rename(layer_id, "Crop")  # 한글 이름이 깨지므로 고정 이름 사용
            layer = Layer(layer_id=layer_id, clip=clip)
            layer.make_current()
            layer.refresh()

            # 콤마 복원: 뒤에서부터 늘려야 앞 그림 위치가 밀리지 않는다
            base = layer.start
            for n in reversed(range(len(spec.instances))):
                length = spec.instances[n].length
                if length > 1:
                    george.tv_exposure_set(base + n - start_frame, length)

            layer.refresh()
            if layer.start != src_start:
                layer.shift(src_start)
            for attr, value in props.items():
                with contextlib.suppress(Exception):
                    if attr == "opacity":
                        self._set_opacity(layer.id, value)
                    else:
                        setattr(layer, attr, value)
            clip.current_frame = src_start

            layer.refresh()
            expected = sum(i.length for i in spec.instances)
            actual = layer.end - layer.start + 1
            if expected != actual:
                raise ToolError(
                    "새 프로젝트의 프레임 수가 원본과 다릅니다.\n"
                    f"원본 {expected}프레임 / 결과 {actual}프레임.\n"
                    "새 프로젝트는 열린 채로 두었으니 확인해 주세요. 원본은 변경되지 않았습니다."
                )
            return project.name
        except ToolError:
            raise
        except Exception:
            with contextlib.suppress(Exception):
                current = george.tv_project_current_id()
                if current != src_id:
                    george.tv_project_close(current)
            with contextlib.suppress(Exception):
                george.tv_project_select(src_id)
            raise

    def build_atlas_project(self, source: LayerRef, atlas_png: Path, layout, work: Path) -> str:
        src_layer = self._layer(source)
        src_project = src_layer.project
        fps = src_project.fps
        par = src_project.pixel_aspect_ratio
        field = src_project.field_order
        start_frame = src_project.start_frame

        new_path = self._new_project_path(src_project, "Atlas")
        src_id = george.tv_project_current_id()
        try:
            project = Project.new(new_path, layout.width, layout.height, par, fps, field, start_frame)
            project.make_current()
            if (project.width, project.height) != (layout.width, layout.height):
                raise ToolError(
                    f"TVPaint가 {layout.width}×{layout.height} 크기의 프로젝트를 만들지 못했습니다 "
                    f"(만들어진 크기 {project.width}×{project.height}). 캔버스 최대 크기를 넘었을 수 있습니다."
                )
            clip = project.current_clip
            defaults = [lyr.id for lyr in clip.layers]
            clip.current_frame = start_frame
            layer_id = self._load_images_as_layer(clip, [atlas_png], work, "atlas")
            for lid in defaults:
                if lid != layer_id:
                    with contextlib.suppress(Exception):
                        george.tv_layer_kill(lid)
            george.tv_layer_rename(layer_id, "Atlas")
            Layer(layer_id=layer_id, clip=clip).make_current()
            clip.current_frame = start_frame
            return project.name
        except Exception:
            with contextlib.suppress(Exception):
                current = george.tv_project_current_id()
                if current != src_id:
                    george.tv_project_close(current)
            with contextlib.suppress(Exception):
                george.tv_project_select(src_id)
            raise

    # ---------- 새 프로젝트 이름/위치 ----------
    @staticmethod
    def _desktop_dir() -> Path:
        """바탕화면 경로. 한글이 섞인 경로(예: OneDrive 의 '바탕 화면')는 TVPaint 에서 깨지므로 피한다."""
        candidates: list[Path] = []
        if os.name == "nt":
            with contextlib.suppress(Exception):
                import ctypes
                from ctypes import wintypes

                class GUID(ctypes.Structure):
                    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

                desktop = GUID(0xB4BFCC3A, 0xDB2C, 0x424C, (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
                out = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(desktop), 0, None, ctypes.byref(out)) == 0:
                    candidates.append(Path(out.value))
                    ctypes.windll.ole32.CoTaskMemFree(out)
        candidates += [Path.home() / "Desktop", Path.home()]
        for c in candidates:
            if str(c).isascii() and c.is_dir():
                return c
        return Path.home()

    def _new_project_path(self, src_project: Project, prefix: str) -> Path:
        """Crop_01, Crop_02 ... 처럼 겹치지 않는 번호로 만든다.

        저장 위치: 원본이 저장돼 있고 경로가 영문이면 원본 폴더, 아니면 바탕화면.
        겹침 확인: 저장 폴더의 파일 + TVPaint 에 열려 있는 프로젝트 이름.
        """

        def clean(text: str) -> str:
            return text.strip().strip('"').strip("'").strip()

        raw = ""
        with contextlib.suppress(Exception):
            raw = clean(george.tv_get_project_name())
        src = Path(raw) if raw else None
        if (
            src is not None
            and src.suffix.lower() in (".tvpp", ".tvp")
            and str(src.parent).isascii()
            and src.parent.is_dir()
        ):
            folder = src.parent
        else:
            folder = self._desktop_dir()

        taken: set[str] = set()
        with contextlib.suppress(Exception):
            for project_id in Project.open_projects_ids():
                with contextlib.suppress(Exception):
                    taken.add(Path(clean(str(george.tv_project_info(project_id).path))).stem.lower())
        n = 1
        while True:
            name = f"{prefix}_{n:02d}"
            if name.lower() not in taken and not (folder / f"{name}.tvpp").exists():
                return folder / f"{name}.tvpp"
            n += 1
