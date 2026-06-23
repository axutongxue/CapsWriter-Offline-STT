# coding: utf-8
"""
服务端热词模块

从 CapsWriter-Offline 客户端完整移植，用于文件转录结果的后处理纠错：
- PhonemeCorrector: 基于音素的两阶段（FastRAG→模糊匹配）热词替换
- RuleCorrector: 基于正则表达式的精确规则替换
- HotwordManager: 热词资源管理 + 文件监视（hot.txt / hot-rule.txt 变更自动重载）

调用入口：ServerFileTranscriber 在拿到 Result 后调用 HotwordManager.apply(result)
"""

from core import get_logger
logger = get_logger('server')


from .hot_phoneme import PhonemeCorrector, CorrectionResult
from .hot_rule import RuleCorrector
from .manager import HotwordManager

__all__ = [
    'PhonemeCorrector',
    'CorrectionResult',
    'RuleCorrector',
    'HotwordManager',
]

