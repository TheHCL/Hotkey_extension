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
    for el in ("conn", "matches", "empty", "no-host", "status", "current-url"):
        assert f'id="{el}"' in html, f"popup.html 缺少 #{el}"


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
