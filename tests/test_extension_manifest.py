"""驗證 chrome_extension/ 的完整性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

EXT_DIR = Path(__file__).resolve().parent.parent / "chrome_extension"


def test_manifest_is_valid_mv3() -> None:
    m = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3
    assert "nativeMessaging" in m["permissions"]
    assert "tabs" in m["permissions"]
    assert "scripting" in m["permissions"]
    assert "activeTab" in m["permissions"]
    assert "<all_urls>" in m["host_permissions"]
    # background script 路徑
    bg = m.get("background", {})
    assert bg.get("service_worker") == "background.js"
    # popup 與 icons
    assert m.get("action", {}).get("default_popup") == "popup.html"


def test_manifest_referenced_files_exist() -> None:
    m = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    files = [m["background"]["service_worker"], m["action"]["default_popup"]]
    for icon_path in m["action"]["default_icon"].values():
        files.append(icon_path)
    for icon_path in m.get("icons", {}).values():
        files.append(icon_path)
    for f in files:
        path = EXT_DIR / f
        assert path.exists(), f"manifest 參考的檔案不存在: {f}"


def test_popup_html_mentions_key_ids() -> None:
    """popup.html 必須有 popup.js 會用到的元素 id。"""
    html = (EXT_DIR / "popup.html").read_text(encoding="utf-8")
    for el in (
        "conn",
        "matches",
        "empty",
        "status",
        "current-url",
        "launch-search",
        "group-menu",
    ):
        assert f'id="{el}"' in html, f"popup.html 缺少 #{el}"


def test_popup_html_has_group_menu() -> None:
    """cascading 群組選單結構必須存在。"""
    html = (EXT_DIR / "popup.html").read_text(encoding="utf-8")
    assert 'id="group-menu"' in html
    assert 'class="group-list"' in html or 'id="group-list"' in html
    # dropdown select 應該拿掉
    assert 'id="group-filter"' not in html


def test_popup_css_has_group_menu_style() -> None:
    """popup.css 必須為 cascading 群組選單定義樣式。"""
    css = (EXT_DIR / "popup.css").read_text(encoding="utf-8")
    assert "#group-menu" in css
    assert ".entry-list" in css
    assert ".group-item:hover" in css
    assert ".group-item.is-open" in css
    # dropdown 樣式應該移除
    assert "#group-filter" not in css


def test_popup_js_renders_group_menu() -> None:
    """popup.js 必須用 renderGroupMenu 建 nested DOM,並掛 hover handlers。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    assert "renderGroupMenu" in js
    assert "group-item" in js
    assert "group-label" in js
    assert "entry-list" in js
    assert "mouseenter" in js
    assert "mouseleave" in js
    assert "launchAndFill" in js
    assert "is-open" in js


def test_popup_js_no_dropdown_or_session_group() -> None:
    """dropdown dropdown state + session 持久化應完全移除(避免殘留 dead code)。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    assert "selectedGroup" not in js
    assert "popup_group" not in js
    assert "populateGroupFilter" not in js
    assert "group-filter" not in js


def test_popup_js_uses_chrome_apis() -> None:
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    assert "chrome.runtime.sendMessage" in js
    assert "chrome.tabs.query" in js


def test_background_js_connects_native() -> None:
    js = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    assert "connectNative" in js
    assert '"com.thehcl.pwmgr"' in js
    assert 'type: "query"' in js
    assert 'type: "report_url"' in js
    assert 'type: "fetch"' in js


def test_content_js_dispatches_input_change() -> None:
    """content script 必須派發 input + change 事件,React/Vue 受控元件才生效。"""
    js = (EXT_DIR / "content.js").read_text(encoding="utf-8")
    assert 'new Event("input"' in js
    assert 'new Event("change"' in js
    # React 相容:透過 prototype setter 設值
    assert "Object.getOwnPropertyDescriptor(proto, \"value\")" in js or "Object.getOwnPropertyDescriptor" in js


def test_icons_present() -> None:
    icons_dir = EXT_DIR / "icons"
    for size in (16, 32, 48, 128):
        assert (icons_dir / f"{size}.png").exists(), f"缺少 icon {size}.png"


def test_background_handles_open_launch_and_get_all_entries() -> None:
    """background.js 接管 openLaunch(開新分頁)+ getAllEntries(整 vault 列表)兩種訊息。"""
    js = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    assert 'msg.type === "openLaunch"' in js
    assert "chrome.tabs.create" in js
    assert "BAD_URL" in js
    assert 'msg.type === "getAllEntries"' in js
    assert '"list"' in js


def test_popup_html_has_no_tabs() -> None:
    """簡化版:沒有 tab bar。"""
    html = (EXT_DIR / "popup.html").read_text(encoding="utf-8")
    assert 'id="tabs"' not in html
    assert "tab-fill" not in html
    assert "tab-launch" not in html


def test_popup_js_no_two_view_state() -> None:
    """單一視窗 — 沒有 currentView/loadLaunchView/switchView。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    assert "currentView" not in js
    assert "loadLaunchView" not in js
    assert "switchView" not in js


