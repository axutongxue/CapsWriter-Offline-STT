# coding: utf-8
"""
CapsWriter Offline 服务端主程序门面类 (Facade)

采用外观模式统一管理进程管理器 (ProcessManager) 和网络管理器 (SocketManager)。
该类是整个服务端应用的中心指挥部，负责初始化生命周期、托盘图标、
并协调子进程与 WebSocket 服务的启动与退出。

增强功能：
- 接收文件路径参数，在模型加载后自动转录文件
- 首次启动时显示悬浮窗提示模型加载
- 单实例检测：已有实例运行时，通过 WebSocket 发送文件路径给已有实例
- 托盘管理：转录进度 tooltip、气泡通知、退出菜单
"""

import os
import sys
import json
import asyncio
import socket
import threading
from pathlib import Path
from typing import List, Optional

from config_server import ServerConfig as Config, __version__
from .state import ServerState, console
from core.tools.signal_handler import register_signal
from .worker.process_manager import ProcessManager
from .connection.server_manager import SocketManager
from .ui.tray_manager import TrayManager
from .ui.floating_window import FloatingWindow
from .file_transcriber import ServerFileTranscriber
from . import logger


class CapsWriterServer:
    """
    CapsWriter 服务端外观类
    
    管理的外部接口极其简洁：start()。
    """
    def __init__(self, files: Optional[List[Path]] = None):
        # 确保正确的工作目录
        self.base_dir = Path(__file__).parents[2]
        os.chdir(self.base_dir)

        # 初始化事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # 初始化状态容器
        self.state = ServerState(app=self)

        # 基本配置与组件实例化
        self.process_manager = ProcessManager(self)
        self.socket_manager = SocketManager(self)
        self.tray_manager = TrayManager(self)

        # 文件转录相关
        self.files = files or []
        self.floating_window = FloatingWindow()

        self.version = __version__
        self.is_alive = False


    def _print_banner(self):
        """打印启动信息"""
        console.line(2)
        console.rule('[bold #d55252]CapsWriter Offline Server[/]'); console.line()
        console.print(f'版本：[bold green]{self.version}[/]', end='\n\n')
        console.print(f'项目地址：[cyan underline]https://github.com/HaujetZhao/CapsWriter-Offline', end='\n\n')
        console.print(f'当前基文件夹：[cyan underline]{self.base_dir}[/]', end='\n\n')
        console.print(f'绑定的服务地址：[cyan underline]{Config.addr}:{Config.port}[/]', end='\n\n')


    def stop(self):
        """
        清理服务端资源
        """
        # 防连续触发
        if not self.is_alive: return
        self.is_alive = False 

        logger.info("=" * 50)
        logger.info("开始清理服务端资源...")

        # 关闭悬浮窗（如果还在显示）
        self.floating_window.close()

        self.state.queue_out.put(None)

        # 1. 关闭 WebSocket 服务（立即释放端口）
        self.socket_manager.stop()

        # 2. 终止识别子进程
        self.process_manager.stop()

        # 3. 停止托盘图标
        self.tray_manager.stop()

        # 4. 最后停止协程（需在其他资源释放之后）
        self.loop.stop()

        logger.info("服务端资源清理完成")
        console.print('[green4]再见！')


    def start(self):
        """
        同步启动服务端 (主入口)
        
        注册信号处理、拉起子进程并进入网络服务监听循环。
        """
        # 防连续触发
        if self.is_alive: return
        self.is_alive = True

        # 注册退出信号处理
        register_signal(self.stop)

        # 检查是否已有实例运行（单实例检测）
        if self._is_port_in_use():
            if self.files:
                # 有文件要转录 → 发送给已有实例
                logger.info("检测到已有 CapsWriter Server 实例运行")
                self._send_files_to_existing_instance()
            else:
                # 无文件但端口冲突 → 提示用户
                logger.error(f"端口 {Config.addr}:{Config.port} 已被占用，无法启动服务端")
                console.print(f'[red]端口 {Config.addr}:{Config.port} 已被占用[/red]')
                console.print('[yellow]请检查是否已有 CapsWriter Server 正在运行[/yellow]')
            return

        # 托盘图标
        self.tray_manager.start()
        self._print_banner()

        logger.info(f"启动参数: files={self.files}, files_count={len(self.files)}")

        # 首次启动且有文件需要转录 → 显示悬浮窗
        if self.files:
            self.floating_window.show("正在加载模型，请稍候...")

        # 拉起识别子进程
        self.process_manager.start()

        # 模型加载完成 → 浮窗过渡为转录状态
        if self.files:
            self.floating_window.show("正在转录中…", position="bottom_right")

        # 如果有待转录文件，在模型加载完成后自动执行
        if self.files:
            logger.info(f"检测到 {len(self.files)} 个待转录文件: {self.files}")
            # 提前注册本地转录的 socket_id
            if 'local_transcribe' not in self.state.sockets_id:
                self.state.sockets_id.append('local_transcribe')
            
            def _run_transcribe():
                try:
                    self._transcribe_files()
                except Exception as e:
                    logger.error(f"转录线程异常: {e}", exc_info=True)
            
            threading.Thread(
                target=_run_transcribe,
                daemon=True
            ).start()
        else:
            logger.info("没有待转录文件")

        # 开启网络服务监听 (接管当前线程直至退出)
        try:
            self.loop.run_until_complete(self.socket_manager.start()) 
        except RuntimeError:
            pass


    def _is_port_in_use(self) -> bool:
        """检测 Server 端口是否已被占用（即是否已有实例运行）"""
        # 使用连接检测而非绑定检测，避免 0.0.0.0 的兼容性问题
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect(('127.0.0.1', int(Config.port)))
                return True  # 连接成功说明端口已被占用
            except socket.error:
                return False


    def _send_files_to_existing_instance(self):
        """
        将文件路径发送给已运行的 Server 实例，然后退出

        通过 WebSocket 连接到已有实例，发送文件转录请求。
        """
        import websockets

        async def _send():
            uri = f"ws://127.0.0.1:{Config.port}"
            try:
                async with websockets.connect(uri, subprotocols=["binary"]) as ws:
                    for file_path in self.files:
                        # 发送文件路径消息
                        msg = json.dumps({
                            "type": "transcribe_file",
                            "path": str(file_path)
                        }, ensure_ascii=False)
                        await ws.send(msg)
                        logger.info(f"已发送文件路径到已有实例: {file_path}")
                    # 等待一小段时间确保消息被接收
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"连接已有实例失败: {e}")
                console.print(f'[red]连接已有 CapsWriter Server 实例失败: {e}[/red]')
                console.print('[yellow]请确认 Server 正在运行，或手动重启[/yellow]')

        try:
            self.loop.run_until_complete(_send())
        except Exception as e:
            logger.error(f"发送文件到已有实例异常: {e}")

        logger.info("文件路径已发送，当前实例退出")


    def _transcribe_files(self):
        """
        在后台线程中转录所有文件

        逐个转录文件，更新托盘 tooltip。
        浮窗在模型加载阶段已显示，转录完成后关闭。
        """
        logger.info(f"转录线程启动，共 {len(self.files)} 个文件待转录")
        for file_path in self.files:
            try:
                logger.info(f"开始转录: {file_path}")

                transcriber = ServerFileTranscriber(self, file_path)
                transcriber.set_progress_callback(
                    lambda p, t, fn=file_path.name: self.tray_manager.set_transcribe_progress(fn, p, t)
                )

                success = transcriber.transcribe()

                if success:
                    logger.info(f"转录成功: {file_path}")
                else:
                    logger.error(f"转录失败: {file_path}")

            except Exception as e:
                logger.error(f"转录异常: {file_path}, 错误: {e}", exc_info=True)

        # 所有文件转录完成，关闭浮窗，清除进度
        self.floating_window.close()
        self.tray_manager.clear_transcribe_progress()
        logger.info("所有文件转录完成")
