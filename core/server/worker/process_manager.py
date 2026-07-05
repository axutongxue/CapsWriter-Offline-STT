# coding: utf-8
"""
识别子进程管理器 (ProcessManager)

负责维护单机识别进程的生命周期，包括启动、模型加载监控、异常退出捕获。
"""
from __future__ import annotations
import sys
import os
import queue
from multiprocessing import Process, Manager
from typing import TYPE_CHECKING
from ..state import console
from . import start_worker
from .check_model import check_model
from . import logger
if TYPE_CHECKING:
    from ..app import CapsWriterServer


class ProcessManager:
    """
    识别子进程管理器
    
    由 CapsWriterServer 调用，专注于进程层级的控制。
    """
    def __init__(self, app: CapsWriterServer):
        self._process = None
        self.app = app
        self.is_alive = False
        self._restart_count = 0           # 已自动重启次数
        self._max_restarts = 3           # 单个 server 生命周期内最多自动重启次数

    def start(self):
        """
        启动识别子进程并等待模型加载完成

        Returns:
            Process: 启动成功的子进程对象
        """
        # 防连续触发
        if self.is_alive: return
        self.is_alive = True

        # 1. 前置检查
        check_model()

        # 2. 初始化共享资源（仅在首次启动时创建 Manager.list，重启时复用）
        state = self.app.state
        if state.sockets_id is None:
            state.sockets_id = Manager().list()

        # 获取标准输入文件描述符，用于 Windows 下的信号传递补丁
        # windowed 模式（console=False）下 sys.stdin 为 None，跳过
        try:
            stdin_fn = sys.stdin.fileno() if sys.stdin else None
        except (AttributeError, OSError):
            stdin_fn = None

        # 3. 创建并启动进程
        self._spawn_process(stdin_fn)

        # 存入状态以便其他模块引用
        state.recognize_process = self._process
        logger.info(f"识别子进程已拉起 (PID: {self._process.pid})")

        # 4. 等待模型加载完成 (轮询方式)
        self._wait_for_models()

        return self._process

    def _spawn_process(self, stdin_fn):
        """创建并启动子进程（私有，被 start / restart 调用）。"""
        state = self.app.state
        self._process = Process(
            target=start_worker,
            args=(state.queue_in,
                  state.queue_out,
                  state.sockets_id,
                  stdin_fn),
            daemon=True
        )
        self._process.start()

    def restart(self) -> bool:
        """
        在 ASR 子进程崩溃后自动重启一个新子进程并重新加载模型。

        Returns:
            bool: 是否重启成功（模型加载完成）
        """
        if self._restart_count >= self._max_restarts:
            logger.error(
                f"ASR 子进程已自动重启 {self._restart_count} 次仍崩溃，"
                f"达到上限 {self._max_restarts}，不再重启，请检查模型/环境后手动重启 Server"
            )
            return False

        self._restart_count += 1
        logger.warning(
            f"ASR 子进程崩溃，开始自动重启 (第 {self._restart_count}/{self._max_restarts} 次)..."
        )

        # 1. 终止旧子进程（若还活着）
        if self._process is not None and self._process.is_alive():
            try:
                self._process.terminate()
                self._process.join(timeout=2)
            except Exception as e:
                logger.debug(f"终止旧子进程失败: {e}")

        # 2. 清空 queue_in 残留任务（旧任务属于已死子进程，交给新子进程处理会乱）
        # 注意：queue_out 里可能有旧 result，ws_send 会处理；
        # 同时清掉主进程侧的 ASR 死亡标记，给新子进程一个干净起点
        try:
            import queue as _q
            state = self.app.state
            while True:
                try:
                    state.queue_in.get_nowait()
                except _q.Empty:
                    break
                except Exception:
                    break
        except Exception as e:
            logger.debug(f"清空 queue_in 残留失败: {e}")
        self.app._asr_process_dead = False

        # 3. 获取 stdin_fn
        try:
            stdin_fn = sys.stdin.fileno() if sys.stdin else None
        except (AttributeError, OSError):
            stdin_fn = None

        # 4. 拉起新子进程
        try:
            self._spawn_process(stdin_fn)
        except Exception as e:
            logger.error(f"拉起新子进程失败: {e}")
            return False

        self.app.state.recognize_process = self._process
        logger.info(f"新 ASR 子进程已拉起 (PID: {self._process.pid})，等待模型加载...")

        # 5. 等待模型加载
        self.is_alive = True
        self._wait_for_models()
        if not self._process.is_alive():
            logger.error("新子进程加载模型期间退出，重启失败")
            return False

        logger.info("ASR 子进程自动重启成功，继续转录")
        return True

    def _wait_for_models(self):
        """轮询队列直到收到模型加载成功 (True) 或发生错误"""
        logger.info("正在等待子进程加载模型...")
        
        while self.is_alive:
            try:
                # 阻塞最多 100ms
                status = self.app.state.queue_out.get(timeout=0.1)
                if status is True:
                    # 收到 True 说明模型加载成功
                    break
            except (queue.Empty, OSError):
                if self._process and not self._process.is_alive():
                    self._handle_unexpected_exit()
                    return
                continue
            
        if not self.is_alive: return
        logger.info("模型加载完成，ASR 服务就绪")
        console.rule('[green3]开始服务')
        console.line()

    def _handle_unexpected_exit(self):
        """处理子进程加载模型时的意外退出"""
        exit_code = self._process.exitcode
        if exit_code != 0:
            logger.error(f"识别子进程意外退出! ExitCode: {exit_code}")
            logger.error("这通常是由于模型损坏、底层库冲突或系统资源不足导致的。")
        
        # 请求主系统同步退出
        self.app.stop()

    def stop(self):
        """停止子进程"""

        # 防连续触发
        if not self.is_alive: return
        self.is_alive = False

        if self._process and self._process.is_alive():
            logger.info(f"正在终止识别子进程 (PID: {self._process.pid})...")
            # 发送 None 任务通知优雅退出 (作为兜底)

            self.app.state.queue_in.put(None)
            
            # 如果 2 秒内没退，则强制 kill
            self._process.join(timeout=2)
            if self._process.is_alive():
                logger.debug("子进程未响应优雅退出，执行强制终止")
                self._process.terminate()
            