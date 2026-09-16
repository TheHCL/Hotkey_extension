"""Outlook 完整 store / folder probe。

列出所有 store + 每個 store 下的 folder hierarchy,確認:
  - 有幾個 data file(PST/OST/Exchange mailbox)
  - 哪個 store 才有 Dell OTP 信
  - GetDefaultFolder(6) 拿到的 Inbox 屬於哪個 store

跑法:python tests/probe_outlook.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _walk(folder, depth: int, prefix: str = "") -> None:
    """遞迴列舉 folder,depth 控制深度避免太深。"""
    if depth <= 0:
        return
    try:
        sub_count = int(folder.Folders.Count)
    except Exception:
        return
    for j in range(sub_count):
        try:
            sub = folder.Folders.Item(j + 1)
            items_count = "(no Items)" if not _has_items(sub) else str(int(sub.Items.Count))
            print(f"{prefix}└─ {sub.Name!r}  (Items.Count={items_count})")
            _walk(sub, depth - 1, prefix + "   ")
        except Exception as e:
            print(f"{prefix}└─ <error: {e}>")


def _has_items(folder) -> bool:
    try:
        _ = folder.Items
        return True
    except Exception:
        return False


def _full_path(folder) -> str:
    """從 folder 一路爬到根,組成 store/.../folder 完整路徑。"""
    parts = [str(folder.Name or "")]
    cur = folder
    while True:
        try:
            parent = cur.Parent
            if parent is None or parent is cur:
                break
            parts.append(str(parent.Name or ""))
            cur = parent
        except Exception:
            break
    return "/".join(reversed(parts))


def main() -> int:
    try:
        import win32com.client
        import pythoncom
    except ImportError as e:
        print(f"FAIL: {e}")
        return 1

    pythoncom.CoInitialize()
    try:
        print("=== Outlook Store / Folder probe ===\n")

        try:
            outlook = win32com.client.GetActiveObject("Outlook.Application")
            print("Connected via GetActiveObject\n")
        except Exception:
            outlook = win32com.client.Dispatch("Outlook.Application")
            print("Connected via Dispatch\n")

        session = outlook.Session
        print(f"Session.CurrentProfileName = {session.CurrentProfileName!r}\n")

        print("Session.Folders (top-level stores):")
        try:
            top_folders = session.Folders
            for i in range(int(top_folders.Count)):
                try:
                    store = top_folders.Item(i + 1)
                    print(f"  [{i+1}] {store.Name!r}")
                    _walk(store, depth=2, prefix="      ")
                except Exception as e:
                    print(f"  [{i+1}] <error: {e}>")
        except Exception as e:
            print(f"  FAIL: {e}")

        print("\nGetDefaultFolder 對照:")
        folder_specs = [
            (6, "Inbox"),
            (5, "Sent"),
            (4, "Outbox"),
            (3, "Deleted"),
            (23, "Junk"),
        ]
        for fid, label in folder_specs:
            try:
                f = session.GetDefaultFolder(fid)
                count = "(no Items)" if not _has_items(f) else str(int(f.Items.Count))
                print(f"  [{fid}] {label}: {f.Name!r} at {_full_path(f)} (Items.Count={count})")
            except Exception as e:
                print(f"  [{fid}] {label}: <{type(e).__name__}: {e}>")
    finally:
        pythoncom.CoUninitialize()

    print("\n=== probe 結束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())