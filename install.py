"""一鍵安裝腳本——互動式。

執行 `python install.py` 會:
1. 檢查相依套件(keyring、pystray、Pillow)。
2. 寫入 `bin/native_host.bat`(用 pythonw.exe 啟動,避免 console 視窗)。
3. 寫入 `bin/com.danielhsieh.pwmgr.json`(Chrome/Edge 原生主機 manifest)。
4. 提示貼上 Extension ID,然後重寫 manifest 的 allowed_origins。
5. 寫入登錄:
   - HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\com.danielhsieh.pwmgr
   - HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.danielhsieh.pwmgr
6. 建立開始功能表捷徑(GUI 常駐)。

設計成可重入——Extension ID 或 Python 路徑變動時直接重跑即可。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pwmgr.config import (
    BIN_DIR,
    EXTENSION_DIR,
    HOST_NAME,
    NATIVE_HOST_BAT,
    NATIVE_HOST_MANIFEST,
    PROJECT_ROOT,
    python_executable,
    pythonw_executable,
)

# Windows console 預設 cp950 編碼,印中文與特殊符號會爆。強制 UTF-8 輸出。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --- 工具函式 ---------------------------------------------------------------


def info(msg: str) -> None:
    print(f"[install] {msg}")


def warn(msg: str) -> None:
    print(f"[install][WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[install][ERROR] {msg}", file=sys.stderr)


def check_deps() -> bool:
    ok = True
    for mod in ("keyring", "pystray", "PIL"):
        try:
            __import__(mod if mod != "PIL" else "PIL.Image")
            info(f"  [OK] {mod}")
        except ImportError:
            warn(f"  [X] {mod} 未安裝;請執行: pip install -r requirements.txt")
            ok = False
    return ok


def write_native_host_bat() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    pyw = pythonw_executable()
    if getattr(sys, "frozen", False):
        # 打包後 pyw 是 PWmgr.exe 本體,不需要 -m pwmgr / cwd 這些 python 才需要的東西。
        launch_line = f'"{pyw}" --native %*'
    else:
        launch_line = f'"{pyw}" -m pwmgr --native %*'
    content = f"""@echo off
