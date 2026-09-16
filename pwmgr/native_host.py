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

from . import outlook_monitor, storage
from .config import (
    MAX_CAPTCHA_BYTES,
    MAX_GROUP_CHARS,
    MAX_PASSWORD_BYTES,
    OTP_CACHE_TTL_SECONDS,
    OTP_CODE_REGEX,
    OTP_LOOKBACK_COUNT,
    OTP_SUBJECT_PATTERNS,
    otp_cache_path,
)
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


# --- OTP 自動填 ----------------------------------------------------------------
#
# 由 Chrome extension 透過 popup 觸發:user 在 OTP 頁按下「取得驗證碼」按鈕 →
# popup → background → native host → 此 handler。
#
# 流程:
#   1. 讀 PWmgr GUI 的 outlook_monitor 寫的 cache JSON(快,通常命中)
#   2. cache miss 或過期 → on-demand 開 Outlook COM 翻最近 Inbox(慢但可用)
#
# outlook_monitor.get_latest_otp 已把以上兩個策略封裝好,直接呼叫。

_OTP_MAX_AGE_SECONDS_LIMIT = 3600  # 上限 1 小時,避免 caller 傳怪值


def _handle_get_otp(req: dict[str, Any]) -> dict[str, Any]:
    """取得最近一封符合 pattern 的 OTP code。

    若 user 在 GUI 把 OTP 主開關關掉(otp_enabled=False),直接回 OTP_DISABLED,
    不開 Outlook COM、不讀 cache。理由:既然 user 明確表示不想被 OTP 機制
    打擾,就不該偷偷 on-demand 去戳 Outlook(那也會卡)。
    """
    try:
        from . import settings as otp_settings

        if not otp_settings.get_otp_enabled():
            return {
                "ok": False,
                "code": "OTP_DISABLED",
                "error": "OTP 監聽已停用(請到 PWmgr GUI「OTP 監聽設定」啟用)",
            }
    except Exception:
        # settings 讀失敗不擋 — fallback 走原本流程
        pass

    max_age = req.get("max_age_seconds")
    if max_age is None:
        max_age = OTP_CACHE_TTL_SECONDS
    if not isinstance(max_age, int) or max_age <= 0:
        raise BadRequestError("max_age_seconds 必須是正整數")
    max_age = min(max_age, _OTP_MAX_AGE_SECONDS_LIMIT)

    result = outlook_monitor.get_latest_otp(
        cache_path=otp_cache_path(),
        subject_patterns=OTP_SUBJECT_PATTERNS,
        code_regex=OTP_CODE_REGEX,
        max_age_seconds=max_age,
        # on-demand fallback 才會用到
        # (get_latest_otp 內部呼叫 fetch_latest_otp;lookback 透過 OTP_LOOKBACK_COUNT 預設)
    )
    # 額外附帶一些 debug 資訊給 popup(不影響 content script 邏輯)
    if isinstance(result, dict) and result.get("ok"):
        return {
            "ok": True,
            "code": str(result.get("code", "")),
            "subject": str(result.get("subject", "")),
            "received_at": float(result.get("received_at", 0.0)),
            "source": str(result.get("source", "")),
        }
    # 失敗也照原樣回(讓前端用 code 欄位判斷錯誤種類)
    return result if isinstance(result, dict) else {
        "ok": False,
        "code": "UNKNOWN",
        "error": "outlook_monitor 回傳格式錯誤",
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
    "get_otp": _handle_get_otp,
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
#
# 為什麼要預熱:
# Chrome MV3 service worker 是 event-driven,popup / content 一觸發就 spawn 一個新的
# native host Python process(沒繼承 GUI 那邊的 import cache,完全 cold start)。
# cold start 成本主要來自 pywin32 native DLL load + Outlook COM proxy 建立 + session
# 列舉,新機器可達 2-5 秒。Chrome 對 native message 有 stdin-read timeout(實測約 5s),
# Python 來不及讀 stdin 就被 Chrome 殺掉 → port disconnect → 訊息丟失。
#
# 解法:在 native host 進主迴圈前用 background thread 把這些東西預熱掉。主 thread
# 第一個訊息進來時,COM proxy 已經建好,只需要 30-100ms 就能回應。background.js 的
# getOtp handler 仍會有 cold-start retry 當最後保險。
#
# _warmup_keyring:預熱 Windows Credential Manager backend(原本就有)
# _warmup_outlook:預熱 pywin32 + Outlook COM session(這次新增)


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


def _warmup_outlook() -> None:
    """背景 thread 預熱 Outlook COM session,把 cold start 成本提前到主迴圈之前。

    跟 outlook_monitor.fetch_latest_otp 走同一條路:
      1. import pythoncom + win32com(DLL load、COM registration,主要 cold start 成本)
      2. pythoncom.CoInitialize()(STA — Outlook 需要)
      3. GetActiveObject → fallback Dispatch 取 Outlook Application proxy
      4. session.Folders.Count 觸發完整 proxy 鏈(stores list)

    不列 Inbox items(那是 main thread 的事,避免跟 OutlookMonitor 搶)。所有錯誤吞
    掉 — warmup 失敗不該 crash native host,後續 fetch_latest_otp fallback 還能救,
    或回 OUTLOOK_UNAVAILABLE 給 caller。
    """
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception:
        return
    try:
        pythoncom.CoInitialize()
    except Exception:
        return
    try:
        try:
            outlook = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
            except Exception:
                return
        # 觸發完整 proxy 鏈(stores list)
        try:
            session = outlook.Session
            _ = int(session.Folders.Count)
        except Exception:
            return
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


# --- 進入點 ------------------------------------------------------------------


def run() -> int:
    """主迴圈:從 stdin 讀、寫 stdout。"""
    # 預熱:在背景 thread 跑,不等它完成
    threading.Thread(target=_warmup_keyring, daemon=True).start()
    threading.Thread(target=_warmup_outlook, daemon=True).start()

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
