"""Chrome/Edge 原生訊息主機。

線框:[4 bytes LE uint32 length][UTF-8 JSON payload]
Chrome 規格:https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging

- 讀 stdin 二進位;訊息分派到 storage 對應方法。
- write_current_url 為擴充功能「心跳」,用於 GUI 高亮。
- Chrome 100ms 逾時:第一次訊息進來前可預熱 keyring。
- 收到 stdin EOF 優雅退出。

Captcha 辨識:
- Chrome extension 偵測到頁面上的 captcha <img> 後,把圖片 bytes 丟過來
  (dataURL 或從 URL 抓下來),native host 跑 ddddocr std+beta 兩個模型,
  一致才回答案,不一致就回失敗(讓使用者手動打)。
- ddddocr 第一次 import + 模型載入要 1-2 秒,刻意做成 lazy:
  第一次 solve_captcha 才載入,避免冷啟動 100ms 逾時爆炸。
"""

from __future__ import annotations

import base64
import json
import re
import struct
import sys
import threading
from typing import Any

from . import storage
from .config import MAX_CAPTCHA_BYTES, MAX_GROUP_CHARS, MAX_PASSWORD_BYTES
from .models import PasswordEntry

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# 允許的 captcha 答案字元:小寫字母 + 數字(實務常見)
_CAPTCHA_TEXT_RE = re.compile(r"^[a-z0-9]{3,12}$")
# dataURL 前綴:data:<mime>;base64,<payload>
_DATAURL_RE = re.compile(r"^data:[^;]+;base64,(.+)$", re.DOTALL)

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


