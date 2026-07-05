# coding: utf-8
"""路径展开工具：把命令行参数 / 远程转写请求里的文件与目录展开成待转录文件列表。"""

from pathlib import Path


# 支持的音视频扩展名（与 install_menu.py 保持一致）
VIDEO_EXT = ["mp4", "mkv", "flv", "webm", "avi", "mov", "wmv", "mpeg", "mpg", "rmvb", "ts", "3gp"]
AUDIO_EXT = ["mp3", "wav", "flac", "ape", "aac", "m4a", "wma", "ogg"]
ALL_EXT = set(VIDEO_EXT + AUDIO_EXT)


def expand_paths(raw_args):
    """
    将参数展开为待转录文件列表：
      - 文件直接收下（任意扩展名，不强过滤，方便用户传非标扩展名也行）
      - 目录递归扫描其中的音视频文件
      - 跳过不存在的路径
    去重并保持入参顺序（目录内按名称排序，便于确定性）。
    """
    seen = set()
    files = []
    for arg in raw_args:
        p = Path(arg)
        if not p.exists():
            continue
        if p.is_file():
            if p in seen:
                continue
            seen.add(p)
            files.append(p)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower().lstrip(".") in ALL_EXT:
                    if child in seen:
                        continue
                    seen.add(child)
                    files.append(child)
    return files