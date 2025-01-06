import base64
import json
import os
import sys
import platform
import uuid
from pathlib import Path
import time
import re
import wave
import asyncio
import subprocess

import numpy as np
import websockets
import typer
import colorama
from util import srt_from_txt
from util.client_cosmic import console, Cosmic
from util.client_check_websocket import check_websocket
from config import ClientConfig as Config

async def transcribe_check(file: Path):
    if not await check_websocket():
        console.print('无法连接到服务端')
        sys.exit()

    if not file.exists():
        console.print(f'文件不存在：{file}')
        return False

async def transcribe_send(file: Path):
    websocket = Cosmic.websocket
    task_id = str(uuid.uuid1())
    console.print(f'\n任务标识：{task_id}')
    console.print(f'    处理文件：{file}')

    ffmpeg_cmd = [
        "ffmpeg",
        "-i", file,
        "-f", "f32le",
        "-ac", "1",
        "-ar", "16000",
        "-",
    ]
    process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    console.print(f'    正在提取音频', end='\r')
    data = process.stdout.read()
    audio_duration = len(data) / 4 / 16000
    console.print(f'    音频长度：{audio_duration:.2f}s')

    offset = 0
    while True:
        chunk_end = offset + 16000*4*60
        is_final = False if chunk_end < len(data) else True
        message = {
            'task_id': task_id,
            'seg_duration': Config.file_seg_duration,
            'seg_overlap': Config.file_seg_overlap,
            'is_final': is_final,
            'time_start': time.time(),
            'time_frame': time.time(),
            'source': 'file',
            'data': base64.b64encode(
                        data[offset: chunk_end]
                    ).decode('utf-8'),
        }
        offset = chunk_end
        progress = min(offset / 4 / 16000, audio_duration)
        await websocket.send(json.dumps(message))
        console.print(f'    发送进度：{progress:.2f}s', end='\r')
        if is_final:
            break

async def transcribe_recv(file: Path):
    websocket = Cosmic.websocket

    async for message in websocket:
        message = json.loads(message)
        console.print(f'    转录进度: {message["duration"]:.2f}s', end='\r')
        if message['is_final']:
            break

    text_merge = message['text']
    text_split = re.sub('[，。？]', '\n', text_merge)
    timestamps = message['timestamps']
    tokens = message['tokens']

    json_filename = Path(file).with_suffix(".json")
    txt_filename = Path(file).with_suffix(".txt")
    merge_filename = Path(file).with_suffix(".merge.txt")

    with open(merge_filename, "w", encoding="utf-8") as f:
        f.write(text_merge)
    with open(txt_filename, "w", encoding="utf-8") as f:
        f.write(text_split)
    with open(json_filename, "w", encoding="utf-8") as f:
        json.dump({'timestamps': timestamps, 'tokens': tokens}, f, ensure_ascii=False)
    
    srt_from_txt.one_task(txt_filename)

    if os.path.exists(json_filename):
        os.remove(json_filename)
    
    if os.path.exists(txt_filename):
        os.remove(txt_filename)
    
    if os.path.exists(merge_filename):
        os.rename(merge_filename, txt_filename)

    process_duration = message['time_complete'] - message['time_start']
    console.print(f'\033[K    处理耗时：{process_duration:.2f}s')
    console.print(f'    识别结果：\n[green]{message["text"]}')
    
    # 将结果复制到剪贴板
    subprocess.run("clip", universal_newlines=True, input=text_merge)
    console.print('识别结果已复制到剪贴板！')
    # 删除与 txt 文件同名的 srt 文件
    
    srt_filename = Path(file).with_suffix(".srt")
    if os.path.exists(srt_filename):
        os.remove(srt_filename)
        # console.print(f'已删除文件: {srt_filename}')
