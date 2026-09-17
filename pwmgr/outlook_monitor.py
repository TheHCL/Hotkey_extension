"""Outlook OTP 查詢(lazy on-demand)。

設計脈絡
--------
只有一個入口:``fetch_latest_otp``,按 extension「取得 OTP 驗證碼」按鈕時
才直接開 Outlook COM 翻 folder 找最近符合的信、抽 code。沒有背景常駐監聽
(沒有 thread 持續訂閱 Outlook event 或 polling Inbox)。

為什麼不做背景監聽:實測 Outlook 2019 中文版 + Exchange 信箱下,任何形式的
背景常駐(event sink 訂閱 / 定時 polling)都會不定期讓 Outlook UI 感覺卡頓
——不管監聽的信有沒有 match、頻率調多低都躲不掉,因為只要 client process
掛著一個常駐 COM 連線,Outlook 自己的操作(切視窗、點信)就可能跟這個連線
的 RPC 撞在一起。Lazy on-demand 只在 user 主動按按鈕的短短幾秒內才碰
Outlook COM,其餘時間完全不干擾。
"""

from __future__ import annotations

import re
import time
from typing import Any

try:
    import win32com.client  # type: ignore
    import pythoncom  # type: ignore

    _HAS_PYWIN32 = True
except ImportError:  # 非 Windows / 沒裝 pywin32(只在 requirements 標 win32)
    _HAS_PYWIN32 = False


# --- 預設 pattern --------------------------------------------------------------

# 預設 subject pattern — 這裡是 **regex**(case-insensitive)。
# 出厂預設只認 Dell 一次性密碼信;若你有其他站,可加在這(每行一條 regex)。
# 例:同時認 Dell + Microsoft:
#   OTP_SUBJECT_PATTERNS = [r"Dell One-time Password", r"Microsoft.*verification code"]
# 注意 regex 用 re.search + IGNORECASE,所以不用 ^/$/大小寫,但可加 .* 容納前綴後綴
DEFAULT_SUBJECT_PATTERNS: list[str] = [r"Dell One-time Password"]

# 預設 code regex — 6 位數字,word boundary。避免 7 位電話 / 4 位年份誤判
DEFAULT_CODE_REGEX: str = r"\b(\d{6})\b"


# Outlook item class 與 folder id 參考 Microsoft 文檔
_OL_MAIL_ITEM = 43
_OL_FOLDER_INBOX = 6

# PidTagBody(純文字 body)的 MAPI proptag,格式見 Microsoft 文檔:
# 0x1000 = property id,001F = PT_UNICODE。
_PROP_TAG_BODY = "http://schemas.microsoft.com/mapi/proptag/0x1000001F"


def _get_body_fast(item) -> str:
    """讀信件 body,優先走 MAPI PropertyAccessor 直接拿 PR_BODY。

    為什麼比 ``item.Body`` 快:Outlook 2007+ 預設用 Word 當 mail editor,
    OOM 的 ``.Body``/``.HTMLBody`` getter 會經過 Word 物件模型轉一手做
    HTML→純文字轉換;對排版/圖片較多的 HTML 信,這層轉換在 Outlook 自己
    的 UI thread 上可以慢到讓 user 感覺卡一下 —— 這是「抓到 OTP 信那一刻
    才卡」的直接原因(平常不 match 的信早就被 subject 過濾掉,不會走到
    這裡)。PropertyAccessor 直接讀 MAPI store 資料,跳過 Word 轉換層。

    拿不到(少數格式沒有這個 property)就 fallback 用 ``.Body``,行為跟
    改之前完全一樣,不會漏抓。
    """
    try:
        return str(item.PropertyAccessor.GetProperty(_PROP_TAG_BODY) or "")
    except Exception:
        return str(item.Body or "")


