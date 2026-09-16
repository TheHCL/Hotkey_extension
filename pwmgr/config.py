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


def otp_cache_path() -> Path:
    """Outlook OTP 快取檔(PWmgr GUI 的 outlook_monitor 寫,native host 讀)。

    與 current_url.json 同一目錄(LOCALAPPDATA\\pwmgr)。
    """
    return app_dir() / "otp_cache.json"


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

# group(群組)字數上限——自由字串,給 popup dropdown 與 Tk 表單共用
MAX_GROUP_CHARS = 64

# Captcha 圖片最大 bytes(dataURL/URL fetch 進來後的長度上限,防 DoS)
MAX_CAPTCHA_BYTES = 256 * 1024  # 256 KB

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

# --- OTP 自動填(Outlook 監聽) ----------------------------------------------------

# OTP 快取有效期:超過此秒數視為過期,native host get_otp 不會回傳
# 10 分鐘 — Dell OTP 一般 5 分鐘內有效,但保險一點
OTP_CACHE_TTL_SECONDS = 600

# Outlook 監聽的 subject pattern 列表(任一包含即視為 OTP 信)
# 後續可擴充其他站的 OTP 信(目前先針對 Dell)
# 注意:Dell 對不同語系網域發的 OTP 信件 subject 會用當地語系(中文網域 →「一次性密碼」,
# 英文網域 → "One-time Password"),所以 pattern 兩種都列,regex 配 IGNORECASE。
OTP_SUBJECT_PATTERNS: list[str] = [
    "Dell One-time Password",
    "Dell 一次性密碼",
    "[External Mail] Dell One-time Password",
    "[External Mail] Dell 一次性密碼",
]

# 從信件 body 抽出 OTP code 的 regex。\b word boundary 避免 7 位電話誤判
OTP_CODE_REGEX: str = r"\b(\d{6})\b"

# on-demand query fallback 翻 Inbox 時最多看幾封(避免掃整個 mailbox 卡住)
OTP_LOOKBACK_COUNT = 20

# --- Python 路徑(給 .bat 與安裝腳本) ----------------------------------------


def python_executable() -> str:
    """回傳 PWmgr(GUI + native host)exe 的絕對路徑。

    開發模式:目前直譯器(`python.exe`)。
    打包後(frozen):
      - 若目前執行的是 `PWmgr.exe` 本體(GUI 與 native host 共用,見
        `pwmgr/__main__.py` 的 `--native` 分流),回傳自己。
      - 若目前執行的是 `PWmgrSetup.exe`(安裝器),回傳同層 `PWmgr\\` 資料夾
        裡的 `PWmgr.exe`。PWmgr 是 onedir,exe 在子目錄,不是直接放在
        `dist\\` 根目錄。
        ⚠ 此處 `PWmgr` 子目錄名必須與 `packaging\\build.py` 的 `--name PWmgr`
        保持一致;改名後要同步。
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if exe.name == "PWmgr.exe":
            return str(exe)
        # PWmgrSetup.exe → 同層 PWmgr\ 資料夾裡的 PWmgr.exe
        return str(exe.parent / "PWmgr" / "PWmgr.exe")
    return sys.executable


def pythonw_executable() -> str:
    """frozen 時同 python_executable()(PWmgr.exe 同時充當 GUI 與 native host,無 console)。

    開發模式:優先找同目錄的 `pythonw.exe`,否則 fallback 到 `python.exe`。
    """
    if getattr(sys, "frozen", False):
        return python_executable()
    py = Path(sys.executable)
    candidate = py.with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    return sys.executable  # fallback
