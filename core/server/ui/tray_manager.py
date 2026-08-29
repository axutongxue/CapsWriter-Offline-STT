# coding: utf-8
from __future__ import annotations
import os
import threading
from typing import TYPE_CHECKING
from config_server import (
    ServerConfig as Config, save_runtime_overrides,
    get_engine_overrides, save_engine_overrides,
    ParaformerArgs, SenseVoiceArgs, FunASRNanoGGUFArgs, Qwen3ASRGGUFArgs,
)
from ..state import console
from .. import logger
if TYPE_CHECKING:
    from ..app import CapsWriterServer


# 可选的转录模型列表：(model_type, 显示名)
# 仅保留 Qwen3-ASR 与 Fun-ASR-Nano，移除 SenseVoice / Paraformer
_AVAILABLE_MODELS = [
    ('qwen_asr',     'Qwen3-ASR'),
    ('fun_asr_nano', 'Fun-ASR-Nano'),
]

# model_type -> 对应 Args 配置类（用于读取引擎默认值，托盘勾选状态回退用）
_ENGINE_ARGS = {
    'sensevoice':   SenseVoiceArgs,
    'fun_asr_nano': FunASRNanoGGUFArgs,
    'qwen_asr':     Qwen3ASRGGUFArgs,
    'paraformer':   ParaformerArgs,
}

