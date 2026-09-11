"""PyInstaller 進入點 — 打包成 PWmgr.exe(windowed,無 console 視窗)。

跟 `python -m pwmgr` 行為完全相同:不帶參數 = 開 GUI;帶 --native = Chrome/Edge
原生訊息主機模式。native_host.bat 打包後會直接呼叫這個 exe 並加上 --native。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 讓這支腳本無論從哪裡被 PyInstaller 呼叫,都能 import 到專案根目錄的 pwmgr 套件。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pwmgr.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
