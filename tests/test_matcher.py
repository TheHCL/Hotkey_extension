"""table-driven URL 比對測試。"""

from __future__ import annotations

import pytest

from pwmgr.matcher import matches, normalize_query, path_prefix, registered_domain


# --- registered_domain -------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("github.com", "github.com"),
        ("GitHub.com", "github.com"),
        ("www.github.com", "github.com"),
        ("login.github.com", "github.com"),
        ("api.staging.github.com", "github.com"),
        ("192.168.1.1", "192.168.1.1"),
        ("", ""),
        # 簡化版已知限制:co.uk 被視為兩段 → "co.uk"
        ("www.bbc.co.uk", "co.uk"),
        ("bbc.co.uk", "co.uk"),
    ],
)
def test_registered_domain(host: str, expected: str) -> None:
    assert registered_domain(host) == expected


# --- path_prefix -------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/login", "/login"),
        ("https://github.com", "/"),
        ("https://github.com/", "/"),
        ("https://github.com/login/sub", "/login/sub"),
    ],
)
def test_path_prefix(url: str, expected: str) -> None:
    assert path_prefix(url) == expected


# --- normalize_query ---------------------------------------------------------


def test_normalize_query_basic() -> None:
    assert normalize_query("https://github.com/login") == ("github.com", "/login")


def test_normalize_query_strips_www() -> None:
    assert normalize_query("https://www.github.com/") == ("github.com", "/")


def test_normalize_query_subdomain() -> None:
    assert normalize_query("https://api.github.com/x") == ("github.com", "/x")


def test_normalize_query_empty_returns_none() -> None:
    assert normalize_query("") is None
    assert normalize_query("not a url") is None  # no scheme/host


# --- matches -----------------------------------------------------------------


@pytest.mark.parametrize(
    "entry_url,query,expected",
    [
        # 精確網域
        ("github.com", "https://github.com/login", True),
        ("github.com", "https://github.com/", True),
        # 子網域應命中
        ("github.com", "https://api.github.com/x", True),
        # 不同網域
        ("github.com", "https://gitlab.com/", False),
        # 大小寫
        ("github.com", "https://GitHub.com/login", True),
        # www.
        ("github.com", "https://www.github.com/", True),
        # IP 位址
        ("192.168.1.1", "http://192.168.1.1/login", True),
        # 路徑前綴要分段對齊
        ("example.com", "https://example.com/login", True),
        ("example.com", "https://example.com/login/sub", True),
        # http / https 視為同
        ("github.com", "http://github.com/login", True),
    ],
)
def test_matches_basic(entry_url: str, query: str, expected: bool) -> None:
    assert matches(entry_url, query, "") is expected


def test_matches_invalid_query() -> None:
    assert matches("github.com", "", "") is False
    assert matches("github.com", "not-a-url", "") is False


def test_matches_with_entry_path() -> None:
    # entry 指定 /login 前綴
    assert matches("github.com", "https://github.com/login", "/login") is True
    assert matches("github.com", "https://github.com/login/sub", "/login") is True
    # /log 不應匹配 /logout
    assert matches("github.com", "https://github.com/logout", "/log") is False
    # 完全不同的路徑
    assert matches("github.com", "https://github.com/settings", "/login") is False


def test_matches_with_entry_path_edge() -> None:
    # 根路徑
    assert matches("github.com", "https://github.com/anything", "/") is True
    # 前綴為空
    assert matches("github.com", "https://github.com/anything", "") is True
