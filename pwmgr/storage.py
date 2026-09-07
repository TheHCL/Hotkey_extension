"""儲存層——唯一接觸 keyring 與 index.json 的地方。

設計:
- index.json 存所有條目的「公開」metadata(不含密碼)。
- 密碼獨立存於 Windows Credential Manager,key 為 (KEYRING_SERVICE, "pwmgr:<entry_id>")。
- 所有 read-modify-write 持 ipc.index_lock,確保 GUI 與原生主機不互相踩。
- 寫入採 atomic rename(os.replace),Windows 上加 retry 對付 antivirus 短暫持有新檔。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import (
    KEYRING_SERVICE,
    MAX_NOTES_CHARS,
    MAX_PASSWORD_BYTES,
    current_url_path,
    index_path,
)
from .ipc import BusyError, index_lock
from .matcher import matches
from .models import PasswordEntry


# --- 例外 ----------------------------------------------------------------------


class PasswordTooLongError(ValueError):
    """密碼超過 Windows Credential Manager 上限。"""


class NotesTooLongError(ValueError):
    """備註超過上限。"""


class EntryNotFoundError(KeyError):
    """指定 id 不存在。"""


class BadRequestError(ValueError):
    """原生主機收到不合法的 request(例如非法 hex 顏色)。"""


# --- keyring 抽象(可被測試 monkeypatch) ---------------------------------------


def _keyring_set(service: str, username: str, password: str) -> None:
    import keyring

    keyring.set_password(service, username, password)


def _keyring_get(service: str, username: str) -> str | None:
    import keyring

    return keyring.get_password(service, username)


def _keyring_delete(service: str, username: str) -> None:
    import keyring

    try:
        keyring.delete_password(service, username)
    except keyring.errors.PasswordDeleteError:
        # 已不存在視為成功
        pass


def _keyring_username(entry_id: str) -> str:
    return f"pwmgr:{entry_id}"


# --- Windows 相容:retry os.replace -------------------------------------------


def _safe_replace(src: str, dst: str, *, retries: int = 8, delay: float = 0.05) -> None:
    """os.replace 的 Windows 容錯版——antivirus 短暫持有新檔時重試。"""
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_exc = e
            time.sleep(delay)
    # 最後一次嘗試,讓例外正常往外拋
    if last_exc is not None:
        os.replace(src, dst)


# --- index.json ----------------------------------------------------------------


def _read_index_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # 損壞的檔案——不丟失資料,備份並回傳空
        backup = path.with_suffix(".json.bak")
        try:
            _safe_replace(str(path), str(backup))
        except OSError:
            pass
        return {"version": 1, "entries": []}
    if not isinstance(data, dict) or "entries" not in data:
        return {"version": 1, "entries": []}
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """寫入 JSON,先寫暫存再 os.replace——避免半寫狀態。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".index.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        _safe_replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validate_password(password: str) -> None:
    if password is None:
        raise PasswordTooLongError("password 不可為 None")
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"密碼編碼後 {len(encoded)} bytes,超過 Credential Manager 上限 {MAX_PASSWORD_BYTES}"
        )


def _validate_notes(notes: str) -> None:
    if notes and len(notes) > MAX_NOTES_CHARS:
        raise NotesTooLongError(
            f"備註 {len(notes)} 字,超過上限 {MAX_NOTES_CHARS}"
        )


# --- 公開 API ------------------------------------------------------------------


def load_index() -> list[PasswordEntry]:
    """讀取所有條目的 metadata。密碼不包含在內。"""
    with index_lock(index_path()):
        data = _read_index_unlocked(index_path())
    return [PasswordEntry.from_dict(e) for e in data.get("entries", [])]


def get_entry(entry_id: str) -> PasswordEntry:
    """取得單筆條目,找不到拋 EntryNotFoundError。"""
    for entry in load_index():
        if entry.id == entry_id:
            return entry
    raise EntryNotFoundError(entry_id)


