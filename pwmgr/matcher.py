"""URL 正規化與比對。

v1 簡化版:
- host 小寫化、去 www. 前綴
- registered_domain = 倒數兩段(不處理 co.uk、com.tw 等公開後綴)
- 比對:兩者 registered_domain 相等 + query.path 是否以 entry 的 path 前綴開頭
"""

from __future__ import annotations

from urllib.parse import urlparse


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _is_ip(host: str) -> bool:
    """粗略判斷 IPv4 或 [IPv6]。"""
    if host.startswith("[") and host.endswith("]"):
        return True
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        n = int(p)
        if n < 0 or n > 255:
            return False
    return True


def registered_domain(host: str) -> str:
    """簡化版 eTLD+1:取倒數兩段。

    範例:
        github.com        -> github.com
        login.github.com  -> github.com
        www.bbc.co.uk     -> bbc.co.uk   (簡化版視為兩段,b.co.uk 才算 bbc)
        192.168.1.1       -> 192.168.1.1
    """
    if not host:
        return ""
    h = _strip_www(host.lower().strip())
    if _is_ip(h):
        return h
    parts = h.split(".")
    if len(parts) <= 2:
        return h
    return ".".join(parts[-2:])


def path_prefix(url: str) -> str:
    """從完整 URL 中抽出 path(沒有則回 '/')。"""
    parsed = urlparse(url)
    return parsed.path or "/"


def normalize_query(url: str) -> tuple[str, str] | None:
    """正規化一個「查詢目標 URL」,回傳 (registered_domain, path)。

    解析失敗(空字串、沒有 host)回傳 None。
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    return registered_domain(parsed.hostname), parsed.path or "/"


def matches(entry_url: str, query_url: str, entry_path: str = "") -> bool:
    """判斷 entry 是否對應到 query URL。

    規則:
        1. 兩者 registered_domain 相等。
        2. entry_path 為空時匹配任何路徑;否則 query.path 必須以 entry_path 開頭,
           且在分段邊界對齊(prefix='/log' 不應匹配 '/logout')。

    範例:
        entry_url='github.com',   query='https://github.com/login'      -> True
        entry_url='github.com',   query='https://api.github.com/x'      -> True
        entry_url='github.com',   query='https://gitlab.com/'            -> False
        entry_url='github.com',   query='https://github.com/settings'    -> True
    """
    q = normalize_query(query_url)
    if q is None:
        return False
    e_dom = registered_domain(entry_url)
    if e_dom != q[0]:
        return False
    prefix = (entry_path or "").rstrip("/")
    q_path = (q[1] or "/").rstrip("/") or "/"
    if not prefix:
        return True
    if q_path == prefix:
        return True
    return q_path.startswith(prefix + "/")
