"""端對端流程測試——模擬 Chrome 擴充功能的完整呼叫序列。"""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from typing import Iterator

import pytest

import pwmgr.native_host as nh
import pwmgr.storage as storage
from pwmgr.models import PasswordEntry


# --- helpers ----------------------------------------------------------------


def _encode(obj: dict) -> bytes:
    payload = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def _drive_loop(input_bytes: bytes, timeout: float = 2.0) -> bytes:
    import threading

    stdin = io.BytesIO(input_bytes)
    stdout = io.BytesIO()
    orig_stdin, orig_stdout = nh.sys.stdin, nh.sys.stdout
    nh.sys.stdin = stdin
    nh.sys.stdout = stdout
    nh.sys.stdin.buffer = stdin
    nh.sys.stdout.buffer = stdout
    t = threading.Thread(target=nh.run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    nh.sys.stdin = orig_stdin
    nh.sys.stdout = orig_stdout
    return stdout.getvalue()


def _read_all(stream: io.BytesIO) -> list[dict]:
    out = []
    while True:
        header = stream.read(4)
        if len(header) < 4:
            break
        (length,) = struct.unpack("<I", header)
        payload = stream.read(length)
        if len(payload) < length:
            break
        out.append(json.loads(payload.decode("utf-8")))
    return out


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def fake_keyring(monkeypatch) -> None:
    import keyring

    class Fake:
        store: dict = {}

        def set_password(self, s, u, p): self.store[(s, u)] = p
        def get_password(self, s, u): return self.store.get((s, u))
        def delete_password(self, s, u): self.store.pop((s, u), None)

    fake = Fake()
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    import keyring.errors
    monkeypatch.setattr(keyring.errors, "PasswordDeleteError", Exception)


@pytest.fixture
def isolated(monkeypatch, tmp_path: Path) -> Iterator[None]:
    idx = tmp_path / "index.json"
    cur = tmp_path / "cur.json"
    monkeypatch.setattr(storage, "index_path", lambda: idx)
    monkeypatch.setattr(storage, "current_url_path", lambda: cur)
    from pwmgr import ipc
    monkeypatch.setattr(storage, "index_lock", lambda *a, **kw: ipc.NullLock(*a, **kw))
    yield


# --- 場景 ------------------------------------------------------------------


def test_extension_user_creates_and_autofills(isolated, fake_keyring) -> None:
    """模擬:
    1. 使用者在 GUI 新增 github.com / alice / s3cret(透過 native host save)
    2. 切到 GitHub tab,擴充送 query → 拿到命中
    3. 擴充送 report_url → 寫 current_url.json(GUI 端輪詢用)
    4. 使用者點 popup → 擴充送 fetch → 拿到帳密
    """
    msgs = b"".join(
        [
            # 1. GUI 新增條目
            _encode({
                "type": "save",
                "entry": {"label": "GitHub", "url": "github.com", "username": "alice", "notes": "2FA on"},
                "password": "s3cret",
            }),
            # 2. 切到 GitHub tab 查詢
            _encode({"type": "query", "url": "https://github.com/login", "tabId": 7}),
            # 3. 報告當前 URL(GUI 高亮用)
            _encode({"type": "report_url", "url": "https://github.com/login", "tabId": 7, "ts": 1234567890.0}),
            # 4. 點擊條目 → 拿密碼
            #    注意:這是第二次 drive_loop,因為要先從第一輪拿 entry id
        ]
    )
    out = _drive_loop(msgs)
    resps = _read_all(io.BytesIO(out))
    assert len(resps) == 3
    assert resps[0]["ok"] is True
    entry_id = resps[0]["id"]
    assert resps[1]["ok"] is True
    assert len(resps[1]["matches"]) == 1
    assert resps[1]["matches"][0]["label"] == "GitHub"
    assert resps[1]["matches"][0]["id"] == entry_id
    assert resps[2]["ok"] is True
    # 確認 current_url.json 被寫入
    assert storage.read_current_url() == "https://github.com/login"

    # 第二輪:fetch
    out2 = _drive_loop(_encode({"type": "fetch", "id": entry_id, "tabId": 7}))
    fetch_resp = _read_all(io.BytesIO(out2))[0]
    assert fetch_resp["ok"] is True
    assert fetch_resp["entry"]["username"] == "alice"
    assert fetch_resp["password"] == "s3cret"
    assert fetch_resp["entry"]["notes"] == "2FA on"


def test_extension_subdomain_match(isolated, fake_keyring) -> None:
    """子網域(api.github.com) 應命中 github.com。"""
    msgs = b"".join(
        [
            _encode({
                "type": "save",
                "entry": {"label": "GH", "url": "github.com", "username": "a"},
                "password": "p",
            }),
            _encode({"type": "query", "url": "https://api.github.com/oauth/authorize", "tabId": 1}),
        ]
    )
    out = _drive_loop(msgs)
    resps = _read_all(io.BytesIO(out))
    assert len(resps[1]["matches"]) == 1


def test_extension_no_match_returns_empty(isolated, fake_keyring) -> None:
    """不同網域 → 0 命中、badge 清空。"""
    msgs = b"".join(
        [
            _encode({
                "type": "save",
                "entry": {"label": "GH", "url": "github.com", "username": "a"},
                "password": "p",
            }),
            _encode({"type": "query", "url": "https://gitlab.com/users/sign_in", "tabId": 1}),
        ]
    )
    out = _drive_loop(msgs)
    resps = _read_all(io.BytesIO(out))
    assert resps[1]["matches"] == []


def test_extension_multi_account_same_domain(isolated, fake_keyring) -> None:
    """同網域多帳號 → 都應命中。"""
    msgs = b"".join(
        [
            _encode({
                "type": "save",
                "entry": {"label": "GH-personal", "url": "github.com", "username": "alice"},
                "password": "p1",
            }),
            _encode({
                "type": "save",
                "entry": {"label": "GH-work", "url": "github.com", "username": "bob"},
                "password": "p2",
            }),
            _encode({"type": "query", "url": "https://github.com/", "tabId": 1}),
        ]
    )
    out = _drive_loop(msgs)
    resps = _read_all(io.BytesIO(out))
    assert len(resps[2]["matches"]) == 2
    usernames = {m["username"] for m in resps[2]["matches"]}
    assert usernames == {"alice", "bob"}


def test_extension_delete_propagates(isolated, fake_keyring) -> None:
    """刪除後 query 應回 0 命中。"""
    msgs1 = _encode({
        "type": "save",
        "entry": {"label": "GH", "url": "github.com", "username": "a"},
        "password": "p",
    })
    out1 = _drive_loop(msgs1)
    eid = _read_all(io.BytesIO(out1))[0]["id"]

    msgs2 = b"".join(
        [
            _encode({"type": "delete", "id": eid}),
            _encode({"type": "query", "url": "https://github.com/", "tabId": 1}),
        ]
    )
    out2 = _drive_loop(msgs2)
    resps = _read_all(io.BytesIO(out2))
    assert resps[0]["ok"] is True
    assert resps[1]["matches"] == []


def test_extension_report_url_updates_current_url(isolated, fake_keyring) -> None:
    """多次 report_url → 最後一次的值生效。"""
    msgs = b"".join(
        [
            _encode({"type": "report_url", "url": "https://github.com/a", "tabId": 1, "ts": 1.0}),
            _encode({"type": "report_url", "url": "https://github.com/b", "tabId": 1, "ts": 2.0}),
            _encode({"type": "report_url", "url": "https://gitlab.com/c", "tabId": 2, "ts": 3.0}),
        ]
    )
    _drive_loop(msgs)
    # 最後一次寫入生效
    assert storage.read_current_url() == "https://gitlab.com/c"
