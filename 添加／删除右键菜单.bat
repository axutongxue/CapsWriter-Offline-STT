@echo off
chcp 65001 >nul
cd /d "%~dp0"
python install_menu.py
if %errorlevel% neq 0 (
    echo.
    echo ⚠ Python 未安装或 install_menu.py 执行失败
    echo 请确保已安装 Python 3，或在终端中手动运行:
    echo     python install_menu.py
)
pause