import json
import asyncio
import threading
from multiprocessing import Queue
from typing import Dict, Optional

from ..state import console
from ..schema import Result
from core.protocol import RecognitionMessage
from core.tools.asyncio_to_thread import to_thread
from .. import logger


# 本地转录结果队列：按 task_id 维护独立队列，避免多 transcriber 共享队列时的
# task_id 不匹配 put-back / ping-pong / 残留泄漏问题。ws_send 按 task_id 路由。
_local_queues: Dict[str, Queue] = {}
_local_queues_lock = threading.Lock()


def register_local_queue(task_id: str) -> Queue:
    """为指定 task_id 注册一个独立的结果队列，返回该队列。"""
    q: Queue = Queue()
    with _local_queues_lock:
        _local_queues[task_id] = q
    return q


def unregister_local_queue(task_id: str) -> None:
    """注销（删除）指定 task_id 的结果队列。"""
    with _local_queues_lock:
        _local_queues.pop(task_id, None)


def route_local_result(result) -> bool:
    """将本地转录结果路由到对应 task_id 的队列。返回是否成功路由。"""
    with _local_queues_lock:
        q = _local_queues.get(result.task_id)
    if q is None:
        return False
    q.put(result)
    return True


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

            # 本地转录结果：按 task_id 路由到专门队列，不通过 WebSocket 发送。
            # 若对应 task_id 无队列（如 transcriber 已超时退出或被注销），丢弃并告警。
            if result.socket_id == 'local_transcribe':
                routed = route_local_result(result)
                if routed:
                    logger.info(f"本地转录结果已路由, 任务ID: {result.task_id}, 进度: {result.duration:.2f}s")
                else:
                    logger.warning(f"本地转录结果无归属队列（task_id={result.task_id}），丢弃")
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
