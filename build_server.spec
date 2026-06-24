# -*- mode: python ; coding: utf-8 -*-
"""
Server-only PyInstaller build spec
仅打包 start_server.exe，用于右键菜单转录场景

改动记录（2026-06-23）：
- hiddenimports 加入 rapidfuzz（热词音素 RAG 需要）
- excludes 移除 rapidfuzz（原为 Client 独有，现在 Server 也用）
- excludes 保留 typer（srt_from_txt 已改为延迟导入，运行时不需要）
- my_files 去掉已删的 hot.txt 和 add_context_menu.bat
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
from os.path import join, basename, dirname, exists
from os import walk, makedirs
from shutil import copyfile, rmtree

# ==================== 打包配置选项 ====================
INCLUDE_CUDA_PROVIDER = False
# ====================================================

# 初始化空列表
binaries = []
hiddenimports = []
datas = []

# 收集 sherpa_onnx 相关文件
try:
    sherpa_datas = collect_data_files('sherpa_onnx', include_py_files=False)
    if not INCLUDE_CUDA_PROVIDER:
        filtered_datas = []
        for src, dest in sherpa_datas:
            if 'providers_cuda' not in basename(src).lower():
                filtered_datas.append((src, dest))
            else:
                print(f"[INFO] 排除 CUDA provider: {basename(src)}")
        sherpa_datas = filtered_datas
    datas += sherpa_datas
except:
    pass

# 收集 Pillow 相关文件（用于托盘图标）
try:
    pillow_datas = collect_data_files('PIL', include_py_files=False)
    datas += pillow_datas
    pillow_binaries = collect_all('PIL')
    binaries += pillow_binaries[1]
except:
    pass

# 收集 pypinyin 数据文件
try:
    pypinyin_datas = collect_data_files('pypinyin', include_py_files=False)
    datas += pypinyin_datas
except:
    pass

# 隐藏导入 - Server 端需要的模块
hiddenimports += [
    'websockets',
    'websockets.client',
    'websockets.server',
    'rich',
    'rich.console',
    'rich.markdown',
    'rich._unicode_data.unicode17-0-0',
    'numpy',
    'pypinyin',
    'watchdog',
    'sherpa_onnx',
    'PIL',
    'PIL.Image',
    'pystray',
    'onnxruntime',
    'tkinter',
    # GGUF 引擎相关
    'gguf',
    'soundfile',
    'srt',
    # 热词后处理链依赖（音素 RAG 模糊匹配）
    'rapidfuzz',
    'rapidfuzz.fuzz',
    'rapidfuzz.distance',
    'rapidfuzz.distance.OSA',
    # 标点模型相关
    'sherpa_onnx.non_python',
]

a_1 = Analysis(
    ['start_server.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['build_hook.py'],
    excludes=[
        'IPython',
        'PySide6', 'PySide2', 'PyQt5',
        'matplotlib', 'wx',
        'funasr', 'pydantic', 'torch',
        # Client 独有依赖，Server 不需要
        'keyboard', 'pynput', 'pyclip', 'sounddevice',
        # typer 仅用于 srt_from_txt 命令行入口，运行时已延迟导入
        'typer',
        'numba',
        'openai', 'ollama', 'httpx',
    ],
    noarchive=True,
)

# 过滤系统 CUDA DLL
filtered_binaries = []
for name, src, type in a_1.binaries:
    src_lower = src.lower() if isinstance(src, str) else ''
    is_system_cuda_dll = (
        '\\nvidia gpu computing toolkit\\cuda\\' in src_lower or
        '\\nvidia\\cudnn\\' in src_lower or
        ('\\cuda\\v' in src_lower and '\\bin\\' in src_lower)
    )
    is_unwanted_onnx_dll = (
        'onnxruntime_providers_cuda.dll' in name.lower()
    )
    if not is_system_cuda_dll and not is_unwanted_onnx_dll:
        filtered_binaries.append((name, src, type))
    else:
        reason = "环境 CUDA DLL" if is_system_cuda_dll else "冗余 ONNX DLL"
        print(f"[INFO] 排除 {reason}: {name} (从 {src} 收集)")
a_1.binaries = filtered_binaries

# 排除私有模块（这些将作为源文件复制，不打包进 PYZ）
private_module = ['core', 'config_client', 'config_server', 'LLM']

filtered = []
for name, src, type in a_1.pure:
    if not any(name == m or name.startswith(m + '.') for m in private_module):
        filtered.append((name, src, type))
a_1.pure = filtered

# noarchive 会将私有模块也编译成 .pyc 放进 datas，排除掉以保持源码运行
filtered = []
for name, src, type in a_1.datas:
    is_private = any(
        name.startswith(m + '/') or name.startswith(m + '\\') or name in (m + '.py', m + '.pyc')
        for m in private_module
    )
    if not is_private:
        filtered.append((name, src, type))
a_1.datas = filtered

pyz_1 = PYZ(a_1.pure)

exe_1 = EXE(
    pyz_1,
    a_1.scripts,
    [],
    exclude_binaries=True,
    name='start_server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
    contents_directory='internal',
)

coll = COLLECT(
    exe_1,
    a_1.binaries,
    a_1.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='root',
)


# 复制额外所需的文件到根目录（exe 所在目录）
my_files = [
    'config_server.py',
    'config_client.py',
    'hot-server.txt',
    'hot-rule.txt',
    'readme.md',
    'LICENSE',
    'install_menu.py',
]
my_folders = []
dest_root = join('dist', basename(coll.name))

# 复制文件夹中的文件
for folder in my_folders:
    if not exists(folder):
        continue
    for dirpath, dirnames, filenames in walk(folder):
        for filename in filenames:
            src_file = join(dirpath, filename)
            if exists(src_file):
                my_files.append(src_file)

# 执行文件复制到 dist/root（不是 internal）
for file in my_files:
    if not exists(file):
        continue
    rel_path = file.replace('\\', '/') if '\\' in file else file
    dest_file = join(dest_root, rel_path)
    dest_folder = dirname(dest_file)
    makedirs(dest_folder, exist_ok=True)
    copyfile(file, dest_file)

# 不再创建软连接 — 根目录的 start_server.exe 直接访问同目录的 core/models/assets/LLM 等
# 打包完成后需手动将 dist/root/ 下的 start_server.exe 和 internal/ 复制到项目根目录
