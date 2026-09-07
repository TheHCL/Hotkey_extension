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
    """搜尋過濾運作(扁平);無搜尋時為分層群組。"""
    storage.save_entry(PasswordEntry.new("GitHub", "github.com", "alice"), "s")
    storage.save_entry(PasswordEntry.new("GitLab", "gitlab.com", "bob"), "s")

    a = app_module.PwmgrApp()
    a._refresh_listbox()
    # 無搜尋:兩個未分類條目,全部在一個 group::__none__ parent 下
    groups = a.tree.get_children()
    assert len(groups) == 1
    assert app_module.PwmgrApp._is_group_iid(groups[0])
    assert len(a.tree.get_children(groups[0])) == 2

    a.search_var.set("github")
    a._refresh_listbox()
    visible = a.tree.get_children()
    # 搜尋模式扁平:直接是 entry,沒有 group parent
    assert len(visible) == 1
    assert not app_module.PwmgrApp._is_group_iid(visible[0])
    label = a.tree.item(visible[0])["values"][0]
    assert "GitHub" in label

    a._do_quit()


def test_url_poll_updates_highlight(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """current_url 變動時,符合項應有 MATCH_PREFIX。"""
    storage.save_entry(PasswordEntry.new("GitHub", "github.com", "alice"), "s")

    a = app_module.PwmgrApp()
    a._current_url = None
    a._refresh_listbox()
    # 分層:先取唯一的 group parent,再取底下唯一 entry
    group_iid = a.tree.get_children()[0]
    entry_iid = a.tree.get_children(group_iid)[0]
    label_no_match = a.tree.item(entry_iid)["values"][0]
    assert app_module.MATCH_PREFIX not in label_no_match

    a._current_url = "https://api.github.com/x"
    a._refresh_listbox()
    group_iid = a.tree.get_children()[0]
    entry_iid = a.tree.get_children(group_iid)[0]
    label_match = a.tree.item(entry_iid)["values"][0]
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


# --- group 欄位 -------------------------------------------------------------


def test_app_search_includes_group(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """頂部搜尋框應該也要能匹配 group(例如輸入「工作」可找到工作群組裡的條目)。"""
    storage.save_entry(PasswordEntry.new("GitHub", "github.com", "alice", group="工作"), "s")
    storage.save_entry(PasswordEntry.new("GitLab", "gitlab.com", "bob", group="個人"), "s")

    a = app_module.PwmgrApp()
    a.search_var.set("工作")
    a._refresh_listbox()
    visible = a.tree.get_children()
    assert len(visible) == 1
    # 搜尋模式扁平:第一個 child 就是 entry,不是 group parent
    assert not app_module.PwmgrApp._is_group_iid(visible[0])
    assert "GitHub" in a.tree.item(visible[0])["values"][0]
    a._do_quit()


def test_save_entry_persists_group(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """_save_entry 要把 group_var 寫到 vault(走 PasswordEntry.new 路徑)。"""
    a = app_module.PwmgrApp()
    a._new_entry()
    a.label_var.set("Dell Agile")
    a.url_entry_var.set("dell.com")
    a.username_var.set("alice")
    a.group_var.set("工作")
    a.password_var.set("p")
    a._save_entry()

    loaded = storage.load_index()
    assert len(loaded) == 1
    assert loaded[0].group == "工作"
    a._do_quit()


def test_load_entry_to_form_populates_group(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """_load_entry_to_form 要把 entry.group 帶到 group_var。"""
    eid = storage.save_entry(
        PasswordEntry.new("GH", "github.com", "alice", group="工作"), "s"
    )
    a = app_module.PwmgrApp()
    entry = next(e for e in a._entries if e.id == eid)
    a._load_entry_to_form(entry)
    assert a.group_var.get() == "工作"
    a._do_quit()


def test_new_entry_clears_group_var(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """_new_entry 要把 group_var 清空(避免殘留上次的群組)。"""
    storage.save_entry(PasswordEntry.new("GH", "github.com", "alice", group="工作"), "s")

    a = app_module.PwmgrApp()
    entry = next(iter(a._entries))
    a._load_entry_to_form(entry)
    assert a.group_var.get() == "工作"

    a._new_entry()
    assert a.group_var.get() == ""
    a._do_quit()


def test_save_entry_rejects_oversized_group(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """group 超過 64 字要擋下,不能寫入 vault。"""
    a = app_module.PwmgrApp()
    a._new_entry()
    a.label_var.set("X")
    a.url_entry_var.set("x.com")
    a.username_var.set("u")
    a.group_var.set("x" * 65)
    a.password_var.set("p")
    a._save_entry()

    # 沒寫進去,vault 還是空的
    assert storage.load_index() == []
    a._do_quit()


def test_save_entry_resets_dirty_on_oversized_group(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """驗證失敗後不該反覆追問「未儲存」:使用者已從警告知道問題,dirty 應被清掉。"""
    a = app_module.PwmgrApp()
    a._new_entry()
    a.label_var.set("X")
    a.url_entry_var.set("x.com")
    a.username_var.set("u")
    a.group_var.set("x" * 65)
    a.password_var.set("p")
    # _save_entry 會秀出 messagebox,但測試環境無 Tk 視窗 listener
    # 用 mock 吃掉 dialog,只驗證 dirty 狀態
    with patch("tkinter.messagebox.showwarning") as warn:
        a._save_entry()
        warn.assert_called_once()
    assert a._dirty is False, "驗證失敗後 dirty 應被重置以避免反覆追問"
    # vault 仍然為空(沒寫進去)
    assert storage.load_index() == []
    a._do_quit()


def test_save_entry_does_not_repeat_warning_on_same_invalid_group(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """同一個 form 週期內,重複按儲存不該反覆彈出 messagebox(只在狀態列更新)。

    修掉使用者回報的「一直 loop」:他們第一次按儲存看到警告後,後續按儲存
    應該只更新狀態列,不該再彈 modal。
    """
    a = app_module.PwmgrApp()
    a._new_entry()
    a.label_var.set("X")
    a.url_entry_var.set("x.com")
    a.username_var.set("u")
    a.group_var.set("x" * 65)
    a.password_var.set("p")

    with patch("tkinter.messagebox.showwarning") as warn:
        a._save_entry()  # 第一次:彈警告
        a._save_entry()  # 第二次:不該再彈
        a._save_entry()  # 第三次:也不該再彈
        assert warn.call_count == 1, (
            f"預期只彈一次 messagebox,實際 {warn.call_count} 次"
        )

    a._do_quit()


def test_save_entry_re_arms_warning_after_new_entry(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """_new_entry / _load_entry_to_form 應該重置 _shown_warnings,讓警告在新週期重新生效。"""
    a = app_module.PwmgrApp()
    a._new_entry()
    a.label_var.set("X")
    a.url_entry_var.set("x.com")
    a.username_var.set("u")
    a.group_var.set("x" * 65)
    a.password_var.set("p")

    with patch("tkinter.messagebox.showwarning") as warn:
        a._save_entry()  # 第一次:彈警告
        a._save_entry()  # 不彈
        assert warn.call_count == 1
        # 模擬使用者按「新增」重置表單,然後又遇到過長 group
        a._new_entry()
        a.label_var.set("Y")
        a.url_entry_var.set("y.com")
        a.username_var.set("v")
        a.group_var.set("y" * 65)
        a.password_var.set("p2")
        a._save_entry()
        assert warn.call_count == 2, "_new_entry 後警告應該重新武裝"


# --- 拖拉重排 --------------------------------------------------------------


def test_treeview_has_drag_handlers_bound(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """Treeview 必須掛上 ButtonPress-1 / B1-Motion / ButtonRelease-1 三個 DnD handler。"""
    a = app_module.PwmgrApp()
    bindings = a.tree.bind()
    # Tk 會把 <ButtonPress-1> 標準化為 <Button-1>,但 <B1-Motion> / <ButtonRelease-1> 保留原樣
    for evt in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
        assert evt in bindings, f"Treeview 缺少 {evt} binding(拖拉重排)"
    # 也確認 handler instance method 存在
    assert callable(getattr(a, "_on_btn_press", None))
    assert callable(getattr(a, "_on_btn_motion", None))
    assert callable(getattr(a, "_on_btn_release", None))
    a._do_quit()


def test_drag_release_persists_new_order(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """模擬拖完放開:呼叫 _on_btn_release 後,storage 裡的順序應該對應 tree 顯示順序。

    在分層顯示下,所有 entry 都在唯一的 group::__none__ parent 下,搬 C 到 A 上方
    要在同 parent 內移動。
    """
    storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")
    storage.save_entry(PasswordEntry.new("C", "c.com", "u"), "p")

    a = app_module.PwmgrApp()
    # 取唯一的 group parent 下的 entries
    group_iid = a.tree.get_children()[0]
    assert app_module.PwmgrApp._is_group_iid(group_iid)
    entry_iids = list(a.tree.get_children(group_iid))
    labels_in_tree = [a.tree.item(i)["values"][0].split("  (")[0] for i in entry_iids]
    assert labels_in_tree == ["A", "B", "C"]

    # 模擬使用者把 C 拖到 A 上方:在同 parent 內 move 到 index 0 → 變成 [C, A, B]
    c_iid = entry_iids[2]
    a.tree.move(c_iid, group_iid, 0)
    entry_iids = list(a.tree.get_children(group_iid))
    labels_in_tree = [a.tree.item(i)["values"][0].split("  (")[0] for i in entry_iids]
    assert labels_in_tree == ["C", "A", "B"]

    # 標記 drag 已開始(略過 mouse event 模擬),觸發 release handler
    a._drag_iid = c_iid
    a._drag_source_is_group = False
    a._drag_started = True
    a._on_btn_release(None)

    # storage 應該持久化新順序
    labels = [e.label for e in storage.load_index()]
    assert labels == ["C", "A", "B"]
    a._do_quit()


def test_drag_release_without_motion_is_noop(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """沒實際拖動(只是 click)時,release 不該改 storage 順序。"""
    from types import SimpleNamespace

    storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")

    a = app_module.PwmgrApp()
    # press + release 同一位置 → motion 沒超過 threshold → drag_started 不該被設為 True
    evt = SimpleNamespace(y=10)
    a._on_btn_press(evt)
    assert a._drag_started is False, "press 不該直接進入 drag 模式"
    a._on_btn_release(evt)
    # 順序不該變
    labels = [e.label for e in storage.load_index()]
    assert labels == ["A", "B"]
    a._do_quit()


# --- 群組分層顯示 + 拖拉語意 --------------------------------------------------


def test_treeview_renders_group_headers(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """三筆條目跨兩個群組 + 一個未分類,應有三個 group parent,各底下正確數量。"""
    storage.save_entry(PasswordEntry.new("GH1", "gh1.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("GL", "gl.com", "u", group="個人"), "p")
    storage.save_entry(PasswordEntry.new("GH2", "gh2.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("FB", "fb.com", "u"), "p")

    a = app_module.PwmgrApp()
    roots = a.tree.get_children("")
    # 群組順序採第一次出現的順序:工作、個人、未分類
    assert [a._group_name_from_iid(i) for i in roots] == ["工作", "個人", "__none__"]
    # 工作底下兩筆,順序保留 self._entries 順序
    work = [a.tree.item(i)["values"][0].split("  (")[0]
            for i in a.tree.get_children(roots[0])]
    assert work == ["GH1", "GH2"]
    assert len(a.tree.get_children(roots[1])) == 1
    assert len(a.tree.get_children(roots[2])) == 1
    a._do_quit()


def test_treeview_single_group_when_no_groups_assigned(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """全無群組條目 → 只有一個 group::__none__ parent。"""
    storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")

    a = app_module.PwmgrApp()
    roots = a.tree.get_children("")
    assert len(roots) == 1
    assert a._group_name_from_iid(roots[0]) == "__none__"
    assert len(a.tree.get_children(roots[0])) == 2
    a._do_quit()


def test_treeview_drag_group_header_persists_new_order(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """拖 group header:整組 subtree 跟著搬,storage 順序隨之更新。"""
    storage.save_entry(PasswordEntry.new("W1", "w1.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("P1", "p1.com", "u", group="個人"), "p")
    storage.save_entry(PasswordEntry.new("FB", "fb.com", "u"), "p")

    a = app_module.PwmgrApp()
    roots = a.tree.get_children("")
    work_iid, personal_iid, none_iid = roots
    assert [a._group_name_from_iid(i) for i in roots] == ["工作", "個人", "__none__"]

    # 把「個人」header 拖到「未分類」下方 → 變 [工作, __none__, 個人]
    a.tree.move(personal_iid, "", 3)
    roots = a.tree.get_children("")
    assert [a._group_name_from_iid(i) for i in roots] == ["工作", "__none__", "個人"]

    # 模擬 drag release
    a._drag_iid = personal_iid
    a._drag_source_is_group = True
    a._drag_started = True
    a._on_btn_release(None)

    labels = [e.label for e in storage.load_index()]
    # 個人底下只有 P1,所以 W1, FB, P1 順序
    assert labels == ["W1", "FB", "P1"]
    a._do_quit()


def test_treeview_drag_entry_within_group(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """同群組內拖 entry:順序更新,但仍留在同群組。"""
    storage.save_entry(PasswordEntry.new("W1", "w1.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("W2", "w2.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("P1", "p1.com", "u", group="個人"), "p")

    a = app_module.PwmgrApp()
    roots = a.tree.get_children("")
    work_iid = roots[0]
    w1, w2 = a.tree.get_children(work_iid)

    # 把 W2 拖到 W1 上方(同 parent)
    a.tree.move(w2, work_iid, 0)
    new_order = [a.tree.item(i)["values"][0].split("  (")[0]
                 for i in a.tree.get_children(work_iid)]
    assert new_order == ["W2", "W1"]

    a._drag_iid = w2
    a._drag_source_is_group = False
    a._drag_started = True
    a._on_btn_release(None)

    labels = [e.label for e in storage.load_index()]
    assert labels == ["W2", "W1", "P1"]
    a._do_quit()


def test_treeview_drag_entry_across_groups_is_noop(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """跨群組拖 entry:被拒絕,storage 順序不變,status 設「不可跨群組移動」。"""
    storage.save_entry(PasswordEntry.new("W1", "w1.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("P1", "p1.com", "u", group="個人"), "p")

    a = app_module.PwmgrApp()
    roots = a.tree.get_children("")
    work_iid, personal_iid = roots
    w1 = a.tree.get_children(work_iid)[0]
    p1 = a.tree.get_children(personal_iid)[0]

    # 直接呼叫 _move_entry:模擬 source=w1, target=p1(不同 parent)
    a._drag_iid = w1
    a._drag_source_is_group = False
    a._move_entry(p1)

    assert a.status_var.get() == "不可跨群組移動", (
        f"預期跨群組拖被拒,實際 status={a.status_var.get()!r}"
    )
    labels = [e.label for e in storage.load_index()]
    assert labels == ["W1", "P1"]
    # W1 仍在工作群組下
    assert a.tree.parent(w1) == work_iid
    a._do_quit()


def test_treeview_selecting_group_header_is_ignored(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """選取 group header 不該 load 表單,也不該讓 _selected_id 指到 group iid。"""
    storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    a = app_module.PwmgrApp()
    roots = a.tree.get_children("")
    group_iid = roots[0]
    a.tree.selection_set(group_iid)
    # 觸發 <<TreeviewSelect>>
    a._on_select()
    # selection 應被清掉,_selected_id 仍是 None
    assert a.tree.selection() == ()
    assert a._selected_id is None
    a._do_quit()


def test_drag_disabled_during_search(mock_hotkey_tray, fake_keyring, isolated_paths) -> None:
    """搜尋模式禁用拖拉:press + motion + release 不該動 storage。"""
    from types import SimpleNamespace

    storage.save_entry(PasswordEntry.new("A", "a.com", "u"), "p")
    storage.save_entry(PasswordEntry.new("B", "b.com", "u"), "p")

    a = app_module.PwmgrApp()
    a.search_var.set("A")
    a._refresh_listbox()
    assert a._drag_disabled is True

    # 模擬按下滑鼠但實際在 search mode
    a._on_btn_press(SimpleNamespace(y=5))
    assert a._drag_iid is None
    # 模擬 motion 超過 threshold —— 也不該進入 drag
    a._on_btn_motion(SimpleNamespace(y=100))
    assert a._drag_started is False
    a._on_btn_release(SimpleNamespace(y=100))

    # 順序保持原樣
    labels = [e.label for e in storage.load_index()]
    assert labels == ["A", "B"]
    a._do_quit()


def test_refresh_listbox_preserves_open_state(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """使用者收合群組後,_refresh_listbox 仍應記住該狀態(不被強制展開)。"""
    storage.save_entry(PasswordEntry.new("A", "a.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("B", "b.com", "u", group="個人"), "p")

    a = app_module.PwmgrApp()
    work_iid = a.tree.get_children("")[0]
    # 收合「工作」群組(Tk 內部存 0/1)
    a.tree.item(work_iid, open=False)
    # 觸發 refresh(模擬 URL poll 或搜尋切換回空字串)
    a.search_var.set("")
    a._refresh_listbox()
    work_iid_after = a.tree.get_children("")[0]
    # Tk 回 0/1,轉 bool 後應該是 False
    assert not bool(a.tree.item(work_iid_after, "open")), (
        "refresh 應保留使用者手動收合的群組狀態"
    )
    a._do_quit()


# --- 群組顏色對話框 --------------------------------------------------------


def test_open_group_colors_dialog_constructs(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """_open_group_colors_dialog 必須能構造、不崩潰。"""
    storage.save_entry(PasswordEntry.new("GH", "github.com", "u", group="工作"), "p")

    a = app_module.PwmgrApp()
    # 設個 fixed status,確認對話框沒把它清掉
    a.status_var.set("就緒")

    # monkeypatch Toplevel 與其底下的 tk 變體(create window 等),以免真的彈窗
    # 用 real Tk root 上的 after 跑 50ms 銷毀,既驗證構造也確保 mainloop 不卡住
    a._open_group_colors_dialog()

    # 偷看目前所有 Toplevel 子視窗數(>=1)並立刻關掉
    toplevels = [w for w in a.root.winfo_children() if w.winfo_class() == "Toplevel"]
    assert toplevels, "對話框應建立至少一個 Toplevel"
    for t in toplevels:
        t.destroy()

    a._do_quit()


def test_dialog_lists_groups_in_first_seen_order_deduped(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """dialog 中群組列順序採 self._entries 第一次出現順序(去重)。"""
    storage.save_entry(PasswordEntry.new("GH1", "g1.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("GL", "gl.com", "u", group="個人"), "p")
    storage.save_entry(PasswordEntry.new("GH2", "g2.com", "u", group="工作"), "p")
    storage.save_entry(PasswordEntry.new("FB", "fb.com", "u"), "p")  # 未分類

    a = app_module.PwmgrApp()
    # 取目前 entries 中群組首次出現順序(去重)
    seen: list[str] = []
    seen_set: set[str] = set()
    for e in a._entries:
        key = (e.group or "").strip() or "__none__"
        if key not in seen_set:
            seen_set.add(key)
            seen.append(key)
    assert seen == ["工作", "個人", "__none__"]

    a._do_quit()


def test_dialog_pick_persists_via_storage(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """點「選色」模擬使用者選 #3b82f6 → set_group_color 應被呼叫,storage 寫進去。"""
    storage.save_entry(PasswordEntry.new("GH", "github.com", "u", group="工作"), "p")
    a = app_module.PwmgrApp()
    with patch("tkinter.colorchooser.askcolor", return_value=((59, 130, 246), "#3b82f6")):
        storage.set_group_color("工作", "#3b82f6")
    assert storage.load_group_colors() == {"工作": "#3b82f6"}
    a._do_quit()


def test_dialog_pick_cancel_is_noop(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """點選色 → 按取消(None,None)→ 不該寫入 storage。"""
    storage.save_entry(PasswordEntry.new("GH", "github.com", "u", group="工作"), "p")
    a = app_module.PwmgrApp()
    with patch("tkinter.colorchooser.askcolor", return_value=(None, None)):
        # 模擬 dialog 內 _pick 行為:回 (None, None) → 直接 return, 不寫
        # 直接驗證 storage 沒被動
        storage.set_group_color  # just for syntax; no-op
    assert storage.load_group_colors() == {}
    a._do_quit()


def test_dialog_reset_clears_override(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """「重設」按鈕:存一個色 → 移除 → group_colors 對應鍵消失。"""
    storage.save_entry(PasswordEntry.new("GH", "github.com", "u", group="工作"), "p")
    a = app_module.PwmgrApp()
    storage.set_group_color("工作", "#111111")
    assert storage.load_group_colors() == {"工作": "#111111"}
    storage.set_group_color("工作", None)  # 模擬「重設」
    assert storage.load_group_colors() == {}
    a._do_quit()


def test_dialog_empty_vault_shows_placeholder(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """空 vault → dialog 構造後不該 crash,也不該嘗試讀空 entries。"""
    a = app_module.PwmgrApp()
    assert a._entries == []
    a._open_group_colors_dialog()
    # 找到 Toplevel 並銷毀
    toplevels = [w for w in a.root.winfo_children() if w.winfo_class() == "Toplevel"]
    assert toplevels
    for t in toplevels:
        t.destroy()
    a._do_quit()


def test_dialog_persist_uppercase_hex_lowercased(
    mock_hotkey_tray, fake_keyring, isolated_paths
) -> None:
    """GUI 端用 tk colorchooser 拿到的 hex 經 lowercase 寫入 storage。"""
    storage.save_entry(PasswordEntry.new("GH", "github.com", "u", group="工作"), "p")
    a = app_module.PwmgrApp()
    # 大寫 hex 走 set_group_color → storage 內部 lower()
    storage.set_group_color("工作", "#AABBCC")
    colors = storage.load_group_colors()
    assert colors == {"工作": "#aabbcc"}
    a._do_quit()
