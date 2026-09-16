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

            print("\n=== POC 全綠 ===")
            return 0
        finally:
            cfg.otp_cache_path = orig_cfg  # type: ignore[assignment]
            nh_mod.otp_cache_path = orig_nh  # type: ignore[assignment]
            outlook_monitor.fetch_latest_otp = orig_fetch


if __name__ == "__main__":
    sys.exit(main())