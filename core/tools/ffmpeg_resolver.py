# coding: utf-8
"""
FFmpeg / ffprobe 路径解析器

为打包分发场景设计：优先使用程序目录下内置的 ffmpeg.exe / ffprobe.exe，
找不到再 fallback 到系统 PATH。

为何不直接用 "ffmpeg" 裸字符串调用 subprocess：
  - shutil.which 与 CreateProcess 确实会搜索 cwd，但右键菜单调用时 cwd 不可控
    （通常是 explorer.exe 的工作目录或被点击文件所在目录，而非 exe 所在目录）
  - 对打包分发场景，终端用户不应被强制装 ffmpeg 并配置 PATH
  - 用绝对路径调用 subprocess 才稳定可靠
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def _app_dir() -> Path:
    """
    定位应用程序根目录。

    PyInstaller 打包后：sys.executable 是 start_server.exe，其所在目录即程序根目录。
    开发模式：sys.argv[0] 是 start_server.py，其所在目录是项目根目录。
    两者其一返回非空路径即可。
    """
    # 1. PyInstaller / Nuitka 等打包场景：exe 同目录
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 2. 开发模式：脚本所在目录
    if getattr(sys, "argv", None) and sys.argv[0]:
        p = Path(sys.argv[0]).resolve()
        if p.exists():
            return p.parent
    # 3. 兜底：当前工作目录
    return Path.cwd().resolve()


def _resolve(name: str) -> Optional[str]:
    """
    解析可执行文件路径：
      1. 优先查找 _app_dir() 下的 name.exe
      2. 找不到再 fallback 到 shutil.which(name) 搜索系统 PATH
    返回绝对路径字符串，找不到返回 None。
    """
    # 1. 程序目录内置
    local_exe = _app_dir() / f"{name}.exe"
    if local_exe.is_file():
        return str(local_exe)

    # 2. 系统 PATH 兜底（自动处理 .exe 后缀）
    return shutil.which(name)


def get_ffmpeg() -> Optional[str]:
    """返回 ffmpeg 可执行文件绝对路径，找不到返回 None。"""
    return _resolve("ffmpeg")


def get_ffprobe() -> Optional[str]:
    """返回 ffprobe 可执行文件绝对路径，找不到返回 None。"""
    return _resolve("ffprobe")


def has_ffmpeg() -> bool:
    """是否存在可用的 ffmpeg。"""
    return get_ffmpeg() is not None


def has_ffprobe() -> bool:
    """是否存在可用的 ffprobe。"""
    return get_ffprobe() is not None


def require_ffmpeg() -> str:
    """
    返回 ffmpeg 绝对路径；找不到抛 RuntimeError。
    供确实需要 ffmpeg 的逻辑使用，调用方无需再判空。
    """
    p = get_ffmpeg()
    if p is None:
        raise RuntimeError(
            "未找到 ffmpeg。请将 ffmpeg.exe 放置在程序根目录，"
            "或安装 FFmpeg 并将其 bin 目录加入系统 PATH。"
        )
    return p


def require_ffprobe() -> str:
    """
    返回 ffprobe 绝对路径；找不到抛 RuntimeError。
    """
    p = get_ffprobe()
    if p is None:
        raise RuntimeError(
            "未找到 ffprobe。请将 ffprobe.exe 放置在程序根目录，"
            "或安装 FFmpeg 并将其 bin 目录加入系统 PATH。"
        )
    return p
