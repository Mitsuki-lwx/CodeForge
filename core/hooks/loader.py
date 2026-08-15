"""两级 YAML 加载、合并、同名冲突、容错。

项目级 `<root>/.codeforge/hooks.yaml` 优先，用户级 `~/.codeforge/hooks.yaml` 兜底。
加载错误一律 stderr warning 后继续，不阻断启动（N1/N2/N9）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from core.hooks.rules import HookRule, parse_rule

logger = logging.getLogger(__name__)

HOOKS_FILENAME = "hooks.yaml"
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smh]?)$")
_UNIT_SECONDS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0}


def project_hooks_path(work_dir: Path) -> Path:
    return work_dir / ".codeforge" / "hooks.yaml"


def user_hooks_path() -> Path:
    return Path.home() / ".codeforge" / "hooks.yaml"


def _parse_duration(s: object) -> float | None:
    """解析 timeout：数字秒或时长串（"30s" / "5m" / "1.5h"）。非法返回 None。"""
    if isinstance(s, (int, float)):
        return float(s) if float(s) > 0 else None
    if isinstance(s, str):
        m = _DURATION_RE.match(s.strip())
        if m:
            seconds = float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
            return seconds if seconds > 0 else None
    return None


def _warn(problems: list[str], msg: str) -> None:
    logger.warning(msg)
    problems.append(msg)


def _load_level(path: Path, problems: list[str]) -> list[HookRule]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        _warn(problems, f"Skipping hooks config {path}: {e}")
        return []
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        _warn(problems, f"Skipping hooks config {path}: top-level 'rules' list missing")
        return []

    rules: list[HookRule] = []
    for i, raw in enumerate(data["rules"]):
        if not isinstance(raw, dict):
            _warn(problems, f"Skipping hook rule #{i}: not an object")
            continue
        name = str(raw.get("name") or "").strip()
        raw = dict(raw)  # 拷贝，避免改写 YAML 原始 dict
        if "timeout" in raw:
            t = _parse_duration(raw["timeout"])
            if t is None:
                _warn(
                    problems,
                    f'Skipping hook rule #{i} (name="{name}"): '
                    f"invalid timeout {raw['timeout']!r}",
                )
                continue
            raw["timeout"] = t
        rule, errors = parse_rule(raw, str(path), i)
        if rule is None:
            _warn(
                problems,
                f'Skipping hook rule #{i} (name="{name}"): {errors}',
            )
            continue
        rules.append(rule)
    return rules


def load_hooks_config(
    work_dir: str | Path,
) -> tuple[list[HookRule], list[str], list[str]]:
    """加载两级配置并合并。

    Returns:
        (rules, problems, sources)
        rules: 合并后的规则，项目级在前、用户级在后（跨层同名项目级胜出）。
        problems: 启动期告警文本（仅日志，不阻断）。
        sources: 实际加载到规则的文件路径列表，供 /hooks 显示。
    """
    problems: list[str] = []
    root = Path(work_dir)
    proj_path = project_hooks_path(root)
    user_path = user_hooks_path()

    project_rules = _load_level(proj_path, problems)
    user_rules = _load_level(user_path, problems)

    project_names = {r.name for r in project_rules}
    kept_user: list[HookRule] = []
    for r in user_rules:
        if r.name in project_names:
            _warn(problems, f'hook "{r.name}": name conflict, skipping (project wins)')
            continue
        kept_user.append(r)

    rules = project_rules + kept_user
    sources: list[str] = []
    if project_rules:
        sources.append(str(proj_path))
    if kept_user:
        sources.append(str(user_path))
    return rules, problems, sources
