# -*- coding: utf-8 -*-
"""
CapsWriter 右键菜单安装/卸载脚本
用 Python winreg 写入注册表确保中文编码正确
使用 HKCU 不需要管理员权限
"""
import sys
import os
import winreg
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

MENU_NAME = "CapsWriter \u97f3\u89c6\u9891\u8f6c\u6587\u5b57"  # 音视频转文字
SCRIPT_DIR = Path(__file__).resolve().parent
EXE_PATH = SCRIPT_DIR / "start_server.exe"

VIDEO_EXT = ["mp4", "mkv", "flv", "webm", "avi", "mov", "wmv", "mpeg", "mpg", "rmvb", "ts", "3gp"]
AUDIO_EXT = ["mp3", "wav", "flac", "ape", "aac", "wma", "ogg"]
ALL_EXT = VIDEO_EXT + AUDIO_EXT

REG_ROOT = winreg.HKEY_CURRENT_USER
REG_BASE = r"Software\Classes\SystemFileAssociations"


def _clean_all_capswriter_keys():
    """清理所有 CapsWriter 相关的注册表项"""
    cleaned = 0
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
                full_path = f"{shell_path}\\{name}"
                try:
                    try:
                        winreg.DeleteKey(REG_ROOT, full_path + r"\command")
                    except FileNotFoundError:
                        pass
                    winreg.DeleteKey(REG_ROOT, full_path)
                    cleaned += 1
                except Exception:
                    pass
        except FileNotFoundError:
            pass
    return cleaned


def is_installed():
    """检查是否已安装"""
    try:
        key_path = rf"{REG_BASE}\.mp3\shell\{MENU_NAME}"
        with winreg.OpenKey(REG_ROOT, key_path):
            return True
    except FileNotFoundError:
        return False


def install():
    """安装右键菜单"""
    if not EXE_PATH.exists():
        print(f"Error: start_server.exe not found ({EXE_PATH})")
        return False

    exe_str = str(EXE_PATH)
    print(f"Installing context menu...")
    print(f"  Menu: {MENU_NAME}")
    print(f"  exe:  {exe_str}")

    for ext in ALL_EXT:
        key_path = rf"{REG_BASE}\.{ext}\shell\{MENU_NAME}"
        try:
            key = winreg.CreateKey(REG_ROOT, key_path)
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MENU_NAME)
            winreg.CloseKey(key)

            cmd_path = key_path + r"\command"
            cmd_key = winreg.CreateKey(REG_ROOT, cmd_path)
            cmd_value = f'"{exe_str}" "%1"'
            winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd_value)
            winreg.CloseKey(cmd_key)
        except Exception as e:
            print(f"  Failed .{ext}: {e}")
            return False

    print("Install complete!")
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
