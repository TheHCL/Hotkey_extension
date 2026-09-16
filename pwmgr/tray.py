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
        on_toggle_otp: Callable[[], None] | None = None,
        is_otp_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.on_show = on_show
        self.on_quit = on_quit
        self.on_toggle_otp = on_toggle_otp
        self.is_otp_enabled = is_otp_enabled
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None
        # OTP 監聽 toggle 在 menu 是 checkable item;toggle 後 caller 透過
        # update_menu() 重新跑 Menu 建構,讓 checked 狀態刷新(避免 menu
        # 凍結後狀態卡住)。pystray 沒有 in-place update checked 的 API。
        self._show_otp_toggle = on_toggle_otp is not None and is_otp_enabled is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._icon = pystray.Icon(
            "pwmgr",
            icon=_make_icon_image(),
            title="PWmgr",
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(
            target=self._icon.run, daemon=True, name="pwmgr-tray"
        )
        self._thread.start()

    def update_menu(self) -> None:
        """重建並換掉 menu。

        用在 OTP toggle 之後 — checkable item 的 checked 旗標是 menu 重建時
        從 ``is_otp_enabled()`` 拉的,沒辦法 in-place mutate。icon.update_menu()
        會立刻刷新 tray menu UI。
        """
        if self._icon is None:
            return
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _build_menu(self) -> pystray.Menu:
        items: list[pystray.MenuItem] = [
            pystray.MenuItem("顯示主視窗 (Ctrl+Shift+L)", self._show, default=True),
        ]
        if self._show_otp_toggle:
            # 為什麼 callback 寫成 ``_noop``:pystray 對 checkable item 點下去會
            # 先 invoke checked-fn(更新視覺),再 invoke action。我們這裡 toggle 後
            # 要做事(改 settings + 重啟 monitor),所以 action 才是重點,checked-fn
            # 只負責回報目前狀態。
            # 注意:pystray 0.19 的 ``checked`` 內部用 ``self._checked(self)``
            # 呼叫,只傳一個參數(MenuItem 本身),不是 ``(icon, item)`` 兩參數
            # (那是更舊版本的簽章)。寫成單參數避免 TypeError。
            items.append(
                pystray.MenuItem(
                    "OTP 監聽",
                    self._toggle_otp,
                    checked=lambda _item: bool(self.is_otp_enabled()),
                )
            )
        items.append(pystray.MenuItem("結束", self._quit))
        return pystray.Menu(*items)

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

    def _toggle_otp(self, icon, item) -> None:
        if self.on_toggle_otp is not None:
            self.on_toggle_otp()
