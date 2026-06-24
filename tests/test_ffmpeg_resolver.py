# coding: utf-8
"""
ffmpeg_resolver 单元测试

验证：
1. 优先返回程序目录内置的 ffmpeg.exe / ffprobe.exe
2. 没有内置时 fallback 到系统 PATH
3. 都没有时返回 None / require_* 抛 RuntimeError
4. frozen 模式（PyInstaller）下使用 sys.executable 定位
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest

import core.tools.ffmpeg_resolver as resolver


@pytest.fixture
def fake_app_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """伪造一个程序目录，里面没有 ffmpeg.exe"""
    monkeypatch.setattr(resolver, "_app_dir", lambda: tmp_path)
    return tmp_path


def test_resolve_finds_local_exe(fake_app_dir: Path):
    """优先返回程序目录下的 ffmpeg.exe"""
    fake_ffmpeg = fake_app_dir / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"\x4d\x5a")  # MZ 魔数

    # 即使系统 PATH 没装 ffmpeg，也应返回本地 exe
    with mock.patch.object(resolver.shutil, "which", return_value=None):
        result = resolver._resolve("ffmpeg")

    assert result is not None
    assert Path(result) == fake_ffmpeg


def test_resolve_falls_back_to_path(fake_app_dir: Path):
    """没有内置 exe 时 fallback 到 shutil.which"""
    with mock.patch.object(resolver.shutil, "which", return_value="C:/some/path/ffmpeg.exe"):
        result = resolver._resolve("ffmpeg")

    assert result == "C:/some/path/ffmpeg.exe"


def test_resolve_returns_none_when_not_found(fake_app_dir: Path):
    """既无内置也无 PATH：返回 None"""
    with mock.patch.object(resolver.shutil, "which", return_value=None):
        result = resolver._resolve("ffmpeg")

    assert result is None


def test_require_ffmpeg_raises_when_missing(fake_app_dir: Path):
    with mock.patch.object(resolver.shutil, "which", return_value=None):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            resolver.require_ffmpeg()


def test_require_ffprobe_raises_when_missing(fake_app_dir: Path):
    with mock.patch.object(resolver.shutil, "which", return_value=None):
        with pytest.raises(RuntimeError, match="ffprobe"):
            resolver.require_ffprobe()


def test_require_returns_path_when_present(fake_app_dir: Path):
    fake_ffprobe = fake_app_dir / "ffprobe.exe"
    fake_ffprobe.write_bytes(b"\x4d\x5a")

    with mock.patch.object(resolver.shutil, "which", return_value=None):
        result = resolver.require_ffprobe()

    assert result is not None
    assert Path(result) == fake_ffprobe


def test_has_ffmpeg_false_when_missing(fake_app_dir: Path):
    with mock.patch.object(resolver.shutil, "which", return_value=None):
        assert resolver.has_ffmpeg() is False


def test_app_dir_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """frozen 模式下用 sys.executable 定位"""
    fake_exe = tmp_path / "start_server.exe"
    fake_exe.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "argv", [], raising=False)

    # 重新计算
    assert resolver._app_dir() == fake_exe.parent


def test_app_dir_dev_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """开发模式：用 sys.argv[0] 定位"""
    fake_script = tmp_path / "start_server.py"
    fake_script.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "argv", [str(fake_script)], raising=False)

    assert resolver._app_dir() == fake_script.parent


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
