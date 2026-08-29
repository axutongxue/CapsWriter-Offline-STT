# coding: utf-8
"""
WebSocket 接收处理模块

处理客户端发送的音频数据，进行分段和缓冲，提交到识别队列。

批量转录：通过全局队列 + 单工作者线程，确保多文件依次转录，
避免 ASR 引擎内部状态被并发访问破坏。
批次进度：入队时即把每个文件的 [idx/total] 计算好放入队列项，
worker 无状态渲染，避免全局计数器永不重置导致的跨批次显示错误。
"""

import json
import queue as _queue_module
import threading
import time
from base64 import b64decode
from pathlib import Path

import websockets

from ..state import console
from ..schema import Task
from config_server import ServerConfig as Config
from core.protocol import AudioMessage
from core.constants import AudioFormat
from core.tools.my_status import Status
from .. import logger


# 麦克风接收状态指示器
status_mic = Status('正在接收音频', spinner='point')

# ── 全局批量转录队列 ──────────────────────────────────
# 所有 transcribe_file 消息都进同一个队列，由一个工作者线程依次处理。
# 队列项是 (path, batch_id, idx, total) 元组：入队时就把显示所需的 idx/total 算好，
# worker 完全无状态地渲染，避免「全局计数器永不重置导致第二批显示 [4/5]」的 bug。
_BATCH_QUEUE: _queue_module.Queue = _queue_module.Queue()
_BATCH_WORKER_STARTED = False
_BATCH_WORKER_THREAD: threading.Thread = None
_BATCH_LOCK = threading.Lock()
_BATCH_NEXT_BATCH_ID = 0        # 下一个批次 id（每次 batch_transcribe 调用自增）
_BATCH_ACTIVE_BATCH_ID = -1     # worker 当前正在处理的批次 id
_BATCH_ACTIVE_TOTAL = 0         # 该批次总数
_BATCH_ACTIVE_DONE = 0          # 该批次已完成数


