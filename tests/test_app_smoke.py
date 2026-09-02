"""GUI 煙霧測試——只驗證 PwmgrApp 構造不崩潰,基本流程 OK。

不實際顯示視窗(假裝 hotkey/tray);不真的跑 mainloop(用 after 排程退出)。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import pwmgr.app as app_module
import pwmgr.storage as storage
from pwmgr.models import PasswordEntry


@pytest.fixture
def fake_keyring(monkeypatch) -> None:
    import keyring

    class Fake:
        def set_password(self, s, u, p): pass
        def get_password(self, s, u): return "stored"
        def delete_password(self, s, u): pass

    monkeypatch.setattr(keyring, "set_password", Fake().set_password)
    monkeypatch.setattr(keyring, "get_password", Fake().get_password)
    monkeypatch.setattr(keyring, "delete_password", Fake().delete_password)
    import keyring.errors
    monkeypatch.setattr(keyring.errors, "PasswordDeleteError", Exception)


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path: Path) -> None:
    idx = tmp_path / "index.json"
    cur = tmp_path / "cur.json"
    monkeypatch.setattr(storage, "index_path", lambda: idx)
    monkeypatch.setattr(storage, "current_url_path", lambda: cur)
    from pwmgr import ipc
    monkeypatch.setattr(storage, "index_lock", lambda *a, **kw: ipc.NullLock(*a, **kw))


@pytest.fixture
def stateful_keyring(monkeypatch) -> None:
    """真的用 dict 記住密碼的假 keyring,用來驗證「編輯不改密碼」不會把密碼洗掉。"""
    import keyring
    import keyring.errors

    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: store.pop((s, u), None))
    monkeypatch.setattr(keyring.errors, "PasswordDeleteError", Exception)


@pytest.fixture
def mock_hotkey_tray():
    with patch.object(app_module, "GlobalHotkey") as HK, \
         patch.object(app_module, "TrayIcon") as TI:
        HK.return_value.start = lambda: None
        HK.return_value.stop = lambda: None
        HK.return_value.drain = lambda: 0
        TI.return_value.start = lambda: None
        TI.return_value.stop = lambda: None
        TI.return_value.notify = lambda *a, **kw: None
        yield HK, TI


def test_app_constructs_and_quits(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """建構 + 排程 100ms 後退出,確認初始化無例外。"""
    a = app_module.PwmgrApp()
    a.root.after(100, a._do_quit)
    a.run()
    # 若跑到這裡代表 mainloop 正常進入並退出
    assert a._entries == []


def test_app_loads_existing_entries(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """預載 2 筆條目,確認啟動時讀取到。"""
    storage.save_entry(PasswordEntry.new("GitHub", "github.com", "alice"), "s")
    storage.save_entry(PasswordEntry.new("GitLab", "gitlab.com", "bob"), "s")

    a = app_module.PwmgrApp()
    assert len(a._entries) == 2
    a._do_quit()


def test_app_search_filters_list(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """搜尋過濾運作。"""
    storage.save_entry(PasswordEntry.new("GitHub", "github.com", "alice"), "s")
    storage.save_entry(PasswordEntry.new("GitLab", "gitlab.com", "bob"), "s")

    a = app_module.PwmgrApp()
    # 觸發 _refresh_listbox
    a._refresh_listbox()
    assert len(a.tree.get_children()) == 2

    a.search_var.set("github")
    a._refresh_listbox()
    visible = a.tree.get_children()
    assert len(visible) == 1
    label = a.tree.item(visible[0])["values"][0]
    assert "GitHub" in label

    a._do_quit()


def test_url_poll_updates_highlight(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """current_url 變動時,符合項應有 MATCH_PREFIX。"""
    storage.save_entry(PasswordEntry.new("GitHub", "github.com", "alice"), "s")

    a = app_module.PwmgrApp()
    a._current_url = None
    a._refresh_listbox()
    label_no_match = a.tree.item(a.tree.get_children()[0])["values"][0]
    assert app_module.MATCH_PREFIX not in label_no_match

    a._current_url = "https://api.github.com/x"
    a._refresh_listbox()
    label_match = a.tree.item(a.tree.get_children()[0])["values"][0]
    assert app_module.MATCH_PREFIX in label_match

    a._do_quit()


def test_editing_entry_without_retyping_password_keeps_it(
    mock_hotkey_tray, stateful_keyring, isolated_paths
) -> None:
    """編輯既有條目時密碼欄位一律留白;留空儲存不該把 keyring 裡的密碼洗成空字串。

    這是實際發生過的 regression:_load_entry_to_form 為避免明文殘留記憶體,
    一律把 password_var 清空,若 _save_entry 沒特別處理,存檔時就會把
    keyring 裡的真密碼覆蓋成空字串,且畫面上完全看不出來。
    """
    eid = storage.save_entry(PasswordEntry.new("Dell Agile", "dell.com", "alice"), "realpassword123")

    a = app_module.PwmgrApp()
    entry = next(e for e in a._entries if e.id == eid)
    a._selected_id = eid
    a._load_entry_to_form(entry)
    assert a.password_var.get() == ""  # 確認畫面上確實看不出密碼

    # 使用者只改了網域,沒有重新輸入密碼
    a.url_entry_var.set("agile.us.dell.com")
    a._save_entry()

    assert storage.get_password(eid) == "realpassword123"
    updated = next(e for e in storage.load_index() if e.id == eid)
    assert updated.url == "agile.us.dell.com"

    a._do_quit()