def test_popup_autofill_and_launch_routes() -> None:
    """popup 必須有兩條 render 路徑:命中模式(autofill) + fallback 模式(launch_url 跳轉)。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    # 兩條後端訊息路由
    assert "queryFresh" in js
    assert "getAllEntries" in js
    # 兩條 render 函式
    assert "renderAutofillList" in js
    assert "renderLaunchList" in js
    # 兩種點擊 handler
    assert "fill(id," in js or "function fill" in js
    assert "openLaunch(url" in js or "function openLaunch" in js
    # fallback 過濾
    assert "launch_url" in js
    assert ".filter(" in js or "filter(e =>" in js


def test_popup_css_no_tab_styles() -> None:
    """CSS 沒有 tab 相關樣式,但 fallback 用的 #launch-search / max-height 還在。"""
    css = (EXT_DIR / "popup.css").read_text(encoding="utf-8")
    assert "#tabs" not in css
    assert ".tab.active" not in css
    assert "#launch-search" in css
    assert "max-height" in css


def test_manifest_has_tabs_permission() -> None:
    """chrome.tabs.create 需要 tabs 權限——任何移除會破壞 launch_url 開新分頁功能。"""
    m = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert "tabs" in m["permissions"]


def test_manifest_storage_permission_kept() -> None:
    """navigate toggle 用 chrome.storage.sync 持久化,storage 權限必須保留。"""
    m = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert "storage" in m["permissions"]


def test_popup_html_has_navigate_toggle() -> None:
    """footer 必須有 navigate-toggle checkbox。"""
    html = (EXT_DIR / "popup.html").read_text(encoding="utf-8")
    assert 'id="navigate-toggle"' in html
    assert 'type="checkbox"' in html


def test_popup_js_uses_chrome_storage() -> None:
    """popup.js 必須用 chrome.storage.sync 讀寫 navigate_enabled。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    assert "chrome.storage" in js
    assert "navigate_enabled" in js


def test_popup_js_branches_on_navigate_toggle() -> None:
    """OFF 時直接 showEmpty + 早 return,不進 fallback 分支。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    assert "navigate_enabled" in js
    assert "showEmpty" in js


def test_popup_uses_launch_and_fill_message() -> None:
    """fallback click 應送 launchAndFill 訊息(openLaunch 不再被 fallback 模式使用)。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    assert '"launchAndFill"' in js
    assert "launchAndFill" in js


def test_background_handles_launch_and_fill() -> None:
    """background.js 接管 launchAndFill:fetch + create + pendingFill + 等 status=complete。"""
    js = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    assert 'msg.type === "launchAndFill"' in js
    # 暫存機制
    assert "pendingFill" in js
    assert "setPendingFill" in js
    # 等 render 完成
    assert 'status === "complete"' in js
    # 30 秒 timeout
    assert "30000" in js
    # 送 fill 訊息給 content script
    assert 'type: "fill"' in js


def test_background_cleanup_pending_on_tab_removed() -> None:
    """tab 關掉時清掉 pendingFill,避免記憶體洩漏。"""
    js = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    assert "clearPendingFill" in js
    assert "chrome.tabs.onRemoved" in js


def test_background_uses_storage_session_for_pending_fill() -> None:
    """pendingFill 同步寫到 chrome.storage.session,content script 主動 pull,
    解決 MV3 sendMessage 與 listener attach 之間的 race condition。"""
    js = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    assert "chrome.storage.session.set" in js
    assert "chrome.storage.session.get" in js
    assert "chrome.storage.session.remove" in js
    assert "claimPendingFill" in js


def test_content_js_claims_pending_fill_on_startup() -> None:
    """content.js 啟動後主動 sendMessage claimPendingFill 拉 credentials。"""
    js = (EXT_DIR / "content.js").read_text(encoding="utf-8")
    assert "claimPendingFill" in js
    assert "fillForm" in js


# --- 群組顏色覆寫 (popup 使用 GUI 的 group_colors) -------------------------


def test_popup_js_uses_group_colors_override() -> None:
    """popup.js 必須讀取後端回傳的 group_colors 並用它覆寫預設 palette。"""
    js = (EXT_DIR / "popup.js").read_text(encoding="utf-8")
    # 接收後端回傳的 group_colors 欄位
    assert "group_colors" in js, "popup.js 應讀取後端的 group_colors 欄位"
    # 還要有解析覆寫的 helper
    assert "resolveGroupColor" in js, "popup.js 應有 resolveGroupColor 解析覆寫"
    # 但原本的 hash palette + 未分類中性色仍要保留(向下相容)
    assert "GROUP_PALETTE" in js
    assert "NEUTRAL_COLOR" in js
    # 派生 {bg, accent} 的工具函式(把單一 hex 展開)
    assert "hexToBgAccent" in js