def _ensure_batch_worker(app):
    """
    启动全局批量转录工作者线程（仅一次）。
    从队列中依次取出 (path, batch_id, idx, total) 执行转录。
    """
    global _BATCH_WORKER_STARTED, _BATCH_WORKER_THREAD
    with _BATCH_LOCK:
        if _BATCH_WORKER_STARTED:
            return
        _BATCH_WORKER_STARTED = True

    def _worker():
        from core.server.file_transcriber import ServerFileTranscriber

        global _BATCH_ACTIVE_BATCH_ID, _BATCH_ACTIVE_TOTAL, _BATCH_ACTIVE_DONE

        while True:
            item = _BATCH_QUEUE.get()
            if item is None:          # sentinel → 退出
                break
            path, batch_id, idx, total = item

            # ASR 子进程已崩溃检测：若上一文件检测到子进程死亡并标记，则停止处理
            # 整个批次剩余文件（避免逐文件傻等 60~600s × N 个的死循环）。
            if getattr(app, '_asr_process_dead', False):
                logger.error(
                    f"ASR 子进程已崩溃，停止批次 (batch={batch_id}) 剩余文件，"
                    f"跳过 [{idx}/{total}]: {path}"
                )
                try:
                    app.floating_window.show(
                        f"⚠️ ASR 子进程崩溃，已停止。请重启 CapsWriter Server。",
                        position="bottom_right",
                    )
                except Exception:
                    pass
                # 继续把队列里本批次剩余项排空，避免遗留
                continue

            # 进入一个新批次时重置活跃计数（用于批次结束判定）
            if batch_id != _BATCH_ACTIVE_BATCH_ID:
                _BATCH_ACTIVE_BATCH_ID = batch_id
                _BATCH_ACTIVE_TOTAL = total
                _BATCH_ACTIVE_DONE = 0

            # UI：浮窗 + 托盘 tooltip
            try:
                app.floating_window.show("正在转录中…", position="bottom_right")
                app.floating_window.show(
                    f"转录 [{idx}/{total}] {path.name}…",
                    position="bottom_right",
                )
                if hasattr(app.tray_manager, 'set_transcribe_progress'):
                    app.tray_manager.set_transcribe_progress(
                        path.name, 0, 0, total, idx,
                    )
            except Exception as e:
                logger.warning(f"更新转录 UI 失败（不影响转录）：{e}")

            # 执行转录
            transcriber = ServerFileTranscriber(app, path)
            transcriber.set_progress_callback(
                lambda p, t, fn=path.name, _idx=idx, _total=total:
                    app.tray_manager.set_transcribe_progress(fn, p, t, _total, _idx)
            )
            success = transcriber.transcribe()

            # ASR 子进程重启成功后，transcriber 标记 _needs_requeue 请求重新入队重转
            if getattr(transcriber, '_needs_requeue', False):
                logger.info(f"ASR 已自动重启，本文件重新入队重转: {path}")
                _BATCH_QUEUE.put((path, batch_id, idx, total))
                # 不计入完成数；重新入队的文件本轮真正转完后才 += 1
                continue

            # ASR 子进程彻底崩溃（重启失败）→ 停止批次剩余文件
            if getattr(app, '_asr_process_dead', False):
                logger.error(
                    f"ASR 子进程崩溃且重启失败，停止批次 (batch={batch_id})，"
                    f"跳过 [{idx}/{total}]: {path} 及后续文件"
                )
                try:
                    app.floating_window.show(
                        f"⚠️ ASR 子进程崩溃且重启失败，已停止。请重启 CapsWriter Server。",
                        position="bottom_right",
                    )
                except Exception:
                    pass
                continue

            with _BATCH_LOCK:
                _BATCH_ACTIVE_DONE += 1
                batch_finished = (_BATCH_ACTIVE_DONE >= _BATCH_ACTIVE_TOTAL)

            if success:
                logger.info(f"批量转录成功 [{idx}/{total}] (batch={batch_id}): {path}")
            else:
                logger.error(f"批量转录失败 [{idx}/{total}] (batch={batch_id}): {path}")

            # 当前批次已全部完成 → 隐藏浮窗/tooltip
            if batch_finished:
                try:
                    app.floating_window.close()
                    if hasattr(app.tray_manager, 'clear_transcribe_progress'):
                        app.tray_manager.clear_transcribe_progress()
                except Exception as e:
                    logger.warning(f"清理转录 UI 失败：{e}")

        # sentinel received → 线程退出（服务关闭时）
        try:
            app.floating_window.close()
            if hasattr(app.tray_manager, 'clear_transcribe_progress'):
                app.tray_manager.clear_transcribe_progress()
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    _BATCH_WORKER_THREAD = t
    t.start()


def batch_transcribe(app, file_paths):
    """
    将一组文件/目录加入批量转录队列。
    可在任意 WebSocket 连接中多次调用（来自不同进程实例的文件都会进同一队列）。
    目录会被递归展开为其中的音视频文件（来自右键文件夹 / 远程目录请求）。
    会跳过已转录文件（同名 .txt 已存在），由 Config.skip_existing 控制。
    每次调用视为一个独立批次：分配新的 batch_id，idx 从 1..N 重新计数，
    total 等于本次入队文件数。worker 按元组里的 idx/total 显示，正确反映本批次进度。
    """
    from ..path_utils import expand_paths
    paths = expand_paths(file_paths)
    if not paths:
        logger.warning(f"批量转录跳过空/不存在: {file_paths}")
        return

    # 跳过已转录文件（同名 .txt 已存在）
    if getattr(Config, 'skip_existing', True):
        kept = []
        skipped = 0
        for p in paths:
            if p.with_suffix('.txt').exists():
                skipped += 1
                continue
            kept.append(p)
        if skipped:
            logger.info(f"批量转录跳过 {skipped} 个已转录文件（同名 .txt 已存在）")
        paths = kept
        if not paths:
            logger.info("批量转录：所有文件均已转录，无需处理")
            return

    _ensure_batch_worker(app)

    with _BATCH_LOCK:
        global _BATCH_NEXT_BATCH_ID
        _BATCH_NEXT_BATCH_ID += 1
        batch_id = _BATCH_NEXT_BATCH_ID
    total = len(paths)

    for i, path in enumerate(paths, start=1):
        logger.info(f"批量转录入队 (batch={batch_id}, [{i}/{total}]): {path}")
        _BATCH_QUEUE.put((path, batch_id, i, total))