# 單一 folder 每次 attempt 最多翻幾封信(不管有沒有命中 subject/時間窗)。
# 沒有這個上限的話,若 10 分鐘內的新信不到 lookback_count 封,
# `for item in items` 不會提早 break,會把整個 Items collection 掃完 ——
# 大信箱下這是外部 process 對 Outlook COM apartment 做的 O(n) 遍歷,
# 容易讓 Outlook UI 感覺卡頓。加這個 cap 讓每次 attempt 的 worst case 有界。
_SCAN_HARD_LIMIT = 200


def _iter_items_newest_first(items):
    """用 GetLast()/GetPrevious() 由新到舊走訪 Outlook Items collection。

    不用 `for item in items`(COM 預設 enumerator,順序是 collection 內部順序,
    通常接近到達順序=舊到新)、也不呼叫 `items.Sort`(Exchange 信箱是網路
    round-trip,大信箱會很慢)。GetLast/GetPrevious 是 Outlook COM 原生支援
    的走訪方式,直接由最新的一封開始,不需要額外排序開銷。
    """
    try:
        item = items.GetLast()
    except Exception:
        return
    while item is not None:
        yield item
        try:
            item = items.GetPrevious()
        except Exception:
            return


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
            # 用 GetLast/GetPrevious 從新到舊走訪(不呼叫 items.Sort,那是網路
            # round-trip,大信箱下會 timeout)。這比原本 `for item in items`
            # (COM 預設 enumerator,常常是舊到新的到達順序)快很多:大信箱下
            # 用 for-loop 得先掃過幾百封舊信才會碰到最新的幾封,每封都要
            # ReceivedTime/Subject/Body 三次 COM property fetch,是實測按鈕
            # 點下去 Outlook 卡住幾秒的主因;新到舊走訪通常 1~2 封就能命中,
            # 而且一旦碰到超出 max_age 的信就能直接整個 break(不用像舊版只是
            # continue、繼續耗 _SCAN_HARD_LIMIT 次數),因為再往前只會更舊。
            matches: list[dict[str, Any]] = []
            for folder_label, folder in targets:
                try:
                    items = folder.Items
                except Exception as e:
                    scan_stats.append(f"[{folder_label}] 取 Items 失敗:{e}")
                    continue
                checked = 0
                examined = 0
                for item in _iter_items_newest_first(items):
                    examined += 1
                    if examined > _SCAN_HARD_LIMIT:
                        scan_stats.append(
                            f"[{folder_label}] 掃到上限 {_SCAN_HARD_LIMIT} 筆仍未收集滿 "
                            f"{lookback_count} 筆,提早停止"
                        )
                        break
                    if checked >= lookback_count:
                        break
                    try:
                        if item.Class != _OL_MAIL_ITEM:
                            continue
                        recv = item.ReceivedTime
                        recv_ts = time.mktime(recv.timetuple()) if recv else 0.0
                        if recv_ts < cutoff:
                            # 新到舊順序下,再往前只會更舊 — 直接整個 break,
                            # 不用像舊版只 continue、白白耗掉剩下的 examined 額度。
                            scan_stats.append(
                                f"[{folder_label}] 已掃到 max_age 之外的信"
                                f"(examined={examined}),提早停止"
                            )
                            break
                        checked += 1
                        subj = str(item.Subject or "")
                        if len(recent_subjects) < 5:
                            recent_subjects.append(f"[{folder_label}] {subj}")
                        if not any(cre.search(subj) for cre in subject_res):
                            continue
                        body = _get_body_fast(item)
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
        跟 ``list_otp_candidate_folders`` 用的一致。
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
    # 跟 list_otp_candidate_folders 一樣
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
    # 跳過這些系統 folder 名稱(不分大小寫)— user 不會想把 OTP 目標 folder 設在這
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


def _safe_get_folder(session, folder_id: int):
    """取 default folder;不存在就 return None(不 raise)。"""
    try:
        return session.GetDefaultFolder(folder_id)
    except Exception:
        return None