REM Chrome/Edge native messaging host wrapper. Do not edit by hand -- rerun install.py instead.
REM cd into the project root first: Chrome launches this from its own install dir, and
REM `-m pwmgr` needs cwd on the package path or it fails with ModuleNotFoundError immediately.
cd /d "{PROJECT_ROOT}"
{launch_line}
"""
    NATIVE_HOST_BAT.write_text(content, encoding="utf-8")
    info(f"  [OK] {NATIVE_HOST_BAT}")


def write_native_host_manifest(extension_ids: list[str]) -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if not extension_ids:
        allowed = [f"chrome-extension://__SET_EXTENSION_ID__/"]
    else:
        allowed = [f"chrome-extension://{eid}/" for eid in extension_ids]
    payload = {
        "name": HOST_NAME,
        "description": "PWmgr native messaging host",
        "path": str(NATIVE_HOST_BAT),
        "type": "stdio",
        "allowed_origins": allowed,
    }
    NATIVE_HOST_MANIFEST.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    info(f"  [OK] {NATIVE_HOST_MANIFEST}")
    if not extension_ids:
        warn("    manifest 內的 allowed_origins 還是 placeholder。")
        warn("    請在擴充功能安裝後重跑 install.py 並貼 Extension ID。")


def register_windows_registry(extension_ids: list[str]) -> None:
    if os.name != "nt":
        info("  非 Windows,跳過登錄註冊(在 macOS / Linux 上 Chrome 不支援原生主機)。")
        return
    if not extension_ids:
        warn("  尚未提供 Extension ID,先跳過登錄註冊。")
        return

    manifest_str = str(NATIVE_HOST_MANIFEST)
    for browser, reg_root in [
        ("Edge",  r"HKCU\Software\Microsoft\Edge\NativeMessagingHosts"),
        ("Chrome", r"HKCU\Software\Google\Chrome\NativeMessagingHosts"),
    ]:
        key_path = f"{reg_root}\\{HOST_NAME}"
        cmd = [
            "reg", "add", key_path,
            "/ve", "/t", "REG_SZ",
            "/d", manifest_str, "/f",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, shell=False)
            if r.returncode == 0:
                info(f"  [OK] {browser}: {key_path}")
            else:
                warn(f"  [X] {browser} 註冊失敗: {r.stderr.strip() or r.stdout.strip()}")
        except FileNotFoundError:
            warn("  reg 指令找不到;請確認在 Windows 上執行。")
            return


def create_start_menu_shortcut() -> None:
    if os.name != "nt":
        return
    try:
        import winreg
        from win32com.client import Dispatch  # type: ignore[import-not-found]
    except ImportError:
        info("  pywin32 未安裝,跳過開始功能表捷徑(可手動建立)。")
        return

    pyw = pythonw_executable()
    working_dir = str(PROJECT_ROOT)
    args = "" if getattr(sys, "frozen", False) else "-m pwmgr"

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            start_menu = winreg.QueryValueEx(key, "Start Menu")[0]
    except OSError:
        start_menu = str(Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu")

    programs_dir = Path(start_menu) / "Programs" / "PWmgr"
    programs_dir.mkdir(parents=True, exist_ok=True)
    shortcut_path = programs_dir / "PWmgr.lnk"

    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(shortcut_path))
    shortcut.Targetpath = pyw
    shortcut.Arguments = args
    shortcut.WorkingDirectory = working_dir
    shortcut.Description = "PWmgr — 本機密碼管理員"
    shortcut.WindowStyle = 7  # 最小化啟動
    shortcut.save()
    info(f"  [OK] 開始功能表捷徑: {shortcut_path}")


def prompt_extension_id() -> list[str]:
    print()
    info("=" * 60)
    info("接下來需要 Chrome / Edge 的 Extension ID。")
    info("操作步驟:")
    info("  1. 打開 edge://extensions 或 chrome://extensions")
    info("  2. 開啟右上「開發者模式」")
    info(f"  3. 「載入未封裝項目」→ 選 {EXTENSION_DIR}")
    info("  4. 複製每個擴充的 ID(類似 'abcdefghijklmnopqrstuvwxyz123456')")
    info("     多個瀏覽器的 ID 用空格或逗號分開。")
    info("     若還沒裝,先按 Enter 跳過,稍後重跑此腳本。")
    info("=" * 60)
    raw = input("Extension ID (可多個,以空白/逗號分隔,Enter 跳過): ").strip()
    if not raw:
        return []
    # 支援空白或逗號分隔
    tokens = [t for t in raw.replace(",", " ").split() if t]
    # 簡單驗證:32 個 [a-z] 字元
    valid = [t for t in tokens if len(t) == 32 and all(c.islower() and c.isalpha() or c.isdigit() for c in t)]
    invalid = [t for t in tokens if t not in valid]
    if invalid:
        warn(f"  以下 ID 看起來不合法(將忽略): {invalid}")
    if not valid:
        warn("  沒有有效的 Extension ID,跳過登錄註冊。")
    return valid


# --- 主流程 ------------------------------------------------------------------


def main() -> int:
    print("=" * 60)
    print("  PWmgr 安裝腳本")
    print("=" * 60)
    print()

    if getattr(sys, "frozen", False):
        info("[1/5] 打包版 exe,相依套件已內建,略過檢查")
    else:
        info("[1/5] 檢查相依套件")
        if not check_deps():
            warn("請先 pip install -r requirements.txt 後重跑。")
            return 1
    print()

    info("[2/5] 寫入原生主機 wrapper")
    write_native_host_bat()
    print()

    info("[3/5] 寫入原生主機 manifest")
    extension_ids = prompt_extension_id()
    write_native_host_manifest(extension_ids)
    print()

    info("[4/5] 註冊登錄(Edge + Chrome)")
    register_windows_registry(extension_ids)
    print()

    info("[5/5] 建立開始功能表捷徑")
    create_start_menu_shortcut()
    print()

    info("=" * 60)
    info("完成!")
    info("")
    info("下一步:")
    info(f"  • 載入擴充功能(若還沒):{EXTENSION_DIR}")
    if not extension_ids:
        info("  • 拿到 Extension ID 後,重跑 python install.py 完成登錄註冊。")
    info("  • 啟動 GUI:在工作列右下角找到 PWmgr 圖示,")
    info("    或從開始功能表執行「PWmgr」,或直接:")
    info(f"      {pythonw_executable()} -m pwmgr")
    info("  • 按 Ctrl+Shift+L 顯示 / 隱藏主視窗。")
    info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
