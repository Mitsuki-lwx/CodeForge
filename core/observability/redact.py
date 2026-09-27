"""落盘脱敏 —— **纯函数**，无 IO、无全局可变状态。

动机与判据：`docs/ref_user_visible_vs_observability.md` §5 P0-3、`docs/spec_show_redact.md` §4.4。
做法参考：`docs/ref_omp_secrets.md`（oh-my-pi `can1357/oh-my-pi` 源码）。

## 为什么只挡"明显的"

用户明确：**"脱敏档明显的吧"**。所以本模块**只挡凭据形态，不挡 PII**：

- 凭据形态有**厂商固定前缀**（`sk-ant-` / `AKIA` / `ghp_`），误伤率接近零
- PII（邮箱 / 手机号）误伤率高：代码里 `user@example.com` 是测试数据、
  日志里的长数字不是手机号 —— 挡了只会让人 distrust 这个功能

oh-my-pi 在 `secrets/patterns.ts:60` 留了一条血泪教训，本模块照抄：

> `No generic keyword/entropy rules: a coding agent must still be able to read
> identifiers like `token_expiry_seconds`.`

⇒ **不做**通用关键词匹配、**不做**纯熵值匹配。只做厂商前缀 + key 名字规则。

## 熵值门槛

抄 oh-my-pi `pi-ai/src/transform-messages.ts:437`：命中形态后，
候选串必须**长度达标**且**至少命中 2 类字符**（小写 / 大写 / 数字 / `-_`）
才动手。⇒ `token_expiry_seconds` 这种标识符绝不会被当成 token。

## 接在哪一层

`docs/spec_show_redact.md` §4.5：只接**落盘**（trace / obs logs）。

- 落盘 → 脱敏
- UI 显示（`/show`、`tui/app.py`）→ **不脱敏**（用户自己看的东西遮了就没法排查）
- 发往模型 → **不动**（脱敏出站会破坏 agent 读 `.env` / 跑测试的正常能力）
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── 熵值门槛 ────────────────────────────────────────────────────

#: 候选串的最小长度。低于此不遮（挡 `sk-x` 这类太短的噪声）
MIN_SECRET_LEN = 12
#: 递归脱敏的最大深度。超过就原样返回（防爆栈）
MAX_DEPTH = 8
#: 值的全量替换标记（key 名字命中时用）
MASK_ALL = "[REDACTED]"


def _entropy_ok(s: str) -> bool:
    """熵值门槛：长度达标 **且** 至少命中 2 类字符。

    抄 oh-my-pi `transform-messages.ts:437`（4 类字符至少 2 类）。
    这里去掉了 `_-` 一类 —— 因为 vendor 前缀本身已含 `-`/`_`，
    留着会让 `sk-abc` 这种真 key 反而不过门槛。
    """
    if len(s) < MIN_SECRET_LEN:
        return False
    classes = 0
    if any("a" <= c <= "z" for c in s):
        classes += 1
    if any("A" <= c <= "Z" for c in s):
        classes += 1
    if any("0" <= c <= "9" for c in s):
        classes += 1
    return classes >= 2


# ── 值规则：厂商前缀形态 ────────────────────────────────────────

# 每个条目 = (形态名, 编译好的正则, 是否需过熵值门槛)。
#
# **顺序有意义**：更具体的前缀在前（`sk-ant-` 必须在 `sk-` 之前，
# 否则 Anthropic key 会被标成 openai_key）。
#
# ★ 第三列的含义（实测教训，见下）：
#   带**厂商固定前缀**的形态本身就足够特异，正则里的长度下限已经起到了
#   "不能太短"的作用，再叠熵值门槛是**死代码**。
#   只有**泛化形态**（`Bearer …`、`scheme://user:pass@…`）才真的需要它 ——
#   `Bearer <40 字符>` 可能是凭据，也可能是文档里的占位说明。
#   ⇒ 只有这两条走 `_entropy_ok`，其余直接替换。
#
# 实测记录（两次踩坑，都写在这）：
#   1. 最初把熵值门槛对**所有**形态打开 ⇒ 对真 key 不起作用，门槛成了摆设。
#   2. 只对泛化形态打开后仍错：门槛检查的候选串**含 `Bearer ` 前缀本身**，
#      那个大写 B 让任何 `Bearer xxx` 都满足"两类字符"⇒ 门槛再次形同虚设。
#      ⇒ 必须**只检查凭据部分**（见 `_ENTROPY_GROUPS` 的组号）。
# 组号从 1 起算（两条正则都只捕获了**一个**组）
_ENTROPY_GROUPS: dict[str, int] = {"bearer_token": 1, "url_credentials": 1}

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"sk-(?!ant-)[A-Za-z0-9_\-]{20,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # `{30,}` 而非 `{35}`：真实 key 是 `AIza`+35，但多一位就整条漏掉（实测踩到），
    # 少几位则宁可放过 —— `AIza` 前缀本身已经足够特异
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{16,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("stripe_webhook", re.compile(r"\bwhsec_[A-Za-z0-9]{16,}\b")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{24,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+")),
    # 组 1 = 凭据部分（门槛只看它，不看 `Bearer ` 前缀）
    ("bearer_token", re.compile(r"[Bb]earer\s+([A-Za-z0-9_\-.=]{20,})")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
        ),
    ),
    # 含凭据的 URL：scheme://user:pass@host（组 2 = 密码部分）
    (
        "url_credentials",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:([^\s/@]+)@[^\s]+"),
    ),
]


def _make_replacer(name: str):
    """构造替换回调：按形态决定要不要过熵值门槛（且只检查凭据部分）。"""
    group = _ENTROPY_GROUPS.get(name)

    def _sub(match: re.Match[str]) -> str:
        if group is not None:
            secret_part = match.group(group) or ""
            if not _entropy_ok(secret_part):
                return match.group(0)  # 不过门槛 ⇒ 保持原样
        return f"[REDACTED:{name}]"

    return _sub


_REPLACERS: dict[str, object] = {name: _make_replacer(name) for name, _ in _PATTERNS}


def redact_text(text: str) -> str:
    """对一段自由文本做**值规则**脱敏。

    命中即替换成 `[REDACTED:<形态>]`（带形态名，便于"我知道原来是什么"而不用猜）。

    ★ 无命中时**原样返回**（零改动的快路径，热路径靠它）。
    """
    if not text or not isinstance(text, str):
        return text

    out = text
    for name, pat in _PATTERNS:
        if not pat.search(out):
            continue
        out = pat.sub(_REPLACERS[name], out)  # type: ignore[arg-type]
    return out


# ── 位置规则：key 名字 ──────────────────────────────────────────

#: key **名字**含这些片段 → 整个值替换。
#: 抄 Gemini CLI 的做法（`docs/ref_user_visible_vs_observability.md` §2.3）。
_NAME_HINTS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "APIKEY",
    "API_KEY",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "SESSION_KEY",
    "AUTH",
)

#: allowlist：这些 key **永不**按名字脱敏（否则输出没法看）。
#: 抄 Gemini 的 allowlist（PATH/HOME/...）。
_ALLOW = (
    "PATH",
    "HOME",
    "LANG",
    "TZ",
    "SHELL",
    "TERM",
    "USER",
    "LOGNAME",
    "PWD",
    "OLDPWD",
    "PYTHONPATH",
    "AUTHOR",
    "COMMIT",
)


def _name_masks(key: str) -> bool:
    """key 名字是否该触发**全值替换**。"""
    k = str(key).upper()
    # allowlist 优先（`PATH` 里含 `TOKEN` 之类不会，但 `AUTHOR`/`COMMIT` 要挡住）
    if any(a in k for a in _ALLOW):
        return False
    return any(h in k for h in _NAME_HINTS)


# ── 映射脱敏 ────────────────────────────────────────────────────


def redact_mapping(data: Any, *, _depth: int = 0) -> Any:
    """递归脱敏一个 JSON-ish 结构（dict / list / 标量）。

    - key 名字命中 → 整个值换成 `[REDACTED]`（**不递归**，反正要全遮）
    - 字符串值 → 走 `redact_text`
    - **不改入参**（纯函数：新建容器）
    - 超过 `MAX_DEPTH` → 原样返回（防爆栈）
    """
    if _depth > MAX_DEPTH:
        return data
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and _name_masks(k):
                out[k] = MASK_ALL
            else:
                out[k] = redact_mapping(v, _depth=_depth + 1)
        return out
    if isinstance(data, list):
        return [redact_mapping(v, _depth=_depth + 1) for v in data]
    if isinstance(data, tuple):
        return tuple(redact_mapping(v, _depth=_depth + 1) for v in data)
    if isinstance(data, str):
        return redact_text(data)
    return data


# ── 开关 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RedactionSettings:
    """脱敏开关。默认**开**。

    与 oh-my-pi 的差别（`docs/ref_omp_secrets.md` §2.4）：omp 的 `secrets.enabled`
    默认 `false`，因为它连**出站**都遮，会破坏"让 agent 读自己 `.env`"这个用法。

    我们**只在落盘侧**遮，不影响模型读文件 ⇒ 没有功能损失 ⇒ **默认开**。
    逃生口：`CODEFORGE_REDACT=0`。
    """

    enabled: bool = True


def redaction_config() -> RedactionSettings:
    """读开关：env `CODEFORGE_REDACT` 优先于 yaml `observability.redact_secrets`。"""
    enabled = True
    raw = os.getenv("CODEFORGE_REDACT")
    if raw is not None and raw.strip():
        return RedactionSettings(
            enabled=raw.strip().lower() not in ("0", "false", "off", "no")
        )

    try:
        import yaml

        path = os.getenv("CODEFORGE_CONFIG", "config.yaml")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            obs = data.get("observability")
            if isinstance(obs, dict) and obs.get("redact_secrets") is not None:
                enabled = bool(obs["redact_secrets"])
    except Exception as e:  # noqa: BLE001 —— 配置读不到就用默认值，绝不阻断
        # 静默兜底是刻意的（脱敏开关不能因为配置文件坏掉就让主流程崩），
        # 但要留痕：否则"为什么没生效"将无从排查。
        logger.debug("redaction config unreadable, using default (enabled=%s): %s", enabled, e)
    return RedactionSettings(enabled=enabled)


#: 进程级缓存的开关值。`TraceWriter.record` 是热路径（每次工具调用一次
#: fsync），每次重读 yaml 会把 0.017ms 的脱敏开销变成 0.2ms 量级。
#: 单进程内配置不会变，所以缓存是安全的；测试用 `reset_redaction_cache()` 清。
_CONFIG_CACHE: list[bool] = []


def redact_config_lazy() -> bool:
    """拿脱敏开关（进程内缓存一次）。"""
    if not _CONFIG_CACHE:
        _CONFIG_CACHE.append(redaction_config().enabled)
    return _CONFIG_CACHE[0]


def reset_redaction_cache() -> None:
    """清掉开关缓存（配置在进程内变化时 / 测试用）。"""
    _CONFIG_CACHE.clear()


__all__ = [
    "MASK_ALL",
    "MAX_DEPTH",
    "MIN_SECRET_LEN",
    "RedactionSettings",
    "redact_config_lazy",
    "redact_mapping",
    "redact_text",
    "redaction_config",
    "reset_redaction_cache",
]
