"""PyInstaller 進入點 — 打包成 PWmgrSetup.exe(console,互動式安裝/解除安裝)。

在新電腦上取代 `python install.py` / `python uninstall.py`,不需要對方裝 Python。
用法:
    PWmgrSetup.exe              等同 python install.py
    PWmgrSetup.exe --uninstall  等同 python uninstall.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        from uninstall import main
    else:
        from install import main
    sys.exit(main())
