"""資料模型。密碼欄位刻意不在此處——避免不小心序列化進 JSON,只在 keyring 中保存。"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PasswordEntry:
    """一筆密碼條目的「公開」部分。密碼不存於此。"""

    id: str
    label: str
    url: str  # 註冊網域,例如 "github.com"
    username: str
    notes: str = ""
    launch_url: str = ""  # 一鍵開啟的完整網址(例如 "https://github.com/login");不影響 matching
    created_at: float = 0.0
    updated_at: float = 0.0

    # --- 工廠方法 -----------------------------------------------------------

    @staticmethod
    def new(
        label: str,
        url: str,
        username: str,
        notes: str = "",
        launch_url: str = "",
    ) -> "PasswordEntry":
        now = time.time()
        return PasswordEntry(
            id=uuid.uuid4().hex,
            label=label,
            url=url,
            username=username,
            notes=notes,
            launch_url=launch_url,
            created_at=now,
            updated_at=now,
        )

    # --- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PasswordEntry":
        # 容忍缺欄位,給未來擴充
        return cls(
            id=data["id"],
            label=data.get("label", ""),
            url=data.get("url", ""),
            username=data.get("username", ""),
            notes=data.get("notes", ""),
            launch_url=data.get("launch_url", ""),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
        )

    def touch(self) -> None:
        self.updated_at = time.time()
