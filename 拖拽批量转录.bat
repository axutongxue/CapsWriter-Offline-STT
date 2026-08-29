@echo off
REM ============================================================
REM  批量转录：把音视频文件拖到这个 bat 上即可单进程批量转录
REM  适用场景：跨文件夹多选 200~300 个零散文件
REM  执行逻辑：Windows 把所有拖入文件的路径作为参数传给 1 个 exe，
REM            由 start_server.py 的 expand_paths 展开为文件列表，
REM            batch_transcribe 依次转录。无多进程竞态。
REM  注意：拖入文件过多、路径总长超 ~32000 字符会启动失败，
REM        那种情况请改用「右键文件夹」方式。
REM ============================================================

setlocal
set "EXE=%~dp0start_server.exe"

if not exist "%EXE%" (
    echo 错误: 找不到 start_server.exe
    echo 预期路径: %EXE%
    pause
    exit /b 1
)

REM %* 是拖入的所有文件路径（已各自带引号）
start "" "%EXE%" %*
endlocal