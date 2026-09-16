"""Outlook 監聽 + OTP 快取。

設計脈絡
--------
- ``OutlookMonitor`` 開守護 thread **輪詢** Outlook Inbox 頂端(預設 3 秒/次),
  用 ``EntryID`` 去重避免重複處理同一封信。符合 subject pattern 的信 regex
  抽出 code,寫進 in-memory list 並 atomic rename 持久化到 cache JSON file。
- ``fetch_latest_otp`` 是 on-demand 入口,直接開 Outlook COM 翻 Inbox 找最近的信。
- ``get_latest_otp`` 是 native host 共用入口:cache 為主,cache miss 才 fallback
  on-demand 查詢。

為什麼用 polling 不用 win32com event sink
----------------------------------------
舊版設計用 ``win32com.client.DispatchWithEvents`` 綁 ``Items.OnItemAdd`` event,
但 Python 3.11+ + 新版 Office(Office 365 / Outlook 2021+)會炸::

    TypeError: metaclass conflict: the metaclass of a derived class must be a
    (non-strict) subclass of the metaclasses of all its bases

成因:DispatchWithEvents 動態建 subclass 繼承 COM class + handler class,
兩個 metaclass 不相容就炸。常見 trigger:Office update 後 gen_py 快取舊了、
Outlook 32/64-bit 不一致、pywin32 build 太舊。

polling 模式完全避開這條路,且更穩:
  - Outlook 重啟 / COM 斷線 → 自動 re-Dispatch
  - 不依賴 gen_py 快取
  - 邏輯透明,debug 友善

延遲成本:3 秒一輪,Dell OTP 從送出到本機收信本就 5-10 秒,完全在預算內。

為什麼 listener 放 PWmgr GUI 主程式而不是 native host
------------------------------------------------------
- Chrome 啟動 native host 是 per-connection(process 隨連線生滅),
  沒辦法長時間掛 Outlook 監聽。
- PWmgr GUI 是常駐 tray app,正好符合需求。
- native host 仍保留 on-demand fallback,涵蓋 GUI 未開的情境(冷啟動 Chrome、
  還沒開 PWmgr 等),讓按鈕永遠能 work(只是首次會慢一點)。

快取策略
--------
- JSON file 寫在 app_dir() 下,格式 ``{"codes": [...], "version": 1}``。
- atomic rename(寫 tmp → replace)避免 GUI crash 寫到一半毀了 cache。
- 啟動時 load cache,讓 GUI 重啟後仍記得最近的 code(避免 GUI 剛開信已經到了)。
- 啟動時預載目前 Inbox 所有信的 EntryID,避免把過去幾小時的 OTP 又當新信觸發。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

try:
    import win32com.client  # type: ignore
    import pythoncom  # type: ignore

    _HAS_PYWIN32 = True
except ImportError:  # 非 Windows / 沒裝 pywin32(只在 requirements 標 win32)
    _HAS_PYWIN32 = False


# --- Module-level event queue ---------------------------------------------------
#
# 為什麼需要這個:
# win32com event sink 的 handler class 不能有 __init__ 或 self state(跟 ref/out.py
# 的 OutlookHandler 一樣極簡,否則會碰到 metaclass conflict)。所以 handler 沒辦法
# 直接 reference monitor instance 呼叫 _ingest。
# 解法:handler 把 EntryID 推到 module-level queue,monitor thread 在 PumpWaitingMessages
# 之後 drain queue,透過 Session.GetItemFromID 延遲解析成 subject/body。
#
# 這跟 ref/out.py 不同(out.py 是 inline print),但架構等價。

_outlook_events_queue: list[str] = []  # EntryID list,還沒解析成 subject/body
_outlook_events_resolved: dict[str, dict[str, Any]] = {}  # EntryID → {subject, body, ts}
_outlook_events_lock = threading.Lock()
# EntryID retry 上限:同 eid 連續 resolve 失敗 N 次就丟掉(避免永遠卡住)
_OUTLOOK_EVENT_MAX_RETRIES = 5


def _push_event(eid: str) -> None:
    with _outlook_events_lock:
        if eid not in _outlook_events_queue:
            _outlook_events_queue.append(eid)


def _drain_resolved() -> list[dict[str, Any]]:
    """取出已解析完的 event(subject/body/EntryID)。"""
    with _outlook_events_lock:
        items = list(_outlook_events_resolved.values())
        _outlook_events_resolved.clear()
    return items


def _record_resolved(eid: str, subject: str, body: str) -> None:
    with _outlook_events_lock:
        _outlook_events_resolved[eid] = {
            "subject": subject,
            "body": body,
            "eid": eid,
        }
        if eid in _outlook_events_queue:
            _outlook_events_queue.remove(eid)


def _drop_eid(eid: str) -> None:
    """解析失敗超過 retry 上限後把 eid 從 queue 拿掉。"""
    with _outlook_events_lock:
        if eid in _outlook_events_queue:
            _outlook_events_queue.remove(eid)


def _pending_eids() -> list[str]:
    """取出目前未解析的 eids(用來在 monitor thread 下一輪重試)。"""
    with _outlook_events_lock:
        return list(_outlook_events_queue)


# --- 預設 pattern --------------------------------------------------------------

# 預設 subject pattern — 這裡是 **regex**(case-insensitive)。
# 出厂預設只認 Dell 一次性密碼信;若你有其他站,可加在這(每行一條 regex)。
# 例:同時認 Dell + Microsoft:
#   OTP_SUBJECT_PATTERNS = [r"Dell One-time Password", r"Microsoft.*verification code"]
# 注意 regex 用 re.search + IGNORECASE,所以不用 ^/$/大小寫,但可加 .* 容納前綴後綴
DEFAULT_SUBJECT_PATTERNS: list[str] = [r"Dell One-time Password"]

# 預設 code regex — 6 位數字,word boundary。避免 7 位電話 / 4 位年份誤判
DEFAULT_CODE_REGEX: str = r"\b(\d{6})\b"


# --- OutlookMonitor (polling listener) ------------------------------------------


# Outlook item class 與 folder id 參考 Microsoft 文檔
_OL_MAIL_ITEM = 43
_OL_FOLDER_INBOX = 6


# Sentinel:用來區分「沒傳 subscribed_stores(default)」vs「user 明確傳 None(自動偵測)」
_SUBSCRIBED_STORES_SENTINEL = object()


# --- win32com event sink(跟 ref/out.py 的 OutlookHandler 結構完全一致) ---------
#
# 重要:這個 class **不能**有 __init__、不能有 type hints、不能有 self state —
# 否則 DispatchWithEvents 在某些 pywin32 + Python 版本下會 metaclass conflict。
# 結構刻意跟 ref/out.py 一樣,因為那個結構已經驗證能 work。
#
# 職責:Outlook 把新信推進 Inbox 時,handler 被呼叫 → 把 EntryID 推到
# module-level queue → monitor thread 下一輪 pump 之後用 GetItemFromID
# 延遲解析(這時 item 已經完全 commit,讀 Class/Subject/Body 才不會 throw)。
#
# 為什麼不在這裡直接讀 Subject/Body:
#   Outlook MAPI 在 ItemAdd fire 時 item 還沒 commit,讀 property 會 throw
#   `MAPI_E_INVALID_PARAMETER` (0x80040107) — 大量這類 error 會拖慢 Outlook UI
#   甚至卡住。EntryID 是少數在 item-add 時就穩定的 property,所以只記這個,
#   延後到 monitor thread 下一次 pump 才 resolve。
class _OutlookEventHandler:
    def OnItemAdd(self, item):
        # 整段 try 包超寬:任何 COM error(包含讀 EntryID 失敗)都吞掉 —
        # event sink 偶爾 fire 但 item 還沒完全 ready 是正常的,不是 bug。
        try:
            eid = str(item.EntryID or "")
        except Exception:
            return
        if eid:
            _push_event(eid)


class OutlookMonitor:
    """背景 Outlook 監聽(polling) + OTP cache 持久化。

    使用方式::

        m = OutlookMonitor(cache_path=Path("otp_cache.json"))
        m.start()        # 啟動守護 thread;失敗不 raise(允許 GUI 不裝 Outlook)
        ...
        latest = m.get_latest(max_age_seconds=600)
        ...
        m.stop()         # GUI 退出時呼叫

    Thread safety: 對外 API(``get_latest`` / ``list_recent`` / ``status``)都
    透過 ``self._lock`` 保護,可從任意 thread 呼叫。``_ingest`` / ``_persist``
    從 monitor thread 內部呼叫,同樣受 lock 保護。
    """

    def __init__(
        self,
        cache_path: Path,
        subject_patterns: list[str] | None = None,
        code_regex: str = DEFAULT_CODE_REGEX,
        ttl_seconds: int = 600,
        max_entries: int = 10,
        poll_interval_seconds: float = 3.0,
        subscribed_stores: list[str] | None | object = None,  # None = 自動偵測, [] = 不訂, list = 指定
        enabled: bool = True,
    ) -> None:
        self._cache_path = Path(cache_path)
        # 編譯成 case-insensitive regex(支援複雜 pattern + 容錯大小寫/前綴後綴)
        raw_patterns = subject_patterns or DEFAULT_SUBJECT_PATTERNS
        self._subject_res: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in raw_patterns
        ]
        # 同時保留 raw strings 給 status() / pattern_key 比對用
        self._subject_patterns_raw: list[str] = list(raw_patterns)
        self._code_re = re.compile(code_regex)
        self._ttl = ttl_seconds
        self._max = max_entries
        self._poll_interval = max(1.0, poll_interval_seconds)
        # 訂閱設定:None = 自動偵測(Exchange mailbox + Outlook profile)
        # 用 sentinel 區分「user 傳 None」跟「沒傳」(use default)
        # 沒傳時用 DEFAULT = None(自動偵測)
        if subscribed_stores is _SUBSCRIBED_STORES_SENTINEL:
            self._subscribed_stores: list[str] | None = None
        else:
            self._subscribed_stores = subscribed_stores  # type: ignore[assignment]

        self._enabled = bool(enabled)

        self._lock = threading.Lock()
        self._codes: list[dict[str, Any]] = []  # newest first
        self._items: Any = None  # 持有 reference 避免 COM 物件被 GC
        self._started = False  # COM 已 attach 並開始輪詢
        self._stopped = False
        self._thread: threading.Thread | None = None

        self._load_cache()

    # --- 公開 API ------------------------------------------------------------

    def start(self) -> bool:
        """啟動 monitor thread。回傳 thread 是否啟動成功(不保證 listener 也 OK)。

        thread 啟動後真正的 Outlook attach 在另一條 thread 跑,失敗會 print
        不 raise;若需精細狀態請看 ``status()``。

        enabled=False 時直接 return False,不開 thread、不 dispatch Outlook —
        給 user 一個明確的「不用 OTP 就完全不打擾 Outlook」的開關。
        """
        if not self._enabled:
            print("[pwmgr][outlook] OTP 監聽已停用(由 otp_enabled 旗標控制),跳過啟動")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        if not _HAS_PYWIN32:
            print("[pwmgr][outlook] pywin32 未安裝,監聽跳過(captcha 不受影響)")
            return False
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="OutlookOTP",
            daemon=True,
        )
        self._thread.start()
        return True

    def set_enabled(self, enabled: bool) -> None:
        """即時切換開關。

        enabled=False:把 _stopped 設 True,thread 下輪 sleep 後自然退出,
        不會中斷當下 PumpWaitingMessages。Outlook COM 也會在 finally 釋放。
        enabled=True:僅更新旗標,需要 caller 額外呼叫 start() 才會真的開 thread
        (GUI 通常會 stop 舊 monitor 再 create 一個新的,邏輯更清楚)。
        """
        self._enabled = bool(enabled)
        if not self._enabled:
            self._stopped = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def stop(self, timeout: float = 3.0) -> None:
        """設 stop flag,等 thread 自然退出(最多 ``timeout`` 秒)。

        daemon=True 所以即便 thread 卡住,主程式也能正常退出。
        """
        self._stopped = True
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def get_latest(self, max_age_seconds: int | None = None) -> dict[str, Any] | None:
        """回傳最新且未過期的 code 記錄,或 ``None``。"""
        ttl = self._ttl if max_age_seconds is None else max_age_seconds
        cutoff = time.time() - ttl
        with self._lock:
            for entry in self._codes:
                if entry.get("received_at", 0) >= cutoff:
                    return dict(entry)
        return None

    def list_recent(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._codes[:limit]]

    def status(self) -> dict[str, Any]:
        with self._lock:
            latest_age = (
                round(time.time() - self._codes[0]["received_at"], 1) if self._codes else None
            )
            return {
                "started": self._started,
                "stopped": self._stopped,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "poll_interval_seconds": self._poll_interval,
                "subject_patterns": list(self._subject_patterns_raw),
                "code_regex": self._code_re.pattern,
                "ttl_seconds": self._ttl,
                "cache_path": str(self._cache_path),
                "entries": len(self._codes),
                "latest_age_seconds": latest_age,
                "subscribed_stores": (
                    None if self._subscribed_stores is None
                    else list(self._subscribed_stores)
                ),
            }

    # --- 內部 ---------------------------------------------------------------

    def _run(self) -> None:
        """Monitor thread 主迴圈:優先 event-driven(跟 ref/out.py 一樣的 pattern)。

        兩種模式:
          1. event-driven:`DispatchWithEvents(inbox.Items, _OutlookEventHandler)`
             + `pythoncom.PumpWaitingMessages()` — Outlook 主動通知新信,
             latency ~0(就像 ref/out.py 一樣能立刻收到)
          2. polling fallback:event-driven 在某些 store 失敗時自動降級,
             用 polling 掃 Items collection

        嘗試 event-driven,任何 store 訂閱成功就走 event-driven 模式;
        全部失敗才 fallback 純 polling。
        """
        try:
            # STA apartment — Outlook event sink 需要 STA;預設 CoInitialize 就 STA
            pythoncom.CoInitialize()
        except Exception as e:
            print(f"[pwmgr][outlook] CoInitialize: {e}")

        try:
            try:
                outlook = _get_outlook_app()
                session = outlook.Session
                # 訂閱策略(由 self._subscribed_stores 控制):
                #   - None(預設):自動偵測 — Exchange mailbox(@ in name)+ Outlook profile
                #   - []:不訂任何 store
                #   - ["name1", "name2"]:只訂這些名字完全符合的 store
                # 沒訂閱就不會 fire event → 沒 COM overhead → Outlook 不會卡
                watched: list[tuple[str, Any]] = []
                try:
                    stores = session.Folders
                    for i in range(int(stores.Count)):
                        store = stores.Item(i + 1)
                        sname = str(store.Name or "")
                        inbox = _find_subfolder(store, ("Inbox", "收件匣", "收件箱"))
                        if inbox is None:
                            continue
                        # 過濾邏輯
                        if self._subscribed_stores is None:
                            # 自動模式:Exchange mailbox + Outlook profile
                            is_exchange = "@" in sname
                            is_outlook = sname == "Outlook"
                            if not (is_exchange or is_outlook):
                                continue
                        else:
                            # 指定模式:名字完全符合才訂
                            if sname not in self._subscribed_stores:
                                continue
                        try:
                            watched.append((f"{sname}/Inbox", inbox.Items))
                        except Exception:
                            pass
                except Exception as e:
                    print(f"[pwmgr][outlook] walk stores failed: {e}")
                if not watched:
                    if self._subscribed_stores == []:
                        print(
                            "[pwmgr][outlook] OTP 監聽已手動關閉(subscribed_stores=[])"
                        )
                    else:
                        print(
                            "[pwmgr][outlook] 啟動失敗:找不到符合條件的 store Inbox "
                            "(檢查 OTP 監聽設定,或許需要新增 store 名稱)"
                        )
                    return

                # 嘗試 event-driven 訂閱
                sinks: list[tuple[str, Any]] = []
                for label, items in watched:
                    try:
                        sink = win32com.client.DispatchWithEvents(
                            items, _OutlookEventHandler
                        )
                        sinks.append((label, sink))
                    except Exception as e:
                        print(f"[pwmgr][outlook] 訂閱 {label} 失敗:{type(e).__name__}: {e}")

                self._started = True
                if sinks:
                    print(
                        f"[pwmgr][outlook] event-driven 啟動,cache={self._cache_path} "
                        f"(訂閱 {len(sinks)}/{len(watched)} 個 store,pump 間隔 {self._poll_interval}s)"
                    )
                    self._run_event_driven(session, sinks)
                else:
                    print(
                        f"[pwmgr][outlook] event-driven 全失敗,fallback polling,"
                        f"cache={self._cache_path} (監看 {len(watched)} 個 collection,poll {self._poll_interval}s)"
                    )
                    self._run_polling(watched)
            except Exception as e:
                print(
                    f"[pwmgr][outlook] 啟動失敗(Outlook 未裝或無 Inbox 存取): "
                    f"{type(e).__name__}: {e}"
                )
                return
        finally:
            self._items = None
            self._started = False
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _run_event_driven(self, session: Any, sinks: list[tuple[str, Any]]) -> None:
        """event-driven 主迴圈:持續 pump COM + 延遲解析 EntryID → item。

        OnItemAdd 只 push EntryID(怕 item 還沒 commit 時讀 property 會 throw),
        這裡 pump 之後用 Session.GetItemFromID 延遲讀 Class/Subject/Body,
        這時 item 通常已經 commit 了,可以安全讀。
        """
        # EntryID 解析失敗 retry counter — 太多次就放棄(eid 可能壞掉或被刪除)
        retry_counts: dict[str, int] = {}
        while not self._stopped:
            try:
                pythoncom.PumpWaitingMessages()
                # 1. 解析 queue 裡的 EntryID → subject/body,推到 _outlook_events_resolved
                for eid in _pending_eids():
                    if self._stopped:
                        break
                    try:
                        item = session.GetItemFromID(eid)
                    except Exception:
                        # Item 還沒 commit 或已被刪除 — 重試,到上限就丟掉
                        retry_counts[eid] = retry_counts.get(eid, 0) + 1
                        if retry_counts[eid] >= _OUTLOOK_EVENT_MAX_RETRIES:
                            _drop_eid(eid)
                            retry_counts.pop(eid, None)
                        continue
                    # resolve 成功,清掉 retry counter
                    retry_counts.pop(eid, None)
                    try:
                        if item.Class != _OL_MAIL_ITEM:
                            _drop_eid(eid)
                            continue
                        subj = str(item.Subject or "")
                        body = str(item.Body or "")
                    except Exception as e:
                        # 極少數狀況:resolve 成功但讀 property 還是有問題
                        retry_counts[eid] = retry_counts.get(eid, 0) + 1
                        if retry_counts[eid] >= _OUTLOOK_EVENT_MAX_RETRIES:
                            _drop_eid(eid)
                            retry_counts.pop(eid, None)
                        continue
                    _record_resolved(eid, subj, body)
                # 2. 把已 resolved 的拿去 _ingest
                for ev in _drain_resolved():
                    try:
                        self._ingest(ev["subject"], ev["body"])
                    except Exception as e:
                        print(f"[pwmgr][outlook] ingest event failed: {e}")
            except Exception as e:
                print(f"[pwmgr][outlook] pump error: {type(e).__name__}: {e}")
            time.sleep(self._poll_interval)

    def _run_polling(self, watched: list[tuple[str, Any]]) -> None:
        """polling fallback:每輪掃所有 collection 的頂端 N 封,自己比對 ReceivedTime。"""
        seen_eids: set[str] = set()
        # 預載所有 collection 的 EntryID,避免把舊信當新信
        for label, items in watched:
            try:
                for it in items:
                    try:
                        eid = it.EntryID
                        if eid:
                            seen_eids.add(eid)
                    except Exception:
                        continue
            except Exception:
                pass
        while not self._stopped:
            try:
                for label, items in watched:
                    seen_eids = self._poll_inbox(items, seen_eids)
            except Exception as e:
                print(f"[pwmgr][outlook] poll error: {type(e).__name__}: {e}")
                time.sleep(10)
                continue
            time.sleep(self._poll_interval)

    def _reconnect(self) -> bool:
        """Outlook COM 斷線後嘗試重連。回傳是否成功。"""
        try:
            outlook = _get_outlook_app()
            inbox = outlook.Session.GetDefaultFolder(_OL_FOLDER_INBOX)
            items = inbox.Items
            try:
                items.Sort("[ReceivedTime]", True)
            except Exception:
                pass
            self._items = items
            print("[pwmgr][outlook] 重連成功")
            return True
        except Exception as e:
            print(f"[pwmgr][outlook] 重連失敗: {type(e).__name__}: {e}")
            return False

    def _poll_inbox(self, items: Any, seen_eids: set[str]) -> set[str]:
        """掃一次 Inbox 頂端,處理新信。回傳更新後的 seen_eids。

        因為 ``items.Sort("[ReceivedTime]", True)`` 是 descending,
        第一封沒看過的 eid 之後都是更舊的,所以遇到已看過的就 break。

        ``items`` 可以是 None(Junk folder 不存在時),此時直接 return 不變。
        """
        if items is None:
            return seen_eids
        new_seen = seen_eids
        # 看每輪 lookback 封頂端 — Dell 場景信通常在頂部 5-10 封,但保險起見用 30
        lookback = 30
        try:
            count = 0
            for item in items:
                if count >= lookback:
                    break
                try:
                    if item.Class != _OL_MAIL_ITEM:
                        continue
                    eid = item.EntryID
                    if not eid:
                        continue
                    if eid in new_seen:
                        break  # 已看過 → 之後都是更舊的
                    new_seen = new_seen | {eid}  # 建新 set,不 mutate 參數
                    self._ingest(str(item.Subject or ""), str(item.Body or ""))
                except Exception:
                    # 單封信處理失敗不要影響整輪
                    continue
                finally:
                    count += 1
        except Exception:
            # iteration 中途失敗 → 讓 caller 走 reconnect 邏輯
            raise
        # Trim:超過 500 個就砍掉最舊一半
        if len(new_seen) > 500:
            new_seen = set(list(new_seen)[len(new_seen) // 2 :])
        return new_seen

    def _ingest(self, subject: str, body: str) -> None:
        """從單封信抽出 code 並加入 cache。從 monitor thread 內部呼叫。"""
        if not subject:
            return
        matched_raw: str | None = None
        for raw, cre in zip(self._subject_patterns_raw, self._subject_res):
            if cre.search(subject):
                matched_raw = raw
                break
        if matched_raw is None:
            return
        # Body 可能是 HTML;strip tags 後 regex。Outlook HTML body 偶爾帶 &nbsp;
        plain = re.sub(r"<[^>]+>", " ", body or "")
        plain = plain.replace("&nbsp;", " ").replace("&amp;", "&")
        match = self._code_re.search(plain)
        if not match:
            return
        code = match.group(1)
        entry: dict[str, Any] = {
            "code": code,
            "subject": subject,
            "received_at": time.time(),
            "pattern_key": matched_raw,
        }
        with self._lock:
            # 同 code 重複出現就更新時間(避免短時間內多次 ingest 擠掉較新的)
            self._codes = [e for e in self._codes if e.get("code") != code]
            self._codes.insert(0, entry)
            self._codes = self._codes[: self._max]
        # 持久化要快速完成(寫 disk 慢),lock 已經釋放才寫
        self._persist()

    def _persist(self) -> None:
        """寫 cache 到 disk。失敗不 raise — 只 print。"""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"codes": self._codes, "version": 1}, f, ensure_ascii=False)
            tmp.replace(self._cache_path)  # atomic rename(同 partition)
        except Exception as e:
            print(f"[pwmgr][outlook] cache persist 失敗: {e}")

    def _load_cache(self) -> None:
        """啟動時讀 cache(讓 GUI 重啟後仍記得最近 code)。"""
        if not self._cache_path.exists():
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = data.get("codes", [])
            if not isinstance(loaded, list):
                return
            # 只挑結構正確的 entry,避免 cache 被外部亂改時 crash
            valid: list[dict[str, Any]] = []
            for e in loaded:
                if (
                    isinstance(e, dict)
                    and isinstance(e.get("code"), str)
                    and isinstance(e.get("received_at"), (int, float))
                ):
                    valid.append(e)
            with self._lock:
                self._codes = valid[: self._max]
        except Exception as e:
            print(f"[pwmgr][outlook] cache load 失敗: {e}")


# --- On-demand query (cache miss 時的 fallback) --------------------------------


def fetch_latest_otp(
    subject_patterns: list[str] | None = None,
    code_regex: str = DEFAULT_CODE_REGEX,
    max_age_seconds: int = 600,
    lookback_count: int = 10,
    target_folder_path: str | None = None,
) -> dict[str, Any]:
    """直接開 Outlook COM,翻 folder 找最近符合的 OTP 信並抽 code。

    設計目標:**5-15 秒內完成**。Exchange mailbox Items 枚舉走網路,
    一次 round-trip 數十~數百 ms;Restrict/Sort 都是網路 call,直接拿掉。

    COM 連線:GetActiveObject → fallback Dispatch。
    搜尋策略:
      1. 若 ``target_folder_path`` 有給(user 在 GUI 指定的 folder)→ 只掃那個 folder
      2. 否則走預設:每個 store 的 Inbox(Exchange mailbox 排前)

    為什麼要支援 target_folder_path:
      Outlook rule 可能把 Dell OTP 信搬到自訂資料夾(例如「Inbox/Dell OTP」)。
      不支援的話,inbox 看不到信 → user 怎麼按按鈕都 NO_CODE。

    COM release:
      用 try/finally 包 CoUninitialize,函式 return 後 COM handle 一定釋放。
      對 user 來說「按按鈕 → poll → 拿到 code → Outlook 不會卡」是基本要求。
    """
    if not _HAS_PYWIN32:
        return {"ok": False, "code": "PYWIN32_MISSING", "error": "pywin32 未安裝"}

    raw_patterns = subject_patterns or DEFAULT_SUBJECT_PATTERNS
    subject_res = [re.compile(p, re.IGNORECASE) for p in raw_patterns]
    code_re = re.compile(code_regex)
    cutoff = time.time() - max_age_seconds
    targets: list[tuple[str, Any]] = []  # (label, folder) — 走訪清單
    scan_stats: list[str] = []

    co_initialized_here = False
    outlook = None
    try:
        try:
            pythoncom.CoInitialize()
            co_initialized_here = True
        except Exception:
            pass

        try:
            outlook = _get_outlook_app()
            session = outlook.Session

            # 路徑 A:user 指定 folder → 只掃那一個
            if target_folder_path:
                folder = _find_folder_by_path(session, target_folder_path)
                if folder is None:
                    return {
                        "ok": False,
                        "code": "OTP_FOLDER_NOT_FOUND",
                        "error": f"找不到指定的 OTP folder: {target_folder_path}",
                    }
                try:
                    n = int(folder.Items.Count)
                except Exception:
                    n = -1
                scan_stats.append(f"[{target_folder_path}]={n}")
                targets.append((target_folder_path, folder))
            else:
                # 路徑 B:預設 — 走所有 store,把 Inbox 加到 targets
                try:
                    stores = session.Folders
                    store_count = int(stores.Count)
                    scan_stats.append(f"找到 {store_count} 個 store")
                    for i in range(store_count):
                        store = stores.Item(i + 1)
                        sname = str(store.Name or "")
                        inbox = _find_subfolder(store, ("Inbox", "收件匣", "收件箱"))
                        if inbox is None:
                            scan_stats.append(f"[{sname}] 無 Inbox")
                            continue
                        try:
                            n = int(inbox.Items.Count)
                        except Exception:
                            n = -1
                        scan_stats.append(f"[{sname}] Inbox={n}")
                        if "@" in sname:
                            targets.insert(0, (f"{sname}/Inbox", inbox))
                        else:
                            targets.append((f"{sname}/Inbox", inbox))
                except Exception as e:
                    scan_stats.append(f"walk stores 失敗:{e}")

                if not targets:
                    fb = _safe_get_folder(session, _OL_FOLDER_INBOX)
                    if fb is not None:
                        targets.append(("default/Inbox", fb))

            recent_subjects: list[str] = []
            # 收集所有符合的 OTP 信,最後按 ReceivedTime 倒序取最新那封。
            # 原因:Outlook COM Items 預設順序不保證 desc by ReceivedTime
            # (Exchange 信箱甚至可能按 EntryID 或其他 key);如果直接拿第一個
            # 符合的,可能拿到的是較舊的信,user 就會看到「按按鈕抓的都是上一次的」。
            # 不呼叫 items.Sort 因為 Exchange 信箱 Sort 是網路 round-trip,
            # 大信箱下會 timeout;改成在 Python 內 sort。
            matches: list[dict[str, Any]] = []
            for folder_label, folder in targets:
                try:
                    items = folder.Items
                except Exception as e:
                    scan_stats.append(f"[{folder_label}] 取 Items 失敗:{e}")
                    continue
                checked = 0
                for item in items:
                    if checked >= lookback_count:
                        break
                    try:
                        if item.Class != _OL_MAIL_ITEM:
                            continue
                        recv = item.ReceivedTime
                        recv_ts = time.mktime(recv.timetuple()) if recv else 0.0
                        if recv_ts < cutoff:
                            continue
                        checked += 1
                        subj = str(item.Subject or "")
                        if len(recent_subjects) < 5:
                            recent_subjects.append(f"[{folder_label}] {subj}")
                        if not any(cre.search(subj) for cre in subject_res):
                            continue
                        body = str(item.Body or "")
                        plain = re.sub(r"<[^>]+>", " ", body)
                        plain = plain.replace("&nbsp;", " ").replace("&amp;", "&")
                        m = code_re.search(plain)
                        if not m:
                            continue
                        matches.append({
                            "ok": True,
                            "code": m.group(1),
                            "subject": subj,
                            "received_at": recv_ts,
                            "source": "live",
                            "folder": folder_label,
                        })
                    except Exception:
                        continue
            if matches:
                # 倒序:最新在前。同一個 max_age 內可能有多封(例如 user 連按多次
                # 「請求 OTP」觸發 Dell 寄多封信),拿最新那封才是 user 現在要用的。
                matches.sort(key=lambda m: m["received_at"], reverse=True)
                return matches[0]
            return {
                "ok": False,
                "code": "NO_CODE",
                "error": (
                    f"最近 {max_age_seconds}s 內 {len(targets)} 個 folder 找不到符合的信。"
                    f"scan 摘要:{' | '.join(scan_stats)}。"
                    f"subject 樣本:{recent_subjects[:5] if recent_subjects else '(沒收到信)'}"
                ),
            }
        finally:
            # 顯式 drop COM reference,讓 pywin32 早點 release。
            # Outlook instance 是 GetActiveObject 拿到的(共用在 user 開的 Outlook),
            # Dispatch 出來的才會自己關;但 folder / items reference drop 還是有幫助。
            outlook = None
            if co_initialized_here:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
    except Exception as e:
        return {
            "ok": False,
            "code": "OUTLOOK_UNAVAILABLE",
            "error": f"{type(e).__name__}: {e}",
        }


def _get_outlook_app():
    """取得 Outlook Application COM 物件。

    優先 ``GetActiveObject("Outlook.Application")`` — 連到 user 已經開的
    Outlook instance(跟 user 手動 terminal 跑 `python out.py` 看到同一個)。

    fallback ``Dispatch("Outlook.Application")`` — 若 Outlook 完全沒開,
    會自動啟動一個新 instance(通常拿 default profile)。
    """
    try:
        return win32com.client.GetActiveObject("Outlook.Application")
    except Exception:
        return win32com.client.Dispatch("Outlook.Application")


def _find_subfolder(parent, names: tuple[str, ...]):
    """在 parent folder 下找名稱符合的子 folder(支援多語系名稱)。"""
    if parent is None:
        return None
    try:
        sub_count = int(parent.Folders.Count)
    except Exception:
        return None
    # 第一輪:精確比對
    for j in range(sub_count):
        try:
            sub = parent.Folders.Item(j + 1)
            if str(sub.Name or "") in names:
                return sub
        except Exception:
            continue
    # 第二輪:case-insensitive
    for j in range(sub_count):
        try:
            sub = parent.Folders.Item(j + 1)
            if str(sub.Name or "").lower() in (n.lower() for n in names):
                return sub
        except Exception:
            continue
    return None


def _find_folder_by_path(session, path: str):
    """在 Outlook session 內,根據 `store/sub1/sub2/...` 路徑找 folder。

    路徑第一段是 store 名稱(對應 ``session.Folders.Item`` 名稱);
    後續每段用 `/` 分隔,逐層找。

    多語系處理 — 重要:
      - 第二段(parent = store):用 ``_find_subfolder`` 配多語系 tuple。
        中文版 Outlook 的 Inbox 是「收件匣」,用純 name 比對會 fail。
        跟 ``list_otp_candidate_folders`` / ``_collect_watch_collections`` 用的一致。
      - 第三段以後(parent = user folder):直接 ``_find_subfolder(folder, (name,))``。
        因為第三段後的名字本來就是從 inbox.Folders.Item 拿出來的真實名稱,
        list_otp_candidate_folders 已經把路徑組好存 settings,所以純比對就對。

    早期實作曾用 ``store.GetDefaultFolder(id)`` 試圖自動解析,但實測
    ``session.Folders.Item(i+1)`` 拿到的是 ``MAPIFolder`` 物件(不是
    ``Outlook.Store``),沒有 ``GetDefaultFolder`` 方法 → 直接 throw。
    改走跟 list_otp_candidate_folders 一樣的 ``_find_subfolder`` 多語系 tuple。

    找不到中間任何一層就回 None(不 raise)— caller 拿到 None 可以回
    OTP_FOLDER_NOT_FOUND 給 user,而不會 crash 整個 native host。

    跟 ``list_otp_candidate_folders`` 配對使用:UI 先列 candidates,user
    選一個後把 path 存到 settings,on-demand lookup 走這條路徑解析。
    """
    if not session or not path or not isinstance(path, str):
        return None
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return None
    # 第一段:store 名稱
    try:
        stores = session.Folders
        store_count = int(stores.Count)
    except Exception:
        return None
    store = None
    for i in range(store_count):
        try:
            s = stores.Item(i + 1)
            if str(s.Name or "") == parts[0]:
                store = s
                break
        except Exception:
            continue
    if store is None:
        return None
    # 第二段:用多語系 tuple 找(中文版 Outlook 的 Inbox 是「收件匣」)
    # 跟 list_otp_candidate_folders / _collect_watch_collections 一樣
    folder = _find_subfolder(store, ("Inbox", "收件匣", "收件箱"))
    if folder is None:
        return None
    # 第三段以後:parent 是 user folder,直接用 name 比對
    for name in parts[2:]:
        folder = _find_subfolder(folder, (name,))
        if folder is None:
            return None
    return folder


def list_otp_candidate_folders(
    max_depth: int = 2,
    max_per_level: int = 30,
) -> dict[str, Any]:
    """列出可選的 OTP target folder candidates(給 GUI 顯示用)。

    走所有 store,列舉 Inbox + 底下最多 ``max_depth`` 層的 subfolders(預設 2 層)。
    跳過明顯跟 OTP 無關的系統 folder(草稿 / 寄件備份 / 垃圾桶 / Junk)避免清單太長。

    回傳格式::
        {
          "ok": True,
          "folders": [
            {"path": "your-email@your-domain.com/Inbox", "name": "Inbox", "depth": 0},
            {"path": "your-email@your-domain.com/Inbox/Dell OTP", "name": "Dell OTP", "depth": 1},
            ...
          ],
          "scan_stats": ["找到 2 個 store", ...],
        }
    """
    if not _HAS_PYWIN32:
        return {"ok": False, "code": "PYWIN32_MISSING", "error": "pywin32 未安裝"}
    out: list[dict[str, Any]] = []
    scan_stats: list[str] = []
    # 跳過這些系統 folder 名稱(不分大小寫)— user 不會想把 OTP 監聽設在這
    _SKIP_NAMES = {
        "drafts", "draft", "草稿",
        "sent items", "sent", "寄件備份", "已传送邮件", "已傳送郵件",
        "deleted items", "trash", "垃圾桶", "刪除的邮件", "刪除的郵件",
        "junk", "junk email", "垃圾邮件", "垃圾郵件", "垃圾信件",
        "outbox", "寄件匣", "寄件箱",
        "notes", "備忘稿", "便签", "便箋",
        "journal", "日誌",
        "contacts", "連絡人", "联系人", "聯絡人",
        "calendar", "行事曆", "日历", "日曆",
        "tasks", "工作", "任务", "工作",
        "rss feeds", "rss 摘要", "rss 源",
    }
    co_initialized_here = False
    try:
        try:
            pythoncom.CoInitialize()
            co_initialized_here = True
        except Exception:
            pass
        outlook = _get_outlook_app()
        session = outlook.Session

        def _walk(parent, prefix: str, current_depth: int):
            try:
                count = int(parent.Folders.Count)
            except Exception:
                return
            for j in range(count):
                if len(out) >= max_per_level * (max_depth + 1) * 4:
                    return
                try:
                    sub = parent.Folders.Item(j + 1)
                    sub_name = str(sub.Name or "")
                    if not sub_name:
                        continue
                    full_path = f"{prefix}/{sub_name}" if prefix else sub_name
                    # 加進清單(不算 skip — user 可以看到完整 tree 自己選)
                    out.append({"path": full_path, "name": sub_name, "depth": current_depth})
                    # 遞迴(到 max_depth 停)
                    if current_depth < max_depth:
                        if sub_name.lower() not in _SKIP_NAMES:
                            _walk(sub, full_path, current_depth + 1)
                except Exception:
                    continue

        try:
            stores = session.Folders
            store_count = int(stores.Count)
            scan_stats.append(f"找到 {store_count} 個 store")
            for i in range(store_count):
                try:
                    store = stores.Item(i + 1)
                    sname = str(store.Name or "")
                    if not sname:
                        continue
                    out.append({"path": sname, "name": sname, "depth": 0})
                    inbox = _find_subfolder(store, ("Inbox", "收件匣", "收件箱"))
                    if inbox is not None:
                        # Inbox 自己加進去
                        out.append({
                            "path": f"{sname}/Inbox",
                            "name": "Inbox",
                            "depth": 1,
                        })
                        # 走 Inbox 底下第一層(略過 skip 的)
                        try:
                            ic = int(inbox.Folders.Count)
                        except Exception:
                            ic = 0
                            inbox = None  # type: ignore[assignment]
                        if inbox is not None:
                            for j in range(ic):
                                if len(out) >= 200:
                                    break
                                try:
                                    sub = inbox.Folders.Item(j + 1)
                                    sub_name = str(sub.Name or "")
                                    if not sub_name or sub_name.lower() in _SKIP_NAMES:
                                        continue
                                    out.append({
                                        "path": f"{sname}/Inbox/{sub_name}",
                                        "name": sub_name,
                                        "depth": 2,
                                    })
                                except Exception:
                                    continue
                except Exception:
                    continue
        except Exception as e:
            scan_stats.append(f"walk stores 失敗:{e}")
        return {"ok": True, "folders": out, "scan_stats": scan_stats}
    except Exception as e:
        return {
            "ok": False,
            "code": "OUTLOOK_UNAVAILABLE",
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        if co_initialized_here:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _safe_get_folder_in_store(store, folder_id: int):
    """在特定 store 內取 default folder by id;不存在或無權限就 return None。"""
    if store is None:
        return None
    try:
        return store.GetDefaultFolder(folder_id)
    except Exception:
        return None


def _collect_watch_collections(session) -> list[tuple[str, Any]]:
    """Walk 所有 store,收集 (label, Items) 給 monitor 用。

    每個 store 找 Inbox(支援多語系 folder 名稱) + Junk(若存在)。
    回傳 [(label, items), ...],label 形如 "your-email@your-domain.com/Inbox"。
    """
    out: list[tuple[str, Any]] = []
    if session is None:
        return out
    try:
        stores = session.Folders
        for i in range(int(stores.Count)):
            store = stores.Item(i + 1)
            store_name = str(store.Name or "")
            inbox = _find_subfolder(store, ("Inbox", "收件匣", "收件箱"))
            if inbox is not None:
                try:
                    out.append((f"{store_name}/Inbox", inbox.Items))
                except Exception:
                    pass
            junk = _safe_get_folder_in_store(store, 23) or _find_subfolder(
                store, ("Junk", "垃圾郵件", "垃圾信件", "垃圾")
            )
            if junk is not None:
                try:
                    out.append((f"{store_name}/Junk", junk.Items))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _safe_get_folder(session, folder_id: int, label: str):
    """取 default folder;不存在就 return None(不 raise)。"""
    try:
        return session.GetDefaultFolder(folder_id)
    except Exception:
        return None


# --- 給 native host 用:cache-first + live fallback ----------------------------


def get_latest_otp(
    cache_path: Path,
    subject_patterns: list[str] | None = None,
    code_regex: str = DEFAULT_CODE_REGEX,
    max_age_seconds: int = 600,
) -> dict[str, Any]:
    """Native host 共用入口。cache 命中就回,miss 才 on-demand 查 Outlook。

    回傳格式同 ``fetch_latest_otp``,但 ``source`` 欄位會標 ``cache`` 或 ``live``。
    """
    cutoff = time.time() - max_age_seconds

    # 1. Cache 先試(快,通常命中)
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.get("codes", []):
                if not isinstance(entry, dict):
                    continue
                recv = entry.get("received_at", 0)
                if isinstance(recv, (int, float)) and recv >= cutoff:
                    return {
                        "ok": True,
                        "code": str(entry.get("code", "")),
                        "subject": str(entry.get("subject", "")),
                        "received_at": recv,
                        "source": "cache",
                    }
        except Exception as e:
            print(f"[pwmgr][outlook] cache read 失敗: {e}")

    # 2. Cache miss → live fallback
    return fetch_latest_otp(
        subject_patterns=subject_patterns,
        code_regex=code_regex,
        max_age_seconds=max_age_seconds,
    )