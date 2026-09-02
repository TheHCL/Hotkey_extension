"""跨行程檔案鎖。

Windows 使用 msvcrt.locking;其他平台用 fcntl.flock(若可用)。
測試時可用 NullLock 替換。
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Iterator

from .config import LOCK_TIMEOUT_SECONDS


class BusyError(RuntimeError):
    """取得檔案鎖逾時。"""


class _WindowsLock:
    """msvcrt.locking 的薄包裝。"""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(
            str(self.path),
            os.O_RDWR | os.O_CREAT,
            0o666,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                import msvcrt  # type: ignore[import-not-found]

                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                return
            except ImportError:
                # 非 Windows,直接視為取得
                return
            except OSError as e:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise BusyError(f"lock {self.path}: {e}") from e
                time.sleep(0.01)

    def __exit__(self, *exc) -> None:
        if self._fd is None:
            return
        try:
            import msvcrt  # type: ignore[import-not-found]

            try:
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        except ImportError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


class _PosixLock:
    """fcntl.flock 實作,主要給 Linux/macOS 開發/測試用。"""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o666)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as e:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise BusyError(f"lock {self.path}: {e}") from e
                time.sleep(0.01)

    def __exit__(self, *exc) -> None:
        if self._fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


class NullLock:
    """無鎖,給測試或單行程使用。"""

    def __init__(self, path: Path, timeout: float = 0.0):
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc) -> None:
        return None


def _select_lock_cls():
    if os.name == "nt":
        return _WindowsLock
    if os.name == "posix":
        return _PosixLock
    return NullLock


def index_lock(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """回傳 index.json 用的檔案鎖 context manager。

    鎖的是獨立的 `<path>.lock` 檔案,不是 index.json 本身——
    Windows 上 os.open() 預設不給 FILE_SHARE_DELETE,若鎖住 index.json
    本身,持鎖期間對它做 os.replace()(atomic write)會被系統拒絕
    (PermissionError: 存取被拒),即使是同一個 process 也一樣。
    """
    lock_path = path.with_name(path.name + ".lock")
    return _select_lock_cls()(lock_path, timeout=timeout)
