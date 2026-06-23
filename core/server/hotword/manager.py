# coding: utf-8
"""
服务端热词管理模块

提供 HotwordManager 类用于管理热词的加载、替换和文件监视。
从客户端 core.client.hotword.manager 移植，适配服务端 Config 与 logger。

新增 apply(result) 方法：一站式把音素纠错 + 规则替换 + token 同步
作用于一个 Result，供 ServerFileTranscriber 直接调用。
"""

from __future__ import annotations

import threading
import time
import unicodedata
from pathlib import Path
from typing import Dict, Optional, Any, List, Tuple

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from rich.console import Console

from .hot_rule import RuleCorrector
from .hot_phoneme import PhonemeCorrector
from . import logger

# 服务端配置：取 hot_thresh / hot_similar，缺失则用默认值
try:
    from config_server import ServerConfig
    HOT_THRESH = getattr(ServerConfig, 'hot_thresh', 0.8)
    HOT_SIMILAR = getattr(ServerConfig, 'hot_similar', 0.6)
except ImportError:
    HOT_THRESH = 0.8
    HOT_SIMILAR = 0.6

# 服务端没有 core.client.state.console，统一用本地 rich Console
console = Console(highlight=False)


class HotwordManager:
    """热词管理器：负责资源协调、热词文件加载与动态监控"""

    def __init__(self,
                 hotword_files: Optional[Dict[str, Path]] = None,
                 threshold: float = 0.7,
                 similar_threshold: Optional[float] = None):
        """
        初始化
        Args:
            hotword_files: 文件映射 {'hot': Path, 'rule': Path}
            threshold: 纠错阈值
            similar_threshold: 相似度阈值
        """
        self.files = hotword_files or {
            'hot': Path('hot.txt'),
            'rule': Path('hot-rule.txt'),
        }

        self.threshold = threshold
        self.similar_threshold = similar_threshold

        # 初始化各个组件
        self.phoneme_corrector = PhonemeCorrector(threshold=threshold, similar_threshold=similar_threshold)
        self.rule_corrector = RuleCorrector()

        self._observer: Optional[Observer] = None
        self._is_watcher_started = False

    def _get_display_width(self, text: str) -> int:
        """计算字符串的显示宽度（考虑中文字符占2个单位）"""
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in ('W', 'F', 'A'):
                width += 2
            else:
                width += 1
        return width

    def _format_msg(self, label: str, filename: str, count: int) -> str:
        """格式化对齐消息"""
        w = self._get_display_width(label)
        padding1 = " " * max(0, 8 - w)
        w2 = self._get_display_width(filename)
        padding2 = " " * max(0, 16 - w2)
        return f"[bold cyan]{label}{padding1}：[/][cyan]{filename}{padding2}[/] 已更新[green]{count:3d}[/]条"

    def load_all(self) -> None:
        """初次加载所有资源"""
        logger.info("正在加载热词资源...")
        self._load_hot()
        self._load_rule()
        logger.info("热词资源加载完成")

    def _read_file(self, key: str) -> str:
        """读取文件的统一辅助函数"""
        path = self.files.get(key)
        if not path: return ""
        try:
            if not path.exists():
                # 缺失则创建空文件
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# 热词文件单行一个\n", encoding='utf-8')
                return ""
            return path.read_text(encoding='utf-8')
        except Exception as e:
            logger.error(f"读取文件失败 {path}: {e}")
            return ""

    def _load_hot(self) -> None:
        content = self._read_file('hot')
        num = self.phoneme_corrector.update_hotwords(content)
        console.print(self._format_msg("热词库", self.files['hot'].name, num))

    def _load_rule(self) -> None:
        content = self._read_file('rule')
        num = self.rule_corrector.update_rules(content)
        console.print(self._format_msg("规则库", self.files['rule'].name, num))

    def get_phoneme_corrector(self) -> PhonemeCorrector:
        return self.phoneme_corrector

    def get_rule_corrector(self) -> RuleCorrector:
        return self.rule_corrector

    def start(self) -> None:
        """开启热词服务：加载资源并启动文件监视"""
        self.load_all()
        self.start_file_watcher()

    def stop(self) -> None:
        """关闭热词服务：停止文件监视"""
        self.stop_file_watcher()

    def apply(self, result) -> Tuple[str, List, List]:
        """
        对一个识别 Result 应用热词后处理链：
          1. 音素热词替换（基于 FastRAG 两阶段检索）
          2. 规则正则替换
          3. 若文本变化且有 tokens/timestamps，同步回 token 序列

        会原地修改 result.text / text_accu / tokens / timestamps。
        仅当热词库非空或规则库非空时才执行；若两者皆空，直接返回不处理。

        Returns:
            (corrected_text, matchs, similars)
            matchs:     [(原词, 热词, 分数), ...] 音素命中
            similars:   [(原词, 热词, 分数), ...] 相似提示
        """
        text_accu = result.text_accu or result.text
        corrected = text_accu
        matchs: List = []
        similars: List = []

        # 1. 音素热词替换
        if self.phoneme_corrector.hotwords:
            correction = self.phoneme_corrector.correct(text_accu, k=10)
            corrected = correction.text
            matchs = correction.matchs
            similars = correction.similars
            for origin, hw, score in matchs:
                logger.info(f"热词匹配: 「{origin}」→「{hw}」(分数={score:.2f})")
            for origin, hw, score in similars:
                logger.debug(f"热词参考: 「{origin}」≈「{hw}」(分数={score:.2f})")

        # 2. 规则替换
        if self.rule_corrector.patterns:
            corrected = self.rule_corrector.substitute(corrected)

        # 3. 有变化则同步 tokens
        if corrected != text_accu and result.tokens:
            from core.tools.token_sync import sync_tokens_from_text
            new_tokens, new_timestamps = sync_tokens_from_text(
                result.tokens, result.timestamps, corrected
            )
            result.text_accu = corrected
            result.text = corrected
            result.tokens = new_tokens
            result.timestamps = new_timestamps
            logger.debug(f"热词修正: {text_accu[:60]} → {corrected[:60]}")
        elif corrected != text_accu:
            # 没有 tokens 也要更新 text
            result.text_accu = corrected
            result.text = corrected

        return corrected, matchs, similars

    def start_file_watcher(self) -> Any:
        """启动文件监视"""
        if self._observer: return

        self._observer = Observer()
        handler = _HotwordFileHandler(self)

        # 监视每一个文件所在的目录 (去重后监听)
        watched_dirs = {p.parent.absolute() for p in self.files.values()}
        for d in watched_dirs:
            self._observer.schedule(handler, path=str(d), recursive=False)

        self._observer.start()
        self._is_watcher_started = True
        logger.debug(f"已启动热词文件监视: {watched_dirs}")
        return self._observer

    def stop_file_watcher(self) -> None:
        """停止文件监视"""
        if self._is_watcher_started and self._observer:
            self._observer.stop()
            self._observer.join()
            self._is_watcher_started = False
            self._observer = None
            logger.debug("热词文件监视已停止")


