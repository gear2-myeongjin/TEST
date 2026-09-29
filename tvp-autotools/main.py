"""TVPaint Auto Tools 진입점."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _backend_factory():
    # pytvpaint import 는 무겁고 TVPaint 쪽 설정을 건드리므로 연결 시점에 늦게 불러온다
    from tvp_autotools.backend_tvp import TVPaintBackend

    return TVPaintBackend()


def main() -> None:
    from tvp_autotools.ui import App

    App(_backend_factory).run()


if __name__ == "__main__":
    main()
