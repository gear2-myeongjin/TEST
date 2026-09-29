"""모든 TVPaint 호출을 스레드 하나에서 순서대로 실행한다. UI 는 멈추지 않는다."""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Any, Callable


class Worker:
    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[Callable[[], Any], Callable[[Any], None], Callable[[BaseException, str], None]]] = queue.Queue()
        self.results: queue.Queue[Callable[[], None]] = queue.Queue()  # UI 스레드에서 꺼내 실행
        self.busy = False
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(
        self,
        job: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException, str], None],
    ) -> None:
        self._jobs.put((job, on_done, on_error))

    def post(self, fn: Callable[[], None]) -> None:
        """워커 스레드에서 UI 스레드로 콜백 전달."""
        self.results.put(fn)

    def _loop(self) -> None:
        while True:
            job, on_done, on_error = self._jobs.get()
            self.busy = True
            try:
                value = job()
            except BaseException as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                self.results.put(lambda e=exc, t=tb, cb=on_error: cb(e, t))
            else:
                self.results.put(lambda v=value, cb=on_done: cb(v))
            finally:
                self.busy = False
        # 주의: 콜백은 반드시 기본 인자(cb=...)로 묶어야 한다. 그냥 참조하면 UI 가 결과를 꺼내기 전에
        # 다음 작업을 가져왔을 때 '새 작업의 콜백'에 '이전 작업의 결과'가 전달된다.
