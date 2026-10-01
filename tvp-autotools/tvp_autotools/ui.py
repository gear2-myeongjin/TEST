"""TVPaint 11.6 톤의 작은 보조 패널 UI."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Callable

from tvp_autotools import installer
from tvp_autotools.ops import (
    AtlasPlan,
    LayerRef,
    ToolError,
    analyze_atlas,
    build_atlas,
    run_compact,
    run_crop,
    run_opacity_diagnosis,
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
    "progress_bg": "#262626",
}
import os as _os

FONT = _os.environ.get("TVP_AT_FONT", "Malgun Gothic")
F_SMALL = (FONT, 8)
F_BODY = (FONT, 9)
F_LAYER = (FONT, 11, "bold")
F_BUTTON = (FONT, 10)

APP_TITLE = "TVPaint Auto Crop & Atlas Maker"

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
        # [임시] 불투명도 0% 문제 진단용. 원인을 찾은 뒤 제거한다.
        self.btn_diag = FlatButton(outer, "불투명도 진단 (임시)", self._diagnose, font=F_SMALL, pady=3)
        self.btn_diag.pack(fill="x", pady=(10, 0))

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
        for btn in (self.btn_clean, self.btn_crop, self.btn_atlas, self.btn_diag):
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
        if self.running or not self.connected:
            return

        def got_layer(layer: LayerRef):
            self._show_layer(layer)
            if which == "atlas":
                # Atlas 는 확인창에 셀/배치 정보를 보여줘야 하므로 분석을 먼저 한다 (원본은 바뀌지 않음)
                self._start(lambda p: analyze_atlas(self.backend, layer.id, p), self._confirm_atlas)
                return
            if which == "crop":
                title = "Crop"
                detail = "선택 레이어의 실제 그림 영역만큼 잘라 새 프로젝트를 만듭니다.\n원본 프로젝트는 바뀌지 않습니다."
                fn = run_crop
            else:
                title = "불필요 프레임 삭제"
                detail = "빈 프레임과 중복 그림을 지우고 전부 1콤마로 만듭니다.\nCtrl+Z 한 번으로 되돌릴 수 있습니다."
                fn = run_compact
            if ConfirmDialog(self.root, title, layer.name, detail).result:
                self._start(lambda p: fn(self.backend, layer.id, p), self._show_summary)

        self.worker.submit(lambda: self.backend.current_layer(), got_layer, self._on_connect_failed)

    def _confirm_atlas(self, plan: AtlasPlan) -> None:
        L = plan.layout
        detail = (
            f"Frames: {L.count}\n"
            f"Cell: {L.cell_w} × {L.cell_h}\n"
            f"Layout: {L.cols}열 × {L.rows}행\n"
            f"Atlas: {L.width} × {L.height}"
        )
        self.message.config(text="", fg=C["text_dim"])
        if ConfirmDialog(self.root, "Atlas 생성", plan.layer.name, detail, question="Atlas를 생성하시겠습니까?").result:
            self._start(lambda p: build_atlas(self.backend, plan, p), self._show_summary)
        else:
            plan.discard()

    def _show_summary(self, summary: str) -> None:
        self.message.config(text="완료", fg=C["ok"])
        MessageDialog(self.root, "완료", summary)

    def _start(self, job: Callable[[Callable[[str, float], None]], object], on_done: Callable[[object], None]) -> None:
        self.running = True
        self._refresh_controls()
        self.message.config(text="")

        def progress(msg: str, value: float) -> None:
            self.worker.post(lambda: (self.message.config(text=msg, fg=C["text_dim"]), self.progress.set(value)))

        def done(value):
            self.running = False
            self.progress.set(0)
            self._refresh_controls()
            on_done(value)

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

        self.worker.submit(lambda: job(progress), done, fail)

    def _diagnose(self) -> None:
        """[임시] 렌더링 단계를 명령 하나마다 멈추고, 화면의 불투명도가 바뀌었는지 사용자에게 확인받는다."""
        if self.running or not self.connected or self.layer is None:
            return
        layer = self.layer
        detail = (
            "Crop/Atlas 분석과 같은 렌더링 단계를 실행하면서,\n"
            "TVPaint에 명령을 하나 보낼 때마다 멈춥니다.\n\n"
            "멈출 때마다 TVPaint 레이어 패널에서 이 레이어의 불투명도를 보고\n"
            "[그대로] 또는 [바뀜]을 눌러 주세요.\n"
            "새 프로젝트는 만들지 않습니다."
        )
        if not ConfirmDialog(self.root, "불투명도 진단", layer.name, detail, question="시작하시겠습니까?").result:
            return

        import threading

        log: list[str] = []
        state = {"n": 0, "stopped": False, "changed_at": None}

        def checkpoint(desc: str, info_value):
            state["n"] += 1
            if state["stopped"]:
                log.append(f"{state['n']:02d}. {desc} (TVPaint가 알려준 값 {info_value})")
                return
            answer = {}
            done = threading.Event()

            def ask():
                answer["v"] = DiagStepDialog(self.root, state["n"], desc).result
                done.set()

            self.worker.post(ask)
            done.wait()
            mark = {"same": "그대로", "changed": "◀ 바뀜", "abort": "중단"}[answer["v"]]
            log.append(f"{state['n']:02d}. {desc} → {mark} (TVPaint가 알려준 값 {info_value})")
            if answer["v"] == "changed":
                state["changed_at"] = desc
            if answer["v"] in ("changed", "abort"):
                state["stopped"] = True  # 이후로는 묻지 않고 끝까지 진행해 설정을 모두 되돌린다

        def job(progress):
            self.backend.checkpoint = checkpoint
            self.backend._diag_layer_id = layer.id
            try:
                run_opacity_diagnosis(self.backend, layer.id, progress)
            finally:
                self.backend.checkpoint = None
                self.backend._diag_layer_id = None

        def finished(_):
            head = (
                f"바뀐 단계: {state['changed_at']}" if state["changed_at"] else "모든 단계에서 '그대로'로 답하셨습니다."
            )
            MessageDialog(self.root, "진단 결과 (이 내용을 그대로 전달해 주세요)", head + "\n\n" + "\n".join(log), copyable=True)

        self._start(job, finished)

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
    def __init__(self, master, title: str, layer_name: str, detail: str, question: str = "정말 진행하시겠습니까?"):
        super().__init__(master, title)
        self.result = False
        tk.Label(self.body, text="현재 선택된 레이어:", fg=C["text_dim"], bg=C["window"], font=F_BODY).pack(anchor="w")
        box = tk.Frame(self.body, bg=C["field"], padx=8, pady=6)
        box.pack(fill="x", pady=(3, 10))
        tk.Label(box, text=layer_name, fg=C["text"], bg=C["field"], font=F_LAYER, anchor="w").pack(fill="x")
        tk.Label(self.body, text=detail, fg=C["text_dim"], bg=C["window"], font=F_SMALL, justify="left").pack(anchor="w")
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

        row = tk.Frame(self.body, bg=C["window"])
        row.pack(fill="x", pady=(14, 0))
        FlatButton(row, "확인", self.destroy, primary=True, font=F_BODY, pady=4).pack(side="right")
        self.bind("<Return>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())
        self._show()


class DiagStepDialog(_Dialog):
    """[임시] 진단 단계 확인창."""

    def __init__(self, master, n: int, desc: str):
        super().__init__(master, f"진단 {n}단계")
        self.result = "same"
        tk.Label(self.body, text=f"{n}단계: 방금 실행한 명령", fg=C["text_dim"], bg=C["window"], font=F_SMALL).pack(anchor="w")
        box = tk.Frame(self.body, bg=C["field"], padx=8, pady=6)
        box.pack(fill="x", pady=(3, 10))
        tk.Label(box, text=desc, fg=C["text"], bg=C["field"], font=F_BODY, anchor="w", justify="left", wraplength=380).pack(fill="x")
        tk.Label(
            self.body, text="TVPaint에서 원본 레이어의 불투명도가 바뀌었나요?", fg=C["text"], bg=C["window"], font=F_BODY
        ).pack(anchor="w", pady=(0, 12))
        row = tk.Frame(self.body, bg=C["window"])
        row.pack(fill="x")
        FlatButton(row, "중단", lambda: self._set("abort"), font=F_BODY, pady=4).pack(side="left")
        FlatButton(row, "바뀜", lambda: self._set("changed"), font=F_BODY, pady=4).pack(side="right")
        FlatButton(row, "그대로", lambda: self._set("same"), primary=True, font=F_BODY, pady=4).pack(side="right", padx=(0, 6))
        self.bind("<Return>", lambda e: self._set("same"))
        self.protocol("WM_DELETE_WINDOW", lambda: self._set("abort"))
        self._show()

    def _set(self, value: str) -> None:
        self.result = value
        self.destroy()
