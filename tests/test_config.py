"""``pwmgr.config`` 的路徑解析測試——重點是 ``python_executable()``。

``PWmgrSetup.exe`` 分支曾經硬編碼假設 ``PWmgr.exe`` 在同層的巢狀 ``PWmgr\\``
子資料夾裡;但正式發布的 release zip(``release.yml``)與 README 安裝說明都是
攤平佈局(``PWmgr.exe``、``_internal\\``、``chrome_extension\\`` 跟
``PWmgrSetup.exe`` 同一層),導致真實使用者裝好後 ``bin\\native_host.bat`` 與
開始功能表捷徑都會指向不存在的路徑。這裡鎖住「攤平優先、找不到才退回巢狀」
的行為,避免回歸。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import pwmgr.config as config


@pytest.fixture()
def frozen(monkeypatch: pytest.MonkeyPatch):
    """模擬 PyInstaller frozen 執行環境,回傳一個 setter(path) -> None。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def _set_executable(path: Path) -> None:
        monkeypatch.setattr(sys, "executable", str(path))

    return _set_executable


def test_pwmgr_exe_returns_itself(tmp_path: Path, frozen) -> None:
    exe = tmp_path / "PWmgr.exe"
    exe.touch()
    frozen(exe)

    assert config.python_executable() == str(exe.resolve())


def test_setup_exe_prefers_flat_layout(tmp_path: Path, frozen) -> None:
    """正式發布 / README 安裝流程的實際佈局:PWmgr.exe 跟 PWmgrSetup.exe 同層。"""
    setup_exe = tmp_path / "PWmgrSetup.exe"
    setup_exe.touch()
    flat_pwmgr = tmp_path / "PWmgr.exe"
    flat_pwmgr.touch()
    frozen(setup_exe)

    assert config.python_executable() == str(flat_pwmgr.resolve())


def test_setup_exe_falls_back_to_nested_layout(tmp_path: Path, frozen) -> None:
    """本機直接對 packaging/build.py 的原始 dist/ 輸出測試(還沒攤平/打包)。"""
    setup_exe = tmp_path / "PWmgrSetup.exe"
    setup_exe.touch()
    nested_dir = tmp_path / "PWmgr"
    nested_dir.mkdir()
    nested_pwmgr = nested_dir / "PWmgr.exe"
    nested_pwmgr.touch()
    frozen(setup_exe)

    assert config.python_executable() == str(nested_pwmgr.resolve())


def test_setup_exe_prefers_flat_when_both_exist(tmp_path: Path, frozen) -> None:
    setup_exe = tmp_path / "PWmgrSetup.exe"
    setup_exe.touch()
    flat_pwmgr = tmp_path / "PWmgr.exe"
    flat_pwmgr.touch()
    nested_dir = tmp_path / "PWmgr"
    nested_dir.mkdir()
    (nested_dir / "PWmgr.exe").touch()
    frozen(setup_exe)

    assert config.python_executable() == str(flat_pwmgr.resolve())


def test_setup_exe_defaults_to_flat_guess_when_neither_exists(tmp_path: Path, frozen) -> None:
    setup_exe = tmp_path / "PWmgrSetup.exe"
    setup_exe.touch()
    frozen(setup_exe)

    assert config.python_executable() == str((tmp_path / "PWmgr.exe").resolve())


def test_dev_mode_returns_current_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert config.python_executable() == sys.executable