def stop_batch_worker():
    """向批量队列投递 sentinel 以优雅停止 worker 线程。"""
    try:
        _BATCH_QUEUE.put(None)
    except Exception:
        pass


class AudioCache:
    """
    音频缓冲区

    用于缓存接收到的音频数据，直到达到分段阈值后提交处理。
    """
    def __init__(self):
        self.chunks: bytes = b''    # 音频数据缓冲
        self.offset: float = 0.0    # 当前偏移时间（秒）
        self.byte_count: int = 0    # 累计接收字节数

    @property
    def duration(self) -> float:
        """缓冲区音频时长（秒）"""
        return AudioFormat.bytes_to_seconds(len(self.chunks))

    @property
    def total_duration(self) -> float:
        """累计接收的音频总时长（秒）"""
        return AudioFormat.bytes_to_seconds(self.byte_count)

    def reset(self) -> None:
        """重置缓冲区"""
        self.chunks = b''
        self.offset = 0.0
        self.byte_count = 0


async def message_handler(websocket, msg: AudioMessage, cache: AudioCache, app) -> None:
    """
    处理客户端发送的音频消息

    根据消息中的分段参数，将音频数据分段后提交到识别队列。
    """
    queue_in = app.state.queue_in

    global status_mic
    is_start = not bool(cache.chunks)
    socket_id = str(websocket.id)

    # 麦克风首次消息 → GPU 加速
    if is_start and msg.source == 'mic' and Config.gpu_boost_enabled:
        queue_in.put(Task(
            type='cmd',
            task_id='gpu_boost',
            data=b'', offset=0, overlap=0,
            socket_id=socket_id, is_final=False,
            time_start=0, time_submit=0,
            command='gpu_boost'
        ))

    # 从消息中获取分段参数
    seg_threshold = msg.seg_duration + msg.seg_overlap * 2

    try:
        # base64 解码音频数据（float32, 16kHz, mono）
        data = b64decode(msg.data)
        cache.chunks += data
        cache.byte_count += len(data)

        if not msg.is_final:
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.start()
            if msg.source == 'file' and is_start:
                console.print('正在接收音频文件...')
                logger.info(f"开始接收音频文件，任务ID: {msg.task_id}")

            # 若缓冲已达到分段阈值，将片段作为任务提交
            segment_bytes = AudioFormat.seconds_to_bytes(msg.seg_duration + msg.seg_overlap)
            stride_bytes = AudioFormat.seconds_to_bytes(msg.seg_duration)

            while cache.duration >= seg_threshold:
                segment_data = cache.chunks[:segment_bytes]
                cache.chunks = cache.chunks[stride_bytes:]

                task = Task(
                    type=msg.source,
                    data=segment_data,
                    offset=cache.offset,
                    task_id=msg.task_id,
                    socket_id=socket_id,
                    overlap=msg.seg_overlap,
                    is_final=False,
                    time_start=msg.time_start,
                    time_submit=time.time(),
                    context=msg.context,
                    language=msg.language,
                )
                cache.offset += msg.seg_duration
                queue_in.put(task)
                logger.debug(
                    f"提交音频片段，任务ID: {msg.task_id}, "
                    f"偏移: {cache.offset}s, 缓冲区: {len(cache.chunks)} bytes"
                )

        else:  # is_final
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.stop()
            elif msg.source == 'file':
                print(f'音频文件接收完毕，时长 {cache.total_duration:.2f}s')
                logger.info(f"音频文件接收完毕，任务ID: {msg.task_id}, 时长: {cache.total_duration:.2f}s")

            # 提交最终片段
            task = Task(
                type=msg.source,
                data=cache.chunks,
                offset=cache.offset,
                task_id=msg.task_id,
                socket_id=socket_id,
                overlap=msg.seg_overlap,
                is_final=True,
                time_start=msg.time_start,
                time_submit=time.time(),
                context=msg.context,
                language=msg.language,
            )
            queue_in.put(task)
            logger.debug(f"提交最终片段，任务ID: {msg.task_id}, 数据大小: {len(cache.chunks)} bytes")

            # 重置缓冲区
            cache.reset()

    except Exception as e:
        logger.error(f"音频数据处理错误，任务ID: {msg.task_id}: {e}", exc_info=True)
        raise


