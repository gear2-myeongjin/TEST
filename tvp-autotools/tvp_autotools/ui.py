"""TVPaint 11.6 톤의 작은 보조 패널 UI."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Callable

from tvp_autotools import installer
from tvp_autotools.ops import LayerRef, ToolError, run_compact, run_crop
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
    "progress_bg": "#262626",
}
import os as _os

FONT = _os.environ.get("TVP_AT_FONT", "Malgun Gothic")
F_SMALL = (FONT, 8)
F_BODY = (FONT, 9)
F_LAYER = (FONT, 11, "bold")
F_BUTTON = (FONT, 10)

POLL_MS = 1500


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


class Progress(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=4, bg=C["progress_bg"], highlightthickness=0)
        self._bar = self.create_rectangle(0, 0, 0, 4, fill=C["accent"], width=0)

    def set(self, value: float) -> None:
        self.coords(self._bar, 0, 0, self.winfo_width() * max(0.0, min(1.0, value)), 4)


class App:
    def __init__(self, backend_factory: Callable[[], object]) -> None:
        self.root = tk.Tk()
        self.root.title("TVPaint Auto Tools")
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
        pin.pack(side="right")
        self._apply_pin()

        panel = tk.Frame(outer, bg=C["panel"], highlightthickness=1, highlightbackground=C["panel_edge"], padx=10, pady=8)
        panel.pack(fill="x", pady=(8, 8))
        tk.Label(panel, text="현재 선택 레이어", fg=C["text_dim"], bg=C["panel"], font=F_SMALL).pack(anchor="w")
        field = tk.Frame(panel, bg=C["field"], padx=8, pady=6)
        field.pack(fill="x", pady=(3, 3))
        self.layer_label = tk.Label(field, text="—", fg=C["text"], bg=C["field"], font=F_LAYER, anchor="w", width=26)
        self.layer_label.pack(fill="x")
        self.layer_sub = tk.Label(panel, text="", fg=C["text_dim"], bg=C["panel"], font=F_SMALL, anchor="w")
        self.layer_sub.pack(fill="x")

        self.btn_crop = FlatButton(outer, "Crop", lambda: self._ask("crop"))
        self.btn_crop.pack(fill="x", pady=(0, 6))
        self.btn_clean = FlatButton(outer, "불필요 프레임 삭제", lambda: self._ask("clean"))
        self.btn_clean.pack(fill="x")

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
        self.btn_crop.set_enabled(can_run)
        self.btn_clean.set_enabled(can_run)
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
        self.connected = True
        self.dot.config(fg=C["ok"])
        self.status.config(text="TVPaint 연결됨")
        self._show_layer(layer)
        self._schedule_poll()

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

        def done(layer):
            self._poll_pending = False
            self._show_layer(layer)
            self._schedule_poll()

        def fail(exc, tb):
            self._poll_pending = False
            self._on_connect_failed(exc, tb)

        self.worker.submit(lambda: self.backend.current_layer(), done, fail)

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
        if self.running or not self.connected:
            return

        def got_layer(layer: LayerRef):
            self._show_layer(layer)
            title = "Crop" if which == "crop" else "불필요 프레임 삭제"
            detail = (
                "선택 레이어의 실제 그림 영역만큼 잘라 새 프로젝트를 만듭니다.\n원본 프로젝트는 바뀌지 않습니다."
                if which == "crop"
                else "빈 프레임과 중복 그림을 지우고 전부 1콤마로 만듭니다.\nCtrl+Z 한 번으로 되돌릴 수 있습니다."
            )
            if ConfirmDialog(self.root, title, layer.name, detail).result:
                self._run(which, layer)

        self.worker.submit(lambda: self.backend.current_layer(), got_layer, self._on_connect_failed)

    def _run(self, which: str, layer: LayerRef) -> None:
        self.running = True
        self._refresh_controls()
        self.message.config(text="")
        fn = run_crop if which == "crop" else run_compact

        def progress(msg: str, value: float) -> None:
            self.worker.post(lambda: (self.message.config(text=msg, fg=C["text_dim"]), self.progress.set(value)))

        def done(summary: str):
            self.running = False
            self.progress.set(0)
            self.message.config(text="완료", fg=C["ok"])
            self._refresh_controls()
            MessageDialog(self.root, "완료", summary)

        def fail(exc: BaseException, tb: str):
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

        self.worker.submit(lambda: fn(self.backend, layer.id, progress), done, fail)

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
    def __init__(self, master, title: str, layer_name: str, detail: str):
        super().__init__(master, title)
        self.result = False
        tk.Label(self.body, text="현재 선택된 레이어:", fg=C["text_dim"], bg=C["window"], font=F_BODY).pack(anchor="w")
        box = tk.Frame(self.body, bg=C["field"], padx=8, pady=6)
        box.pack(fill="x", pady=(3, 10))
        tk.Label(box, text=layer_name, fg=C["text"], bg=C["field"], font=F_LAYER, anchor="w").pack(fill="x")
        tk.Label(self.body, text=detail, fg=C["text_dim"], bg=C["window"], font=F_SMALL, justify="left").pack(anchor="w")
        tk.Label(self.body, text="정말 진행하시겠습니까?", fg=C["text"], bg=C["window"], font=F_BODY).pack(anchor="w", pady=(10, 12))
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
