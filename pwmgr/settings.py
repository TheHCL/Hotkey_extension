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

# OTP 主開關預設 OFF(lazy mode)— Outlook 不會被 PWmgr background monitor thread
# 持續 polling 卡頓,只有按 extension「取得 OTP 驗證碼」才戳 Outlook(poll 15 秒內
# 拿到就停、沒拿到也停)。
#
# User 想恢復 background monitor(按按鈕秒回、cache 預熱)→ 到 GUI「OTP 監聽設定」
# 勾選「啟用 OTP 監聽」即可。
#
# 為什麼改 OFF:Outlook 2019 中文版 + Exchange 信箱背景 polling 會讓 Outlook UI
# 卡頓(每秒 round-trip 一次)。Lazy 預設讓 SA(這個 user)跟新裝 user 不踩這個坑。
# 既有 user 的 settings.json 已經存了 otp_enabled 值不會被 default 影響。
DEFAULT_OTP_ENABLED: bool = False

# None = 自動偵測(Exchange mailbox @開頭 + Outlook profile)
# []  = 不訂閱任何 store(等同關閉 OTP monitor)
# ["storeA", "storeB"] = 只訂閱這些 store
DEFAULT_OTP_SUBSCRIBED_STORES: list[str] | None = None

# OTP 監聽的目標 folder 路徑(由 user 從 GUI「OTP 監聽設定」選擇)。
# None = 走原本的預設行為(每個 store 的 Inbox)。
# "your-email@your-domain.com/Inbox/Dell OTP" = 只掃這個 folder,不遞迴全 subfolder。
# 路徑格式:`<store name>/<subfolder path>`,subfolder 用 `/` 分隔。
# 設計理由:Outlook rule 可能把 Dell OTP 信搬到自訂資料夾,user 手動指定比
# 程式自動遞迴所有 subfolder 更精準、也避免誤掃(草稿 / 寄件備份)。
DEFAULT_OTP_TARGET_FOLDER: str | None = None


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


def get_otp_target_folder() -> str | None:
    """讀 OTP 監聽的目標 folder 路徑。None 表示走預設行為(掃每個 store 的 Inbox)。

    路徑格式:`<store name>/<subfolder path>`,例如
    `"your-email@your-domain.com/Inbox/Dell OTP"`。
    """
    val = load_settings().get("otp_target_folder", DEFAULT_OTP_TARGET_FOLDER)
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def set_otp_target_folder(path: str | None) -> None:
    """寫 OTP 監聽的目標 folder 路徑。None 或空字串表示清掉(回預設行為)。"""
    data = load_settings()
    if path is None or not str(path).strip():
        # 移除這個 key,讓讀取時走 DEFAULT
        data.pop("otp_target_folder", None)
    else:
        data["otp_target_folder"] = str(path).strip()
    save_settings(data)