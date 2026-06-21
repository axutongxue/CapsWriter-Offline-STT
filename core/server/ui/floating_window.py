# coding: utf-8
"""
悬浮窗模块

在 Server 启动时创建一个置顶悬浮窗，可反复显示/隐藏。
- 模型加载时居中显示「正在加载模型，请稍候...」
- 转录时右下角显示「正在转录中…」

使用 Tkinter 实现，仅在 Windows 平台有效。
设计要点：只创建一次 tk.Tk()，通过 withdraw/deiconify 控制显隐，
避免多次创建/销毁 Tk root 导致的崩溃。
"""

import threading
from typing import Optional


class FloatingWindow:
    """
    置顶悬浮窗（单例复用）

    在独立线程中创建唯一的 Tkinter 窗口，通过 withdraw/deiconify 控制显隐，
    转录期间在屏幕右下角显示「正在转录中…」浮窗，转录完成后隐藏。
    """

    def __init__(self):
        self._root: Optional[object] = None
        self._label: Optional[object] = None
        self._thread: Optional[threading.Thread] = None
        self._initialized = False  # Tk 窗口是否已创建
        self._visible = False
        self._pending_show = None  # (message, position) 等待显示
        self._pending_hide = False  # 等待隐藏

    def show(self, message: str = "正在加载模型，请稍候...", position: str = "center"):
        """
        显示悬浮窗

        Args:
            message: 显示的提示文字
            position: 窗口位置，"center" 居中，"bottom_right" 右下角
        """
        if self._visible:
            # 已可见，仅更新文字和位置
            self._update_window(message, position)
            return

        self._visible = True

        if not self._initialized:
            # 首次：启动 Tk 线程
            self._pending_show = (message, position)
            self._thread = threading.Thread(
                target=self._run_window,
                daemon=True
            )
            self._thread.start()
        else:
            # 已初始化：更新内容并显示
            self._pending_show = (message, position)
            self._notify_tk()

    def close(self):
        """隐藏悬浮窗（不销毁，保留复用）"""
        self._visible = False
        if self._initialized and self._root is not None:
            self._pending_hide = True
            self._notify_tk()

    def _update_window(self, message: str, position: str):
        """更新窗口文字和位置（在 Tk 线程中安全调用）"""
        if self._root is None:
            return
        try:
            self._root.after(0, lambda: self._do_update(message, position))
        except Exception:
            pass

    def _do_update(self, message: str, position: str):
        """在 Tk 线程中执行更新"""
        if self._label is not None:
            try:
                self._label.config(text=message)
            except Exception:
                pass
        self._reposition(position)

    def _reposition(self, position: str):
        """重新定位窗口"""
        if self._root is None:
            return
        try:
            win_width = 320
            win_height = 80
            screen_width = self._root.winfo_screenwidth()
            screen_height = self._root.winfo_screenheight()

            if position == "bottom_right":
                x = screen_width - win_width - 30
                y = screen_height - win_height - 80
            else:
                x = (screen_width - win_width) // 2
                y = (screen_height - win_height) // 2 - 100

            self._root.geometry(f"{win_width}x{win_height}+{x}+{y}")
        except Exception:
            pass

    def _notify_tk(self):
        """通知 Tk 线程处理待办操作"""
        if self._root is None:
            return
        try:
            self._root.after(0, self._process_pending)
        except Exception:
            pass

    def _process_pending(self):
        """在 Tk 线程中处理待办操作"""
        if self._pending_show is not None:
            message, position = self._pending_show
            self._pending_show = None
            if self._label is not None:
                try:
                    self._label.config(text=message)
                except Exception:
                    pass
            self._reposition(position)
            try:
                self._root.deiconify()
            except Exception:
                pass

        if self._pending_hide:
            self._pending_hide = False
            try:
                self._root.withdraw()
            except Exception:
                pass

    def _run_window(self):
        """在独立线程中运行 Tkinter 窗口（仅执行一次）"""
        try:
            import tkinter as tk
        except ImportError:
            self._visible = False
            return

        root = tk.Tk()
        root.overrideredirect(True)  # 无边框
        root.attributes('-topmost', True)  # 置顶

        # 尝试设置 DPI 感知（Windows）
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        # 窗口尺寸
        win_width = 320
        win_height = 80

        # 默认居中位置
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()

        # 如果有 pending_show，使用其位置
        if self._pending_show:
            message, position = self._pending_show
            self._pending_show = None
        else:
            message = "正在加载模型，请稍候..."
            position = "center"

        if position == "bottom_right":
            x = screen_width - win_width - 30
            y = screen_height - win_height - 80
        else:
            x = (screen_width - win_width) // 2
            y = (screen_height - win_height) // 2 - 100

        root.geometry(f"{win_width}x{win_height}+{x}+{y}")

        # 背景色
        root.configure(bg='#2b2b2b')

        # 文字标签
        label = tk.Label(
            root,
            text=message,
            font=("Microsoft YaHei UI", 13),
            fg='white',
            bg='#2b2b2b',
            wraplength=280
        )
        label.pack(expand=True, fill='both', padx=15, pady=10)

        self._root = root
        self._label = label
        self._initialized = True

        # 定期检查待办操作
        def poll_pending():
            self._process_pending()
            root.after(100, poll_pending)

        root.after(100, poll_pending)

        try:
            root.mainloop()
        except Exception:
            pass

        # mainloop 退出后清理
        self._initialized = False
        self._root = None
        self._label = None
        self._visible = False
