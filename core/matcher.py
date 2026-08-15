"""共享匹配器 — exact / not / regex / glob 四类不可变匹配器。

Hook 条件表达式与未来的规则共用本模块；
权限规则沿用旧逻辑（core/permissions/rules.py 不动，不迁移）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal, Protocol

MatcherOp = Literal["exact", "not", "regex", "glob"]


class Matcher(Protocol):
    """规则匹配统一接口。四种实现 + 内部 ContainsMatcher。"""

    def match(self, s: str) -> bool: ...

    def __str__(self) -> str: ...


@dataclass(frozen=True)
class ExactMatcher:
    """整串精确相等（大小写敏感）。"""

    value: str

    def match(self, s: str) -> bool:
        return s == self.value

    def __str__(self) -> str:
        return f"={self.value}"


@dataclass(frozen=True)
class GlobMatcher:
    """通配符匹配（fnmatch 语义）。"""

    pattern: str

    def match(self, s: str) -> bool:
        return fnmatch(s, self.pattern)

    def __str__(self) -> str:
        return self.pattern


@dataclass(frozen=True)
class RegexMatcher:
    """正则部分匹配（re.search 语义）。"""

    src: str
    compiled: re.Pattern[str]

    def match(self, s: str) -> bool:
        return self.compiled.search(s) is not None

    def __str__(self) -> str:
        return f"~{self.src}"


@dataclass(frozen=True)
class ContainsMatcher:
    """大小写不敏感子串包含，仅作为 NotMatcher 的内层使用。"""

    value: str

    def match(self, s: str) -> bool:
        if not self.value:
            return False  # 空模式不含于任何串 → not 后恒 True
        return self.value.lower() in s.lower()

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class NotMatcher:
    """对内层匹配器取反。"""

    inner: Matcher

    def match(self, s: str) -> bool:
        return not self.inner.match(s)

    def __str__(self) -> str:
        return f"!{self.inner}"


def compile_matcher(op: MatcherOp, value: str) -> Matcher:
    """按操作符构造不可变匹配器。正则编译失败抛 ValueError。"""
    if op == "exact":
        return ExactMatcher(value)
    if op == "glob":
        return GlobMatcher(value)
    if op == "regex":
        if not value:
            return RegexMatcher("", re.compile(r""))  # 空正则恒命中
        try:
            return RegexMatcher(value, re.compile(value))
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
    if op == "not":
        return NotMatcher(ContainsMatcher(value))
    raise ValueError(f"unknown matcher op: {op!r}")
