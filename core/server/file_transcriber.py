# coding: utf-8
"""
服务端文件转录模块

在 Server 端直接完成文件转录，无需 Client 参与。
流程：FFmpeg 提取音频 → 按 seg_duration 切分 → 送入 queue_in → 从本地转录队列收结果 → 保存多格式输出

结果路由说明：
  - ws_send 在主进程 asyncio 循环中从 queue_out 取结果
  - 当 socket_id == 'local_transcribe' 时，ws_send 将结果路由到 _local_transcribe_queue
  - ServerFileTranscriber 从 _local_transcribe_queue 取结果
  - 避免了直接从 queue_out 取结果导致的竞态条件

输出格式由 ServerConfig 控制：
  - file_save_txt  : smart_split 后的切分文本（每行一句）
  - file_save_json : 原始字级 tokens + timestamps，供手动校正后重新生成 srt
  - file_save_srt  : srt 字幕（依赖 tokens/timestamps + smart_split 分行）
  - file_save_merge: 未切分的整段文本
"""

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Optional, Callable

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

    @staticmethod
    def smart_split(text: str, min_chars: int = 2) -> str:
        """
        智能分行：按标点切分，强标点（。？！.?!）必换行，弱标点（，,) 累积够字数才换行。
        英文标点需后跟空白或结尾才切分，避免误切 3.14 这类数字。
        从原始项目 ResultHandler.smart_split 搬迁，保持逻辑一致。
        """
        parts = re.split(r'([，。？]|[.,?!](?:\s+|$))', text)
        lines: List[str] = []
        buffer = ""

        strong_punct = {'。', '？', '.', '?', '!'}
        punct_chars = set('，。？,.?!')

        for part in parts:
            clean_part = part.strip()
            if clean_part and clean_part in punct_chars and len(clean_part) == 1:
                buffer += part
                is_strong = clean_part in strong_punct
                if is_strong or len(buffer) > min_chars:
                    lines.append(buffer)
                    buffer = ""
            else:
                buffer += part

        if buffer:
            lines.append(buffer)

        return "\n".join(lines)

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

        # 分段参数（从 Config 取，文件转录场景可适当加大 overlap 提升拼接质量）
        seg_duration = getattr(Config, 'file_seg_duration', 60)
        seg_overlap = getattr(Config, 'file_seg_overlap', 8)
        seg_threshold = seg_duration + seg_overlap * 2

        # 提示词上下文和语言从 Config 取（可在 config_server.py 配置或运行时覆盖）
        task_context = getattr(Config, 'context', '')
        task_language = getattr(Config, 'language', 'auto')

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
                    context=task_context,
                    language=task_language,
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
            context=task_context,
            language=task_language,
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

        # 热词后处理：音素纠错 + 规则替换 + token 同步
        # 即使没有热词库也安全（apply 内部会跳过）；任何异常都不阻断保存流程
        if final_result and (final_result.text_accu or final_result.text):
            try:
                hotword_mgr = getattr(self.app, 'hotword_manager', None)
                if hotword_mgr is not None:
                    corrected, matchs, _ = hotword_mgr.apply(final_result)
                    if matchs:
                        logger.info(
                            f"热词命中 {len(matchs)} 处: "
                            + ', '.join(f"「{o}」→「{h}」" for o, h, _ in matchs[:5])
                        )
            except Exception as e:
                logger.warning(f"热词后处理失败（不影响保存）：{e}")

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
        """
        保存转录结果到文件，按 Config.file_save_* 开关分别输出：
          - merge.txt : 未切分的整段文本
          - txt       : smart_split 智能分行后的文本（每行一句）
          - json      : 字级 tokens + timestamps，供手动校正后重新生成 srt
          - srt       : srt 字幕（依赖 tokens/timestamps + smart_split 分行对齐）

        Args:
            result: 识别结果
            use_text: True 表示 text_accu 为空，回退用 text
        """
        # 优先用 text_accu（时间戳去重拼接，适合字幕），为空才回退到 text
        text_accu = result.text_accu if (not use_text and result.text_accu) else result.text
        text_split = self.smart_split(text_accu)
        timestamps = result.timestamps or []
        tokens = result.tokens or []

        txt_filename = self.file.with_suffix('.txt')
        json_filename = self.file.with_suffix('.json')
        merge_filename = self.file.with_suffix('.merge.txt')
        srt_filename = self.file.with_suffix('.srt')

        logger.info(
            f"准备保存转录结果: use_text={use_text}, "
            f"text_len={len(text_accu)}, tokens={len(tokens)}, "
            f"save_srt={Config.file_save_srt}, save_txt={Config.file_save_txt}, "
            f"save_json={Config.file_save_json}, save_merge={Config.file_save_merge}"
        )

        # 1. merge.txt —— 未切分的整段文本
        if Config.file_save_merge:
            try:
                merge_filename.write_text(text_accu, encoding='utf-8')
                logger.debug(f"保存合并文本: {merge_filename}")
            except Exception as e:
                logger.warning(f"保存 merge.txt 失败: {e}")

        # 2. txt —— smart_split 后的分行文本
        if Config.file_save_txt:
            try:
                txt_filename.write_text(text_split, encoding='utf-8')
                logger.debug(f"保存切分文本: {txt_filename}")
            except Exception as e:
                logger.warning(f"保存 txt 失败: {e}")

        # 3. json —— 字级 tokens + timestamps
        if Config.file_save_json and tokens:
            try:
                with open(json_filename, 'w', encoding='utf-8') as f:
                    json.dump(
                        {'timestamps': timestamps, 'tokens': tokens},
                        f, ensure_ascii=False
                    )
                logger.debug(f"保存 JSON 结果: {json_filename}")
            except Exception as e:
                logger.warning(f"保存 json 失败: {e}")

        # 4. srt —— 依赖 tokens/timestamps + smart_split 分行对齐
        if Config.file_save_srt and tokens and timestamps:
            try:
                self._generate_srt(tokens, timestamps, text_split, srt_filename)
            except Exception as e:
                logger.warning(f"生成 SRT 字幕失败: {e}（如缺少 srt 依赖请 pip install srt）")
        elif Config.file_save_srt and not tokens:
            logger.warning(
                "file_save_srt 开启但 result.tokens 为空，跳过 SRT 生成。"
                "可能模型不支持返回 token 时间戳。"
            )

        logger.info(f"已保存转录结果: {self.file.name}")

    @staticmethod
    def _generate_srt(
        tokens: List[str],
        timestamps: List[float],
        text_split: str,
        srt_file: Path,
    ):
        """
        由 tokens/timestamps + smart_split 分行文本生成 SRT 字幕。
        复用 core/tools/srt_from_txt.generate_srt_file 的对齐逻辑。

        Args:
            tokens: 字级 token 列表（可能含 '@' 填充符）
            timestamps: 与 tokens 对应的时间戳（秒）
            text_split: smart_split 后的分行文本
            srt_file: 输出的 .srt 文件路径
        """
        from core.tools import srt_from_txt

        # 构建 words 列表（与原始项目 ResultHandler.save_results 一致）
        words = [
            {
                'word': token.replace('@', ''),
                'start': ts,
                'end': ts + 0.2,
            }
            for (ts, token) in zip(timestamps, tokens)
        ]
        # 让相邻 word 的 end 不超过下一个的 start，避免字幕重叠
        for i in range(len(words) - 1):
            words[i]['end'] = min(words[i]['end'], words[i + 1]['start'])

        text_lines = text_split.splitlines()
        srt_from_txt.generate_srt_file(words, text_lines, srt_file)
        logger.debug(f"生成 SRT 字幕: {srt_file}")
