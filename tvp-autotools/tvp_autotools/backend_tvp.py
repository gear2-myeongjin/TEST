"""PyTVPaint(tvpaint-rpc 플러그인)를 통한 실제 TVPaint 백엔드.

주의: 모든 메서드는 반드시 같은 워커 스레드 하나에서만 호출해야 한다 (RPC 클라이언트가 스레드 안전하지 않음).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
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


# ---------- 작업 기록 (중단 복구용) ----------
# 렌더링 전에 바꾸는 설정의 원래 값을 적어 두고, 정상적으로 되돌리면 지운다.
# EXE 를 다시 켰을 때 파일이 남아 있으면 지난 작업이 중간에 끊긴 것이다.
JOURNAL_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TvpAAM" / "recovery.json"


def _journal_write(data: dict) -> None:
    import json

    with contextlib.suppress(Exception):
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOURNAL_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(JOURNAL_PATH)


def _journal_clear() -> None:
    with contextlib.suppress(Exception):
        JOURNAL_PATH.unlink()


def _journal_load() -> dict | None:
    import json

    with contextlib.suppress(Exception):
        return json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
    return None


def _clean_path(text: str) -> str:
    return str(text).strip().strip('"').strip("'").strip()


class TVPaintBackend:
    def __init__(self) -> None:
        self._layer_cache: dict[int, Layer] = {}
        self._strategy: str | None = None  # 이 TVPaint 에서 동작이 확인된 저장 방식
        self.opacity_trace: list[str] = []  # 불투명도 읽기/쓰기 기록 (검증 실패 시 진단용)
        self.opacity_check: str | None = None

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

    # 불투명도(George 의 density) 처리 — 자동 보정 방식
    # 실기에서 확인된 사실: 레이어 정보(tv_LayerInfo)의 불투명도 칸이 화면 값과 다를 수 있고, 쓰기 단위도
    # 버전에 따라 0~1 / 0~100 으로 다르다. 그래서 첫 사용 때 '임시 프로젝트'에서 쓰기 2가지 × 읽기 2가지를
    # 직접 시험해, 쓴 값이 그대로 읽히는 조합만 사용한다. 맞는 조합이 없으면 불투명도를 아예 건드리지 않는다.
    _calibration: dict | None = None

    @staticmethod
    def _raw_info_density(layer_id: int) -> str | None:
        from pytvpaint.george.client import send_cmd

        with contextlib.suppress(Exception):
            return str(send_cmd("tv_LayerInfo", layer_id)).split()[2]
        return None

    @staticmethod
    def _raw_current_density(layer_id: int) -> str | None:
        from pytvpaint.george.client import send_cmd

        with contextlib.suppress(Exception):
            george.tv_layer_set(layer_id)
            return str(send_cmd("tv_LayerDensity")).strip()
        return None

    @staticmethod
    def _to_float(raw: str | None) -> float | None:
        with contextlib.suppress(Exception):
            return float(str(raw).strip().strip('"'))
        return None

    @staticmethod
    def _send_density(layer_id: int, value: float) -> None:
        from pytvpaint.george.client import send_cmd

        george.tv_layer_set(layer_id)
        text = str(int(value)) if value == int(value) else f"{value:.6f}".rstrip("0").rstrip(".")
        send_cmd("tv_LayerDensity", text)

    def _calibrate(self) -> dict:
        if self._calibration is not None:
            return self._calibration
        log: list[str] = []
        result = {"reader": None, "scale": None, "destructive": False, "log": log}
        src_pid = src_lid = None
        with contextlib.suppress(Exception):
            src_pid = george.tv_project_current_id()
            src_lid = george.tv_layer_current_id()
        tmp_pid = None
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="tvpaam_cal_"))
            george.tv_project_new(tmp_dir / "calib.tvpp", 8, 8, 1.0, 24.0, george.FieldOrder.NONE, 1)
            tmp_pid = george.tv_project_current_id()
            if tmp_pid == src_pid:
                raise RuntimeError("임시 프로젝트가 만들어지지 않았습니다.")
            lid = george.tv_layer_current_id()

            readers = {
                "LayerInfo": lambda: self._to_float(self._raw_info_density(lid)),
                "LayerDensity": lambda: self._to_float(self._raw_current_density(lid)),
            }
            ok: dict[tuple[str, str], bool] = {}
            destructive = False
            for scale, value in (("fraction", 0.25), ("percent", 25.0), ("fraction", 0.75), ("percent", 75.0)):
                try:
                    self._send_density(lid, value)
                except Exception as exc:  # noqa: BLE001
                    log.append(f"쓰기 {value} ({scale}): 실패 {exc}")
                    ok[("LayerInfo", scale)] = ok[("LayerDensity", scale)] = False
                    continue
                raw_i = self._raw_info_density(lid)
                raw_d1 = self._raw_current_density(lid)
                raw_d2 = self._raw_current_density(lid)  # 두 번 읽어 읽기 자체가 값을 바꾸는지 확인
                log.append(f"쓰기 {value} ({scale}) → LayerInfo={raw_i!r}, LayerDensity={raw_d1!r} / 재확인 {raw_d2!r}")
                got_d = self._to_float(raw_d1)
                if raw_d1 != raw_d2:
                    destructive = True  # 이 TVPaint 는 값 없는 tv_LayerDensity 가 값을 돌려준 뒤 0 으로 만든다
                for name, got in (("LayerInfo", self._to_float(raw_i)), ("LayerDensity", got_d)):
                    match = got is not None and abs(got - value) < 1e-4
                    ok[(name, scale)] = ok.get((name, scale), True) and match
            for name in ("LayerInfo", "LayerDensity"):  # 부작용 없는 LayerInfo 를 먼저 고려
                for scale in ("fraction", "percent"):
                    if ok.get((name, scale)):
                        result["reader"], result["scale"] = name, scale
                        break
                if result["reader"]:
                    break
            result["destructive"] = destructive and result["reader"] == "LayerDensity"
            if result["reader"]:
                self._readers = readers
            log.append(
                f"선택: 읽기={result['reader']}, 단위={result['scale']}"
                + (" (읽은 직후 원래 값으로 되돌림)" if result.get("destructive") else "")
            )
        except Exception as exc:  # noqa: BLE001
            log.append(f"보정 실패: {exc}")
        finally:
            if tmp_pid is not None:
                with contextlib.suppress(Exception):
                    george.tv_project_close(tmp_pid)
            if src_pid is not None:
                with contextlib.suppress(Exception):
                    george.tv_project_select(src_pid)
            if src_lid is not None:
                with contextlib.suppress(Exception):
                    george.tv_layer_set(src_lid)
        self._calibration = result
        return result

    @property
    def opacity_mode(self) -> str:
        cal = self._calibration
        if cal is None:
            return "미확인"
        if cal["reader"] is None:
            return "안전 모드 (불투명도를 건드리지 않음)"
        mode = f"{cal['reader']} 읽기, {'0~1' if cal['scale'] == 'fraction' else '0~100'} 단위"
        return mode + (", 읽은 직후 복원" if cal.get("destructive") else "")

    def _read_density(self, layer_id: int) -> float | None:
        cal = self._calibrate()
        if cal["reader"] == "LayerInfo":
            return self._to_float(self._raw_info_density(layer_id))
        if cal["reader"] == "LayerDensity":
            value = self._to_float(self._raw_current_density(layer_id))
            if cal.get("destructive") and value is not None:
                # 읽기가 레이어를 0% 로 만들었으므로, 읽은 값을 즉시 다시 써서 원래대로 돌린다
                self._send_density(layer_id, value)
            return value
        return None  # 안전 모드: 값을 모르면 쓰지도 않는다

    def _write_density(self, layer_id: int, value: float) -> None:
        if self._calibrate()["reader"] is None:
            return
        self._send_density(layer_id, value)

    def _full_density(self, layer_id: int, current: float) -> float:
        return 1.0 if self._calibrate()["scale"] == "fraction" else 100.0

    def _percent(self, value: float | None) -> str:
        if value is None:
            return "?"
        return f"{value * 100:.0f}%" if (self._calibration or {}).get("scale") == "fraction" else f"{value:.0f}%"

    def _get_opacity(self, layer_id: int) -> float:
        value = self._read_density(layer_id)
        self._trace(f"읽기 layer {layer_id}: {value}")
        if value is None:
            raise ToolError("레이어 불투명도를 읽지 못했습니다.")
        return value

    def _set_opacity(self, layer_id: int, value: float) -> None:
        self._write_density(layer_id, value)
        self._trace(f"쓰기 layer {layer_id}: {value} -> 확인 {self._read_density(layer_id)}")

    @staticmethod
    def _same(a: float | None, b: float | None) -> bool:
        return a is not None and b is not None and abs(a - b) < 1e-4

    def read_layer_opacity(self, ref: LayerRef) -> float | None:
        self.opacity_trace = []
        self.opacity_check = None
        layer = self._layer(ref)
        try:
            return self._get_opacity(layer.id)
        except Exception:  # noqa: BLE001
            return None
        finally:
            with contextlib.suppress(Exception):
                layer.make_current()

    def _trace(self, text: str) -> None:
        self.opacity_trace.append(text)

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
            self._isolate_layer(layer, clip, restore.opacity)
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
        self._journal_render_state(layer, visibility, opacity, blending, background, alpha_save, save_mode)

        def restore() -> None:
            steps = []
            if background is not None:
                steps.append(("복구: 배경 설정", lambda: george.tv_background_set(background[0], background[1])))
            if alpha_save is not None:
                steps.append(("복구: 알파 저장 방식", lambda: george.tv_alpha_save_mode_set(alpha_save)))
            if save_mode is not None:
                steps.append(("복구: 저장 형식", lambda: george.tv_save_mode_set(save_mode[0], *save_mode[1])))
            if opacity is not None:
                steps.append(("복구: 불투명도", lambda: self._set_opacity(layer.id, opacity)))
            if blending is not None:
                steps.append(("복구: 블렌딩 모드", lambda: setattr(layer, "blending_mode", blending)))
            vis_steps = [
                (lambda l=lyr, v=was_visible: l.is_visible != v and setattr(l, "is_visible", v))
                for lyr, was_visible in visibility
                if was_visible is not None
            ]
            steps.append(("복구: 레이어 표시 여부", lambda: [f() for f in vis_steps]))
            if frame is not None:
                steps.append(("복구: 현재 프레임", lambda: setattr(clip, "current_frame", frame)))
            steps.append(("복구: 원본 레이어 선택", layer.make_current))
            all_ok = True
            for _label, step in steps:
                try:
                    step()
                except Exception:  # noqa: BLE001
                    all_ok = False
            # 불투명도는 되돌린 뒤 실제 값을 다시 읽어 확인하고, 다르면 한 번 더 맞춘다
            if opacity is not None:
                try:
                    if not self._same(self._read_density(layer.id), opacity):
                        self._set_opacity(layer.id, opacity)
                        layer.make_current()
                except Exception:  # noqa: BLE001
                    all_ok = False
            if all_ok:
                _journal_clear()  # 전부 되돌렸으면 기록을 지운다. 하나라도 실패하면 다음 실행 때 복구를 제안한다

        restore.opacity = opacity
        return restore

    def _isolate_layer(self, layer: Layer, clip, opacity: int | None) -> None:
        # 렌더 중에만 100% 로 (TVPaint 단위로). 이미 100% 면 건드리지 않는다.
        if opacity is not None:
            full = self._full_density(layer.id, opacity)
            if not self._same(opacity, full):
                self._set_opacity(layer.id, full)
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

    # ---------- 중단 복구 ----------
    operation_label: str = ""

    def _project_identity(self) -> tuple[str, str]:
        path = name = ""
        with contextlib.suppress(Exception):
            path = _clean_path(george.tv_get_project_name())
        with contextlib.suppress(Exception):
            name = Path(path).stem if path.lower().endswith((".tvpp", ".tvp")) else ""
        return path, name

    def _journal_render_state(self, layer, visibility, opacity, blending, background, alpha_save, save_mode) -> None:
        bg_args = None
        if background is not None:
            mode, color = background
            bg_args = [mode.value]
            if isinstance(color, tuple):
                for c in color:
                    bg_args += [c.r, c.g, c.b]
            elif color is not None:
                bg_args += [color.r, color.g, color.b]
        path, name = self._project_identity()
        position = None
        with contextlib.suppress(Exception):
            position = layer.position
        _journal_write(
            {
                "stage": "render",
                "operation": self.operation_label,
                "time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "project_path": path,
                "project_name": name,
                "layer": {"id": layer.id, "name": layer.name, "position": position},
                "opacity": opacity,
                "opacity_scale": (self._calibration or {}).get("scale"),
                "blending": blending.value if blending is not None else None,
                "background": bg_args,
                "alpha_save": alpha_save.value if alpha_save is not None else None,
                "save_mode": ([save_mode[0].value] + list(save_mode[1])) if save_mode is not None else None,
                "visibility": [
                    {"id": lyr.id, "name": lyr.name, "visible": vis} for lyr, vis in visibility if vis is not None
                ],
            }
        )

    @staticmethod
    def pending_recovery() -> dict | None:
        return _journal_load()

    @staticmethod
    def discard_recovery() -> None:
        _journal_clear()

    @staticmethod
    def describe_recovery(j: dict) -> str:
        op = j.get("operation") or "작업"
        where = j.get("project_name") or "저장되지 않은 프로젝트"
        layer = (j.get("layer") or {}).get("name", "?")
        if j.get("stage") == "replace":
            return (
                f"지난 '{op}'이(가) 레이어를 바꿔 끼우는 도중에 끊겼습니다 ({j.get('time', '')}).\n"
                f"프로젝트: {where} / 레이어: {layer}\n\n"
                "원본 레이어가 지워졌거나 새 레이어가 덜 만들어졌을 수 있습니다.\n"
                "TVPaint에서 실행취소(Ctrl+Z)로 작업 전 상태로 되돌려 주세요."
            )
        scale = j.get("opacity_scale")
        op_val = j.get("opacity")
        if op_val is None:
            op_text = "?"
        else:
            op_text = f"{op_val * 100:.0f}%" if scale == "fraction" else f"{op_val:.0f}%"
        return (
            f"지난 '{op}'이(가) 끝나기 전에 끊겼습니다 ({j.get('time', '')}).\n"
            f"프로젝트: {where} / 레이어: {layer}\n\n"
            "다음 설정이 작업 중 상태로 남아 있을 수 있습니다.\n"
            f"레이어 불투명도(원래 {op_text}), 블렌딩 모드(원래 {j.get('blending')}), "
            "다른 레이어 표시 여부, 배경, 저장 형식\n\n"
            "[진행]을 누르면 원래 값으로 되돌립니다. 해당 프로젝트가 TVPaint에서 선택돼 있어야 합니다."
        )

    def recover(self, j: dict) -> str:
        """기록된 원래 설정으로 되돌린다. 성공하면 기록을 지우고, 대상을 못 찾으면 기록을 남긴 채 안내한다."""
        from pytvpaint.george.client import send_cmd

        if j.get("stage") == "replace":
            _journal_clear()
            return "기록을 지웠습니다. TVPaint에서 Ctrl+Z로 되돌렸는지 확인해 주세요."

        path, _ = self._project_identity()
        saved_path = j.get("project_path") or ""
        if saved_path.lower().endswith((".tvpp", ".tvp")) and path != saved_path:
            raise ToolError(
                f"지금 선택된 프로젝트가 기록과 다릅니다.\n기록된 프로젝트: {saved_path}\n\n"
                "TVPaint에서 해당 프로젝트를 선택한 뒤 EXE를 다시 실행해 주세요. 기록은 남겨 두었습니다."
            )

        clip = Layer.current_layer().clip
        layers = list(clip.layers)
        by_id = {lyr.id: lyr for lyr in layers}

        def find(entry: dict):
            lyr = by_id.get(entry.get("id"))
            if lyr is not None and lyr.name == entry.get("name"):
                return lyr
            same = [l for l in layers if l.name == entry.get("name")]
            return same[0] if len(same) == 1 else None

        target = find(j.get("layer") or {})
        if target is None:
            raise ToolError(
                "기록된 레이어를 지금 클립에서 찾지 못했습니다.\n"
                "해당 클립을 선택한 뒤 EXE를 다시 실행하거나, 아래 값으로 직접 맞춰 주세요.\n\n"
                + self.describe_recovery(j).split("\n\n")[1]
            )

        done, failed = [], []

        def attempt(label: str, fn) -> None:
            try:
                fn()
                done.append(label)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{label} ({exc})")

        if j.get("background"):
            attempt("배경", lambda: send_cmd("tv_Background", *j["background"]))
        if j.get("alpha_save"):
            attempt("알파 저장 방식", lambda: send_cmd("tv_AlphaSaveMode", j["alpha_save"]))
        if j.get("save_mode"):
            attempt("저장 형식", lambda: send_cmd("tv_SaveMode", *j["save_mode"]))
        if j.get("blending"):
            attempt("블렌딩 모드", lambda: send_cmd("tv_LayerBlendingMode", target.id, j["blending"]))
        if j.get("opacity") is not None:
            attempt("불투명도", lambda: self._set_opacity(target.id, j["opacity"]))
        for entry in j.get("visibility") or []:
            lyr = find(entry)
            if lyr is not None:
                attempt(f"표시 여부({lyr.name})", lambda l=lyr, v=entry["visible"]: george.tv_layer_display_set(l.id, v))
        with contextlib.suppress(Exception):
            target.make_current()

        if failed:
            return "일부만 복구했습니다.\n복구 실패: " + ", ".join(failed) + "\n\n기록은 남겨 두었습니다."
        _journal_clear()
        return "복구했습니다: " + ", ".join(done)

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
            "opacity": self._read_density(old.id),
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

        path, name = self._project_identity()
        _journal_write(
            {
                "stage": "replace",
                "operation": self.operation_label,
                "time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "project_path": path,
                "project_name": name,
                "layer": {"id": old.id, "name": props["name"]},
            }
        )
        try:
            george.tv_layer_kill(old.id)
            george.tv_layer_rename(new_id, props["name"])
            new.refresh()
            if new.start != props["start"]:
                new.shift(props["start"])
            for attr in ("blending_mode", "pre_behavior", "post_behavior", "is_visible"):
                with contextlib.suppress(Exception):
                    setattr(new, attr, props[attr])
            if props["opacity"] is not None:
                with contextlib.suppress(Exception):
                    if not self._same(self._read_density(new_id), props["opacity"]):
                        self._set_opacity(new_id, props["opacity"])
            if color_index is not None:
                with contextlib.suppress(Exception):
                    george.tv_layer_color_set(new_id, color_index)
            with contextlib.suppress(Exception):
                new.position = props["position"]
            new.make_current()
            self._layer_cache = {new_id: new}

        except Exception as exc:
            _journal_clear()  # 프로그램이 살아 있으므로 지금 바로 안내한다 (다음 실행 때 다시 묻지 않음)
            raise ToolError(
                f"레이어를 바꿔 끼우는 중 오류가 났습니다 ({exc}).\n"
                "TVPaint에서 실행취소(Ctrl+Z)로 작업 전 상태로 되돌려 주세요."
            ) from exc
        new.refresh()
        _journal_clear()
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
            "opacity": self._read_density(src_layer.id),
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
                if attr == "opacity":
                    continue  # 마지막 검증 단계에서 처리
                with contextlib.suppress(Exception):
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
            new_id = george.tv_project_current_id()
            self.opacity_check = self._verify_opacity(
                src_id, src_layer.id, new_id, layer.id, spec.source_opacity
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

    def _verify_opacity(self, src_pid, src_lid, new_pid, new_lid, expected) -> str | None:
        """원본 레이어와 Crop 레이어의 불투명도를 작업 시작 전 값(TVPaint 단위)과 맞추고 다시 읽어 확인한다."""
        if expected is None:
            if (self._calibration or {}).get("reader") is None:
                return None  # 안전 모드: 불투명도를 건드리지 않았으므로 확인할 것도 없다
            return "작업 시작 전 불투명도를 읽지 못해 확인을 건너뛰었습니다."
        problems = []
        for pid, lid, label in ((src_pid, src_lid, "원본"), (new_pid, new_lid, "Crop")):
            with contextlib.suppress(Exception):
                george.tv_project_select(pid)
            value = self._read_density(lid)
            self._trace(f"{label} 확인 전: {value} (기대 {expected})")
            if not self._same(value, expected):
                with contextlib.suppress(Exception):
                    self._set_opacity(lid, expected)
                value = self._read_density(lid)
                if not self._same(value, expected):
                    problems.append(f"{label} 레이어: 기대 {self._percent(expected)}, 실제 {self._percent(value)}")
        with contextlib.suppress(Exception):
            george.tv_project_select(new_pid)
            george.tv_layer_set(new_lid)
        if problems:
            return "\n".join(problems + ["--- 기록 ---"] + self.opacity_trace[-30:])
        return None

    def build_atlas_project(self, source: LayerRef, atlas_png: Path, layout, work: Path) -> str:
        src_layer = self._layer(source)
        src_project = src_layer.project
        fps = src_project.fps
        par = src_project.pixel_aspect_ratio
        field = src_project.field_order
        start_frame = src_project.start_frame

        src_opacity = self._read_density(src_layer.id)  # Atlas 레이어도 원본 불투명도를 따른다
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
            if src_opacity is not None:
                with contextlib.suppress(Exception):
                    self._set_opacity(layer_id, src_opacity)
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
