import sys
import os
from os.path import dirname, join, exists

# PyInstaller windowed 模式 (console=False) 下，
# sys.stdin/stdout/stderr 可能为 None，导致 Rich/logger/multiprocessing 崩溃。
# 用 os.devnull 替代 None，确保所有 I/O 操作不会抛出 AttributeError。
if sys.stdin is None:
    sys.stdin = open(os.devnull, 'r', encoding='utf-8')
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

# 将「执行文件所在目录」添加到「模块查找路径」
# 这确保了可以找到复制的源文件（config.py, core/ 等）
executable_dir = dirname(sys.executable)
sys.path.insert(0, executable_dir)

# PyInstaller 打包时，第三方依赖（DLL, PYD）放在 internal/ 目录
# 需要将 internal/ 也添加到路径，否则 Python 无法找到这些依赖
internal_dir = join(executable_dir, 'internal')
if exists(internal_dir):
    sys.path.insert(0, internal_dir)
