# coding: utf-8
"""
WebSocket 管理器 (SocketManager)

负责维护 ASR 服务器的异步通讯层，包括 WebSocket Server 的生命周期管理、
心跳监控、数据发送任务的编排。
"""

import asyncio
import functools
import websockets
from config_server import ServerConfig as Config
from .ws_recv import ws_recv
from .ws_send import ws_send
from .. import logger # Server module logger


class SocketManager:
    """
    WebSocket 网络管理器
    
    负责拉起并维护 WebSocket Server 以及识别结果的异步发送任务。
    """
    def __init__(self, app):
        self.app = app
        self._is_running = False
        self._server = None  # websockets.serve 返回的 server 对象

    async def start(self):
        """
        启动 WebSocket 网络服务

        端口抢占已在 CapsWriterServer.start() 用 _try_bind_port() 完成，
        这里不再重复检测，直接拉起 websockets.serve。
        极端竞态下仍可能 bind 失败（_try_bind_port 与 serve 之间又有实例先 bind），
        抛 OSError 由上层 start() 兜住转 client，不在此处崩溃。
        """
        if self._is_running: return

        self._is_running = True

        loop = self.app.loop

        # 1. 优化守护线程执行器 (防止阻塞事件循环)
        from core.tools.daemon_executor import SimpleDaemonExecutor
        loop.set_default_executor(SimpleDaemonExecutor())

        # 2. 准备连接处理器 (注入 app 引用)
        handler = functools.partial(ws_recv, app=self.app)

        # 3. 启动服务
        logger.info(f"正在拉起 WebSocket 服务 (监听: {Config.addr}:{Config.port})")

        # 释放 app._try_bind_port 占住的占位 listening socket，让 websockets.serve 立即
        # 重新 bind。释放与 serve 之间是毫秒级窗口，远小于进程间启动间隔，竞态可忽略。
        self.app._release_port_holder()

        async with websockets.serve(
            handler,
            Config.addr,
            Config.port,
            subprotocols=["binary"],
            max_size=None
        ) as server:
            self._server = server  # 保存 server 引用，用于外部关闭

            # 4. 进入识别结果发送循环 (作为主阻塞任务)
            logger.info("WebSocket 发送协程已就绪")
            await ws_send(self.app)

        self._is_running = False
        logger.info("SocketManager: WebSocket 服务已退出")

    def stop(self):
        """停止 WebSocket 网络服务"""
        # 主动关闭 WebSocket 服务器，让 ws_send 的 await 尽快返回
        if self._server:
            self._server.close()
        self._is_running = False