def get_password(entry_id: str) -> str | None:
    """從 keyring 拉單筆密碼。條目不存在回 None。"""
    # 先確認 id 存在,避免誤刪無關的 keyring 條目
    try:
        get_entry(entry_id)
    except EntryNotFoundError:
        return None
    return _keyring_get(KEYRING_SERVICE, _keyring_username(entry_id))


def save_entry(entry: PasswordEntry, password: str) -> str:
    """新增或更新條目(以 id 為鍵)。回傳 entry.id。"""
    _validate_password(password)
    _validate_notes(entry.notes)
    entry.touch()
    with index_lock(index_path()):
        data = _read_index_unlocked(index_path())
        entries: list[dict[str, Any]] = data.setdefault("entries", [])
        replaced = False
        for i, e in enumerate(entries):
            if e.get("id") == entry.id:
                entries[i] = entry.to_dict()
                replaced = True
                break
        if not replaced:
            entries.append(entry.to_dict())
        _atomic_write_json(index_path(), data)
    # 密碼寫 keyring 在鎖外——避免鎖內 I/O 過久
    _keyring_set(KEYRING_SERVICE, _keyring_username(entry.id), password)
    return entry.id


def update_entry(entry: PasswordEntry) -> str:
    """只更新 metadata,不動 keyring 密碼。

    給「編輯既有條目、密碼欄位留空(=不變更密碼)」的情境使用——
    save_entry 會無條件把傳入的密碼寫進 keyring,若呼叫端誤傳空字串
    會把既有密碼覆蓋成空的。
    """
    _validate_notes(entry.notes)
    entry.touch()
    with index_lock(index_path()):
        data = _read_index_unlocked(index_path())
        entries: list[dict[str, Any]] = data.setdefault("entries", [])
        replaced = False
        for i, e in enumerate(entries):
            if e.get("id") == entry.id:
                entries[i] = entry.to_dict()
                replaced = True
                break
        if not replaced:
            entries.append(entry.to_dict())
        _atomic_write_json(index_path(), data)
    return entry.id


def delete_entry(entry_id: str) -> bool:
    """刪除條目與其密碼。回傳是否有刪到東西。"""
    removed = False
    with index_lock(index_path()):
        data = _read_index_unlocked(index_path())
        entries: list[dict[str, Any]] = data.setdefault("entries", [])
        new_entries = [e for e in entries if e.get("id") != entry_id]
        if len(new_entries) != len(entries):
            removed = True
            data["entries"] = new_entries
            _atomic_write_json(index_path(), data)
    # 從 keyring 刪——無論如何嘗試(冪等)
    _keyring_delete(KEYRING_SERVICE, _keyring_username(entry_id))
    return removed


def set_entry_order(ids: list[str]) -> None:
    """重設整個條目順序。ids 是新的 id 序列。

    - 不在 ids 裡的既有條目會被 append 到尾端(防呆,不被丟失)
    - 重複的 id 只取第一次出現
    - 不動 keyring、不呼叫 touch()、不更新 updated_at
    - 整個操作在單一 index_lock acquisition 內完成
    """
    with index_lock(index_path()):
        data = _read_index_unlocked(index_path())
        existing: list[dict[str, Any]] = data.get("entries", [])
        by_id = {e.get("id"): e for e in existing}
        new_entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for eid in ids:
            if eid in by_id and eid not in seen:
                new_entries.append(by_id[eid])
                seen.add(eid)
        for e in existing:
            eid = e.get("id")
            if eid not in seen:
                new_entries.append(e)
                seen.add(eid)
        data["entries"] = new_entries
        _atomic_write_json(index_path(), data)


