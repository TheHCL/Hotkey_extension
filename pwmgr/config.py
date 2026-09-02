"""集中路徑與常數。所有跨行程資源的位置都從這裡讀,避免散落各處。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- native messaging -------------------------------------------------------

HOST_NAME = "com.thehcl.pwmgr"

# --- 儲存路徑 ---------------------------------------------------------------

_APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "pwmgr"

# keyring 服務名稱(每個條目是一個 credential: (service, "pwmgr:<id>") -> password)
KEYRING_SERVICE = "pwmgr"


def app_dir() -> Path:
    """回傳資料目錄並確保其存在。"""
    _APP_DIR.mkdir(parents=True, exist_ok=True)
    return _APP_DIR


def index_path() -> Path:
    return app_dir() / "index.json"


def current_url_path() -> Path:
    return app_dir() / "current_url.json"


# --- 安裝路徑 ---------------------------------------------------------------

# 專案根目錄。
# 開發模式(python -m pwmgr):此檔所在位置的父目錄。
# 打包後(PyInstaller frozen):__file__ 指向解壓縮到暫存目錄的路徑,不能用;
# 改用執行中 exe 自己的所在目錄(exe 旁邊要放 chrome_extension/,bin/ 會自動建立)。
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"
NATIVE_HOST_BAT = BIN_DIR / "native_host.bat"
NATIVE_HOST_MANIFEST = BIN_DIR / f"{HOST_NAME}.json"
EXTENSION_DIR = PROJECT_ROOT / "chrome_extension"

# --- 行為常數 ---------------------------------------------------------------

# Windows Credential Manager 對單一 credential 密碼的硬限制(實測 ~2560)
MAX_PASSWORD_BYTES = 2500

# 預設 notes 上限
MAX_NOTES_CHARS = 500

# 複製密碼後自動清空剪貼簿的秒數
CLIPBOARD_CLEAR_SECONDS = 20

# GUI 輪詢目前 URL 的間隔
URL_POLL_INTERVAL_MS = 2000

# GUI 與原生主機搶鎖的等待時間
LOCK_TIMEOUT_SECONDS = 0.2

# 全域熱鍵
HOTKEY_MODIFIERS = 0x0002 | 0x0004  # MOD_CONTROL | MOD_SHIFT
HOTKEY_KEY = ord("L")
HOTKEY_ID = 1

# --- Python 路徑(給 .bat 與安裝腳本) ----------------------------------------


def python_executable() -> str:
    """回傳目前直譯器的絕對路徑(GUI 走 python.exe,native host 走 pythonw.exe)。

    打包後(frozen):直接回傳 PWmgr.exe 的路徑(GUI 與 native host 是同一個 exe,
    用 --native 分流,見 pwmgr/__main__.py)。
    """
    if getattr(sys, "frozen", False):
        return str(PROJECT_ROOT / "PWmgr.exe")
    return sys.executable


def pythonw_executable() -> str:
    """尋找 pythonw.exe,優先在同一目錄。frozen 時同 python_executable()。"""
    if getattr(sys, "frozen", False):
        return python_executable()
    py = Path(sys.executable)
    candidate = py.with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    return sys.executable  # fallback
