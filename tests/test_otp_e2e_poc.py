"""OTP 自動填端到端 POC(lazy on-demand,沒有背景監聽 / 沒有 cache)。

模擬完整管線:
  1. Native host 收到 `get_otp` 訊息 → 直接呼叫(mock 過的)
     outlook_monitor.fetch_latest_otp → 回 code
  2. Native host 的回應 bytes(模擬 Chrome 收的訊框格式)解回 JSON 確認

不啟動 GUI / Chrome,直接 inline 呼叫;也不需要真的裝 Outlook —
outlook_monitor.fetch_latest_otp 整個被 mock 掉。
跑法:python tests/test_otp_e2e_poc.py
"""

from __future__ import annotations

import io
import json
import struct
import sys
import time
from pathlib import Path

# 把專案根加進 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pwmgr import native_host, outlook_monitor


def write_msg(stream: io.BytesIO, msg: dict) -> None:
    """模擬 native host 對 Chrome 寫訊框:[4B LE length][UTF-8 JSON]"""
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.seek(0)  # BytesIO 不會自動 rewind,讀端需要從頭開始


def read_msg(stream: io.BytesIO) -> dict:
    """模擬 native host 從 Chrome 讀訊框

    假設 stream 已經在要讀的位置(write_msg 會 seek(0);
    native host _write_message 之後 caller 自己 seek)。
    """
    header = stream.read(4)
    if len(header) < 4:
        raise EOFError("no more messages")
    (length,) = struct.unpack("<I", header)
    payload = stream.read(length)
    return json.loads(payload.decode("utf-8"))


def roundtrip(msg: dict) -> dict:
    """模擬 Chrome → native host → Chrome 一次完整訊框往返。"""
    stdin = io.BytesIO()
    stdout = io.BytesIO()
    write_msg(stdin, msg)
    req = native_host._read_message(stdin)
    try:
        resp = native_host._dispatch(req)
    except native_host.BadRequestError as e:
        resp = {"ok": False, "code": "BAD_REQUEST", "error": str(e)}
    native_host._write_message(stdout, resp)
    stdout.seek(0)
    return read_msg(stdout)


