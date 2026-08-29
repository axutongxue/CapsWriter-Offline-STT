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
import random
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
from .hotword import HotwordManager
from . import logger


class CapsWriterServer:
    """
    CapsWriter 服务端外观类
    
    管理的外部接口极其简洁：start()。
    """
    def __init__(self, files: Optional[List[Path]] = None, raw_args: Optional[List[str]] = None):
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

        # 热词后处理管理器（音素纠错 + 规则替换 + token 同步）
        # 路径来自 ServerConfig.hotwords_path / hot_rule_path；阈值取 hot_thresh / hot_similar
        # 若配置是相对路径则锚定到 base_dir，避免依赖 cwd
        hot_path = Path(str(Config.hotwords_path))
        if not hot_path.is_absolute():
            hot_path = self.base_dir / hot_path
        rule_path = Path(str(Config.hot_rule_path))
        if not rule_path.is_absolute():
            rule_path = self.base_dir / rule_path
        self.hotword_manager = HotwordManager(
            hotword_files={'hot': hot_path, 'rule': rule_path},
            threshold=getattr(Config, 'hot_thresh', 0.8),
            similar_threshold=getattr(Config, 'hot_similar', 0.6),
        )

        # 文件转录相关
        self.files = files or []
        # 原始命令行入参（含目录），用于 client 模式把目录原样转发给已有 server
        # （让 server 端 batch_transcribe 用 expand_paths 展开为同一批次 [1/N]..[N/N]）
        self.raw_args = raw_args or [str(f) for f in self.files]
        self.floating_window = FloatingWindow()

        self.version = __version__
        self.is_alive = False
        self._port_holder = None  # 端口占位 socket，serve 前释放
        self._asr_process_dead = False  # ASR 子进程崩溃标记，让 batch worker 停止剩余文件


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

        # 0. 停止批量转录工作者线程（投递 sentinel 并等待其退出）
        try:
            from .connection.ws_recv import stop_batch_worker, _BATCH_WORKER_THREAD
            stop_batch_worker()
            if _BATCH_WORKER_THREAD is not None:
                _BATCH_WORKER_THREAD.join(timeout=3)
                logger.info("批量转录工作者线程已停止")
        except Exception as e:
            logger.debug(f"停止批量工作者线程失败: {e}")

        # 关闭悬浮窗（如果还在显示）
        self.floating_window.close()

        # 释放端口占位 socket（若还持有）
        self._release_port_holder()

        self.state.queue_out.put(None)

        # 1. 关闭 WebSocket 服务（立即释放端口）
        self.socket_manager.stop()

        # 2. 终止识别子进程
        self.process_manager.stop()

        # 3. 停止托盘图标
        self.tray_manager.stop()

        # 3.5 停止热词文件监视
        try:
            self.hotword_manager.stop()
        except Exception:
            pass

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

        # 用 bind 原子抢占端口，作为 server / client 角色判定。
        # Windows 下若端口已被占用，bind 立即抛 WinError 10048 → 我方为 client。
        # 失败者不加载模型，直接把文件发给已有实例后退出，避免：
        #   1) 多实例都加载模型浪费资源；
        #   2) bind 阶段崩溃 → 本实例的文件无人转录（旧代码"只转录一个文件"根因）。
        # 失败者稍等后再试连接 6016，若成功说明 server 已在线（旧实例/新任 server）
        # 若连接也失败，则随机退避重试（给 server 加载监听留时间）。
        if not self._try_bind_port():
            # 端口被占用（已有实例运行）→ 走 client 路径
            if self.files:
                logger.info("检测到已有 CapsWriter Server 实例运行，转发送模式")
                self._send_files_to_existing_instance_with_retry()
            else:
                logger.error(f"端口 {Config.addr}:{Config.port} 已被占用，无法启动服务端")
                console.print(f'[red]端口 {Config.addr}:{Config.port} 已被占用[/red]')
                console.print('[yellow]请检查是否已有 CapsWriter Server 正在运行[/yellow]')
            return

        # 托盘图标
        self.tray_manager.start()
        self._print_banner()

        # 启动热词服务（加载 hot.txt / hot-rule.txt，开启文件监视）
        # 即便热词库为空也安全：apply 内部会判断后跳过
        try:
            self.hotword_manager.start()
        except Exception as e:
            logger.warning(f"热词服务启动失败（不影响转录）：{e}")

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

            # 统一走批量队列：由 ws_recv.batch_transcribe → 单工作者线程依次转录。
            # 不再单独起 _transcribe_files 线程直转，避免直转路径与 batch worker
            # 并发运行两个 ServerFileTranscriber 而冲突（ASR 引擎竞争 / UI 互相覆盖）。
            from .connection.ws_recv import batch_transcribe
            batch_transcribe(self, self.files)
        else:
            logger.info("没有待转录文件")

        # 开启网络服务监听 (接管当前线程直至退出)
        # 万一在 _try_bind_port 后、websockets.serve 前，又有实例先 bind 了 6016
        # （极端竞态），这里兜住 OSError，优雅转 client 而非弹错框崩溃。
        try:
            self.loop.run_until_complete(self.socket_manager.start())
        except RuntimeError:
            pass
        except OSError as e:
            logger.warning(f"websockets.serve 绑定失败（{e}），转为发送模式")
            self.is_alive = False
            if self.files:
                self._send_files_to_existing_instance_with_retry()
            else:
                logger.error(f"端口 {Config.addr}:{Config.port} 绑定失败，无法启动服务端")


    def _try_bind_port(self) -> bool:
        """
        用 bind 原子抢占端口，成功则保持 listening 占住端口（不 close），
        交由 SocketManager.start 在 websockets.serve 之前瞬间释放。
        这样在整个模型加载期间（数秒）端口都被本实例独占，其它实例 bind 立即失败
        → 走 client 路径，不会出现"两个实例都当 server、落后者浪费一次模型加载"。
        不设 SO_REUSEADDR / SO_REUSEPORT。
        """
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind((Config.addr, int(Config.port)))
            s.listen(1)
            self._port_holder = s  # 保持占有，serve 前释放
            logger.info(f"已抢占端口 {Config.addr}:{Config.port}（占位 listening）")
            return True
        except socket.error as e:
            logger.info(f"端口抢占失败（{e}），判定为已有实例运行")
            return False


    def _release_port_holder(self):
        """释放占位 socket，供 websockets.serve 立即重新 bind。"""
        s = getattr(self, '_port_holder', None)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
            self._port_holder = None


    def _send_files_to_existing_instance_with_retry(self):
        """
        向已有实例发送文件路径，带重试：第一次发送若连不上，
        说明可能所有实例都还在加载模型、server 尚未 listening。
        随机退避重试最多 ~30s。
        """
        import time
        import random
        max_attempts = 15
        for attempt in range(max_attempts):
            ok = self._send_files_to_existing_instance()
            if ok:
                return
            # 发送失败：server 可能还没 listening，随机退避后重试
            backoff = 0.5 + random.random() * 1.0
            logger.info(f"发送失败，{backoff:.1f}s 后重试 ({attempt + 1}/{max_attempts})")
            time.sleep(backoff)
        logger.error(f"发送文件到已有实例失败，已重试 {max_attempts} 次")
        console.print(f'[red]连接已有 CapsWriter Server 实例失败，请确认 Server 正在运行[/red]')


    def _send_files_to_existing_instance(self) -> bool:
        """
        将原始入参（含目录）批量发送给已运行的 Server 实例，然后退出。

        通过 WebSocket 连接到已有实例，发送一条 transcribe_files 批量消息，
        由已有实例的 batch_transcribe 统一展开、并入同一批次转录。
        返回是否成功发送（供重试逻辑判断）。
        """
        import websockets

        async def _send() -> bool:
            # 用 Config.addr 作为连接目标；若 server 绑 0.0.0.0/::，回退到 127.0.0.1
            host = Config.addr if Config.addr not in ('0.0.0.0', '::') else '127.0.0.1'
            uri = f"ws://{host}:{Config.port}"
            try:
                async with websockets.connect(uri, subprotocols=["binary"], open_timeout=5) as ws:
                    # 用 raw_args（含目录）发，server 端 expand_paths 展开，
                    # 这样 300 个文件聚成一个批次 [1/300]..[300/300]
                    msg = json.dumps({
                        "type": "transcribe_files",
                        "paths": [str(p) for p in self.raw_args],
                    }, ensure_ascii=False)
                    await ws.send(msg)
                    logger.info(f"已发送批量请求到已有实例: {len(self.raw_args)} 项")
                    # 等待一小段时间确保消息被接收
                    await asyncio.sleep(0.5)
                    return True
            except Exception as e:
                logger.warning(f"连接已有实例失败: {e}")
                return False

        try:
            ok = self.loop.run_until_complete(_send())
            logger.info("文件路径已发送，当前实例退出")
            return bool(ok)
        except Exception as e:
            logger.error(f"发送文件到已有实例异常: {e}")
            return False
