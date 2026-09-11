"""打包成 exe——在有裝 Python + requirements-dev.txt 的開發機上執行:

    python packaging\\build.py

會在 dist\\ 產生:
    PWmgr\\             — 資料夾(PWmgr.exe + _internal\\),含 GUI + native host + ddddocr
    PWmgrSetup.exe     — 互動式安裝 / 解除安裝(console,單檔,不含 ddddocr)

其他電腦不需要裝 Python,只需把 dist\\PWmgr\\ 整個資料夾 + chrome_extension\\
(放同一層目錄)複製給對方。

設計取捨:
- PWmgr 用 --onedir:啟動時不用解到 %TEMP%,cold start 較快(尤其首次
  import ddddocr + onnxruntime 就要好幾秒)。整個資料夾大小 180MB 左右,
  但每個檔案都已是 final form,disk 友善。
- PWmgrSetup 用 --onefile:純安裝用,不需要 ddddocr / onnxruntime,
  省下那 170MB。複製也方便(單一檔)。
- --collect-all ddddocr + onnxruntime:ddddocr 是 lazy import(在
  _ensure_ocr() 內才 import ddddocr),PyInstaller 6 static analysis
  抓不到;onnxruntime 的 native DLL 散在子模組,沒 collect-all 會缺。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON = PROJECT_ROOT / "packaging" / "pwmgr.ico"

# 只有 PWmgr(GUI + native host)需要 ddddocr 跟 onnxruntime;
# PWmgrSetup 是安裝器,絕對用不到。
_CAPTCHA_BUNDLE_ARGS = [
    "--collect-all", "ddddocr",
    "--collect-all", "onnxruntime",
    "--hidden-import", "ddddocr",
    "--hidden-import", "onnxruntime",
]


def run_pyinstaller(*args: str) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", *args]
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def main() -> int:
    # PWmgr --onedir(資料夾形式,啟動快)
    run_pyinstaller(
        "--onedir", "--windowed",
        "--name", "PWmgr",
        "--icon", str(ICON),
        "--paths", str(PROJECT_ROOT),
        *_CAPTCHA_BUNDLE_ARGS,
        str(PROJECT_ROOT / "packaging" / "entry_gui.py"),
    )
    # PWmgrSetup --onefile(單檔,ddddocr 不包進去)
    run_pyinstaller(
        "--onefile", "--console",
        "--name", "PWmgrSetup",
        "--icon", str(ICON),
        "--paths", str(PROJECT_ROOT),
        str(PROJECT_ROOT / "packaging" / "entry_setup.py"),
    )
    print()
    print("[build] 完成。輸出在 dist\\PWmgr\\(資料夾) 與 dist\\PWmgrSetup.exe。")
    print("[build] 分發時:")
    print("  - 把 dist\\PWmgr\\ 整個資料夾 + chrome_extension\\ 放同一層,給對方")
    print("  - 把 dist\\PWmgrSetup.exe 給對方雙擊跑一次(安裝/解除用)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

