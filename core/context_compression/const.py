"""上下文压缩常量定义。

全部硬编码阈值与参数集中管理，不散落到其他模块。
"""

from __future__ import annotations

# ── Layer 1: 工具结果落盘阈值（字符数） ──

# 单条工具结果超过此值则落盘
SINGLE_RESULT_LIMIT = 50000

# 单条消息内所有工具结果合计超过此值则依次落盘
MESSAGE_AGGREGATE_LIMIT = 200000

# ── Layer 2: 摘要压缩阈值（token 数） ──

# 给摘要 LLM 输出预留的 token 空间
SUMMARY_RESERVE = 20000

# 自动触发的额外安全余量（防估算误差与单轮波动）
AUTO_SAFETY_MARGIN = 13000

# 手动/紧急触发安全余量（只用来判断摘要请求本身能否塞下）
MANUAL_SAFETY_MARGIN = 3000

# ── 恢复段 ──

# 恢复段最多展示几个文件
RECOVERY_FILE_LIMIT = 5

# 单文件快照的 token 上限，超出时保留头部、截掉尾部
RECOVERY_TOKENS_PER_FILE = 5000

# ── 近期原文保留 ──

# 摘要后保留近期原文的 token 下界
RECENT_KEEP_TOKENS = 10000

# 摘要后保留近期原文的条数下界
RECENT_KEEP_MESSAGES = 5

# ── 熔断 ──

# 自动压缩连续失败次数达到此值后跳闸
MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES = 3

# ── PTL 自重试 ──

# 摘要请求自身的直接重试次数（逐组丢弃阶段）
PTL_RETRY_LIMIT = 3

# 直接重试耗尽后每次再丢的比例
PTL_DROP_PERCENTAGE = 0.2

# ── Token 估算 ──

# 字符/token 估算比（英文+代码混合场景经验值）
ESTIMATE_CHARS_PER_TOKEN = 3.5

# ── 预览体 ──

# 预览体头部字节数上限
PREVIEW_HEAD_BYTES = 2048

# 预览体头部行数上限
PREVIEW_HEAD_LINES = 20
