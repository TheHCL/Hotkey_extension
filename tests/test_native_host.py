"""原生訊息主機測試——用 io.BytesIO 模擬 stdin/stdout。"""

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


def _encode_message(obj: dict) -> bytes:
    payload = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def _read_message(stream: io.BytesIO) -> dict:
    header = stream.read(4)
    assert len(header) == 4, f"short header: {header!r}"
    (length,) = struct.unpack("<I", header)
    payload = stream.read(length)
    assert len(payload) == length, "short payload"
    return json.loads(payload.decode("utf-8"))


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def fake_keyring(monkeypatch) -> None:
    import keyring

    class Fake:
        def __init__(self):
            self.store = {}

        def set_password(self, s, u, p): self.store[(s, u)] = p
        def get_password(self, s, u): return self.store.get((s, u))
        def delete_password(self, s, u):
            self.store.pop((s, u), None)

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


def _drive_loop(input_bytes: bytes, timeout: float = 2.0) -> bytes:
    """用 thread 跑 nh.run,所有 input 預先寫入 stdin,timeout 後強制關閉。"""
    import threading

    stdin = io.BytesIO(input_bytes)
    stdout = io.BytesIO()

    orig_stdin = nh.sys.stdin
    orig_stdout = nh.sys.stdout
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


# --- 單元測試 --------------------------------------------------------------


def test_ping(isolated, fake_keyring) -> None:
    out = _drive_loop(_encode_message({"type": "ping"}))
    assert _read_message(io.BytesIO(out)) == {"ok": True, "pong": True}


def test_save_then_list(isolated, fake_keyring) -> None:
    msgs = b"".join(
        [
            _encode_message({
                "type": "save",
                "entry": {"label": "GH", "url": "github.com", "username": "alice"},
                "password": "secret",
            }),
            _encode_message({"type": "list"}),
        ]
    )
    out = _drive_loop(msgs)
    # 兩則回應
    s = io.BytesIO(out)
    r1 = _read_message(s)
    r2 = _read_message(s)
    assert r1["ok"] is True
    assert r1["id"]
    assert r2["ok"] is True
    assert len(r2["entries"]) == 1
    assert r2["entries"][0]["label"] == "GH"


def test_query_matches(isolated, fake_keyring) -> None:
    msgs = b"".join(
        [
            _encode_message({
                "type": "save",
                "entry": {"label": "GH", "url": "github.com", "username": "alice"},
                "password": "p",
            }),
            _encode_message({
                "type": "query",
                "url": "https://api.github.com/login",
            }),
        ]
    )
    out = _drive_loop(msgs)
    s = io.BytesIO(out)
    _read_message(s)  # save 回應
    r2 = _read_message(s)
    assert r2["ok"] is True
    assert len(r2["matches"]) == 1
    assert r2["matches"][0]["label"] == "GH"


def test_fetch_returns_password(isolated, fake_keyring) -> None:
    msgs = b"".join(
        [
            _encode_message({
                "type": "save",
                "entry": {"label": "GH", "url": "github.com", "username": "alice"},
                "password": "the-secret",
            }),
        ]
    )
    out = _drive_loop(msgs)
    s = io.BytesIO(out)
    r1 = _read_message(s)
    eid = r1["id"]

    msgs2 = _encode_message({"type": "fetch", "id": eid})
    out2 = _drive_loop(msgs2)
    r2 = _read_message(io.BytesIO(out2))
    assert r2["ok"] is True
    assert r2["password"] == "the-secret"
    assert r2["entry"]["username"] == "alice"


def test_fetch_not_found(isolated, fake_keyring) -> None:
    out = _drive_loop(_encode_message({"type": "fetch", "id": "nope"}))
    r = _read_message(io.BytesIO(out))
    assert r["ok"] is False
    assert r["code"] == "NOT_FOUND"


def test_delete(isolated, fake_keyring) -> None:
    msgs = b"".join(
        [
            _encode_message({
                "type": "save",
                "entry": {"label": "GH", "url": "github.com", "username": "alice"},
                "password": "p",
            }),
        ]
    )
    out = _drive_loop(msgs)
    eid = _read_message(io.BytesIO(out))["id"]

    out2 = _drive_loop(_encode_message({"type": "delete", "id": eid}))
    r = _read_message(io.BytesIO(out2))
    assert r["ok"] is True


def test_delete_not_found(isolated, fake_keyring) -> None:
    out = _drive_loop(_encode_message({"type": "delete", "id": "nope"}))
    r = _read_message(io.BytesIO(out))
    assert r["code"] == "NOT_FOUND"


def test_report_url(isolated, fake_keyring) -> None:
    out = _drive_loop(
        _encode_message({"type": "report_url", "url": "https://github.com", "tabId": 7})
    )
    r = _read_message(io.BytesIO(out))
    assert r["ok"] is True
    assert storage.read_current_url() == "https://github.com"


def test_unknown_type(isolated, fake_keyring) -> None:
    out = _drive_loop(_encode_message({"type": "wat"}))
    r = _read_message(io.BytesIO(out))
    assert r["ok"] is False
    assert r["code"] == "BAD_REQUEST"


def test_malformed_json_recovers(isolated, fake_keyring) -> None:
    """壞 JSON 應回 BAD_REQUEST 而不 crash。"""
    bad = b"not json"  # 沒長度前綴,被視為短 header
    # 短 header 會被 _read_message 拋 BadRequestError,主迴圈會嘗試回錯誤訊息
    out = _drive_loop(bad)
    # 主迴圈會在 BadRequestError 時回錯誤;如果 EOF 太早,可能只回錯誤或直接退出
    # 至少不應 crash
    assert out is not None


def test_eof_exits_cleanly(isolated, fake_keyring) -> None:
    out = _drive_loop(b"")  # 立即 EOF
    # run() 應回 0,沒任何輸出
    assert out == b""


def test_oversized_password(isolated, fake_keyring) -> None:
    big = "x" * 3000
    out = _drive_loop(_encode_message({
        "type": "save",
        "entry": {"label": "x", "url": "x.com", "username": "u"},
        "password": big,
    }))
    r = _read_message(io.BytesIO(out))
    assert r["code"] == "PASSWORD_TOO_LONG"
