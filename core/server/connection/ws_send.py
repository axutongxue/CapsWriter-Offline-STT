import json
import asyncio
from multiprocessing import Queue
from typing import Optional

from ..state import console
from ..schema import Result
from core.protocol import RecognitionMessage
from core.tools.asyncio_to_thread import to_thread
from .. import logger


# 本地转录结果队列（用于 Server 端直接转录文件时接收结果）
_local_transcribe_queue: Optional[Queue] = None


def get_local_transcribe_queue() -> Queue:
    """获取本地转录结果队列（懒初始化）"""
    global _local_transcribe_queue
    if _local_transcribe_queue is None:
        _local_transcribe_queue = Queue()
    return _local_transcribe_queue


async def ws_send(app):

    state = app.state
    queue_out = state.queue_out
    sockets = state.sockets

    logger.info("WebSocket 发送任务已启动")

    while True:
        try:
            # 获取识别结果（从多进程队列）
            result: Result = await to_thread(queue_out.get)

            # 得到退出的通知
            if result is None:
                logger.info("收到退出通知，停止发送任务")
                return

            # 调试：记录所有从 queue_out 取到的结果
            logger.info(f"[ws_send] 取到结果: type={type(result).__name__}, socket_id={getattr(result, 'socket_id', 'N/A')}, task_id={getattr(result, 'task_id', 'N/A')}, is_final={getattr(result, 'is_final', 'N/A')}")

            # 如果取到的是 True（模型加载信号），跳过
            if result is True:
                logger.info("[ws_send] 取到模型加载信号(True)，跳过")
                continue

            # 本地转录结果：路由到专门的队列，不通过 WebSocket 发送
            if result.socket_id == 'local_transcribe':
                local_queue = get_local_transcribe_queue()
                local_queue.put(result)
                logger.info(f"本地转录结果已路由, 任务ID: {result.task_id}, 进度: {result.duration:.2f}s")
                continue

            # 1. 将内部 Result 转换为标准的协议消息对象
            msg = RecognitionMessage(
                task_id=result.task_id,
                is_final=result.is_final,
                duration=result.duration,
                time_start=result.time_start,
                time_submit=result.time_submit,
                time_complete=result.time_complete,
                text=result.text,
                text_accu=result.text_accu,
                tokens=result.tokens,
                timestamps=result.timestamps
            )

            # 获得 socket
            websocket = next(
                (ws for ws in sockets.values() if str(ws.id) == result.socket_id),
                None,
            )

            if not websocket:
                logger.warning(f"客户端 {result.socket_id} 不存在，跳过发送结果，任务ID: {result.task_id}")
                continue

            # 发送消息
            await websocket.send(msg.to_json())
            logger.debug(f"发送识别结果，任务ID: {result.task_id}, 文本长度: {len(result.text)}")

            if result.type == 'mic':
                logger.info(f"麦克风识别结果: {result.text}")
            elif result.type == 'file':
                console.print(f'    转录进度：{result.duration:.2f}s', end='\r')
                logger.debug(f"文件转录进度: {result.duration:.2f}s")
                if result.is_final:
                    console.print('\n    [green]转录完成')
                    logger.info(f"文件转录完成，任务ID: {result.task_id}, 总时长: {result.duration:.2f}s")

        except Exception as e:
            logger.error(f"发送结果时发生错误: {e}", exc_info=True)
            print(e)
