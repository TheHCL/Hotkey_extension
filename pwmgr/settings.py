"""PWmgr runtime 設定。

跟 ``pwmgr.config`` 不同:config 是 module-load 時的常數(路徑、上限、預設值);
settings 是 user 可在 GUI 改的執行期設定,存到 ``LOCALAPPDATA\\pwmgr\\settings.json``。

目前放 OTP 相關設定(主開關 + 目標 folder)。設計為一般化 dict,
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

# OTP 主開關預設 OFF——只有按 extension「取得 OTP 驗證碼」才會查 Outlook
# (查 poll_seconds 秒內拿到就停、沒拿到也停),不會有任何背景常駐監聽。
# User 想使用 OTP 自動填入 → 到 GUI「OTP 設定」勾選「啟用 OTP 自動填入」即可。
DEFAULT_OTP_ENABLED: bool = False

# OTP 查詢的目標 folder 路徑(由 user 從 GUI「OTP 設定」選擇)。
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
    """讀 OTP 主開關。預設 OFF。"""
    return bool(load_settings().get("otp_enabled", DEFAULT_OTP_ENABLED))


def set_otp_enabled(enabled: bool) -> None:
    """寫 OTP 主開關。"""
    data = load_settings()
    data["otp_enabled"] = bool(enabled)
    save_settings(data)


def get_otp_target_folder() -> str | None:
    """讀 OTP 查詢的目標 folder 路徑。None 表示走預設行為(掃每個 store 的 Inbox)。

    路徑格式:`<store name>/<subfolder path>`,例如
    `"your-email@your-domain.com/Inbox/Dell OTP"`。
    """
    val = load_settings().get("otp_target_folder", DEFAULT_OTP_TARGET_FOLDER)
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def set_otp_target_folder(path: str | None) -> None:
    """寫 OTP 查詢的目標 folder 路徑。None 或空字串表示清掉(回預設行為)。"""
    data = load_settings()
    if path is None or not str(path).strip():
        # 移除這個 key,讓讀取時走 DEFAULT
        data.pop("otp_target_folder", None)
    else:
        data["otp_target_folder"] = str(path).strip()
    save_settings(data)