def main() -> int:
    print("=== OTP 端到端 POC(lazy on-demand)===\n")

    orig_fetch = outlook_monitor.fetch_latest_otp

    # 用戶的真實 settings.json 可能把 OTP 關掉(SA 測試環境跟 dev 環境
    # 共用 %LOCALAPPDATA%),這會讓測試拿到 OTP_DISABLED。覆寫 load_settings
    # 顯式回 {"otp_enabled": True} 確保測試環境 OTP 是開的(覆蓋 DEFAULT_OTP_ENABLED)。
    # 同時 mock save_settings 避免污染用戶的 settings.json。
    import pwmgr.settings as otp_settings_mod
    _test_enabled = [True]
    _test_target_folder = [None]  # 模擬 OTP folder 的 in-memory state
    orig_load_settings = otp_settings_mod.load_settings
    orig_save_settings = otp_settings_mod.save_settings
    orig_get_enabled = otp_settings_mod.get_otp_enabled
    orig_set_enabled = otp_settings_mod.set_otp_enabled
    orig_get_target = otp_settings_mod.get_otp_target_folder
    orig_set_target = otp_settings_mod.set_otp_target_folder
    otp_settings_mod.load_settings = lambda: {}
    otp_settings_mod.save_settings = lambda data: None  # 不寫 disk
    otp_settings_mod.get_otp_enabled = lambda: _test_enabled[0]
    otp_settings_mod.set_otp_enabled = lambda v: _test_enabled.__setitem__(0, bool(v))
    otp_settings_mod.get_otp_target_folder = lambda: _test_target_folder[0]
    otp_settings_mod.set_otp_target_folder = lambda p: _test_target_folder.__setitem__(0, p)

    try:
        # --- Step 1: 單次(poll_seconds=0)直接命中 ---
        print("[1] poll_seconds=0:單次 fetch 命中")
        outlook_monitor.fetch_latest_otp = lambda **kwargs: {
            "ok": True,
            "code": "481902",
            "subject": "Dell One-time Password",
            "received_at": time.time(),
            "source": "live",
            "folder": "your-email@your-domain.com/Inbox",
        }
        got = roundtrip({"type": "get_otp"})
        print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
        assert got.get("ok") is True, f"應為 ok,得到 {got}"
        assert got.get("code") == "481902"
        assert got.get("attempts") == 1
        assert got.get("poll_seconds") == 0

        # --- Step 2: 單次沒找到 → NO_CODE ---
        print("\n[2] poll_seconds=0:單次沒找到 → NO_CODE")
        outlook_monitor.fetch_latest_otp = lambda **kwargs: {
            "ok": False,
            "code": "NO_CODE",
            "error": "沒收到信",
        }
        got = roundtrip({"type": "get_otp"})
        print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
        assert got.get("ok") is False
        assert got.get("code") == "NO_CODE"

        # --- Step 3: max_age_seconds 防呆 ---
        print("\n[3] 防呆:max_age_seconds 負數 → BAD_REQUEST")
        got = roundtrip({"type": "get_otp", "max_age_seconds": -1})
        print(f"    resp: {got}")
        assert got.get("code") == "BAD_REQUEST"

        # --- Step 4: poll_seconds 輪詢 + 第 2 次 fetch 才命中 ---
        print("\n[4] poll_seconds=1:第 2 次 fetch 才命中 → 走 polling 邏輯")
        call_count = {"n": 0}

        def fake_fetch(**kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return {
                    "ok": True,
                    "code": "654321",
                    "subject": "[External Mail] Dell 一次性密碼",
                    "received_at": time.time(),
                    "source": "live",
                    "folder": "your-email@your-domain.com/Inbox/Dell OTP",
                }
            return {
                "ok": False,
                "code": "NO_CODE",
                "error": "第 1 次還沒收到",
            }

        outlook_monitor.fetch_latest_otp = fake_fetch
        got = roundtrip({"type": "get_otp", "poll_seconds": 5, "max_age_seconds": 600})
        print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
        assert got.get("ok") is True, f"應為 ok,得到 {got}"
        assert got.get("code") == "654321"
        assert got.get("source") == "live"
        assert got.get("folder") == "your-email@your-domain.com/Inbox/Dell OTP"
        assert got.get("attempts") == 2, f"應為 2 次 attempts,得到 {got.get('attempts')}"
        assert got.get("poll_seconds") == 5
        assert call_count["n"] == 2, f"fetch 應被呼叫 2 次,得到 {call_count['n']}"

        # --- Step 5: poll_seconds 防呆:負數 → BAD_REQUEST ---
        print("\n[5] 防呆:poll_seconds 負數 → BAD_REQUEST")
        got = roundtrip({"type": "get_otp", "poll_seconds": -1})
        print(f"    resp: {got}")
        assert got.get("code") == "BAD_REQUEST"

        # --- Step 6: target_folder_path 不存在 → OTP_FOLDER_NOT_FOUND(不重試) ---
        print("\n[6] target_folder_path 設到不存在的 folder → OTP_FOLDER_NOT_FOUND")
        otp_settings_mod.set_otp_target_folder("your-email@your-domain.com/Inbox/不存在資料夾")
        call_count["n"] = 0

        def fake_fetch_not_found(**kwargs):
            call_count["n"] += 1
            return {
                "ok": False,
                "code": "OTP_FOLDER_NOT_FOUND",
                "error": "找不到指定的 OTP folder",
            }

        outlook_monitor.fetch_latest_otp = fake_fetch_not_found
        got = roundtrip({"type": "get_otp", "poll_seconds": 5})
        print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
        assert got.get("ok") is False
        assert got.get("code") == "OTP_FOLDER_NOT_FOUND"
        assert call_count["n"] == 1, "OTP_FOLDER_NOT_FOUND 不該重試"
        otp_settings_mod.set_otp_target_folder(None)  # 清掉避免污染其他測試

        # --- Step 7: master 開關關掉 → OTP_DISABLED,不呼叫 fetch_latest_otp ---
        print("\n[7] otp_enabled=False → OTP_DISABLED,不查 Outlook")
        call_count["n"] = 0
        outlook_monitor.fetch_latest_otp = fake_fetch_not_found  # 若被呼叫,call_count 會 > 0
        _test_enabled[0] = False
        got = roundtrip({"type": "get_otp"})
        print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
        assert got.get("ok") is False
        assert got.get("code") == "OTP_DISABLED"
        assert call_count["n"] == 0, "OTP_DISABLED 不該呼叫 fetch_latest_otp"
        _test_enabled[0] = True

        # --- Step 8: list_otp_folders dispatch ---
        print("\n[8] list_otp_folders dispatch(有裝 pywin32 + Outlook → 列真實 folder;沒裝 → PYWIN32_MISSING)")
        outlook_monitor.fetch_latest_otp = orig_fetch  # 還原成真的實作(這個 handler 不走 fetch_latest_otp)
        got = roundtrip({"type": "list_otp_folders"})
        print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
        # 兩種結果都接受:
        #   - 裝了 pywin32 + Outlook 在跑 → 真的列出 folders,ok=True
        #   - 沒裝 pywin32 → PYWIN32_MISSING,ok=False
        if got.get("ok"):
            folders = got.get("folders", [])
            assert isinstance(folders, list), f"folders 應為 list,得到 {type(folders)}"
            assert "scan_stats" in got
            assert "current_folder" in got
            for f in folders[:5]:
                assert "path" in f
                assert "name" in f
                assert "depth" in f
                assert isinstance(f["depth"], int)
            print(f"    列出 {len(folders)} 個 folder (含真實 Outlook tree)")
        else:
            assert got.get("code") == "PYWIN32_MISSING", f"應為 PYWIN32_MISSING,得到 {got.get('code')}"

        # --- Step 9: set_otp_folder round-trip ---
        print("\n[9] set_otp_folder dispatch(寫入 + 讀回)")
        got = roundtrip({"type": "set_otp_folder", "path": "your-email@your-domain.com/Inbox/Dell OTP"})
        print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
        assert got.get("ok") is True
        assert got.get("path") == "your-email@your-domain.com/Inbox/Dell OTP"
        assert otp_settings_mod.get_otp_target_folder() == "your-email@your-domain.com/Inbox/Dell OTP"
        otp_settings_mod.set_otp_target_folder(None)  # 清掉

        print("\n=== POC 全綠 ===")
        return 0
    finally:
        outlook_monitor.fetch_latest_otp = orig_fetch
        otp_settings_mod.load_settings = orig_load_settings  # type: ignore[assignment]
        otp_settings_mod.save_settings = orig_save_settings  # type: ignore[assignment]
        otp_settings_mod.get_otp_enabled = orig_get_enabled  # type: ignore[assignment]
        otp_settings_mod.set_otp_enabled = orig_set_enabled  # type: ignore[assignment]
        otp_settings_mod.get_otp_target_folder = orig_get_target  # type: ignore[assignment]
        otp_settings_mod.set_otp_target_folder = orig_set_target  # type: ignore[assignment]


if __name__ == "__main__":
    sys.exit(main())