class _HotwordFileHandler(FileSystemEventHandler):
    """热词文件变化处理器"""

    _debounce_delay = 3

    def __init__(self, manager: HotwordManager):
        super().__init__()
        self.manager = manager
        self._last_event = None
        self._timer = None
        self._lock = threading.Lock()

        # 映射文件路径名到加载函数
        # 注意：这里直接与 manager.files 动态保持一致
        self._update_mapping()

    def _update_mapping(self):
        m = self.manager
        self._file_mapping = {
            m.files['hot'].name: m._load_hot,
            m.files['rule'].name: m._load_rule,
        }

    def on_modified(self, event):
        """文件修改时触发"""
        if event.is_directory: return

        event_path = Path(event.src_path)
        filename = event_path.name

        # 检查是否是我们关心的文件
        if filename not in self._file_mapping:
            return

        logger.debug(f"[watchdog] 热词文件变化: {filename}")
        current_time = time.time()

        with self._lock:
            self._last_event = (filename, current_time)
            if self._timer is None or not self._timer.is_alive():
                self._timer = threading.Thread(target=self._debounced_worker, daemon=True)
                self._timer.start()

    def _debounced_worker(self):
        """防抖工作线程"""
        while True:
            time.sleep(self._debounce_delay)

            with self._lock:
                if self._last_event is None:
                    break

                filename, event_time = self._last_event
                if time.time() - event_time < self._debounce_delay:
                    continue

                self._last_event = None

            # 执行加载
            handler = self._file_mapping.get(filename)
            if handler:
                try:
                    handler()
                    logger.info(f"热词文件已自动重新加载: {filename}")
                except Exception as e:
                    console.print(f'热词自动更新失败：{e}', style='bright_red')
                    logger.error(f"更新热词失败: {e}", exc_info=True)
            break



