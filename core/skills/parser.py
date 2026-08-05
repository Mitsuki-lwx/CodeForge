"""SKILL.md 解析器。

解析 Markdown 文件的 YAML frontmatter + 正文，校验元信息合法性。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from core.skills.types import SkillDef, SkillMeta, SkillSource

logger = logging.getLogger(__name__)

# name 必须小写字母开头，后续可含数字和连字符
_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]*$")

# 有效的 mode 值
_VALID_MODES = frozenset({"inline", "fork"})

# 有效的 fork_context 值
_VALID_CONTEXTS = frozenset({"none", "recent", "full"})


class SkillParseError(Exception):
    """Skill 解析失败（frontmatter 格式错误、缺必填字段等）。"""


def parse_skill_file(path: Path, source: SkillSource) -> SkillDef:
    """解析单个 SKILL.md 文件，返回 SkillDef。

    Args:
        path: SKILL.md 文件的绝对路径。
        source: Skill 来源（PROJECT / USER）。

    Returns:
        解析完成的 SkillDef。

    Raises:
        SkillParseError: frontmatter 格式错误或缺少必填字段。
    """
    if not path.is_file():
        raise SkillParseError(f"SKILL.md not found: {path}")

    raw = path.read_text(encoding="utf-8")
    frontmatter_dict, body = _split_frontmatter(raw)
    meta = _validate_meta(frontmatter_dict)

    # 判断是否为目录型 Skill（与 SKILL.md 同级的 references 目录存在）
    is_directory = (path.parent / "references").is_dir()

    return SkillDef(
        meta=meta,
        prompt_body=body.strip(),
        source_path=path.parent,
        source=source,
        is_directory=is_directory,
    )


def substitute_arguments(prompt_body: str, args: str) -> str:
    """替换 SOP 正文中的 $ARGUMENTS 占位符。

    Args:
        prompt_body: SKILL.md 正文。
        args: 用户传入的参数（可为空字符串）。

    Returns:
        替换后的文本。若无占位符且 args 非空，在末尾追加 User Request 段。
    """
    if "$ARGUMENTS" in prompt_body:
        return prompt_body.replace("$ARGUMENTS", args)

    if args.strip():
        return f"{prompt_body}\n\n## User Request\n\n{args}"

    return prompt_body


# ── 内部函数 ──────────────────────────────────────────────────────


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """将 SKILL.md 原始文本分离为 (frontmatter_dict, body)。

    frontmatter 由首行 ``---`` 开始、下一个 ``---`` 结束。
    不支持仅有开 ``---`` 无闭 ``---`` 的格式。

    Raises:
        SkillParseError: 缺少开闭 ``---`` 或 YAML 解析失败。
    """
    stripped = raw.lstrip("﻿")  # BOM
    if not stripped.startswith("---"):
        raise SkillParseError("Missing opening '---' frontmatter delimiter")

    # 找第二个 ---
    end_idx = stripped.find("---", 3)
    if end_idx == -1:
        raise SkillParseError("Unclosed frontmatter (missing closing '---')")

    yaml_str = stripped[3:end_idx].strip()
    body = stripped[end_idx + 3 :].strip()

    if not yaml_str:
        raise SkillParseError("Empty frontmatter")

    try:
        frontmatter_dict = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise SkillParseError(f"Invalid YAML in frontmatter: {e}") from e

    if not isinstance(frontmatter_dict, dict):
        raise SkillParseError(
            f"Frontmatter must be a YAML mapping, got {type(frontmatter_dict).__name__}"
        )

    return frontmatter_dict, body


def _validate_meta(raw: dict) -> SkillMeta:
    """校验 frontmatter 字典并构造 SkillMeta。

    Raises:
        SkillParseError: 缺少必填字段或值不合法。
    """
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SkillParseError(
            "Missing or invalid 'name' field (must be a non-empty string)"
        )
    name = name.strip()
    if not _NAME_RE.match(name):
        raise SkillParseError(
            f"Invalid skill name '{name}': must match {_NAME_RE.pattern}"
        )

    description = raw.get("description")
    if not description or not isinstance(description, str):
        raise SkillParseError("Missing or invalid 'description' field")
    description = description.strip()

    # allowed_tools
    allowed_tools = raw.get("allowed_tools", [])
    if allowed_tools is None:
        allowed_tools = []
    if not isinstance(allowed_tools, list):
        raise SkillParseError("'allowed_tools' must be a list")
    for t in allowed_tools:
        if not isinstance(t, str):
            raise SkillParseError(
                f"Each entry in 'allowed_tools' must be a string, got {type(t).__name__}"
            )

    # mode
    mode = raw.get("mode", "inline")
    if mode is None:
        mode = "inline"
    if not isinstance(mode, str):
        raise SkillParseError("'mode' must be a string")
    mode = mode.strip().lower()
    if mode not in _VALID_MODES:
        logger.warning(
            "Skill '%s': invalid mode '%s', falling back to 'inline'. Valid modes: %s",
            name,
            mode,
            sorted(_VALID_MODES),
        )
        mode = "inline"

    # fork_context
    fork_context = raw.get("fork_context", raw.get("context", "none"))
    if fork_context is None:
        fork_context = "none"
    if not isinstance(fork_context, str):
        raise SkillParseError("'fork_context' must be a string")
    fork_context = fork_context.strip().lower()
    if fork_context not in _VALID_CONTEXTS:
        logger.warning(
            "Skill '%s': invalid fork_context '%s', falling back to 'none'. Valid contexts: %s",
            name,
            fork_context,
            sorted(_VALID_CONTEXTS),
        )
        fork_context = "none"

    # model (optional)
    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        raise SkillParseError("'model' must be a string or omitted")
    if isinstance(model, str):
        model = model.strip() or None

    return SkillMeta(
        name=name,
        description=description,
        allowed_tools=allowed_tools,
        mode=mode,  # type: ignore[arg-type]
        fork_context=fork_context,  # type: ignore[arg-type]
        model=model,
    )
