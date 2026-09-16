"""PWmgr runtime 設定。

跟 ``pwmgr.config`` 不同:config 是 module-load 時的常數(路徑、上限、預設值);
settings 是 user 可在 GUI 改的執行期設定,存到 ``LOCALAPPDATA\\pwmgr\\settings.json``。

目前放 OTP 相關設定(主開關 + 訂閱 store 清單)。設計為一般化 dict,
之後要加新設定在這裡擴充就好。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .config import app_dir


SETTINGS_PATH: Path = app_dir() / "settings.json"
_settings_lock = threading.Lock()


# --- 預設值 ------------------------------------------------------------------

# OTP 主開關預設 ON — 保持現有行為(Outlook 常駐、monitor thread 跑)
# user 可在 GUI「OTP 監聽設定」隨時關掉 → monitor thread 停、native host
# get_otp 直接回 OTP_DISABLED,避免 Outlook 被 PWmgr 一直 polling 卡頓。
DEFAULT_OTP_ENABLED: bool = True

# None = 自動偵測(Exchange mailbox @開頭 + Outlook profile)
# []  = 不訂閱任何 store(等同關閉 OTP monitor)
# ["storeA", "storeB"] = 只訂閱這些 store
DEFAULT_OTP_SUBSCRIBED_STORES: list[str] | None = None


# --- 讀寫 --------------------------------------------------------------------

def load_settings() -> dict[str, Any]:
    """讀 settings.json,若不存在或壞掉就回預設空 dict(由 caller 補預設值)。"""
    with _settings_lock:
        if not SETTINGS_PATH.exists():
            return {}
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return data
        except Exception:
            return {}


def save_settings(data: dict[str, Any]) -> None:
    """寫 settings.json(atomic rename 避免寫一半被讀)。"""
    with _settings_lock:
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(SETTINGS_PATH)
        except Exception as e:
            print(f"[pwmgr][settings] save 失敗: {e}")


def get_otp_enabled() -> bool:
    """讀 OTP 主開關。預設 ON。"""
    return bool(load_settings().get("otp_enabled", DEFAULT_OTP_ENABLED))


def set_otp_enabled(enabled: bool) -> None:
    """寫 OTP 主開關。"""
    data = load_settings()
    data["otp_enabled"] = bool(enabled)
    save_settings(data)


def get_otp_subscribed_stores() -> list[str] | None:
    """讀目前設定中的 OTP 訂閱 store 清單;若無設定就回 None(自動偵測)。

    回傳值:
      - None:自動偵測(預設)
      - list[str]:user 指定要訂閱的 store 名稱
    """
    return load_settings().get("otp_subscribed_stores", DEFAULT_OTP_SUBSCRIBED_STORES)


def set_otp_subscribed_stores(stores: list[str] | None) -> None:
    """寫 OTP 訂閱 store 清單。None 表示回到自動偵測。"""
    data = load_settings()
    data["otp_subscribed_stores"] = stores
    save_settings(data)