"""自動更新——由 `PWmgrSetup.exe --update` 觸發,或直接 `python update.py`。

流程:
1. 問 GitHub 有沒有比目前版本新的 release(``pwmgr.updater.check_for_update``)。
2. 有的話,先確保沒有殘留的 PWmgr.exe process 鎖著檔案。
3. 下載 PWmgr-<tag>.zip、解壓、備份舊檔、換新檔(任何一步失敗就從備份還原)。
4. 重新啟動 PWmgr.exe。

只更新 PWmgr.exe / _internal/ / chrome_extension/ 三者(release zip 的內容)。
PWmgrSetup.exe 本身不會自我更新——它變動的頻率遠低於程式邏輯,需要換版時
使用者手動重新下載即可。

前提:發布的 PWmgr-<tag>.zip 解壓後是「攤平」的(PWmgr.exe、_internal/、
chrome_extension/ 同一層,見 release.yml 的 packaging 步驟),使用者依照
README 安裝的實際目錄結構也是這個攤平佈局,PWmgrSetup.exe 就跟它們放在
同一層——``config.PROJECT_ROOT`` 在 frozen 模式下就是目前執行中 exe 自己
的所在目錄,剛好等於這個安裝目錄。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from pwmgr import updater
from pwmgr.config import PROJECT_ROOT

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def info(msg: str) -> None:
    print(f"[update] {msg}")


def warn(msg: str) -> None:
    print(f"[update][WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[update][ERROR] {msg}", file=sys.stderr)


# 更新只替換 release zip 的內容;PWmgrSetup.exe 本身不動。
_MANAGED_ITEMS = ("PWmgr.exe", "_internal", "chrome_extension")


def _kill_running_pwmgr() -> None:
    r = subprocess.run(
        ["taskkill", "/IM", "PWmgr.exe", "/F"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        info("  [OK] 已關閉正在執行的 PWmgr.exe")
    else:
        # 找不到 process 是正常情況(本來就沒開著),不算錯誤。
        info("  [..] 沒有偵測到執行中的 PWmgr.exe")


def _download(url: str, dest: Path) -> None:
    info(f"  下載 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "PWmgr-updater"})
    last_pct = -1
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        chunk_size = 1024 * 256
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            read += len(chunk)
            if total:
                pct = read * 100 // total
                if pct != last_pct and pct % 10 == 0:
                    info(f"  ...{pct}%")
                    last_pct = pct
    info(f"  [OK] 下載完成: {dest}")


def _swap_in_new_files(extracted_dir: Path, install_dir: Path) -> None:
    """把 extracted_dir 底下的檔案換到 install_dir,先備份舊的,失敗就還原。"""
    backups: list[tuple[Path, Path]] = []
    try:
        for name in _MANAGED_ITEMS:
            target = install_dir / name
            if not target.exists():
                continue
            backup = install_dir / f"{name}.bak"
            if backup.exists():
                shutil.rmtree(backup) if backup.is_dir() else backup.unlink()
            target.rename(backup)
            backups.append((target, backup))
            info(f"  [OK] 備份 {name} -> {name}.bak")

        for name in _MANAGED_ITEMS:
            src = extracted_dir / name
            if not src.exists():
                warn(f"  release 內容缺少 {name},跳過")
                continue
            shutil.move(str(src), str(install_dir / name))
            info(f"  [OK] 換上新版 {name}")
    except Exception as e:
        err(f"  替換檔案失敗,回復備份: {e}")
        for target, backup in backups:
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            backup.rename(target)
        raise

    for target, backup in backups:
        if backup.exists():
            shutil.rmtree(backup) if backup.is_dir() else backup.unlink()


def _relaunch_pwmgr(install_dir: Path) -> None:
    exe = install_dir / "PWmgr.exe"
    if not exe.exists():
        warn(f"  找不到 {exe},略過重新啟動")
        return
    try:
        subprocess.Popen(
            [str(exe)],
            cwd=str(install_dir),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        info(f"  [OK] 已重新啟動 {exe}")
    except Exception as e:
        warn(f"  重新啟動失敗: {e}")


def main() -> int:
    print("=" * 60)
    print("  PWmgr 更新")
    print("=" * 60)
    print()

    if not getattr(sys, "frozen", False):
        err("開發模式(未打包)沒有 exe 可更新,請改用 git pull。")
        return 1

    info("[1/5] 檢查 GitHub 最新版本")
    avail = updater.check_for_update()
    if avail is None:
        info("  目前已是最新版本,或查詢失敗。")
        return 0
    info(f"  [OK] 發現新版本 v{avail.version}")
    print()

    install_dir = PROJECT_ROOT
    info(f"[2/5] 關閉正在執行的 PWmgr.exe(安裝目錄: {install_dir})")
    _kill_running_pwmgr()
    print()

    with tempfile.TemporaryDirectory(prefix="pwmgr-update-") as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "update.zip"
        extract_dir = tmp_path / "extracted"

        info("[3/5] 下載新版本")
        try:
            _download(avail.zip_url, zip_path)
        except Exception as e:
            err(f"  下載失敗: {e}")
            return 1
        print()

        info("[4/5] 解壓縮並替換檔案")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
            _swap_in_new_files(extract_dir, install_dir)
        except Exception as e:
            err(f"  更新失敗,已還原舊版本: {e}")
            return 1
        print()

    info("[5/5] 重新啟動 PWmgr")
    _relaunch_pwmgr(install_dir)
    print()

    info("=" * 60)
    info(f"更新完成,目前版本 v{avail.version}。")
    info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
