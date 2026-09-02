"""解除安裝——移除登錄、捷徑;詢問是否刪資料。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pwmgr.config import (
    BIN_DIR,
    HOST_NAME,
    NATIVE_HOST_BAT,
    NATIVE_HOST_MANIFEST,
    PROJECT_ROOT,
    app_dir,
)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def info(msg: str) -> None:
    print(f"[uninstall] {msg}")


def confirm(msg: str, default_no: bool = True) -> bool:
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    raw = input(msg + suffix).strip().lower()
    if not raw:
        return not default_no
    return raw in ("y", "yes")


def unregister_windows_registry() -> None:
    if os.name != "nt":
        return
    for browser, reg_root in [
        ("Edge",  r"HKCU\Software\Microsoft\Edge\NativeMessagingHosts"),
        ("Chrome", r"HKCU\Software\Google\Chrome\NativeMessagingHosts"),
    ]:
        key_path = f"{reg_root}\\{HOST_NAME}"
        r = subprocess.run(
            ["reg", "delete", key_path, "/f"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            info(f"  [OK] 移除 {browser} 登錄")
        elif "無法找到" in r.stderr or "cannot find" in r.stderr.lower():
            info(f"  [..] {browser} 登錄本來就不存在")
        else:
            info(f"  [!] {browser} 登錄移除失敗: {r.stderr.strip() or r.stdout.strip()}")


def remove_shortcut() -> None:
    if os.name != "nt":
        return
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            start_menu = winreg.QueryValueEx(key, "Start Menu")[0]
    except OSError:
        return
    shortcut_dir = Path(start_menu) / "Programs" / "PWmgr"
    if shortcut_dir.exists():
        try:
            shutil.rmtree(shortcut_dir)
            info(f"  [OK] 移除開始功能表捷徑: {shortcut_dir}")
        except OSError as e:
            info(f"  [!] 捷徑移除失敗: {e}")


def remove_bin_files() -> None:
    for p in (NATIVE_HOST_BAT, NATIVE_HOST_MANIFEST):
        if p.exists():
            try:
                p.unlink()
                info(f"  [OK] 移除 {p.name}")
            except OSError as e:
                info(f"  [!] 移除 {p.name} 失敗: {e}")
    # 不刪 bin/ 整個目錄——可能還有其他檔


def remove_data() -> None:
    data = app_dir()
    if not data.exists():
        info("  [..] 資料目錄本來就不存在")
        return
    if not confirm(f"刪除資料目錄 {data} (index.json + current_url.json)?", default_no=True):
        info("  [..] 保留資料")
        return
    try:
        shutil.rmtree(data)
        info(f"  [OK] 移除 {data}")
    except OSError as e:
        info(f"  [!] 移除失敗: {e}")
    info("  [..] Credential Manager 中的密碼條目需手動到「認證管理員」刪除")
    info("       (篩選: pwmgr)")


def remove_keyring_entries() -> None:
    try:
        import keyring
    except ImportError:
        info("  [..] keyring 未安裝,跳過")
        return
    try:
        import keyring.errors
    except ImportError:
        keyring.errors = None
    backend = keyring.get_keyring()
    creds = []
    if hasattr(backend, "credentials"):
        try:
            creds = list(backend.credentials())
        except Exception:
            creds = []
    pwmgr_creds = [c for c in creds if getattr(c, "username", "").startswith("pwmgr:")]
    if not pwmgr_creds:
        info("  [..] 沒有 pwmgr:* 的 keyring 條目")
        return
    if not confirm(f"刪除 {len(pwmgr_creds)} 個 Credential Manager 條目?", default_no=False):
        info("  [..] 保留 keyring 條目")
        return
    for c in pwmgr_creds:
        try:
            keyring.delete_password("pwmgr", c.username)
            info(f"  [OK] 刪除 {c.username}")
        except Exception as e:
            info(f"  [!] 刪除 {c.username} 失敗: {e}")


def main() -> int:
    print("=" * 60)
    print("  PWmgr 解除安裝")
    print("=" * 60)
    print()
    info("[1/4] 移除登錄(Edge + Chrome)")
    unregister_windows_registry()
    print()
    info("[2/4] 移除開始功能表捷徑")
    remove_shortcut()
    print()
    info("[3/4] 移除 bin/ 中的原生主機檔案")
    remove_bin_files()
    print()
    info("[4/4] 處理資料與 keyring")
    remove_data()
    remove_keyring_entries()
    print()
    info("=" * 60)
    info("完成。如需重裝,再執行 python install.py 即可。")
    info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
