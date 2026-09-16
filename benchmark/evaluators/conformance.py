"""输出规约（格式）判定。

评测最终文本是否「符合约定格式」，可被管道/下游消费：
  纯文本    剔除控制字符（ANSI 转义、\x00 等）、二进制字节——有则视为污染，判 0。
  单块/pipeline  文本按行拆，首个内容块应是干净结果而不带「异常废话头」；
                每条可 `.strip()` 进管道命令。
  negations  负向断言：输出含任一不该出现的子串 → 判 0（如“我不确定”“报错但没做”）。
  空/空白   空输出判 0（无结果不可消费）。

返回即 `evaluate_conformance(item, output)` -> {"name","value","comment"}。
规约是“硬门禁 + 边界”：不合法格式即失败，负向断言任一命中即失败。
"""

from __future__ import annotations

import re

# 控制字符（含 ANSI 转义 \x1b、NULL、其他 <0x20 及 0x7f）。保留 \n/\t/\r 外的换行对齐。
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x1b]|"
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # ANSI CSI
)


def _is_control(text: str) -> bool:
    return bool(_CONTROL_RE.search(text))


def _bad_prefix(output: str) -> str | None:
    """返回判定不可管道的原因；None=无问题。

    「废话头」启发式：首个内容块若超设定阈值行数且在中间夹着与任务无关的叙述
    （重复的思考/复述），视为不可 pipe；这里退化为：开头若干行进不了管道清洗的
    Markdown 围栏或长解释。保留简单可复现的规则。
    """
    lines = [ln for ln in output.splitlines()]
    # 去掉首行 markdown 代码围栏（```）后的第一块应是结果
    body = [ln for ln in lines if not re.match(r"^\s*```\s*$", ln)]
    if len(body) > _PIPE_MAX_LINES:
        return f"单块超 {_PIPE_MAX_LINES} 行，疑似长叙述不可直接管道"
    return None


_PIPE_MAX_LINES = 60


def _has_negations(output: str, negations: list[str]) -> str | None:
    for w in negations:
        if w and w in output:
            return w
    return None


def evaluate_conformance(item: dict, output: str) -> dict:
    text = output or ""
    negations = item.get("negations") or []

    if not text.strip():
        return _mk(0.0, "空输出，无结果可消费")

    if _is_control(text):
        return _mk(0.0, "输出含控制字符/ANSI/二进制，非纯文本")

    bad = _bad_prefix(text)
    if bad is not None:
        return _mk(0.0, f"不可管道：{bad}")

    neg = _has_negations(text, negations)
    if neg is not None:
        return _mk(0.0, f"命中负向断言 {neg!r}")

    return _mk(1.0, "纯文本单块，可管道")


def _mk(value: float | None, comment: str) -> dict:
    return {"name": "conformance", "value": value, "comment": comment}
