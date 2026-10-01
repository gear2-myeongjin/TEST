"""TVPaint 11.6 톤의 작은 보조 패널 UI."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Callable

from tvp_autotools import installer
from tvp_autotools.core import MODE_CANCEL, MODE_SCALE, parse_nonneg_int
from tvp_autotools.ops import (
    AtlasPlan,
    CleanPlan,
    CropPlan,
    LayerRef,
    ToolError,
    analyze_atlas,
    analyze_compact,
    analyze_crop,
    apply_compact,
    build_atlas,
    build_crop,
)
from tvp_autotools.worker import Worker

# TVPaint 11.6 계열: 짙은 회색 패널, 낮은 대비 버튼, 청회색 선택 강조
C = {
    "window": "#2e2e2e",
    "panel": "#3a3a3a",
    "panel_edge": "#262626",
    "field": "#262626",
    "button": "#4b4b4b",
    "button_hover": "#575757",
    "button_down": "#404040",
    "button_edge": "#2a2a2a",
    "text": "#d9d9d9",
    "text_dim": "#8f8f8f",
    "accent": "#4a78a8",
    "accent_hover": "#5687ba",
    "ok": "#6fae6a",
    "bad": "#c0605a",
    "warn": "#d4a24c",
    "progress_bg": "#262626",
}
import os as _os

FONT = _os.environ.get("TVP_AT_FONT", "Malgun Gothic")
F_SMALL = (FONT, 8)
F_BODY = (FONT, 9)
F_LAYER = (FONT, 11, "bold")
F_BUTTON = (FONT, 10)

APP_TITLE = "TVPaint Auto Crop & Atlas Maker"
WORK_WARNING = "작업이 끝날 때까지 TVPaint를 조작하지 마세요. 결과가 잘못되거나 작업 내용이 사라질 수 있습니다."
NAME_WARNING = (
    "레이어 이름에 영문이 아닌 글자가 있어, 작업 후 레이어 이름이 깨질 수 있습니다.\n"
    "레이어 이름을 영문으로 바꾼 뒤 실행하는 것을 권장합니다."
)

POLL_MS = 300  # 가벼운 조회(레이어 id, 이름)만 하므로 짧게 잡는다


class FlatButton(tk.Label):
    def __init__(self, master, text: str, command: Callable[[], None], primary: bool = False, font=F_BUTTON, pady=8):
        self._base = C["accent"] if primary else C["button"]
        self._hover = C["accent_hover"] if primary else C["button_hover"]
        super().__init__(
            master, text=text, bg=self._base, fg=C["text"], font=font, pady=pady, padx=12,
            cursor="hand2", highlightthickness=1, highlightbackground=C["button_edge"],
        )
        self._command = command
        self._enabled = True
        self.bind("<Enter>", lambda e: self._enabled and self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self._enabled and self.config(bg=self._base))
        self.bind("<ButtonPress-1>", lambda e: self._enabled and self.config(bg=C["button_down"]))
        self.bind("<ButtonRelease-1>", self._release)

    def _release(self, event) -> None:
        if not self._enabled:
            return
        self.config(bg=self._hover)
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.config(fg=C["text"] if enabled else C["text_dim"], bg=self._base, cursor="hand2" if enabled else "arrow")

    # ---- 작업 중 가위질 애니메이션: 글자를 숨기고 버튼 중앙에서 열린/닫힌 가위를 번갈아 보여준다 ----
    def start_cutting(self, frames: list, interval_ms: int = 500) -> None:
        if not frames or getattr(self, "_anim_job", None) is not None:
            return
        hl = int(self.cget("highlightthickness"))
        self._saved = {k: self.cget(k) for k in ("text", "padx", "pady", "width", "height")}
        w, h = self.winfo_width(), self.winfo_height()
        # 그림만 넣으면 라벨 크기가 바뀌므로, 지금 크기를 픽셀로 고정해 버튼이 줄어들지 않게 한다
        self.config(text="", padx=0, pady=0, width=max(1, w - 2 * hl), height=max(1, h - 2 * hl), compound="center")
        self._anim_frames = frames
        self._anim_index = 0
        self._anim_interval = interval_ms
        self._anim_step()

    def _anim_step(self) -> None:
        self.config(image=self._anim_frames[self._anim_index % len(self._anim_frames)])
        self._anim_index += 1
        self._anim_job = self.after(self._anim_interval, self._anim_step)

    def stop_cutting(self) -> None:
        job = getattr(self, "_anim_job", None)
        if job is None:
            return
        self.after_cancel(job)
        self._anim_job = None
        self.config(image="", **self._saved)


class Progress(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=4, bg=C["progress_bg"], highlightthickness=0)
        self._bar = self.create_rectangle(0, 0, 0, 4, fill=C["accent"], width=0)

    def set(self, value: float) -> None:
        self.coords(self._bar, 0, 0, self.winfo_width() * max(0.0, min(1.0, value)), 4)


class App:
    def __init__(self, backend_factory: Callable[[], object]) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.minsize(470, 0)  # 긴 제목이 제목 표시줄에서 잘리지 않도록
        self.root.configure(bg=C["window"])
        self.root.resizable(False, False)
        self._set_icon()

        self.worker = Worker()
        self.backend = None
        self._backend_factory = backend_factory
        self.connected = False
        self.layer: LayerRef | None = None
        self.running = False
        self._poll_pending = False
        self._reconnect_scheduled = False

        self._build()
        self._scissors = _load_scissors(26)  # 참조를 붙잡아 두지 않으면 이미지가 사라진다
        self.root.after(50, self._drain)
        self._connect()

    # ---------------- 화면 구성 ----------------
    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=C["window"], padx=10, pady=10)
        outer.pack(fill="both")

        top = tk.Frame(outer, bg=C["window"])
        top.pack(fill="x")
        self.dot = tk.Label(top, text="●", fg=C["bad"], bg=C["window"], font=F_BODY)
        self.dot.pack(side="left")
        self.status = tk.Label(top, text="TVPaint 연결 중…", fg=C["text_dim"], bg=C["window"], font=F_SMALL)
        self.status.pack(side="left", padx=(4, 0))
        self.pin_var = tk.BooleanVar(value=True)
        pin = tk.Checkbutton(
            top, text="항상 위", variable=self.pin_var, command=self._apply_pin,
            bg=C["window"], fg=C["text_dim"], selectcolor=C["field"], activebackground=C["window"],
            activeforeground=C["text"], font=F_SMALL, bd=0, highlightthickness=0,
        )
        FlatButton(top, "도움말", self._show_help, font=F_SMALL, pady=1).pack(side="right", padx=(8, 0))
        pin.pack(side="right")
        self._apply_pin()

        panel = tk.Frame(outer, bg=C["panel"], highlightthickness=1, highlightbackground=C["panel_edge"], padx=10, pady=8)
        panel.pack(fill="x", pady=(8, 8))
        tk.Label(panel, text="현재 선택 레이어", fg=C["text_dim"], bg=C["panel"], font=F_SMALL).pack(anchor="w")
        field = tk.Frame(panel, bg=C["field"], padx=8, pady=6)
        field.pack(fill="x", pady=(3, 3))
        self.layer_label = tk.Label(field, text="—", fg=C["text"], bg=C["field"], font=F_LAYER, anchor="w", width=34)
        self.layer_label.pack(fill="x")
        self.layer_sub = tk.Label(panel, text="", fg=C["text_dim"], bg=C["panel"], font=F_SMALL, anchor="w")
        self.layer_sub.pack(fill="x")

        # 권장 작업 순서대로 배치: 불필요 프레임 삭제 → Crop → Atlas 생성
        self.btn_clean = FlatButton(outer, "불필요 프레임 삭제", lambda: self._ask("clean"))
        self.btn_clean.pack(fill="x", pady=(0, 6))
        self.btn_crop = FlatButton(outer, "Crop", lambda: self._ask("crop"))
        self.btn_crop.pack(fill="x", pady=(0, 6))
        self.btn_atlas = FlatButton(outer, "Atlas 생성", lambda: self._ask("atlas"))
        self.btn_atlas.pack(fill="x")

        self.progress = Progress(outer)
        self.progress.pack(fill="x", pady=(10, 4))
        self.message = tk.Label(outer, text="", fg=C["text_dim"], bg=C["window"], font=F_SMALL, anchor="w", justify="left")
        self.message.pack(fill="x")

        self.conn_row = tk.Frame(outer, bg=C["window"])
        FlatButton(self.conn_row, "다시 연결", self._connect, font=F_BODY, pady=3).pack(side="left", fill="x", expand=True)
        tk.Frame(self.conn_row, bg=C["window"], width=6).pack(side="left")
        FlatButton(self.conn_row, "플러그인 설치", self._install_plugin, font=F_BODY, pady=3).pack(
            side="left", fill="x", expand=True
        )
        self._refresh_controls()

    def _set_icon(self) -> None:
        import sys

        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        ico = base / "assets" / "icon.ico"
        if ico.exists():
            try:
                self.root.iconbitmap(str(ico))
            except tk.TclError:
                pass

    def _apply_pin(self) -> None:
        self.root.attributes("-topmost", bool(self.pin_var.get()))

    def _refresh_controls(self) -> None:
        can_run = self.connected and not self.running and self.layer is not None
        for btn in (self.btn_clean, self.btn_crop, self.btn_atlas):
            btn.set_enabled(can_run)
        if self.connected:
            self.conn_row.pack_forget()
        else:
            self.conn_row.pack(fill="x", pady=(8, 0))

    # ---------------- 워커 연동 ----------------
    def _drain(self) -> None:
        try:
            while not self.worker.results.empty():
                self.worker.results.get_nowait()()
        finally:
            self.root.after(50, self._drain)

    def _connect(self) -> None:
        self.status.config(text="TVPaint 연결 중…")

        def job():
            if self.backend is None:
                self.backend = self._backend_factory()
            self.backend.connect()
            return self.backend.current_layer()

        self.worker.submit(job, self._on_connected, self._on_connect_failed)

    def _on_connected(self, layer: LayerRef) -> None:
        first = not getattr(self, "_recovery_checked", False)
        self._recovery_checked = True
        self.connected = True
        self.dot.config(fg=C["ok"])
        self.status.config(text="TVPaint 연결됨")
        self._show_layer(layer)
        self._schedule_poll()
        if first:
            self._check_recovery()

    def _check_recovery(self) -> None:
        """지난 작업이 중간에 끊겼으면(기록 파일이 남아 있으면) 복구를 제안한다."""
        pending = getattr(self.backend, "pending_recovery", lambda: None)()
        if not pending:
            return
        choice = ConfirmDialog(
            self.root,
            "지난 작업 복구",
            (pending.get("layer") or {}).get("name", "?"),
            self.backend.describe_recovery(pending),
            question="복구하시겠습니까?",
            layer_label="기록된 레이어:",
        ).result
        if not choice:
            if ConfirmDialog(
                self.root, "지난 작업 복구", (pending.get("layer") or {}).get("name", "?"),
                "복구하지 않으면 기록을 지웁니다. 다음 실행 때 다시 묻지 않습니다.",
                question="기록을 지우시겠습니까?",
                layer_label="기록된 레이어:",
                work_warning=False,
            ).result:
                self.backend.discard_recovery()
            return
        self._start(lambda p: self.backend.recover(pending), lambda msg: MessageDialog(self.root, "복구 결과", msg))

    def _on_connect_failed(self, exc: BaseException, tb: str) -> None:
        self.connected = False
        self.layer = None
        self.dot.config(fg=C["bad"])
        self.status.config(text="TVPaint에 연결되지 않음")
        self.layer_label.config(text="—")
        self.layer_sub.config(text="TVPaint를 켜고 플러그인이 설치됐는지 확인해 주세요.")
        self._refresh_controls()
        if not self._reconnect_scheduled:
            self._reconnect_scheduled = True
            self.root.after(3000, self._auto_reconnect)

    def _auto_reconnect(self) -> None:
        self._reconnect_scheduled = False
        if not self.connected and not self.running:
            self._connect()

    def _schedule_poll(self) -> None:
        self.root.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        if not self.connected:
            return
        if self.running or self._poll_pending:
            self._schedule_poll()
            return
        self._poll_pending = True
        shown = (self.layer.id, self.layer.name) if self.layer else None

        def job():
            sig = self.backend.layer_signature()
            # 레이어가 바뀌었거나 이름이 바뀐 경우에만 전체 정보를 다시 읽는다
            return self.backend.current_layer() if sig != shown else None

        def done(layer):
            self._poll_pending = False
            if layer is not None:
                self._show_layer(layer)
            self._schedule_poll()

        def fail(exc, tb):
            self._poll_pending = False
            if isinstance(exc, (ConnectionError, OSError)):
                self._on_connect_failed(exc, tb)
            else:
                # 일시적인 조회 실패(TVPaint 가 바쁜 순간 등)는 연결 끊김으로 보지 않고 다시 시도한다
                self._schedule_poll()

        self.worker.submit(job, done, fail)

    def _show_layer(self, layer: LayerRef) -> None:
        self.layer = layer
        self.layer_label.config(text=layer.name)
        notes = [layer.clip_name]
        if not layer.is_anim:
            notes.append("애니메이션 레이어 아님")
        if layer.is_locked:
            notes.append("잠김")
        self.layer_sub.config(text=" / ".join(n for n in notes if n))
        self._refresh_controls()

    # ---------------- 실행 ----------------
    def _ask(self, which: str) -> None:
        """세 기능 모두: 분석(원본 변경 없음) → Preview 확인창 → 그 분석 결과로 적용."""
        if self.running or not self.connected:
            return

        def got_layer(layer: LayerRef):
            self._show_layer(layer)
            if which == "atlas":
                self._start(lambda p: analyze_atlas(self.backend, layer.id, p), self._confirm_atlas, self.btn_atlas)
            elif which == "crop":
                self._start(lambda p: analyze_crop(self.backend, layer.id, p), self._confirm_crop, self.btn_crop)
            else:
                self._start(lambda p: analyze_compact(self.backend, layer.id, p), self._confirm_clean, self.btn_clean)

        self.worker.submit(lambda: self.backend.current_layer(), got_layer, self._on_connect_failed)

    def _confirm_clean(self, clean: CleanPlan) -> None:
        p = clean.plan
        self.message.config(text="", fg=C["text_dim"])
        if p.is_noop:
            clean.discard()
            MessageDialog(self.root, "불필요 프레임 삭제", f"'{clean.layer.name}': 정리할 프레임이 없습니다. (이미 {len(p.keep)}장 1콤마)")
            return
        detail = (
            f"{p.total} → {len(p.keep)} frames\n\n"
            f"빈 프레임 제거: {p.removed_empty}\n"
            f"중복 프레임 제거: {p.removed_repeat}\n\n"
            "Ctrl+Z 한 번으로 되돌릴 수 있습니다."
        )
        warnings = [NAME_WARNING] if not clean.layer.name.isascii() else []
        question = "그래도 진행하시겠습니까?" if warnings else "진행하시겠습니까?"
        if ConfirmDialog(self.root, "불필요 프레임 삭제", clean.layer.name, detail, question=question, warnings=warnings).result:
            self._start(lambda pr: apply_compact(self.backend, clean, pr), self._show_summary, self.btn_clean)
        else:
            clean.discard()

    def _confirm_crop(self, crop: CropPlan) -> None:
        self.message.config(text="", fg=C["text_dim"])
        original = f"{crop.canvas[0]} × {crop.canvas[1]}" if crop.canvas else "?"
        detail = f"Original:\n{original}\n\nCrop:\n{crop.spec.width} × {crop.spec.height}\n\n원본 프로젝트는 바뀌지 않습니다."
        if ConfirmDialog(self.root, "Crop", crop.layer.name, detail, question="새 Crop 프로젝트를 생성하시겠습니까?").result:
            self._start(lambda pr: build_crop(self.backend, crop, pr), self._show_summary, self.btn_crop)
        else:
            crop.discard()

    def _confirm_atlas(self, plan: AtlasPlan) -> None:
        self.message.config(text="", fg=C["text_dim"])
        output = AtlasSettingsDialog(self.root, plan).output
        if output is not None:
            self._start(lambda pr: build_atlas(self.backend, plan, pr, output), self._show_summary, self.btn_atlas)
        else:
            plan.discard()

    def _show_summary(self, summary: str) -> None:
        self.message.config(text="완료", fg=C["ok"])
        MessageDialog(self.root, "완료", summary)

    def _start(
        self,
        job: Callable[[Callable[[str, float], None]], object],
        on_done: Callable[[object], None],
        button: "FlatButton | None" = None,
    ) -> None:
        self.running = True
        self._refresh_controls()
        self.message.config(text="")
        if button is not None:
            button.start_cutting(self._scissors)

        def progress(msg: str, value: float) -> None:
            self.worker.post(lambda: (self.message.config(text=msg, fg=C["text_dim"]), self.progress.set(value)))

        def done(value):
            if button is not None:
                button.stop_cutting()
            self.running = False
            self.progress.set(0)
            self._refresh_controls()
            on_done(value)

        def fail(exc: BaseException, tb: str):
            if button is not None:
                button.stop_cutting()
            self.running = False
            self.progress.set(0)
            self._refresh_controls()
            if isinstance(exc, ToolError):
                self.message.config(text="중단됨", fg=C["bad"])
                MessageDialog(self.root, "진행할 수 없습니다", str(exc))
            elif isinstance(exc, (ConnectionError, OSError)):
                self._on_connect_failed(exc, tb)
                MessageDialog(self.root, "연결 끊김", f"TVPaint와 연결이 끊겼습니다.\n\n{exc}")
            else:
                self.message.config(text="오류", fg=C["bad"])
                MessageDialog(self.root, "오류", f"{exc}\n\n--- 상세 ---\n{tb[-1500:]}", copyable=True)

        self.worker.submit(lambda: job(progress), done, fail)

    def _show_help(self) -> None:
        HelpDialog(self.root)

    def _install_plugin(self) -> None:
        dirs = installer.find_plugin_dirs()
        if len(dirs) == 1:
            target = dirs[0]
        else:
            chosen = filedialog.askdirectory(
                title="TVPaint 11 설치 폴더 안의 plugins 폴더를 선택하세요",
                initialdir=str(dirs[0]) if dirs else r"C:\Program Files",
            )
            if not chosen:
                return
            target = Path(chosen)
        if installer.is_installed(target):
            MessageDialog(self.root, "플러그인", f"이미 설치되어 있습니다.\n{target}\n\nTVPaint를 재시작한 뒤 '다시 연결'을 눌러 주세요.")
            return
        try:
            msg = installer.install(target)
        except Exception as exc:  # noqa: BLE001
            MessageDialog(self.root, "설치 실패", str(exc))
            return
        MessageDialog(self.root, "플러그인 설치", f"{msg}\n\nTVPaint를 완전히 종료했다가 다시 켠 뒤 '다시 연결'을 눌러 주세요.")

    def run(self) -> None:
        self.root.mainloop()


class _Dialog(tk.Toplevel):
    def __init__(self, master, title: str):
        super().__init__(master)
        self.title(title)
        self.configure(bg=C["window"])
        self.resizable(False, False)
        self.transient(master)
        self.attributes("-topmost", True)
        self.body = tk.Frame(self, bg=C["window"], padx=16, pady=14)
        self.body.pack(fill="both")

    def _show(self) -> None:
        self.update_idletasks()
        m = self.master
        x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
        y = m.winfo_rooty() + 40
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()
        self.wait_window()


class ConfirmDialog(_Dialog):
    def __init__(
        self,
        master,
        title: str,
        layer_name: str,
        detail: str,
        question: str = "정말 진행하시겠습니까?",
        warnings: list[str] | None = None,
        layer_label: str = "현재 선택된 레이어:",
        work_warning: bool = True,
    ):
        super().__init__(master, title)
        self.result = False
        tk.Label(self.body, text=layer_label, fg=C["text_dim"], bg=C["window"], font=F_BODY).pack(anchor="w")
        box = tk.Frame(self.body, bg=C["field"], padx=8, pady=6)
        box.pack(fill="x", pady=(3, 10))
        tk.Label(box, text=layer_name, fg=C["text"], bg=C["field"], font=F_LAYER, anchor="w").pack(fill="x")
        tk.Label(self.body, text=detail, fg=C["text_dim"], bg=C["window"], font=F_SMALL, justify="left").pack(anchor="w")
        for w in (warnings or []) + ([WORK_WARNING] if work_warning else []):
            tk.Label(self.body, text=w, fg=C["warn"], bg=C["window"], font=F_SMALL, justify="left", wraplength=400).pack(
                anchor="w", pady=(8, 0)
            )
        tk.Label(self.body, text=question, fg=C["text"], bg=C["window"], font=F_BODY).pack(anchor="w", pady=(10, 12))
        row = tk.Frame(self.body, bg=C["window"])
        row.pack(fill="x")
        FlatButton(row, "취소", self._cancel, font=F_BODY, pady=4).pack(side="right")
        FlatButton(row, "진행", self._ok, primary=True, font=F_BODY, pady=4).pack(side="right", padx=(0, 6))
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._show()

    def _ok(self) -> None:
        self.result = True
        self.destroy()

    def _cancel(self) -> None:
        self.destroy()


class MessageDialog(_Dialog):
    def __init__(self, master, title: str, text: str, copyable: bool = False):
        super().__init__(master, title)
        if copyable:
            t = tk.Text(self.body, width=60, height=14, bg=C["field"], fg=C["text"], font=F_SMALL, bd=0, wrap="word")
            t.insert("1.0", text)
            t.pack(fill="both")
        else:
            tk.Label(self.body, text=text, fg=C["text"], bg=C["window"], font=F_BODY, justify="left", wraplength=360).pack(anchor="w")
        row = tk.Frame(self.body, bg=C["window"])
        row.pack(fill="x", pady=(14, 0))
        FlatButton(row, "확인", self.destroy, primary=True, font=F_BODY, pady=4).pack(side="right")
        self.bind("<Return>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())
        self._show()


HELP_STEPS = [
    ("1. 불필요 프레임 삭제", "복사된 프레임, 콤마가 적용된 프레임, 빈 프레임을 감지해 제거합니다.\n실행 취소가 가능합니다."),
    ("2. Crop", "모든 프레임을 감지해 최적화된 영역으로 잘라냅니다. 새 프로젝트를 만듭니다."),
    ("3. Atlas 생성", "이미지를 최대한 정사각형에 가까운 형태의 아틀라스로 만듭니다.\n새 프로젝트를 만듭니다."),
]


def _load_scissors(size: int = 26) -> list:
    """열린 가위 → 닫힌 가위 순서. 두 이미지는 캔버스 안 위치가 의도적으로 다르므로 캔버스째 같은 크기로 줄인다."""
    import sys

    frames = []
    try:
        from PIL import Image, ImageTk

        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        for name in ("scissors_open.png", "scissors_closed.png"):
            with Image.open(base / "assets" / name) as src:
                frames.append(ImageTk.PhotoImage(src.convert("RGBA").resize((size, size), Image.LANCZOS)))
    except Exception:  # noqa: BLE001
        return []
    return frames


def _load_app_icon(size: int):
    """도움말 머리글용 아이콘. EXE 에 들어 있는 icon.ico 를 읽는다 (없으면 None)."""
    import sys

    try:
        from PIL import Image, ImageTk

        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        with Image.open(base / "assets" / "icon.ico") as ico:
            im = ico.convert("RGBA").resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(im)
    except Exception:  # noqa: BLE001
        return None


class HelpDialog(_Dialog):
    def __init__(self, master):
        super().__init__(master, "도움말")
        head = tk.Frame(self.body, bg=C["window"])
        head.pack(fill="x")
        self._icon = _load_app_icon(40)  # 참조를 붙잡아 두지 않으면 이미지가 사라진다
        if self._icon is not None:
            tk.Label(head, image=self._icon, bg=C["window"]).pack(side="left", padx=(0, 10))
        tk.Label(head, text=APP_TITLE, fg=C["text"], bg=C["window"], font=F_LAYER).pack(side="left", anchor="w")

        intro = (
            "시퀀스 이미지의 영역을 크롭해 게임용 아틀라스 이미지로 만드는 도구입니다.\n"
            "모든 작업은 표기된 '현재 선택 레이어' 기준으로 진행됩니다."
        )
        tk.Label(self.body, text=intro, fg=C["text"], bg=C["window"], font=F_BODY, justify="left", wraplength=460).pack(
            anchor="w", pady=(12, 12)
        )

        panel = tk.Frame(self.body, bg=C["panel"], highlightthickness=1, highlightbackground=C["panel_edge"], padx=12, pady=10)
        panel.pack(fill="x")
        tk.Label(panel, text="권장 순서", fg=C["text_dim"], bg=C["panel"], font=F_SMALL).pack(anchor="w", pady=(0, 6))
        for n, (title, desc) in enumerate(HELP_STEPS):
            tk.Label(panel, text=title, fg=C["text"], bg=C["panel"], font=(FONT, 10, "bold")).pack(
                anchor="w", pady=(0 if n == 0 else 10, 2)
            )
            tk.Label(panel, text=desc, fg=C["text_dim"], bg=C["panel"], font=F_BODY, justify="left", wraplength=440).pack(
                anchor="w"
            )

        tk.Label(
            self.body,
            text="주의 : 너무 큰 해상도는 렉과 오류를 유발합니다. 아틀라스의 사이즈가 10000px 이하가 되도록 작업해 주세요.",
            fg=C["warn"], bg=C["window"], font=F_BODY, justify="left", wraplength=460,
        ).pack(anchor="w", pady=(12, 0))
        tk.Label(
            self.body,
            text="주의 : " + WORK_WARNING,
            fg=C["warn"], bg=C["window"], font=F_BODY, justify="left", wraplength=460,
        ).pack(anchor="w", pady=(6, 0))

        row = tk.Frame(self.body, bg=C["window"])
        row.pack(fill="x", pady=(14, 0))
        FlatButton(row, "확인", self.destroy, primary=True, font=F_BODY, pady=4).pack(side="right")
        self.bind("<Return>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())
        self._show()



class AtlasSettingsDialog(_Dialog):
    """Atlas 설정(Padding / Max Size / 모드)과 결과 Preview 를 한 창에. 입력을 바꾸면 바로 다시 계산한다."""

    def __init__(self, master, plan: AtlasPlan):
        super().__init__(master, "Atlas 생성")
        self.plan = plan
        self.output = None
        self._current = None

        tk.Label(self.body, text="현재 레이어:", fg=C["text_dim"], bg=C["window"], font=F_BODY).pack(anchor="w")
        box = tk.Frame(self.body, bg=C["field"], padx=8, pady=6)
        box.pack(fill="x", pady=(3, 10))
        tk.Label(box, text=plan.layer.name, fg=C["text"], bg=C["field"], font=F_LAYER, anchor="w").pack(fill="x")

        form = tk.Frame(self.body, bg=C["panel"], highlightthickness=1, highlightbackground=C["panel_edge"], padx=12, pady=10)
        form.pack(fill="x")
        self.pad_var = tk.StringVar(value="0")
        self.max_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value=MODE_CANCEL)
        self._entry(form, 0, "Padding (px)", self.pad_var, "기본 0, 공란은 0")
        self._entry(form, 1, "Max Size (px)", self.max_var, "공란 또는 0은 제한 없음")
        self.radios = []
        for row, (value, text) in enumerate(((MODE_CANCEL, "최대 크기 초과 시 생성하지 않음"), (MODE_SCALE, "최대 크기에 맞춰 정비율 축소")), start=2):
            rb = tk.Radiobutton(
                form, text=text, variable=self.mode_var, value=value, command=self._update,
                bg=C["panel"], fg=C["text"], selectcolor=C["field"], activebackground=C["panel"],
                activeforeground=C["text"], disabledforeground=C["text_dim"], font=F_BODY, bd=0, highlightthickness=0,
            )
            rb.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6 if row == 2 else 2, 0))
            self.radios.append(rb)

        self.preview = tk.Label(self.body, text="", fg=C["text"], bg=C["window"], font=F_BODY, justify="left", anchor="w")
        self.preview.pack(fill="x", pady=(12, 0))
        self.status = tk.Label(self.body, text="", fg=C["warn"], bg=C["window"], font=F_SMALL, justify="left", wraplength=400, anchor="w")
        self.status.pack(fill="x", pady=(6, 0))
        tk.Label(self.body, text=WORK_WARNING, fg=C["warn"], bg=C["window"], font=F_SMALL, justify="left", wraplength=400).pack(
            anchor="w", pady=(8, 0)
        )
        tk.Label(self.body, text="진행하시겠습니까?", fg=C["text"], bg=C["window"], font=F_BODY).pack(anchor="w", pady=(10, 12))

        row = tk.Frame(self.body, bg=C["window"])
        row.pack(fill="x")
        FlatButton(row, "취소", self._cancel, font=F_BODY, pady=4).pack(side="right")
        self.ok_btn = FlatButton(row, "진행", self._ok, primary=True, font=F_BODY, pady=4)
        self.ok_btn.pack(side="right", padx=(0, 6))
        for var in (self.pad_var, self.max_var):
            var.trace_add("write", lambda *_: self._update())
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._update()
        self._show()

    def _entry(self, parent, row: int, label: str, var: tk.StringVar, hint: str) -> None:
        tk.Label(parent, text=label, fg=C["text"], bg=C["panel"], font=F_BODY, width=14, anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(
            parent, textvariable=var, width=8, bg=C["field"], fg=C["text"], insertbackground=C["text"],
            relief="flat", font=F_BODY, highlightthickness=1, highlightbackground=C["panel_edge"], highlightcolor=C["accent"],
        ).grid(row=row, column=1, sticky="w", padx=(0, 8), pady=2, ipady=2)
        tk.Label(parent, text=hint, fg=C["text_dim"], bg=C["panel"], font=F_SMALL).grid(row=row, column=2, sticky="w")

    def _update(self) -> None:
        self._current = None
        try:
            padding = parse_nonneg_int(self.pad_var.get(), "Padding")
            max_size = parse_nonneg_int(self.max_var.get(), "Max Size")
        except ValueError as exc:
            self.preview.config(text="")
            self.status.config(text=str(exc), fg=C["bad"])
            self.ok_btn.set_enabled(False)
            return
        for rb in self.radios:
            rb.config(state="normal" if max_size > 0 else "disabled")
        mode = self.mode_var.get()
        out = self.plan.output(padding, max_size, mode)
        B, F = out.base, out.final
        lines = [
            f"Frames: {B.count}",
            f"Cell: {B.cell_w} × {B.cell_h}",
            f"Layout: {B.cols}열 × {B.rows}행",
            f"Padding: {padding} px",
            f"Atlas: {B.width} × {B.height}",
            f"Max Size: {max_size if max_size else '제한 없음'}",
        ]
        if max_size:
            lines.append(f"Mode: {'정비율 축소' if mode == MODE_SCALE else '초과 시 생성하지 않음'}")
        if out.status == "ok" and out.scaled:
            lines += [f"Final Cell: {F.cell_w} × {F.cell_h}", f"Final Atlas: {F.width} × {F.height}"]
        self.preview.config(text="\n".join(lines))
        if out.status == "too_big":
            self.status.config(text=f"Atlas 크기가 최대값을 초과합니다. ({out.reason}) 진행하면 생성하지 않고 중단합니다.", fg=C["warn"])
        elif out.status == "impossible":
            self.status.config(text=out.reason, fg=C["bad"])
        else:
            self.status.config(text="")
        self._current = out
        self.ok_btn.set_enabled(True)

    def _ok(self) -> None:
        if self._current is None:
            return
        self.output = self._current
        self.destroy()

    def _cancel(self) -> None:
        self.output = None
        self.destroy()
