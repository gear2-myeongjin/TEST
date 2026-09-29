"""EXE 에 내장된 tvpaint-rpc.dll 을 TVPaint plugins 폴더에 복사한다."""

from __future__ import annotations

import ctypes
import glob
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

DLL_NAME = "tvpaint-rpc.dll"


def bundled_dll() -> Path | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for candidate in (base / "plugin" / DLL_NAME, base / DLL_NAME):
        if candidate.exists():
            return candidate
    return None


def find_plugin_dirs() -> list[Path]:
    roots = {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", r"C:\Program Files")}
    found: list[Path] = []
    for root in filter(None, roots):
        for pattern in ("TVPaint Developpement/TVPaint Animation 11*", "TVPaint*/TVPaint Animation 11*"):
            for d in glob.glob(str(Path(root) / pattern)):
                plugins = Path(d) / "plugins"
                if plugins.is_dir() and plugins not in found:
                    found.append(plugins)
    return sorted(found)


def is_installed(plugin_dir: Path) -> bool:
    return (plugin_dir / DLL_NAME).exists()


def install(plugin_dir: Path) -> str:
    """설치 결과 메시지를 돌려준다. 권한이 없으면 관리자 권한 요청(UAC)으로 한 번 더 시도한다."""
    src = bundled_dll()
    if src is None:
        raise RuntimeError("EXE 안에 플러그인 파일이 없습니다. 빌드 설정을 확인해 주세요.")
    dst = plugin_dir / DLL_NAME

    try:
        shutil.copy2(src, dst)
        return f"설치 완료: {dst}"
    except PermissionError:
        pass

    # Program Files 는 관리자 권한이 필요하다. 임시 폴더에 복사본을 두고 권한 상승 복사를 요청한다.
    staged = Path(tempfile.gettempdir()) / DLL_NAME
    shutil.copy2(src, staged)
    params = f'/c copy /Y "{staged}" "{dst}"'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", params, None, 0)  # type: ignore[attr-defined]
    if rc <= 32:
        raise RuntimeError("관리자 권한 요청이 거절되었거나 실패했습니다.")
    for _ in range(40):
        if dst.exists():
            return f"설치 완료: {dst}"
        time.sleep(0.25)
    raise RuntimeError(f"복사를 확인하지 못했습니다. 직접 확인해 주세요: {dst}")