async def ws_recv(websocket, app) -> None:
    """
    WebSocket 接收主函数

    处理单个客户端连接，接收音频数据并分发处理。
    """
    global status_mic

    # 登记 socket 到连接池
    state = app.state
    sockets = state.sockets
    sockets_id = state.sockets_id
    socket_id = str(websocket.id)
    sockets[socket_id] = websocket
    sockets_id.append(socket_id)
    remote = websocket.remote_address
    console.print(f'[bold green]客户端已连接: {remote[0]}:{remote[1]}[/bold green]\n')
    logger.info(f"新客户端连接: {websocket}, ID: {socket_id}")

    # 创建音频缓冲区
    cache = AudioCache()

    # 接收并处理消息
    try:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)

                # 处理"transcribe_file"特殊消息（来自第二实例的文件转录请求）
                if data.get('type') == 'transcribe_file':
                    file_path = data.get('path', '')
                    if file_path:
                        logger.info(f"收到远程转录请求: {file_path}")
                        # 直接入全局队列，由单工作者线程依次处理（path 也可以是目录，
                        # batch_transcribe 会递归展开）
                        batch_transcribe(app, [file_path])
                    continue

                # 处理"transcribe_files"批量消息（来自第二实例的批量文件/目录请求）
                # 支持混合文件与目录，全部并入同一批次，UI 显示 [1/N]..[N/N]
                if data.get('type') == 'transcribe_files':
                    paths = data.get('paths', []) or []
                    if paths:
                        logger.info(f"收到远程批量转录请求: {len(paths)} 项")
                        batch_transcribe(app, paths)
                    continue

                msg = AudioMessage.from_dict(data)
                # 处理音频数据
                await message_handler(websocket, msg, cache, app)
            except Exception as e:
                logger.error(f"消息解析失败: {str(e)}")
                continue

        logger.info(f"客户端正常关闭连接: {socket_id}")

    except websockets.ConnectionClosed:
        console.print("ConnectionClosed...")
        logger.warning(f"客户端连接已关闭: {socket_id}")
    except websockets.InvalidState:
        console.print("InvalidState...")
        logger.error(f"WebSocket 状态异常: {socket_id}")
    except Exception as e:
        console.print("Exception:", e)
        logger.error(f"WebSocket 接收异常，客户端ID {socket_id}: {e}", exc_info=True)
    finally:
        # 清理资源
        status_mic.stop()
        status_mic.on = False
        sockets.pop(socket_id, None)
        if socket_id in sockets_id:
            sockets_id.remove(socket_id)

        console.print(f'[bold red]客户端已断开: {remote[0]}:{remote[1]}[/bold red]\n')

        # 注意：session 清理由 TaskHandler 在子进程中定期执行
        # （通过检查 sockets_id 判断客户端是否已断开）
        logger.debug(f"客户端资源已清理: {socket_id}")