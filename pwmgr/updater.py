"""檢查 GitHub Release 是否有新版本。

只負責「查」,不動檔案——實際下載/替換由 ``update.py``(打包進
``PWmgrSetup.exe --update``)執行,因為執行中的 PWmgr.exe 沒辦法覆寫自己的
exe/dll。這裡刻意不用 ``requests``,專案目前沒有這個依賴,stdlib
``urllib.request`` 就夠。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from .version import __version__ as CURRENT_VERSION

_logger = logging.getLogger(__name__)

REPO = "TheHCL/Hotkey_extension"
RELEASES_API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ZIP_ASSET_PREFIX = "PWmgr-"
ZIP_ASSET_SUFFIX = ".zip"


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # 例如 "1.1.0"(不含開頭 v)
    zip_url: str  # PWmgr-<tag>.zip 的直接下載連結
    release_url: str  # Release 頁面(給瀏覽器 fallback / 展示用)


def get_current_version() -> str:
    return CURRENT_VERSION


def _parse_version(v: str) -> tuple[int, ...]:
    """``"1.2.3"`` -> ``(1, 2, 3)``。格式不對就丟 ValueError,由呼叫端擋掉。"""
    return tuple(int(p) for p in v.strip().split("."))


def check_for_update(timeout: float = 5.0) -> UpdateInfo | None:
    """回傳比目前版本新的 ``UpdateInfo``,沒有新版本或查詢失敗都回傳 ``None``。

    絕不拋例外——任何網路/格式問題都吞掉,因為這個函式會被 GUI 背景 thread
    呼叫,不能讓一次查詢失敗拖垮 app。
    """
    try:
        req = urllib.request.Request(
            RELEASES_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "PWmgr-update-checker",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)

        tag_name = str(data.get("tag_name") or "").strip()
        latest_version = tag_name[1:] if tag_name.startswith("v") else tag_name
        if not latest_version:
            return None

        if _parse_version(latest_version) <= _parse_version(CURRENT_VERSION):
            return None

        zip_name = f"{ZIP_ASSET_PREFIX}{tag_name}{ZIP_ASSET_SUFFIX}"
        zip_url = None
        for asset in data.get("assets") or []:
            if asset.get("name") == zip_name:
                zip_url = asset.get("browser_download_url")
                break
        if not zip_url:
            return None

        release_url = str(data.get("html_url") or f"https://github.com/{REPO}/releases/latest")

        return UpdateInfo(version=latest_version, zip_url=zip_url, release_url=release_url)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError, OSError) as e:
        _logger.info("check_for_update 失敗(可能是網路問題): %s", e)
        return None
    except Exception:
        # 保底:更新檢查絕對不能讓呼叫端炸掉。
        _logger.exception("check_for_update 未預期例外")
        return None
