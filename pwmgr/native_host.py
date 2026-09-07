"""Chrome/Edge 原生訊息主機。

線框:[4 bytes LE uint32 length][UTF-8 JSON payload]
Chrome 規格:https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging

- 讀 stdin 二進位;訊息分派到 storage 對應方法。
- write_current_url 為擴充功能「心跳」,用於 GUI 高亮。
- Chrome 100ms 逾時:第一次訊息進來前可預熱 keyring。
- 收到 stdin EOF 優雅退出。
"""

from __future__ import annotations

import json
import struct
import sys
import threading
from typing import Any

from . import storage
from .config import MAX_PASSWORD_BYTES
from .models import PasswordEntry

# --- 例外 --------------------------------------------------------------------


class BadRequestError(ValueError):
    pass


class NativeHostError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message}


# --- I/O ---------------------------------------------------------------------


def _read_exact(stream, n: int) -> bytes:
    """讀取剛好 n bytes;EOF 回空 bytes。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return b""
        buf.extend(chunk)
    return bytes(buf)


def _read_message(stdin) -> dict[str, Any] | None:
    """讀取一則訊息,回傳 dict;EOF 回 None。"""
    header = _read_exact(stdin, 4)
    if not header:
        return None
    if len(header) < 4:
        raise BadRequestError("訊息長度標頭不完整")
    (length,) = struct.unpack("<I", header)
    if length == 0:
        raise BadRequestError("訊息長度為 0")
    if length > 1024 * 1024:
        # 防 DoS——大於 1 MB 拒絕
        raise BadRequestError(f"訊息過大: {length} bytes")
    payload = _read_exact(stdin, length)
    if len(payload) < length:
        raise BadRequestError("訊息內容不完整")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BadRequestError(f"JSON 解析失敗: {e}") from e


def _write_message(stdout, message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stdout.write(struct.pack("<I", len(payload)))
    stdout.write(payload)
    stdout.flush()


# --- 訊息分派 ----------------------------------------------------------------


def _handle_query(req: dict[str, Any]) -> dict[str, Any]:
    url = req.get("url", "")
    if not isinstance(url, str):
        raise BadRequestError("url 必須是字串")
    matches = storage.query_by_url(url)
    return {
        "ok": True,
        "matches": [
            {
                "id": e.id,
                "label": e.label,
                "username": e.username,
                "url": e.url,
                "launch_url": e.launch_url,
            }
            for e in matches
        ],
    }


def _handle_fetch(req: dict[str, Any]) -> dict[str, Any]:
    eid = req.get("id", "")
    if not isinstance(eid, str) or not eid:
        raise BadRequestError("id 必填")
    try:
        entry = storage.get_entry(eid)
    except storage.EntryNotFoundError:
        raise NativeHostError("NOT_FOUND", f"id={eid} 不存在")
    pwd = storage.get_password(eid)
    return {"ok": True, "entry": entry.to_dict(), "password": pwd or ""}


def _handle_list(_req: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "entries": [e.to_dict() for e in storage.load_index()],
    }


def _handle_save(req: dict[str, Any]) -> dict[str, Any]:
    entry_data = req.get("entry")
    password = req.get("password", "")
    if not isinstance(entry_data, dict):
        raise BadRequestError("entry 必填")
    if not isinstance(password, str):
        raise BadRequestError("password 必須是字串")
    if not password:
        raise BadRequestError("password 不可為空字串")
    eid = entry_data.get("id")
    if eid:
        if not isinstance(eid, str) or not eid:
            raise BadRequestError("id 若提供須為非空字串")
        try:
            entry = storage.get_entry(eid)
        except storage.EntryNotFoundError:
            raise NativeHostError("NOT_FOUND", f"id={eid} 不存在")
        for k in ("label", "url", "username", "notes", "launch_url"):
            if k in entry_data:
                setattr(entry, k, entry_data[k])
    else:
        # 新建——從 entry_data 拿 label/url/username/notes/launch_url,id 由 PasswordEntry.new 產生
        entry = PasswordEntry.new(
            label=str(entry_data.get("label", "")),
            url=str(entry_data.get("url", "")),
            username=str(entry_data.get("username", "")),
            notes=str(entry_data.get("notes", "")),
            launch_url=str(entry_data.get("launch_url", "")),
        )
    new_id = storage.save_entry(entry, password)
    return {"ok": True, "id": new_id}


def _handle_delete(req: dict[str, Any]) -> dict[str, Any]:
    eid = req.get("id", "")
    if not isinstance(eid, str) or not eid:
        raise BadRequestError("id 必填")
    removed = storage.delete_entry(eid)
    if not removed:
        raise NativeHostError("NOT_FOUND", f"id={eid} 不存在")
    return {"ok": True}


def _handle_report_url(req: dict[str, Any]) -> dict[str, Any]:
    url = req.get("url", "")
    if not isinstance(url, str):
        raise BadRequestError("url 必須是字串")
    tab_id = req.get("tabId")
    storage.write_current_url(url, tab_id)
    return {"ok": True}


def _handle_ping(_req: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "pong": True}


_DISPATCH: dict[str, Any] = {
    "query": _handle_query,
    "fetch": _handle_fetch,
    "list": _handle_list,
    "save": _handle_save,
    "delete": _handle_delete,
    "report_url": _handle_report_url,
    "ping": _handle_ping,
}


def _dispatch(req: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(req, dict):
        raise BadRequestError("訊息必須是 JSON 物件")
    msg_type = req.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise BadRequestError("type 必填")
    handler = _DISPATCH.get(msg_type)
    if handler is None:
        raise BadRequestError(f"未知訊息類型: {msg_type}")
    return handler(req)


# --- 預熱 --------------------------------------------------------------------


def _warmup_keyring() -> None:
    """第一次訊息進來前在背景 thread 預熱,降低 100ms 逾時風險。"""
    try:
        import keyring

        # 嘗試 set/get/delete 一個暫時條目以觸發 backend 載入
        try:
            keyring.set_password("pwmgr", "__warmup__", "x")
            try:
                keyring.delete_password("pwmgr", "__warmup__")
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        pass


# --- 進入點 ------------------------------------------------------------------


def run() -> int:
    """主迴圈:從 stdin 讀、寫 stdout。"""
    # 預熱:在背景 thread 跑,不等它完成
    threading.Thread(target=_warmup_keyring, daemon=True).start()

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        try:
            req = _read_message(stdin)
        except BadRequestError as e:
            # 協定錯誤——回錯誤並繼續
            try:
                _write_message(stdout, NativeHostError("BAD_REQUEST", str(e)).to_dict())
            except Exception:
                return 1
            continue
        except Exception as e:
            # 無法恢復的 I/O 錯誤
            return 1

        if req is None:
            # stdin EOF——Chrome 斷線,優雅退出
            return 0

        try:
            resp = _dispatch(req)
        except NativeHostError as e:
            resp = e.to_dict()
        except storage.PasswordTooLongError as e:
            resp = NativeHostError("PASSWORD_TOO_LONG", str(e)).to_dict()
        except storage.NotesTooLongError as e:
            resp = NativeHostError("NOTES_TOO_LONG", str(e)).to_dict()
        except storage.EntryNotFoundError as e:
            resp = NativeHostError("NOT_FOUND", str(e)).to_dict()
        except storage.BusyError:
            resp = NativeHostError("BUSY", "密碼管理員忙碌中,稍後重試").to_dict()
        except BadRequestError as e:
            resp = NativeHostError("BAD_REQUEST", str(e)).to_dict()
        except Exception as e:
            resp = NativeHostError("INTERNAL", f"{type(e).__name__}: {e}").to_dict()

        try:
            _write_message(stdout, resp)
        except Exception:
            return 1
