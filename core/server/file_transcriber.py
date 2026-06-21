# coding: utf-8
"""
服务端文件转录模块

在 Server 端直接完成文件转录，无需 Client 参与。
流程：FFmpeg 提取音频 → 按 seg_duration 切分 → 送入 queue_in → 从本地转录队列收结果 → 保存 txt

结果路由说明：
  - ws_send 在主进程 asyncio 循环中从 queue_out 取结果
  - 当 socket_id == 'local_transcribe' 时，ws_send 将结果路由到 _local_transcribe_queue
  - ServerFileTranscriber 从 _local_transcribe_queue 取结果
  - 避免了直接从 queue_out 取结果导致的竞态条件
"""

import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional, Callable

from config_server import ServerConfig as Config
from core.constants import AudioFormat
from core.server.schema import Task, Result
from . import logger


# 本地转录结果队列的固定 socket_id
LOCAL_SOCKET_ID = 'local_transcribe'


class ServerFileTranscriber:
    """
    服务端文件转录器

    直接使用 Server 内部的 multiprocessing 队列与子进程通信，
    完成音频文件的转录并保存结果。
    """

    def __init__(self, app, file: Path):
        """
        Args:
            app: CapsWriterServer 实例
            file: 要转录的文件路径
        """
        self.app = app
        self.file = file
        self.task_id = str(uuid.uuid1())
        self._audio_duration: float = 0.0
        self._progress_callback: Optional[Callable] = None

    def set_progress_callback(self, callback: Callable):
        """设置进度回调，回调参数为 (processed_seconds, total_seconds)"""
        self._progress_callback = callback

    def _check_environment(self) -> bool:
        """检查 FFmpeg 环境"""
        if shutil.which('ffmpeg') is None:
            logger.error("未检测到 FFmpeg，无法进行文件转录")
            return False
        return True

    def _get_audio_duration(self) -> float:
        """通过 ffprobe 获取音频时长"""
        ffprobe_path = shutil.which('ffprobe')
        if ffprobe_path is None:
            return 0.0
        cmd = [
            ffprobe_path, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(self.file)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"ffprobe 获取时长失败: {e}")
        return 0.0

    def transcribe(self) -> bool:
        """
        执行完整的文件转录流程（同步方法）

        Returns:
            bool: 是否成功完成转录
        """
        logger.info(f"ServerFileTranscriber.transcribe() called for: {self.file}")
        
        if not self._check_environment():
            logger.error(f"环境检查失败: ffmpeg not found in PATH")
            return False

        if not self.file.exists():
            logger.error(f"文件不存在: {self.file}")
            return False

        logger.info(f"环境检查通过，文件存在，正在获取音频时长...")

        # 获取音频时长
        self._audio_duration = self._get_audio_duration()
        logger.info(f"音频时长获取完成: {self._audio_duration:.2f}s, 任务ID: {self.task_id}")

        state = self.app.state

        # 确保本地转录的 socket_id 在共享列表中，防止 TaskHandler 跳过
        if LOCAL_SOCKET_ID not in state.sockets_id:
            state.sockets_id.append(LOCAL_SOCKET_ID)

        # FFmpeg 提取音频
        ffmpeg_cmd = [
            "ffmpeg", "-i", str(self.file),
            "-f", "f32le", "-ac", "1", "-ar", "16000", "-"
        ]

        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            logger.info(f"FFmpeg 进程已启动, PID: {process.pid}")
        except Exception as e:
            logger.error(f"FFmpeg 启动失败: {e}")
            return False

        # 分段参数
        seg_duration = getattr(Config, 'file_seg_duration', 60)
        seg_overlap = getattr(Config, 'file_seg_overlap', 4)
        seg_threshold = seg_duration + seg_overlap * 2

        segment_bytes = AudioFormat.seconds_to_bytes(seg_duration + seg_overlap)
        stride_bytes = AudioFormat.seconds_to_bytes(seg_duration)

        buffer = b''
        offset = 0.0
        bytes_read = 0
        seg_count = 0

        # 读取并分段提交
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            buffer += chunk
            bytes_read += len(chunk)

            # 达到分段阈值时提交
            while len(buffer) >= segment_bytes:
                seg_count += 1
                segment_data = buffer[:segment_bytes]
                buffer = buffer[stride_bytes:]

                task = Task(
                    type='file',
                    data=segment_data,
                    offset=offset,
                    task_id=self.task_id,
                    socket_id=LOCAL_SOCKET_ID,
                    overlap=seg_overlap,
                    is_final=False,
                    time_start=time.time(),
                    time_submit=time.time(),
                    context='',
                    language='auto',
                )
                offset += seg_duration
                state.queue_in.put(task)

                # 进度回调（基于已发送的音频时长）
                if self._progress_callback:
                    processed = bytes_read / AudioFormat.BYTES_PER_SECOND
                    self._progress_callback(processed, self._audio_duration)

        # 提交剩余数据作为最终片段
        logger.info(f"FFmpeg 读取完成, 共读取 {bytes_read} 字节, 提交 {seg_count} 个分段")
        task = Task(
            type='file',
            data=buffer if buffer else b'',
            offset=offset,
            task_id=self.task_id,
            socket_id=LOCAL_SOCKET_ID,
            overlap=seg_overlap if buffer else 0,
            is_final=True,
            time_start=time.time(),
            time_submit=time.time(),
            context='',
            language='auto',
        )
        state.queue_in.put(task)

        process.wait()
        logger.info("音频数据发送完成，等待识别结果...")

        # 从本地转录队列收集结果（ws_send 负责路由到这里）
        from .connection.ws_send import get_local_transcribe_queue
        local_queue = get_local_transcribe_queue()
        logger.info(f"开始从 local_queue 等待结果, task_id={self.task_id}, queue_empty={local_queue.empty()}")

        final_result = None
        while True:
            try:
                logger.info(f"正在 local_queue.get() 等待, task_id={self.task_id}...")
                result = local_queue.get(timeout=120)  # 最多等 120 秒
                logger.info(f"从 local_queue 取到结果: task_id={result.task_id}, is_final={result.is_final}, socket_id={result.socket_id}")

                # 过滤：只关注当前 task_id 的结果
                if result.task_id != self.task_id:
                    # 不是当前任务的结果，放回队列
                    local_queue.put(result)
                    logger.info(f"结果不属于当前任务，放回队列: result_task_id={result.task_id}, my_task_id={self.task_id}")
                    continue

                if result.is_final:
                    final_result = result
                    break
                else:
                    # 非最终结果，更新进度
                    if self._progress_callback and self._audio_duration > 0:
                        self._progress_callback(result.duration, self._audio_duration)

            except Exception as e:
                logger.error(f"等待识别结果超时或出错: {e}")
                return False

        # 保存结果
        if final_result and final_result.text_accu:
            self._save_result(final_result)
            logger.info(f"转录完成: {self.file}, 时长: {final_result.duration:.2f}s")
            return True
        elif final_result and final_result.text:
            # 回退到 text
            self._save_result(final_result, use_text=True)
            logger.info(f"转录完成(text回退): {self.file}, 时长: {final_result.duration:.2f}s")
            return True
        else:
            logger.warning(f"转录结果为空: {self.file}")
            return False

    def _save_result(self, result: Result, use_text: bool = False):
        """保存转录结果为 txt 文件"""
        txt_filename = self.file.with_suffix('.txt')
        content = result.text if use_text else result.text_accu
        
        logger.info(f"准备保存转录结果: use_text={use_text}, content_len={len(content)}, path={txt_filename}")

        with open(txt_filename, 'w', encoding='utf-8') as f:
            f.write(content)

        logger.info(f"已保存转录结果: {txt_filename}")
