@echo off
setlocal enabledelayedexpansion

set "menu_name=CapsWriter 音视频转文字"
set "exe_path=%~dp0start_server.exe"

if not exist "%exe_path%" (
    echo 错误: 找不到 start_server.exe
    pause
    exit /b 1
)

set "video_ext=mp4 mkv flv webm avi mov wmv mpeg mpg rmvb ts 3gp"
set "audio_ext=mp3 wav flac ape aac wma ogg"

reg query "HKCU\Software\Classes\SystemFileAssociations\.mp4\shell\%menu_name%" >nul 2>nul
if %errorlevel%==0 (
    echo.
    echo   正在卸载右键菜单...
    for %%i in (%video_ext%) do (
        reg delete "HKCU\Software\Classes\SystemFileAssociations\.%%i\shell\%menu_name%" /f >nul 2>nul
    )
    for %%i in (%audio_ext%) do (
        reg delete "HKCU\Software\Classes\SystemFileAssociations\.%%i\shell\%menu_name%" /f >nul 2>nul
    )
    echo.
    echo   卸载完成！
) else (
    echo.
    echo   正在安装右键菜单...
    for %%i in (%video_ext%) do (
        reg add "HKCU\Software\Classes\SystemFileAssociations\.%%i\shell\%menu_name%" /d "%menu_name%" /f >nul 2>nul
        reg add "HKCU\Software\Classes\SystemFileAssociations\.%%i\shell\%menu_name%\command" /d ""%exe_path%" "%%1"" /f >nul 2>nul
    )
    for %%i in (%audio_ext%) do (
        reg add "HKCU\Software\Classes\SystemFileAssociations\.%%i\shell\%menu_name%" /d "%menu_name%" /f >nul 2>nul
        reg add "HKCU\Software\Classes\SystemFileAssociations\.%%i\shell\%menu_name%\command" /d ""%exe_path%" "%%1"" /f >nul 2>nul
    )
    echo.
    echo   安装完成！右键点击音视频文件即可使用。
)

echo.
pause