def _normalize_group(value: Any) -> str:
    """正規化 group 欄位:strip 空白、長度上限、空字串保留(代表「未分類」)。"""
    if not isinstance(value, str):
        raise BadRequestError("group 必須是字串")
    value = value.strip()
    if len(value) > MAX_GROUP_CHARS:
        raise BadRequestError(f"group 超過 {MAX_GROUP_CHARS} 字")
    return value


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
                "group": e.group,
            }
            for e in matches
        ],
        "group_colors": storage.load_group_colors(),
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
        "group_colors": storage.load_group_colors(),
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
        if "group" in entry_data:
            entry.group = _normalize_group(entry_data["group"])
    else:
        # 新建——從 entry_data 拿 label/url/username/notes/launch_url/group,id 由 PasswordEntry.new 產生
        entry = PasswordEntry.new(
            label=str(entry_data.get("label", "")),
            url=str(entry_data.get("url", "")),
            username=str(entry_data.get("username", "")),
            notes=str(entry_data.get("notes", "")),
            launch_url=str(entry_data.get("launch_url", "")),
            group=_normalize_group(entry_data.get("group", "")),
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


def _handle_get_group_colors(_req: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "group_colors": storage.load_group_colors()}


def _handle_set_group_color(req: dict[str, Any]) -> dict[str, Any]:
    """設定(或移除,color=null)某個 group 的展示色。"""
    group = _normalize_group(req.get("group", ""))
    # _normalize_group 允許空字串("未分類"在 GUI 用空字串表示)
    # 但 group_colors 不該為空字串(既不會被視為一個有意義的群組,
    # 也避免和未分類本身的 NEUTRAL_COLOR 衝突)
    if not group:
        raise BadRequestError("group 不可為空字串")
    color_raw = req.get("color")
    if color_raw is None:
        # 移除路徑
        storage.set_group_color(group, None)
        return {"ok": True}
    # 設定路徑:同一個 regex 在 storage 層再驗一次(defense-in-depth)
    if not isinstance(color_raw, str) or not _HEX_RE.match(color_raw):
        raise BadRequestError("color 必須是 #rrggbb 形式")
    storage.set_group_color(group, color_raw.lower())
    return {"ok": True}


# --- Captcha 辨識 ------------------------------------------------------------
#
# 設計:Chrome extension 把 captcha 圖片的 bytes 丟過來,我們跑 ddddocr 兩個
# 預訓練模型(std + beta),只有答案一致時才回傳,不一致就回失敗代碼讓使用者
# 自己打。理由見 commit message:此 ensemble 在 19 張樣本上 100% precision
# (一致即正確),0 錯誤自動填入風險。
#
# ddddocr 第一次 import + 兩個模型載入要 1-2 秒,刻意做成 lazy
# (第一次呼叫 _ensure_ocr 才載入),避免 native host 冷啟動 100ms 逾時。
#
# 圖片來源支援兩種:
#   1. dataURL(由 extension 端 toDataURL):直接 base64 解碼
#   2. http(s) URL:用 urllib 抓(若 captcha 需要 session cookie 則
#      走不上,改走 (1) 在 extension canvas 轉 dataURL 再送)
_OCR_LOCK = threading.Lock()
_OCR_STD: Any = None
_OCR_BETA: Any = None


def _ensure_ocr() -> tuple[Any, Any] | None:
    """Lazy-load 兩個 ddddocr 模型。回傳 (std, beta);載入失敗回 None。"""
    global _OCR_STD, _OCR_BETA
    with _OCR_LOCK:
        if _OCR_STD is not None and _OCR_BETA is not None:
            return _OCR_STD, _OCR_BETA
        try:
            import ddddocr  # type: ignore
        except ImportError:
            return None
        try:
            _OCR_STD = ddddocr.DdddOcr(show_ad=False)
            _OCR_BETA = ddddocr.DdddOcr(show_ad=False, beta=True)
        except Exception:
            _OCR_STD = None
            _OCR_BETA = None
            return None
        return _OCR_STD, _OCR_BETA


def _decode_captcha_payload(req: dict[str, Any]) -> bytes:
    """從 req 裡的 image(dataURL 或 URL)解出圖片 bytes。

    兩種輸入格式:
      {"image": "data:image/png;base64,..."} -> 直接 base64 解碼
      {"image_url": "https://..."}            -> urllib 抓
    """
    image = req.get("image")
    if isinstance(image, str) and image:
        m = _DATAURL_RE.match(image)
        if not m:
            raise BadRequestError("image 必須是 data URL (data:<mime>;base64,...)")
        try:
            raw = base64.b64decode(m.group(1), validate=False)
        except Exception as e:
            raise BadRequestError(f"dataURL base64 解碼失敗: {e}") from e
        if len(raw) == 0:
            raise BadRequestError("image 為空")
        if len(raw) > MAX_CAPTCHA_BYTES:
            raise BadRequestError(f"image 超過 {MAX_CAPTCHA_BYTES} bytes")
        return raw
    image_url = req.get("image_url")
    if isinstance(image_url, str) and image_url:
        if not re.match(r"^https?://", image_url, re.IGNORECASE):
            raise BadRequestError("image_url 必須是 http(s)")
        try:
            import urllib.request
            with urllib.request.urlopen(image_url, timeout=5) as resp:
                raw = resp.read()
        except Exception as e:
            raise NativeHostError("FETCH_FAIL", f"抓 captcha 圖失敗: {e}") from e
        if len(raw) > MAX_CAPTCHA_BYTES:
            raise BadRequestError(f"image 超過 {MAX_CAPTCHA_BYTES} bytes")
        return raw
    raise BadRequestError("image 或 image_url 必填")


def _handle_solve_captcha(req: dict[str, Any]) -> dict[str, Any]:
    """解 captcha 圖:ddddocr std + beta 兩個模型,只有一致才回答案。"""
    raw = _decode_captcha_payload(req)

    ocr_pair = _ensure_ocr()
    if ocr_pair is None:
        raise NativeHostError(
            "OCR_UNAVAILABLE",
            "ddddocr 未安裝或載入失敗,請 pip install ddddocr",
        )
    std, beta = ocr_pair

    try:
        text_std = std.classification(raw).strip().lower()
        text_beta = beta.classification(raw).strip().lower()
    except Exception as e:
        raise NativeHostError("OCR_FAIL", f"識別失敗: {type(e).__name__}: {e}") from e

    # 過濾:只接受 [a-z0-9]{3,12} 形式(實務 captcha 都是),否則視為亂判
    if not _CAPTCHA_TEXT_RE.match(text_std):
        text_std = ""
    if not _CAPTCHA_TEXT_RE.match(text_beta):
        text_beta = ""

    if text_std and text_std == text_beta:
        return {
            "ok": True,
            "text": text_std,
            "confidence": "high",  # 兩模型一致 = 高信心
            "std": text_std,
            "beta": text_beta,
        }
    # 不一致:回 ok=False 但附上兩個原始答案,前端可以決定要不要顯示讓人挑
    return {
        "ok": False,
        "code": "LOW_CONFIDENCE",
        "std": text_std,
        "beta": text_beta,
        "message": "兩個模型答案不一致,請手動輸入",
    }


_DISPATCH: dict[str, Any] = {
    "query": _handle_query,
    "fetch": _handle_fetch,
    "list": _handle_list,
    "save": _handle_save,
    "delete": _handle_delete,
    "report_url": _handle_report_url,
    "ping": _handle_ping,
    "get_group_colors": _handle_get_group_colors,
    "set_group_color": _handle_set_group_color,
    "solve_captcha": _handle_solve_captcha,
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
        except storage.BadRequestError as e:
            resp = NativeHostError("BAD_REQUEST", str(e)).to_dict()
        except Exception as e:
            resp = NativeHostError("INTERNAL", f"{type(e).__name__}: {e}").to_dict()

        try:
            _write_message(stdout, resp)
        except Exception:
            return 1
