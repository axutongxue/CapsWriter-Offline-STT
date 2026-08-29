# -*- coding: utf-8 -*-
"""
CapsWriter 右键菜单安装/卸载脚本
用 Python winreg 写入注册表确保中文编码正确
使用 HKCU 不需要管理员权限

注册两类菜单：
  1) 文件类型菜单：SystemFileAssociations 下对应扩展名 shell 子键 —— 右键单个音视频文件
     (Windows 行为：选中 N 个文件右键会启动 N 个进程，每进程 1 文件)
  2) 文件夹菜单：Directory\\shell 下 —— 右键一个文件夹
     (1 个进程收到目录路径，由 start_server.py 递归扫描其中音视频文件，
      适合 200~300 文件批量场景，无多进程竞态、无命令行长度限制)
"""

import sys
import os
import winreg
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

MENU_NAME = "CapsWriter 音视频转文字"  # 音视频转文字
SCRIPT_DIR = Path(__file__).resolve().parent
EXE_PATH = SCRIPT_DIR / "start_server.exe"

VIDEO_EXT = ["mp4", "mkv", "flv", "webm", "avi", "mov", "wmv", "mpeg", "mpg", "rmvb", "ts", "3gp"]
AUDIO_EXT = ["mp3", "wav", "flac", "ape", "aac", "m4a", "wma", "ogg"]
ALL_EXT = VIDEO_EXT + AUDIO_EXT

REG_ROOT = winreg.HKEY_CURRENT_USER
REG_BASE = r"Software\Classes\SystemFileAssociations"
# 文件夹右键菜单注册点：右键选中的文件夹本身
REG_DIR_BASE = r"Software\Classes\Directory\shell"
# 驱动器根目录右键菜单（C:\ 等盘符背景）注册点
REG_DRIVE_BASE = r"Software\Classes\Drive\shell"


def _delete_key_tree(root, path):
    """递归删除注册表键（含子键）。忽略不存在。"""
    try:
        # 先删 command 子键（如果有）
        try:
            winreg.DeleteKey(root, path + r"\command")
        except FileNotFoundError:
            pass
        winreg.DeleteKey(root, path)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _clean_all_capswriter_keys():
    """清理所有 CapsWriter 相关的注册表项（文件类型 + 文件夹 + 驱动器）"""
    cleaned = 0

    # 1. 文件类型菜单
    for ext in ALL_EXT:
        shell_path = rf"{REG_BASE}\.{ext}\shell"
        try:
            key = winreg.OpenKey(REG_ROOT, shell_path)
            names_to_delete = []
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                    if name.startswith("CapsWriter"):
                        names_to_delete.append(name)
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
            for name in names_to_delete:
                if _delete_key_tree(REG_ROOT, f"{shell_path}\\{name}"):
                    cleaned += 1
        except FileNotFoundError:
            pass

    # 2. 文件夹菜单
    if _delete_key_tree(REG_ROOT, rf"{REG_DIR_BASE}\{MENU_NAME}"):
        cleaned += 1

    # 3. 驱动器菜单
    if _delete_key_tree(REG_ROOT, rf"{REG_DRIVE_BASE}\{MENU_NAME}"):
        cleaned += 1

    return cleaned


def is_installed():
    """检查是否已安装（以文件夹菜单为标志，更可靠）"""
    try:
        with winreg.OpenKey(REG_ROOT, rf"{REG_DIR_BASE}\{MENU_NAME}"):
            return True
    except FileNotFoundError:
        return False


def _install_file_menu(exe_str):
    """安装文件类型右键菜单"""
    for ext in ALL_EXT:
        key_path = rf"{REG_BASE}\.{ext}\shell\{MENU_NAME}"
        try:
            key = winreg.CreateKey(REG_ROOT, key_path)
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MENU_NAME)
            winreg.CloseKey(key)

            cmd_path = key_path + r"\command"
            cmd_key = winreg.CreateKey(REG_ROOT, cmd_path)
            cmd_value = f'"{exe_str}" "%1" %*'
            winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd_value)
            winreg.CloseKey(cmd_key)
        except Exception as e:
            print(f"  Failed .{ext}: {e}")
            return False
    return True


def _install_dir_menu(exe_str, reg_base, label):
    """在指定根（Directory / Drive）下安装文件夹右键菜单"""
    key_path = rf"{reg_base}\{MENU_NAME}"
    try:
        key = winreg.CreateKey(REG_ROOT, key_path)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MENU_NAME)
        winreg.CloseKey(key)

        cmd_path = key_path + r"\command"
        cmd_key = winreg.CreateKey(REG_ROOT, cmd_path)
        # %1 对应被右键的目录/盘符路径，单进程入参
        cmd_value = f'"{exe_str}" "%1"'
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd_value)
        winreg.CloseKey(cmd_key)
    except Exception as e:
        print(f"  Failed {label} menu: {e}")
        return False
    return True


def install():
    """安装右键菜单（文件 + 文件夹 + 驱动器根目录）"""
    if not EXE_PATH.exists():
        print(f"Error: start_server.exe not found ({EXE_PATH})")
        return False

    exe_str = str(EXE_PATH)
    print(f"Installing context menu...")
    print(f"  Menu: {MENU_NAME}")
    print(f"  exe:  {exe_str}")

    print("  - 文件类型菜单 (右键单个音视频文件)...")
    if not _install_file_menu(exe_str):
        return False

    print("  - 文件夹菜单 (右键文件夹, 递归扫描其中的音视频文件)...")
    if not _install_dir_menu(exe_str, REG_DIR_BASE, "Directory"):
        return False

    print("  - 驱动器菜单 (右键盘符根目录)...")
    if not _install_dir_menu(exe_str, REG_DRIVE_BASE, "Drive"):
        return False

    print("Install complete!")
    print("用法：")
    print("  - 右键单个文件 → 转录该文件")
    print("  - 右键一个文件夹 → 递归转录该文件夹下所有音视频文件（适合 200~300 文件批量）")
    return True


def uninstall():
    """卸载右键菜单"""
    print("Uninstalling context menu...")
    cleaned = _clean_all_capswriter_keys()
    print(f"Uninstall complete! Cleaned {cleaned} entries.")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
    else:
        # 先清理旧的，再安装新的
        _clean_all_capswriter_keys()
        if is_installed():
            uninstall()
        else:
            install()