def export_all(path: Path) -> int:
    """將所有條目連同密碼匯出成 JSON(明文密碼——僅供使用者自行備份)。回傳筆數。"""
    entries = load_index()
    payload = {
        "version": 1,
        "exported_at": time.time(),
        "entries": [
            {**e.to_dict(), "password": get_password(e.id) or ""} for e in entries
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return len(entries)


def import_all(path: Path) -> tuple[int, list[str]]:
    """從匯出的 JSON 匯入條目(以 id 為鍵,重複則覆蓋)。回傳 (成功筆數, 錯誤訊息列表)。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("entries", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("檔案格式錯誤:找不到 entries 陣列")

    imported = 0
    errors: list[str] = []
    for item in items:
        try:
            if not item.get("id"):
                item = {**item, "id": PasswordEntry.new("", "", "").id}
            entry = PasswordEntry.from_dict(item)
            password = item.get("password", "") or ""
            save_entry(entry, password)
            imported += 1
        except Exception as e:
            errors.append(f"{item.get('label', '?')}: {type(e).__name__}: {e}")
    return imported, errors


def query_by_url(url: str) -> list[PasswordEntry]:
    """回傳所有匹配此 URL 的條目,依 updated_at 倒序。"""
    results: list[PasswordEntry] = []
    for e in load_index():
        if matches(e.url, url, ""):
            results.append(e)
    results.sort(key=lambda x: x.updated_at, reverse=True)
    return results


# --- current_url.json(擴充 → GUI) ----------------------------------------------


def write_current_url(url: str, tab_id: int | None = None) -> None:
    """擴充功能呼叫,寫入目前瀏覽的 URL。GUI 端輪詢讀取。"""
    path = current_url_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"url": url, "tabId": tab_id, "ts": time.time()}
    fd, tmp = tempfile.mkstemp(prefix=".cururl.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        _safe_replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_current_url() -> str | None:
    """GUI 端輪詢讀取最近一次擴充回報的 URL。"""
    path = current_url_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    url = data.get("url")
    return url if isinstance(url, str) and url else None


# --- group_colors(每個群組的展示色票) -----------------------------------------


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_hex_color(value: Any) -> str:
    """驗證並標準化十六進色碼為 lowercase #rrggbb。"""
    if not isinstance(value, str) or not _HEX_RE.match(value):
        raise BadRequestError("color 必須是 #rrggbb 形式")
    return value.lower()


def _normalize_group_colors(raw: Any) -> dict[str, str]:
    """防禦性讀取:把磁碟上各種格式的 group_colors 標準化為 {name: #rrggbb}。

    - 非 dict → {}
    - 非字串 key / 非合法 hex value → 跳過那筆(不影響其他)
    - 全部 lowercase
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        try:
            normalized = _validate_hex_color(v)
        except BadRequestError:
            continue
        out[k] = normalized
    return out


def load_group_colors() -> dict[str, str]:
    """在 index_lock 內讀取 {group_name: #rrggbb}。缺鍵或格式錯一律回空 dict。"""
    with index_lock(index_path()):
        data = _read_index_unlocked(index_path())
    return _normalize_group_colors(data.get("group_colors"))


def set_group_color(group: str, color: str | None) -> None:
    """單筆設定/移除某個 group 的展示色。

    - group:群組字串(已通過 native host 的 _normalize_group 正規化)
    - color:#rrggbb 形式設定進去;None 表示移除

    為避免磁碟上殘留空 dict,當全部移除乾淨時把 group_colors 鍵整個清掉。
    """
    with index_lock(index_path()):
        data = _read_index_unlocked(index_path())
        raw = data.get("group_colors")
        colors = _normalize_group_colors(raw)
        if color is None:
            colors.pop(group, None)
        else:
            # 雙重驗證:_validate_hex_color 在 native host 已經收過一次,
            # storage 這層是 defense-in-depth,萬一 GUI 端直接呼叫也擋。
            colors[group] = _validate_hex_color(color)
        if colors:
            data["group_colors"] = colors
        else:
            data.pop("group_colors", None)
        _atomic_write_json(index_path(), data)

