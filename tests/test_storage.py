"""Storage 測試——用 monkeypatch 模擬 keyring。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

import pwmgr.storage as storage
from pwmgr.config import MAX_NOTES_CHARS, MAX_PASSWORD_BYTES
from pwmgr.models import PasswordEntry


# --- fixtures ---------------------------------------------------------------


class FakeKeyring:
    """模擬 keyring,用 dict 存放。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.errors = __import__("keyring").errors

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        key = (service, username)
        if key not in self.store:
            raise self.errors.PasswordDeleteError("not found")
        del self.store[key]


@pytest.fixture
def fake_keyring(monkeypatch) -> FakeKeyring:
    fake = FakeKeyring()

    import keyring
    import keyring.errors

    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    monkeypatch.setattr(keyring.errors, "PasswordDeleteError", fake.errors.PasswordDeleteError)
    return fake


@pytest.fixture
def tmp_index(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    idx = tmp_path / "index.json"
    cur = tmp_path / "current_url.json"
    monkeypatch.setattr(storage, "index_path", lambda: idx)
    monkeypatch.setattr(storage, "current_url_path", lambda: cur)
    yield idx


@pytest.fixture
def null_locks(monkeypatch) -> None:
    """讓 lock 變 no-op,簡化單元測試。多行程 lock 測試見 test_ipc_lock。

    注意:要 patch `storage.index_lock`,因為 storage 在 import 時把名字綁定了;
    只 patch `ipc.index_lock` 沒用。
    """
    from pwmgr import ipc

    monkeypatch.setattr(
        storage, "index_lock", lambda *a, **kw: ipc.NullLock(*a, **kw)
    )


# --- CRUD ------------------------------------------------------------------


def test_save_and_load(fake_keyring, tmp_index, null_locks) -> None:
    entry = PasswordEntry.new("GitHub", "github.com", "alice", notes="2FA on")
    eid = storage.save_entry(entry, "secret")
    assert eid == entry.id

    entries = storage.load_index()
    assert len(entries) == 1
    assert entries[0].label == "GitHub"
    assert entries[0].username == "alice"
    # 密碼不在 index.json
    assert "password" not in json.loads(tmp_index.read_text())["entries"][0]
    # 但在 keyring
    assert storage.get_password(eid) == "secret"


def test_save_updates_existing(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new("GH", "github.com", "alice")
    eid = storage.save_entry(e, "old")
    e.label = "GitHub - work"
    e.url = "github.com"
    storage.save_entry(e, "new")

    entries = storage.load_index()
    assert len(entries) == 1
    assert entries[0].label == "GitHub - work"
    assert storage.get_password(eid) == "new"


def test_update_entry_preserves_password(fake_keyring, tmp_index, null_locks) -> None:
    """update_entry 只改 metadata,不該動到 keyring 裡的密碼(GUI 編輯條目、密碼欄位留白時使用)。"""
    e = PasswordEntry.new("GH", "github.com", "alice")
    eid = storage.save_entry(e, "secret")

    e.label = "GitHub - work"
    storage.update_entry(e)

    entries = storage.load_index()
    assert entries[0].label == "GitHub - work"
    assert storage.get_password(eid) == "secret"


def test_delete_entry(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new("GH", "github.com", "alice")
    eid = storage.save_entry(e, "secret")
    assert storage.delete_entry(eid) is True
    assert storage.load_index() == []
    assert storage.get_password(eid) is None
    # 重複刪除回 False
    assert storage.delete_entry(eid) is False


def test_get_entry_not_found(fake_keyring, tmp_index, null_locks) -> None:
    with pytest.raises(storage.EntryNotFoundError):
        storage.get_entry("nope")


# --- 2560 byte 限制 --------------------------------------------------------


def test_save_rejects_oversized_password(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new("GH", "github.com", "alice")
    big = "x" * (MAX_PASSWORD_BYTES + 1)
    with pytest.raises(storage.PasswordTooLongError):
        storage.save_entry(e, big)


def test_save_rejects_oversized_notes(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new("GH", "github.com", "alice", notes="x" * (MAX_NOTES_CHARS + 1))
    with pytest.raises(storage.NotesTooLongError):
        storage.save_entry(e, "secret")


def test_password_at_limit_accepted(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new("GH", "github.com", "alice")
    # 多位元組字元:每字 3 bytes,扣到上限內
    pwd = "中" * (MAX_PASSWORD_BYTES // 3)
    storage.save_entry(e, pwd)
    assert storage.get_password(e.id) == pwd


# --- query_by_url ----------------------------------------------------------


def test_query_by_url_matches(fake_keyring, tmp_index, null_locks) -> None:
    storage.save_entry(PasswordEntry.new("GH", "github.com", "alice"), "a")
    storage.save_entry(PasswordEntry.new("GL", "gitlab.com", "bob"), "b")
    results = storage.query_by_url("https://github.com/login")
    assert len(results) == 1
    assert results[0].label == "GH"


def test_query_by_url_subdomain_matches(fake_keyring, tmp_index, null_locks) -> None:
    storage.save_entry(PasswordEntry.new("GH", "github.com", "alice"), "a")
    results = storage.query_by_url("https://api.github.com/x")
    assert len(results) == 1


def test_query_by_url_sorted_by_updated(fake_keyring, tmp_index, null_locks) -> None:
    e1 = PasswordEntry.new("GH-old", "github.com", "alice")
    storage.save_entry(e1, "a")
    # 強制 e1 的 updated_at 早一點
    e1.updated_at -= 100
    storage.save_entry(e1, "a")

    e2 = PasswordEntry.new("GH-new", "github.com", "alice2")
    storage.save_entry(e2, "b")

    results = storage.query_by_url("https://github.com/")
    assert [r.label for r in results] == ["GH-new", "GH-old"]


# --- current_url -----------------------------------------------------------


def test_write_and_read_current_url(fake_keyring, tmp_index, null_locks) -> None:
    storage.write_current_url("https://github.com/login", tab_id=42)
    assert storage.read_current_url() == "https://github.com/login"


def test_read_current_url_missing_returns_none(fake_keyring, tmp_index, null_locks) -> None:
    assert storage.read_current_url() is None


# --- 損壞的 index.json 備援 -----------------------------------------------


def test_corrupted_index_recovers(fake_keyring, tmp_index, null_locks) -> None:
    tmp_index.parent.mkdir(parents=True, exist_ok=True)
    tmp_index.write_text("{ not json", encoding="utf-8")
    # 不應崩潰,應回空清單
    entries = storage.load_index()
    assert entries == []
    # 備份檔應存在
    assert (tmp_index.with_suffix(".json.bak")).exists()


# --- launch_url 欄位 ---------------------------------------------------------


def test_save_entry_roundtrips_launch_url(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new(
        "GitHub",
        "github.com",
        "alice",
        notes="2FA on",
        launch_url="https://github.com/login",
    )
    storage.save_entry(e, "secret")

    loaded = storage.load_index()
    assert len(loaded) == 1
    assert loaded[0].launch_url == "https://github.com/login"
    # to_dict() 也要帶到
    assert loaded[0].to_dict()["launch_url"] == "https://github.com/login"

    # 舊資料沒有 launch_url 鍵時,from_dict 要能容錯讀成空字串
    raw = json.loads(tmp_index.read_text())["entries"][0]
    raw.pop("launch_url", None)
    assert PasswordEntry.from_dict(raw).launch_url == ""


def test_update_entry_preserves_launch_url(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new(
        "GH", "github.com", "alice", launch_url="https://github.com/login"
    )
    storage.save_entry(e, "secret")
    # 模擬 GUI 編輯既有條目、密碼欄位留空(走 update_entry 而非 save_entry)
    e.launch_url = "https://github.com/settings/tokens"
    storage.update_entry(e)

    reloaded = storage.load_index()[0]
    assert reloaded.launch_url == "https://github.com/settings/tokens"


# --- group 欄位 -------------------------------------------------------------


def test_save_entry_roundtrips_group(fake_keyring, tmp_index, null_locks) -> None:
    e = PasswordEntry.new("Dell", "dell.com", "alice", group="工作")
    storage.save_entry(e, "secret")

    loaded = storage.load_index()
    assert len(loaded) == 1
    assert loaded[0].group == "工作"
    assert loaded[0].to_dict()["group"] == "工作"

    # 舊資料沒有 group 鍵時,from_dict 要能容錯讀成空字串
    raw = json.loads(tmp_index.read_text(encoding="utf-8"))["entries"][0]
    raw.pop("group", None)
    assert PasswordEntry.from_dict(raw).group == ""


# --- group_colors (每群組展示色票) -------------------------------------------


def test_group_colors_default_empty_when_missing(fake_keyring, tmp_index, null_locks) -> None:
    """索引檔完全沒有 group_colors 鍵時,load_group_colors 應回 {}。"""
    # 沒存過任何條目也不寫入 index.json,直接讀 group_colors
    assert storage.load_group_colors() == {}


def test_group_colors_round_trip(fake_keyring, tmp_index, null_locks) -> None:
    """set 後 load 應能讀回來,並 lowercase 化。"""
    storage.set_group_color("工作", "#3B82F6")
    storage.set_group_color("個人", "#ef4444")
    colors = storage.load_group_colors()
    assert colors == {"工作": "#3b82f6", "個人": "#ef4444"}
    # 並寫進 index.json 的 group_colors 鍵
    raw = json.loads(tmp_index.read_text(encoding="utf-8"))
    assert raw["group_colors"] == {"工作": "#3b82f6", "個人": "#ef4444"}


def test_set_group_color_rejects_invalid_hex(fake_keyring, tmp_index, null_locks) -> None:
    """非法 hex 必須擋下,不可寫入索引(None 是合法移除路徑,不在此測)。"""
    for bad in ["#abc", "#zzz", "blue", "#12345", "#1234567", "  ", "", 123]:
        with pytest.raises(storage.BadRequestError):
            storage.set_group_color("X", bad)
    # 一次都沒寫進去
    assert storage.load_group_colors() == {}


def test_set_group_color_replaces_not_merges(fake_keyring, tmp_index, null_locks) -> None:
    """同名稱再 set 一次,要覆蓋,不是合併。"""
    storage.set_group_color("工作", "#111111")
    storage.set_group_color("工作", "#222222")
    assert storage.load_group_colors() == {"工作": "#222222"}


def test_set_group_color_none_clears_entry(fake_keyring, tmp_index, null_locks) -> None:
    """color=None 應移除該 group 的覆寫。"""
    storage.set_group_color("工作", "#111111")
    storage.set_group_color("個人", "#222222")
    storage.set_group_color("工作", None)
    assert storage.load_group_colors() == {"個人": "#222222"}


def test_set_group_color_none_when_already_absent_is_noop(
    fake_keyring, tmp_index, null_locks
) -> None:
    """對不存在的 group 設 None 不該 crash,也不該在索引裡新增空鍵。"""
    storage.set_group_color("不存在", None)
    assert storage.load_group_colors() == {}
    raw = json.loads(tmp_index.read_text(encoding="utf-8"))
    assert "group_colors" not in raw


def test_legacy_index_loads_without_group_colors(fake_keyring, tmp_index, null_locks) -> None:
    """舊 index.json 沒有 group_colors 鍵時,load_group_colors 仍應回空 dict。"""
    idx = tmp_index
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    assert storage.load_group_colors() == {}


def test_group_colors_removed_when_last_entry_cleared(
    fake_keyring, tmp_index, null_locks
) -> None:
    """全部 entry 都清掉後,group_colors 鍵也應從索引消失(不留空 dict)。"""
    storage.set_group_color("工作", "#111111")
    assert "group_colors" in json.loads(tmp_index.read_text(encoding="utf-8"))
    storage.set_group_color("工作", None)
    raw = json.loads(tmp_index.read_text(encoding="utf-8"))
    assert "group_colors" not in raw, "全部清掉後 group_colors 鍵不該殘留"


def test_update_entry_preserves_group(fake_keyring, tmp_index, null_locks) -> None:
    """update_entry(編輯既有條目、密碼留空的路徑)要能把 group 一起寫進去。"""
    e = PasswordEntry.new("GH", "github.com", "alice", group="工作")
    storage.save_entry(e, "s")
    e.group = "個人"
    storage.update_entry(e)
    assert storage.load_index()[0].group == "個人"


def test_password_entry_default_group_empty() -> None:
    """PasswordEntry.new() 不指定 group 時要預設空字串(代表「未分類」)。"""
    assert PasswordEntry.new("X", "x.com", "u").group == ""


# --- set_entry_order (拖拉排序持久化) --------------------------------------


def test_set_entry_order_reorders(fake_keyring, tmp_index, null_locks) -> None:
    """set_entry_order 應該按指定 id 序列重排 load_index() 結果。"""
    a = storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    b = storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")
    c = storage.save_entry(PasswordEntry.new("C", "c.com", "u"), "p")
    # 反轉順序
    storage.set_entry_order([c, b, a])
    labels = [e.label for e in storage.load_index()]
    assert labels == ["C", "B", "A"]


def test_set_entry_order_preserves_unchanged_entries(
    fake_keyring, tmp_index, null_locks
) -> None:
    """order 漏列既有 id 時,該條目要 append 到尾端、不被丟失。"""
    a = storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    b = storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")
    c = storage.save_entry(PasswordEntry.new("C", "c.com", "u"), "p")
    # 只列 b,a → c 應被 append
    storage.set_entry_order([b, a])
    labels = [e.label for e in storage.load_index()]
    assert labels == ["B", "A", "C"]


def test_set_entry_order_dedups(fake_keyring, tmp_index, null_locks) -> None:
    """order 內重複的 id 只生效一次(防呆)。"""
    a = storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    b = storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")
    storage.set_entry_order([b, b, a, a])
    labels = [e.label for e in storage.load_index()]
    assert labels == ["B", "A"]


def test_set_entry_order_does_not_touch_updated_at(
    fake_keyring, tmp_index, null_locks
) -> None:
    """reorder 屬於手動排序,不應更新 updated_at(否則會干擾 query_by_url 命中模式排序)。

    用 id 索引比對,避免誤把「順序不同」當成「值不同」。
    """
    a = storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    b = storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")
    original = {e.id: e.updated_at for e in storage.load_index()}
    storage.set_entry_order([b, a])
    after = {e.id: e.updated_at for e in storage.load_index()}
    assert original == after, "reorder 不該動到 updated_at"
    # 順序確實換了
    assert [e.label for e in storage.load_index()] == ["B", "A"]


def test_set_entry_order_does_not_touch_passwords(
    fake_keyring, tmp_index, null_locks
) -> None:
    """reorder 不該動 keyring 裡的密碼(純 metadata 操作)。"""
    a = storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "secret-A")
    b = storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "secret-B")
    storage.set_entry_order([b, a])
    assert storage.get_password(a) == "secret-A"
    assert storage.get_password(b) == "secret-B"
