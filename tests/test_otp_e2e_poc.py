"""OTP 自動填端到端 POC。

模擬完整管線:
  1. PWmgr GUI 的 OutlookMonitor 收到一封 Dell OTP 信(直接呼叫 _ingest,
     繞過 win32com 因實機不一定有 Outlook)
  2. cache 寫到 disk
  3. Native host 收到 `get_otp` 訊息 → 讀 cache → 回 code
  4. Native host 的回應 bytes(模擬 Chrome 收的訊框格式)解回 JSON 確認

不啟動 GUI / Chrome,直接 inline 呼叫。
跑法:python tests/test_otp_e2e_poc.py
"""

from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import time
from pathlib import Path

# 把專案根加進 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pwmgr import native_host, outlook_monitor
from pwmgr.config import OTP_CODE_REGEX, OTP_SUBJECT_PATTERNS, otp_cache_path


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


def main() -> int:
    print("=== OTP 端到端 POC ===\n")

    with tempfile.TemporaryDirectory() as td:
        cache_path = Path(td) / "otp_cache.json"
        # 把 otp_cache_path monkey-patch 到暫存目錄。
        # 重要:native_host.py 用 `from .config import otp_cache_path`,
        # 那建立了 native_host 自己的 local binding,patch cfg.otp_cache_path
        # 不會傳到 native_host。必須同時 patch native_host.otp_cache_path。
        import pwmgr.config as cfg
        import pwmgr.native_host as nh_mod

        orig_cfg = cfg.otp_cache_path
        orig_nh = nh_mod.otp_cache_path
        cfg.otp_cache_path = lambda: cache_path  # type: ignore[assignment]
        nh_mod.otp_cache_path = lambda: cache_path  # type: ignore[assignment]

        # Test 環境沒 Outlook → live fallback 會卡在 COM,直接 mock 掉
        # 讓 fetch_latest_otp 立刻回 OUTLOOK_UNAVAILABLE
        orig_fetch = outlook_monitor.fetch_latest_otp
        outlook_monitor.fetch_latest_otp = lambda **kwargs: {
            "ok": False,
            "code": "OUTLOOK_UNAVAILABLE",
            "error": "mocked in test",
        }

        # 用戶的真實 settings.json 可能把 OTP 關掉(SA 測試環境跟 dev 環境
        # 共用 %LOCALAPPDATA%),這會讓測試拿到 OTP_DISABLED。覆寫 load_settings
        # 顯式回 {"otp_enabled": True} 確保測試環境 OTP 是開的(覆蓋 DEFAULT_OTP_ENABLED)。
        # 同時 mock save_settings 避免污染用戶的 settings.json。
        import pwmgr.settings as otp_settings_mod
        _test_target_folder = [None]  # 模擬 OTP folder 的 in-memory state
        orig_load_settings = otp_settings_mod.load_settings
        orig_save_settings = otp_settings_mod.save_settings
        orig_get_target = otp_settings_mod.get_otp_target_folder
        orig_set_target = otp_settings_mod.set_otp_target_folder
        otp_settings_mod.load_settings = lambda: {"otp_enabled": True}
        otp_settings_mod.save_settings = lambda data: None  # 不寫 disk
        otp_settings_mod.get_otp_target_folder = lambda: _test_target_folder[0]
        otp_settings_mod.set_otp_target_folder = lambda p: _test_target_folder.__setitem__(0, p)

        try:
            # --- Step 1: 模擬 OutlookMonitor 收到 Dell OTP 信 ---
            print("[1] OutlookMonitor 啟動 + ingest 一封假信")
            m = outlook_monitor.OutlookMonitor(
                cache_path=cache_path,
                subject_patterns=OTP_SUBJECT_PATTERNS,
                code_regex=OTP_CODE_REGEX,
                ttl_seconds=600,
            )
            # 不真的 start()(避免 win32com),直接呼叫 _ingest
            fake_subject = "Dell One-time Password"
            fake_body = """
                <html><body>
                <p>Your Dell verification code is <b>481902</b>.</p>
                <p>This code expires in 10 minutes.</p>
                </body></html>
            """
            m._ingest(fake_subject, fake_body)
            print(f"    cache entries: {len(m.list_recent())}")
            print(f"    cache file exists: {cache_path.exists()}")
            assert cache_path.exists(), "cache 檔應該被 _persist 寫出來"
            print(f"    cache file content: {cache_path.read_text(encoding='utf-8')[:200]}")
            # 確認 native host 看到的路徑跟我們寫的一致
            print(f"    native_host.otp_cache_path() -> {cfg.otp_cache_path()}")
            # 直接 call get_latest_otp 看會不會命中(排除 native host dispatch 干擾)
            direct = outlook_monitor.get_latest_otp(cache_path=cache_path)
            print(f"    direct get_latest_otp: {direct}")

            # --- Step 2: 透過 native host 訊框協議 round-trip ---
            print("\n[2] Native host round-trip:`get_otp` 訊息")
            stdin = io.BytesIO()
            stdout = io.BytesIO()

            # 模擬 Chrome 寫一個 get_otp 訊框到 stdin
            write_msg(stdin, {"type": "get_otp"})

            # 模擬 native host 從 stdin 讀 → dispatch → 寫到 stdout
            req = native_host._read_message(stdin)
            assert req is not None and req.get("type") == "get_otp"
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)  # 同 write_msg

            # 模擬 Chrome 從 stdout 讀回一個訊框
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")

            assert got.get("ok") is True, f"應為 ok,得到 {got}"
            assert got.get("code") == "481902", f"code 應為 481902,得到 {got.get('code')}"
            assert got.get("source") == "cache", f"應來自 cache,得到 {got.get('source')}"
            assert "Dell One-time Password" in got.get("subject", "")

            # --- Step 3: 過期的 code ---
            print("\n[3] 過期 code → cache miss → live fallback")
            # 把 cache 寫成 9999 秒前
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "codes": [{
                        "code": "111111",
                        "subject": "Dell One-time Password",
                        "received_at": time.time() - 9999,
                        "pattern_key": "Dell One-time Password",
                    }],
                    "version": 1,
                }, f)

            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "get_otp", "max_age_seconds": 600})
            req = native_host._read_message(stdin)
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
            # live fallback 在沒裝 Outlook 的環境會回 NO_CODE 或 OUTLOOK_UNAVAILABLE
            assert got.get("ok") is False, "過期 cache → 必須 fail"
            assert got.get("code") in ("NO_CODE", "OUTLOOK_UNAVAILABLE"), \
                f"error code 應為 NO_CODE/OUTLOOK_UNAVAILABLE,得到 {got.get('code')}"

            # --- Step 4: 沒收到信 + live 也不通 ---
            print("\n[4] 空 cache + live fallback → NO_CODE")
            cache_path.unlink()
            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "get_otp"})
            req = native_host._read_message(stdin)
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
            assert got.get("ok") is False
            assert got.get("code") in ("NO_CODE", "OUTLOOK_UNAVAILABLE", "PYWIN32_MISSING")

            # --- Step 5: max_age_seconds 防呆 ---
            print("\n[5] 防呆:max_age_seconds 負數 → BAD_REQUEST")
            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "get_otp", "max_age_seconds": -1})
            req = native_host._read_message(stdin)
            try:
                resp = native_host._dispatch(req)
            except native_host.BadRequestError as e:
                resp = {"ok": False, "code": "BAD_REQUEST", "error": str(e)}
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {got}")
            assert got.get("code") == "BAD_REQUEST"

            # --- Step 6: poll_seconds 輪詢 + 第 2 次 fetch 才命中 ---
            print("\n[6] poll_seconds=1:第 2 次 fetch 才命中 → 走 polling 邏輯")
            # override mock:前 1 次回 NO_CODE,第 2 次回 ok
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

            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "get_otp", "poll_seconds": 5, "max_age_seconds": 600})
            req = native_host._read_message(stdin)
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
            assert got.get("ok") is True, f"應為 ok,得到 {got}"
            assert got.get("code") == "654321"
            assert got.get("source") == "live"
            assert got.get("folder") == "your-email@your-domain.com/Inbox/Dell OTP"
            assert got.get("attempts") == 2, f"應為 2 次 attempts,得到 {got.get('attempts')}"
            assert got.get("poll_seconds") == 5
            assert call_count["n"] == 2, f"fetch 應被呼叫 2 次,得到 {call_count['n']}"

            # --- Step 7: poll_seconds=0 (單次) → 走 cache-first 路徑 ---
            print("\n[7] poll_seconds=0:單次 → 走 get_latest_otp cache-first")
            # 先把 cache 寫一筆新的(覆寫 step 6 留下的)
            m._ingest(
                "Dell One-time Password",
                "<html>Your code is <b>112233</b></html>",
            )
            outlook_monitor.fetch_latest_otp = lambda **kwargs: {
                "ok": False,
                "code": "OUTLOOK_UNAVAILABLE",
                "error": "fetch 不該被走到(cache 應命中)",
            }

            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "get_otp", "poll_seconds": 0})
            req = native_host._read_message(stdin)
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
            assert got.get("ok") is True
            assert got.get("code") == "112233"
            assert got.get("source") == "cache"
            assert got.get("attempts") == 1
            assert got.get("poll_seconds") == 0

            # --- Step 8: poll_seconds 防呆:負數 → BAD_REQUEST ---
            print("\n[8] 防呆:poll_seconds 負數 → BAD_REQUEST")
            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "get_otp", "poll_seconds": -1})
            req = native_host._read_message(stdin)
            try:
                resp = native_host._dispatch(req)
            except native_host.BadRequestError as e:
                resp = {"ok": False, "code": "BAD_REQUEST", "error": str(e)}
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {got}")
            assert got.get("code") == "BAD_REQUEST"

            # --- Step 9: target_folder_path 不存在 → OTP_FOLDER_NOT_FOUND ---
            print("\n[9] target_folder_path 設到不存在的 folder → OTP_FOLDER_NOT_FOUND")
            # polling 模式現在一律先查 cache(見 native_host._handle_get_otp),
            # 上一步(Step 7)寫的 "112233" 還在 max_age 內、會蓋掉這裡想測的
            # live-scan 失敗路徑,所以先清空 cache 讓這步真的走到 live scan。
            cache_path.write_text('{"codes": []}', encoding="utf-8")
            # 透過 settings 設一個不存在路徑,然後觸發 polling
            otp_settings_mod.set_otp_target_folder("your-email@your-domain.com/Inbox/不存在資料夾")
            outlook_monitor.fetch_latest_otp = lambda **kwargs: {
                "ok": False,
                "code": "OTP_FOLDER_NOT_FOUND",
                "error": "找不到指定的 OTP folder",
            }

            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "get_otp", "poll_seconds": 1})
            req = native_host._read_message(stdin)
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
            assert got.get("ok") is False
            assert got.get("code") == "OTP_FOLDER_NOT_FOUND"
            # 清掉設定避免污染其他測試
            otp_settings_mod.set_otp_target_folder(None)

            # --- Step 10: list_otp_folders dispatch ---
            print("\n[10] list_otp_folders dispatch(有裝 pywin32 + Outlook → 列真實 folder;沒裝 → PYWIN32_MISSING)")
            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "list_otp_folders"})
            req = native_host._read_message(stdin)
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
            # 兩種結果都接受:
            #   - 裝了 pywin32 + Outlook 在跑 → 真的列出 folders,ok=True
            #   - 沒裝 pywin32 → PYWIN32_MISSING,ok=False
            if got.get("ok"):
                folders = got.get("folders", [])
                assert isinstance(folders, list), f"folders 應為 list,得到 {type(folders)}"
                assert "scan_stats" in got
                assert "current_folder" in got
                # folder 格式檢查
                for f in folders[:5]:
                    assert "path" in f
                    assert "name" in f
                    assert "depth" in f
                    assert isinstance(f["depth"], int)
                print(f"    列出 {len(folders)} 個 folder (含真實 Outlook tree)")
            else:
                assert got.get("code") == "PYWIN32_MISSING", f"應為 PYWIN32_MISSING,得到 {got.get('code')}"

            # --- Step 11: set_otp_folder round-trip ---
            print("\n[11] set_otp_folder dispatch(寫入 + 讀回)")
            stdin = io.BytesIO()
            stdout = io.BytesIO()
            write_msg(stdin, {"type": "set_otp_folder", "path": "your-email@your-domain.com/Inbox/Dell OTP"})
            req = native_host._read_message(stdin)
            resp = native_host._dispatch(req)
            native_host._write_message(stdout, resp)
            stdout.seek(0)
            got = read_msg(stdout)
            print(f"    resp: {json.dumps(got, ensure_ascii=False)}")
            assert got.get("ok") is True
            assert got.get("path") == "your-email@your-domain.com/Inbox/Dell OTP"
            assert otp_settings_mod.get_otp_target_folder() == "your-email@your-domain.com/Inbox/Dell OTP"
            # 清掉
            otp_settings_mod.set_otp_target_folder(None)

            print("\n=== POC 全綠 ===")
            return 0
        finally:
            cfg.otp_cache_path = orig_cfg  # type: ignore[assignment]
            nh_mod.otp_cache_path = orig_nh  # type: ignore[assignment]
            outlook_monitor.fetch_latest_otp = orig_fetch
            otp_settings_mod.load_settings = orig_load_settings  # type: ignore[assignment]
            otp_settings_mod.save_settings = orig_save_settings  # type: ignore[assignment]
            otp_settings_mod.get_otp_target_folder = orig_get_target  # type: ignore[assignment]
            otp_settings_mod.set_otp_target_folder = orig_set_target  # type: ignore[assignment]


if __name__ == "__main__":
    sys.exit(main())