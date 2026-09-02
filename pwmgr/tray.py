"""System tray——pystray + Pillow。"""

from __future__ import annotations

import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw


def _make_icon_image(size: int = 64) -> Image.Image:
    """產生簡單的鎖頭圖示。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 鎖頭本體
    d.rounded_rectangle(
        [size * 0.18, size * 0.42, size * 0.82, size * 0.86],
        radius=size * 0.05,
        fill=(40, 110, 200, 255),
    )
    # 鎖頭 U 形
    d.arc(
        [size * 0.28, size * 0.16, size * 0.72, size * 0.60],
        start=180,
        end=360,
        fill=(40, 110, 200, 255),
        width=max(2, int(size * 0.08)),
    )
    return img


class TrayIcon:
    def __init__(
        self,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.on_show = on_show
        self.on_quit = on_quit
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("顯示主視窗 (Ctrl+Shift+L)", self._show, default=True),
            pystray.MenuItem("結束", self._quit),
        )
        self._icon = pystray.Icon(
            "pwmgr",
            icon=_make_icon_image(),
            title="PWmgr",
            menu=menu,
        )
        self._thread = threading.Thread(
            target=self._icon.run, daemon=True, name="pwmgr-tray"
        )
        self._thread.start()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass

    def _show(self, icon, item) -> None:
        self.on_show()

    def _quit(self, icon, item) -> None:
        self.on_quit()
