# coding: utf-8
from multiprocessing import freeze_support
from config_server import ServerConfig as Config
from core.server.path_utils import expand_paths
from core.server.app import CapsWriterServer


def _filter_already_transcribed(files):
    """过滤掉已存在转录输出文件的音视频文件，避免重复转录。

    判定规则：若该音视频文件的同名 .txt 已存在，则视为已转录，跳过。
    用 .txt 作为标志因为它是最通用的输出（file_save_txt 默认开启）。
    可通过 Config.skip_existing 关闭此行为。
    """
    if not getattr(Config, 'skip_existing', True):
        return files
    kept = []
    skipped = 0
    for f in files:
        txt_path = f.with_suffix('.txt')
        if txt_path.exists():
            skipped += 1
            continue
        kept.append(f)
    if skipped:
        print(f"[skip] 跳过 {skipped} 个已转录文件（同名 .txt 已存在）", flush=True)
    return kept


if __name__ == '__main__':
    # 启用对 PyInstaller 打包后的多进程支持
    freeze_support()

    # 原始命令行入参（含目录），保留传给 app，用于 client 模式批量转发给已有 server
    raw_args = __import__('sys').argv[1:]
    # 展开为待转录文件列表（支持文件与目录混合，目录递归扫描音视频文件）
    files = expand_paths(raw_args)
    # 跳过已转录（同名 .txt 已存在）
    files = _filter_already_transcribed(files)

    # 直接实例化并启动门面类即可
    # 环境初始化职责已下放至 CapsWriterServer
    CapsWriterServer(files=files, raw_args=raw_args).start()