"""入口分流:`python -m pwmgr` 走 GUI,`python -m pwmgr --native` 走原生主機。"""

from __future__ import annotations

import sys


def main() -> int:
    if "--native" in sys.argv:
        from . import native_host

        return native_host.run()
    from . import app

    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