# 支持 GGUF GPU 解码 (llm_use_gpu) 的引擎
_GGUF_ENGINES = ('qwen_asr', 'fun_asr_nano', )


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
        """构建 GPU 加速勾选项 + 转录模型子菜单 + 输出格式子菜单 + 关于作者。"""
        import pystray
        from pystray import MenuItem as item

        menu_items = [
            item(
                '⚡ GPU 预加速',
                self._on_toggle_gpu_boost,
                checked=lambda _it: bool(Config.gpu_boost_enabled),
            ),
            item(
                '🎤 显卡加速',
                self._build_gpu_accel_submenu(),
            ),
            item(
                '🎙️ 转录模型',
                self._build_model_submenu(),
            ),
            item(
                '📄 输出格式',
                self._build_output_format_submenu(),
            ),
            item(
                'ℹ️ 关于作者',
                self._make_about_action(),
            ),
        ]
        return menu_items

    def _build_output_format_submenu(self):
        """
        构建"输出格式"子菜单：SRT / TXT / JSON / merge 4 个可勾选项。
        切换后只更新主进程 Config 并持久化（子进程不读取这些字段）。
        """
        import pystray
        from pystray import MenuItem as item

        # (Config 字段名, 显示名, 默认值) —— 默认 SRT 关，其余开
        entries = [
            ('file_save_srt',   'SRT 字幕 (.srt)',  False),
            ('file_save_txt',   '文本 (.txt)',       True),
            ('file_save_json',  'JSON 时间戳 (.json)', True),
            ('file_save_merge', '合并文本 (.merge.txt)', False),
        ]
        sub_items = []
        for key, display, default in entries:
            sub_items.append(item(
                display,
                self._make_format_action(key),
                checked=self._make_format_checked(key),
            ))
        return pystray.Menu(*sub_items)

    def _make_format_action(self, key):
        """为输出格式开关 key 生成 action(icon, item) 闭包，2 参数符合 pystray 校验。"""
        def action(icon, item):
            self._on_toggle_format(key, icon)
        return action

    @staticmethod
    def _make_format_checked(key):
        """为输出格式开关 key 生成 checked(item) 闭包，1 参数符合 pystray 运行时调用。"""
        def checked(item):
            from config_server import ServerConfig as Config
            return bool(getattr(Config, key, False))
        return checked

    def _on_toggle_format(self, key: str, icon=None):
        """输出格式开关切换：即时生效（主进程 Config）+ 持久化 + 刷新菜单。"""
        old_val = bool(getattr(Config, key, False))
        new_val = not old_val
        setattr(Config, key, new_val)
        save_runtime_overrides({key: new_val})
        logger.info(f"输出格式 {key} 已切换为: {new_val}")
        # 文件保存开关只影响主进程的 _save_result，不需要通知子进程
        self._refresh_menu(icon)

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

    # ── 显卡加速子菜单 ──────────────────────────────

    def _engine_default(self, model_type: str, key: str):
        """读取引擎 Args 类中某字段的默认值（无覆盖时的出厂状态）。"""
        ArgsCls = _ENGINE_ARGS.get(model_type)
        if ArgsCls is None:
            return None
        return getattr(ArgsCls, key, None)

    def _engine_setting(self, model_type: str, key: str):
        """读引擎设置当前生效值：托盘覆盖项优先，否则回落到 Args 默认值。"""
        overrides = get_engine_overrides(model_type)
        if key in overrides:
            return overrides[key]
        return self._engine_default(model_type, key)

    def _build_gpu_accel_submenu(self):
        """构建「显卡加速」子菜单（作用于当前转录模型的引擎，改后需重启生效）。

        依据文档《显卡加速的若干问题》暴露的设置项：
        - ONNX 编码加速 (DirectML)：onnx_provider CPU ↔ DML
        - GGUF 解码用显卡 (llm_use_gpu)：仅 GGUF 引擎显示
        - 集显兼容补丁 (VK_DISABLE_COOPMAT / VK_DISABLE_F16)：仅 GGUF 引擎显示
        """
        import pystray
        from pystray import MenuItem as item

        mt = str(Config.model_type).lower()
        sub_items = []

        # 1. ONNX 编码加速（DML）
        sub_items.append(item(
            'ONNX 编码加速 (DirectML)',
            self._make_engine_toggle_action('onnx_provider', 'DML', 'CPU'),
            checked=self._make_provider_checked(),
        ))

        # 2. GGUF 解码 + 集显补丁（仅 GGUF 引擎）
        if mt in _GGUF_ENGINES:
            sub_items.append(item(
                'GGUF 解码用显卡',
                self._make_engine_toggle_action('llm_use_gpu', True, False),
                checked=self._make_engine_checked('llm_use_gpu'),
            ))
            sub_items.append(pystray.Menu.SEPARATOR)
            sub_items.append(item(
                '集显补丁: 禁用 COOPMAT',
                self._make_compat_action('vk_disable_coopmat'),
                checked=self._make_compat_checked('vk_disable_coopmat'),
            ))
            sub_items.append(item(
                '集显补丁: 禁用 F16',
                self._make_compat_action('vk_disable_f16'),
                checked=self._make_compat_checked('vk_disable_f16'),
            ))

        return pystray.Menu(*sub_items)

    def _make_provider_checked(self):
        """onnx_provider 勾选状态：DML 为勾选。"""
        def checked(item):
            mt = str(Config.model_type).lower()
            return str(self._engine_setting(mt, 'onnx_provider')).upper() == 'DML'
        return checked

    def _make_engine_checked(self, key):
        """布尔引擎字段（llm_use_gpu 等）勾选状态。"""
        def checked(item):
            mt = str(Config.model_type).lower()
            return bool(self._engine_setting(mt, key))
        return checked

    def _make_engine_toggle_action(self, key, on_val, off_val):
        """切换引擎字段：持久化到 engine_overrides[当前model_type]，提示重启。"""
        def action(icon, item):
            mt = str(Config.model_type).lower()
            cur = self._engine_setting(mt, key)
            # onnx_provider 是字符串比较，其他是布尔
            is_on = (str(cur).upper() == str(on_val).upper()) if isinstance(on_val, str) else bool(cur)
            new_val = off_val if is_on else on_val
            save_engine_overrides(mt, {key: new_val})
            logger.info(f"引擎设置 {mt}.{key} 已切换为: {new_val}（需重启生效）")
            self.show_notification(
                "CapsWriter",
                f"设置已保存（{key} = {new_val}）\n请点击托盘菜单的「重启」以生效"
            )
            self._refresh_menu(icon)
        return action

    def _make_compat_action(self, key):
        """集显补丁开关：写 ServerConfig 顶层字段（全局生效，不分引擎）。"""
        def action(icon, item):
            new_val = not bool(getattr(Config, key, False))
            setattr(Config, key, new_val)
            save_runtime_overrides({key: new_val})
            logger.info(f"集显补丁 {key} 已切换为: {new_val}（需重启生效）")
            self.show_notification(
                "CapsWriter",
                f"集显补丁已{'开启' if new_val else '关闭'}\n请点击托盘菜单的「重启」以生效"
            )
            self._refresh_menu(icon)
        return action

    @staticmethod
    def _make_compat_checked(key):
        """集显补丁勾选状态（读 ServerConfig 顶层字段）。"""
        def checked(item):
            return bool(getattr(Config, key, False))
        return checked

    # ── 菜单回调 ──────────────────────────────────

    def _make_about_action(self):
        """生成「关于作者」菜单 action(icon, item) 闭包，2 参数符合 pystray 校验。"""
        def action(icon, item):
            self._on_show_about(icon)
        return action

    def _on_show_about(self, icon=None):
        """显示「关于作者」弹窗（独立线程，避免阻塞托盘）。"""
        logger.info("「关于作者」菜单已点击，启动弹窗线程")
        try:
            t = threading.Thread(target=self._show_about_dialog, daemon=True)
            t.start()
        except Exception as e:
            logger.error(f"启动「关于作者」弹窗线程失败: {e}")

    @staticmethod
    def _show_about_dialog():
        """用 tkinter 弹出「关于作者」窗口，含可点击链接。

        关键点：PyInstaller 打包后 internal/tkinter 目录不含 ttk 子模块，
        所以只用 tk.Label / tk.Button / tk.Frame，避免 import ttk 失败。
        """
        try:
            import webbrowser
            import tkinter as tk
        except ImportError as e:
            logger.error(f"tkinter 导入失败，无法显示关于弹窗: {e}")
            return

        root = tk.Tk()
        root.title("关于作者")
        root.geometry("520x340")
        root.resizable(False, False)

        # 内容（Markdown 风格转纯文本 + 可点击链接）
        info = [
            ("作者微信公众号", "阿虚同学", None),
            ("软件官网", "axutongxue.net", "https://axutongxue.net/"),
            ("本项目开源地址", "CapsWriter-Offline-STT", "https://github.com/axutongxue/CapsWriter-Offline-STT"),
            ("基于项目", "HaujetZhao/CapsWriter-Offline", "https://github.com/HaujetZhao/CapsWriter-Offline"),
        ]

        # 标题
        tk.Label(root, text="CapsWriter-Offline-STT", font=("Microsoft YaHei", 14, "bold"),
                 pady=15).pack()

        # 用普通 Frame 替代 ttk.Frame
        content_frame = tk.Frame(root, padx=15, pady=5)
        content_frame.pack(fill="both", expand=True)

        def make_click_handler(url):
            def handler(_event=None):
                try:
                    webbrowser.open(url)
                except Exception as e:
                    logger.error(f"打开链接失败 {url}: {e}")
            return handler

        for label_text, text, url in info:
            row = tk.Frame(content_frame)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=f"{label_text}：", width=14, anchor="e").pack(side="left")
            if url:
                link = tk.Label(row, text=text, fg="#1a73e8", cursor="hand2",
                                font=("Microsoft YaHei", 9, "underline"))
                link.pack(side="left")
                link.bind("<Button-1>", make_click_handler(url))
            else:
                tk.Label(row, text=text).pack(side="left")

        # 致谢
        tk.Label(root, text="感谢原作者的付出", font=("Microsoft YaHei", 9),
                 fg="#666", pady=10).pack()

        # 关闭按钮（普通 tk.Button 替代 ttk.Button）
        tk.Button(root, text="关闭", command=root.destroy, padx=20).pack(pady=5)

        try:
            root.mainloop()
        except Exception as e:
            logger.error(f"关于弹窗 mainloop 异常: {e}")

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
