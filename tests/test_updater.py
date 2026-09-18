"""更新檢查測試——monkeypatch urllib,不打真的網路。"""

from __future__ import annotations

import json
import urllib.error

import pytest

import pwmgr.updater as updater
import pwmgr.version as version


class _FakeResponse:
    """模擬 ``urllib.request.urlopen`` 回傳的 context-manager response。"""

    def __init__(self, payload: dict | bytes) -> None:
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _release_payload(tag: str, zip_name: str | None = None) -> dict:
    zip_name = zip_name if zip_name is not None else f"PWmgr-{tag}.zip"
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/TheHCL/Hotkey_extension/releases/tag/{tag}",
        "assets": [
            {"name": zip_name, "browser_download_url": f"https://example.com/{zip_name}"},
        ],
    }


@pytest.fixture()
def _pin_current_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "CURRENT_VERSION", "1.0.0")


def test_update_available(monkeypatch: pytest.MonkeyPatch, _pin_current_version: None) -> None:
    payload = _release_payload("v1.1.0")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    result = updater.check_for_update()

    assert result is not None
    assert result.version == "1.1.0"
    assert result.zip_url == "https://example.com/PWmgr-v1.1.0.zip"
    assert result.release_url == payload["html_url"]


def test_same_version_is_not_an_update(
    monkeypatch: pytest.MonkeyPatch, _pin_current_version: None
) -> None:
    payload = _release_payload("v1.0.0")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    assert updater.check_for_update() is None


def test_older_version_is_not_an_update(
    monkeypatch: pytest.MonkeyPatch, _pin_current_version: None
) -> None:
    payload = _release_payload("v0.9.0")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    assert updater.check_for_update() is None


def test_missing_zip_asset_returns_none(
    monkeypatch: pytest.MonkeyPatch, _pin_current_version: None
) -> None:
    payload = _release_payload("v1.1.0", zip_name="something-else.zip")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    assert updater.check_for_update() is None


def test_network_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _raise)

    assert updater.check_for_update() is None


def test_malformed_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"not json")
    )

    assert updater.check_for_update() is None


def test_empty_tag_name_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _release_payload("v1.1.0")
    payload["tag_name"] = ""
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    assert updater.check_for_update() is None


def test_get_current_version_matches_module() -> None:
    assert updater.get_current_version() == version.__version__


def test_version_string_is_three_dot_separated_ints() -> None:
    parts = version.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
