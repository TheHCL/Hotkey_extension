"""入口分流:`python -m pwmgr` 走 GUI,`python -m pwmgr --native` 走原生主機。

`--show-logs`:dev 快速取得 fail log——直接開 log 資料夾,不啟動 GUI/native host。
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if "--show-logs" in sys.argv:
        from .config import logs_dir

        os.startfile(logs_dir())  # type: ignore[attr-defined]  # Windows-only
        return 0

    if "--native" in sys.argv:
        from . import native_host
        from .logging_setup import setup_logging

        setup_logging("native_host")
        return native_host.run()

    from . import app
    from .logging_setup import setup_logging

    setup_logging("gui")
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
