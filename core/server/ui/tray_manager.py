# coding: utf-8
from __future__ import annotations
import os
import threading
from typing import TYPE_CHECKING
from config_server import ServerConfig as Config, save_runtime_overrides
from ..state import console
from .. import logger
if TYPE_CHECKING:
    from ..app import CapsWriterServer


# 可选的转录模型列表：(model_type, 显示名)
_AVAILABLE_MODELS = [
    ('qwen_asr',     'Qwen3-ASR'),
    ('fun_asr_nano', 'Fun-ASR-Nano'),
    ('sensevoice',   'SenseVoice'),
    ('paraformer',   'Paraformer'),
]


class TrayManager:
    """
    托盘管理器：负责系统托盘图标的初始化、菜单构建及回调处理。

    增强功能：
    - 退出菜单：用户可通过托盘退出 Server
    - Tooltip 进度：转录时实时更新 tooltip 显示剩余时间
    - 气泡通知：转录开始时弹出气泡通知
    - GPU 加速开关：可勾选的菜单项，即时切换并持久化
    - 转录模型切换：子菜单单选，切换后持久化（需重启生效）
    """
    def __init__(self, app: CapsWriterServer):
        self.app = app
        self._tray_instance = None
        # 持有 pystray.Icon 引用，用于刷新菜单勾选状态
        self._icon = None

    def start(self):
        """初始化系统托盘图标"""
        if not Config.enable_tray:
            return

        try:
            from core.ui.tray import enable_min_to_tray, _TraySystem
        except ImportError as e:
            logger.warning(f"托盘模块导入失败，跳过托盘功能: {e}")
            return

        # 获取图标路径
        icon_path = os.path.join(self.app.base_dir, 'assets', 'icon.ico')

        # 构建自定义菜单项：GPU 加速勾选 + 转录模型子菜单
        extra_menu_items = self._build_extra_menu_items()

        # 启用托盘
        enable_min_to_tray(
            'CapsWriter Server',
            icon_path,
            exit_callback=self._request_exit,
            more_options=[],
            extra_menu_items=extra_menu_items,
        )

        # 保存托盘实例引用，用于后续更新 tooltip
        try:
            from core.ui.tray import _tray_instance
            self._tray_instance = _tray_instance
            if _tray_instance is not None:
                self._icon = _tray_instance.icon
        except Exception:
            pass

        logger.info("托盘图标已启用")

    # ── 菜单构建 ──────────────────────────────────

    def _build_extra_menu_items(self):
        """构建 GPU 加速勾选项 + 转录模型子菜单项。"""
        import pystray
        from pystray import MenuItem as item

        menu_items = [
            item(
                '⚡ GPU 预加速',
                self._on_toggle_gpu_boost,
                checked=lambda _it: bool(Config.gpu_boost_enabled),
            ),
            item(
                '🎙️ 转录模型',
                self._build_model_submenu(),
            ),
        ]
        return menu_items

    def _build_model_submenu(self):
        """构建转录模型单选子菜单。

        注意：pystray 的 _assert_action 用 co_argcount 校验 action 参数数量，
        只允许 0/1/2 个参数（含默认值参数），所以不能用 lambda 默认参数 trick
        来捕获循环变量。必须用闭包工厂函数。
        """
        import pystray
        from pystray import MenuItem as item

        sub_items = []
        for model_type, display_name in _AVAILABLE_MODELS:
            sub_items.append(item(
                display_name,
                self._make_model_action(model_type),
                radio=True,
                checked=self._make_model_checked(model_type),
            ))
        return pystray.Menu(*sub_items)

    def _make_model_action(self, mt):
        """为模型 mt 生成 action(icon, item) 闭包，2 参数符合 pystray 校验。"""
        def action(icon, item):
            self._on_switch_model(mt)
        return action

    @staticmethod
    def _make_model_checked(mt):
        """为模型 mt 生成 checked(item) 闭包，1 参数符合 pystray 运行时调用。"""
        def checked(item):
            from config_server import ServerConfig as Config
            return Config.model_type.lower() == mt
        return checked

    # ── 菜单回调 ──────────────────────────────────

    def _on_toggle_gpu_boost(self, icon, item):
        """GPU 预加速勾选切换：即时生效 + 持久化 + 通知子进程。"""
        new_val = not bool(Config.gpu_boost_enabled)
        Config.gpu_boost_enabled = new_val
        save_runtime_overrides({'gpu_boost_enabled': new_val})
        logger.info(f"GPU 预加速已切换为: {new_val}")

        # 通过 cmd 任务让子进程同步 Config 变更
        self._send_config_update_to_worker({'gpu_boost_enabled': new_val})

        # 弹气泡提示
        status_text = '开启' if new_val else '关闭'
        self.show_notification("CapsWriter", f"GPU 预加速已{status_text}")

        # 刷新菜单勾选状态
        self._refresh_menu(icon)

    def _on_switch_model(self, model_type: str):
        """转录模型切换：持久化 + 提示重启生效。"""
        if model_type == Config.model_type.lower():
            return
        Config.model_type = model_type
        save_runtime_overrides({'model_type': model_type})
        logger.info(f"转录模型已切换为: {model_type}（需重启生效）")

        # 通知子进程同步（子进程下次重启加载模型时会用新值）
        self._send_config_update_to_worker({'model_type': model_type})

        display_name = dict(_AVAILABLE_MODELS).get(model_type, model_type)
        self.show_notification(
            "CapsWriter",
            f"转录模型已切换为 {display_name}\n请点击托盘菜单的「重启」以加载新模型"
        )

    def _send_config_update_to_worker(self, updates: dict):
        """通过 queue_in 向识别子进程发送 config_update 命令。"""
        try:
            from ..schema import Task
            self.app.state.queue_in.put(Task(
                type='cmd',
                task_id='config_update',
                data=b'', offset=0, overlap=0,
                socket_id='', is_final=False,
                time_start=0, time_submit=0,
                command='config_update',
                config_updates=dict(updates),
            ))
        except Exception as e:
            logger.debug(f"发送 config_update 到子进程失败: {e}")

    def _refresh_menu(self, icon):
        """刷新托盘菜单的勾选状态。"""
        try:
            if icon is not None and hasattr(icon, 'update_menu'):
                icon.update_menu()
        except Exception as e:
            logger.debug(f"刷新托盘菜单失败: {e}")

    # ── 原有功能 ──────────────────────────────────

    def update_tooltip(self, text: str):
        """更新托盘图标的 tooltip 文字"""
        if self._tray_instance and hasattr(self._tray_instance, 'icon'):
            try:
                self._tray_instance.icon.title = text
            except Exception as e:
                logger.debug(f"更新 tooltip 失败: {e}")

    def show_notification(self, title: str, message: str):
        """
        显示气泡通知（Windows Balloon Tip）

        Args:
            title: 通知标题
            message: 通知内容
        """
        if self._tray_instance and hasattr(self._tray_instance, 'icon'):
            try:
                self._tray_instance.icon.notify(message, title)
            except Exception as e:
                logger.debug(f"气泡通知失败: {e}")

    def set_transcribe_progress(self, filename: str, processed: float, total: float):
        """
        更新转录进度（tooltip + 无气泡通知）

        Args:
            filename: 正在转录的文件名
            processed: 已处理的音频时长（秒）
            total: 音频总时长（秒）
        """
        if total > 0:
            remaining = max(0, total - processed)
            tooltip_text = f"CapsWriter - 转录中… 预计剩余 {remaining:.0f}s"
        else:
            tooltip_text = f"CapsWriter - 转录中… {processed:.1f}s"
        self.update_tooltip(tooltip_text)

    def clear_transcribe_progress(self):
        """清除转录进度，恢复默认 tooltip"""
        self.update_tooltip("CapsWriter Server")

    def _request_exit(self, icon=None, item=None):
        """托盘图标引用的退出回调"""
        logger.info("托盘退出: 用户点击退出菜单，准备清理资源并退出")
        self.app.stop()

    def stop(self):
        """停止托盘图标"""
        if not Config.enable_tray:
            return

        try:
            from core.ui.tray import stop_tray
            stop_tray()
            logger.info("TrayManager: 托盘图标已卸载")
        except Exception as e:
            logger.debug(f"TrayManager: 卸载托盘时发生错误: {e}")
