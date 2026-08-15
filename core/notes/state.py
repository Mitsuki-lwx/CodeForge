"""会话状态存储：用户目标 / 待办 / 硬性约束。

会话级状态（目标、待办、约束默认）落 `<session_dir>/state/` 下，一个条目一个
.md 文件（YAML frontmatter + body）。约束可**显式提升**到 project/user memory
（跨会话/跨项目持久，见 spec_session_state.md）。

复用 `core/notes/store` 的序列化 / 安全 slug / 锁模式；提升依赖传入的 `NoteStore`
实例（写 memory 目录）。线程安全：单锁串行。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from core.notes.store import _now_iso, _parse_note, _render_note, _safe_slug

SESSION_GOAL = "session_goal"
TASK_TODO = "task_todo"
HARD_CONSTRAINT = "hard_constraint"
STATE_TYPES = frozenset({SESSION_GOAL, TASK_TODO, HARD_CONSTRAINT})

# 待办完成状态存 frontmatter 的字段名
_DONE_KEY = "done"


class SessionStateStore:
    """会话级状态的读写；约束支持 promote 到 memory。"""

    def __init__(
        self, session_dir: str | Path, notes: Any | None = None
    ) -> None:
        self._state_dir = Path(session_dir) / "state"
        self._notes = notes  # NoteStore | None：promote 约束到 project/user 用
        self._lock = threading.Lock()

    # ── 目标 ────────────────────────────────────────────────────

    def set_goal(self, text: str) -> Path:
        """设置当前目标（覆盖上一条目标）。"""
        return self._write(SESSION_GOAL, "current", "当前目标", text)

    def get_goal(self) -> str | None:
        p = self._state_dir / f"{SESSION_GOAL}_current.md"
        if not p.exists():
            return None
        return _parse_note(p)["body"]

    # ── 待办 ────────────────────────────────────────────────────

    def add_todo(self, text: str) -> Path:
        """新增一条待办；slug 由文本前 20 字生成。"""
        slug = _safe_slug(text[:20])
        if not slug:
            slug = f"todo-{len(list(self._state_dir.glob(f'{TASK_TODO}_*.md'))) + 1}"
        return self._write_todo(slug, text, done=False)

    def toggle_todo(self, slug: str, done: bool) -> Path:
        """勾选/取消勾选一条待办（done 存 frontmatter）。"""
        path = self._state_dir / f"{TASK_TODO}_{_safe_slug(slug)}.md"
        if not path.exists():
            raise FileNotFoundError(f"待办不存在: {path}")
        return self._write_todo(_safe_slug(slug), _parse_note(path)["body"], done=done)

    def list_todos(self) -> list[dict]:
        out: list[dict] = []
        for p in sorted(self._state_dir.glob(f"{TASK_TODO}_*.md")):
            try:
                fm = _parse_note(p)
            except (OSError, ValueError):
                continue
            done = _read_frontmatter_field(p, _DONE_KEY, default=False)
            out.append({"id": p.name, "text": fm["body"], "done": bool(done)})
        return out

    # ── 硬性约束 ────────────────────────────────────────────────

    def add_constraint(self, text: str, persist: str | None = None) -> Path:
        """新增一条硬性约束。

        persist=None → 会话级（落会话目录）；"project"/"user" → 直接提升到对应
        memory（跨会话/跨项目）。提升由调用方显式传，绝不默认。
        """
        slug = _safe_slug(text[:20]) or "constraint"
        if persist in ("project", "user"):
            return self._promote(slug, text, persist)
        return self._write(HARD_CONSTRAINT, slug, "硬性约束", text)

    def list_constraints(self, include_persisted: bool = False) -> list[dict]:
        out: list[dict] = []
        for p in sorted(self._state_dir.glob(f"{HARD_CONSTRAINT}_*.md")):
            try:
                fm = _parse_note(p)
            except (OSError, ValueError):
                continue
            out.append({"id": p.name, "text": fm["body"], "level": "session"})
        if include_persisted and self._notes is not None:
            for lvl in ("project", "user"):
                for it in self._notes.list_notes(lvl):
                    if it.get("type") == HARD_CONSTRAINT:
                        out.append(
                            {
                                "id": it["path"].name,
                                "text": it["body"],
                                "level": lvl,
                            }
                        )
        return out

    def promote_constraint(self, slug: str, target: str) -> Path:
        """把会话级约束显式提升到 project/user memory（跨会话/跨项目）。"""
        path = self._state_dir / f"{HARD_CONSTRAINT}_{_safe_slug(slug)}.md"
        if not path.exists():
            raise FileNotFoundError(f"约束不存在: {path}")
        text = _parse_note(path)["body"]
        return self._promote(_safe_slug(slug), text, target)

    def _promote(self, slug: str, text: str, target: str) -> Path:
        """提升：写入 memory（用传入的 NoteStore），并从会话目录删除（避免重复）。"""
        if self._notes is None:
            raise RuntimeError("promote 需要注入 NoteStore 实例")
        if target not in ("project", "user"):
            raise ValueError(f"未知提升目标: {target!r}")
        title = "硬性约束"
        p = self._notes.create_note(target, HARD_CONSTRAINT, title, slug, text)
        # 会话级副本删除，避免跨会话后重复注入
        self._state_dir.joinpath(f"{HARD_CONSTRAINT}_{_safe_slug(slug)}.md").unlink(
            missing_ok=True
        )
        return p

    # ── 汇总 ────────────────────────────────────────────────────

    def list_state(self) -> dict:
        return {
            "goal": self.get_goal(),
            "todos": self.list_todos(),
            "constraints": [c["text"] for c in self.list_constraints()],
        }

    # ── 内部 ────────────────────────────────────────────────────

    def _write(self, note_type: str, slug: str, title: str, content: str) -> Path:
        """写一个普通状态条目（goal/constraint），复用笔记 md 格式。"""
        if note_type not in STATE_TYPES:
            raise ValueError(f"非法状态类型: {note_type!r}")
        path = self._state_dir / f"{note_type}_{_safe_slug(slug)}.md"
        now = _now_iso()
        with self._lock:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            if path.exists():
                try:
                    created = _parse_note(path)["created"]
                except (OSError, ValueError):
                    created = now
            else:
                created = now
            path.write_text(
                _render_note(note_type, title, created, now, content),
                encoding="utf-8",
            )
        return path

    def _write_todo(self, slug: str, text: str, done: bool) -> Path:
        """写待办：frontmatter 带 done 字段。"""
        path = self._state_dir / f"{TASK_TODO}_{_safe_slug(slug)}.md"
        now = _now_iso()
        with self._lock:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            if path.exists():
                try:
                    created = _parse_note(path)["created"]
                except (OSError, ValueError):
                    created = now
            else:
                created = now
            fm = {
                "type": TASK_TODO,
                "title": text,
                _DONE_KEY: bool(done),
                "created": created,
                "updated": now,
            }
            body = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
            path.write_text(f"---\n{body}---\n{text}\n", encoding="utf-8")
        return path


def _read_frontmatter_field(path: Path, key: str, default: Any = None) -> Any:
    """读 md frontmatter 的单个字段（todo 的 done 用）。"""
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return default
        _, fm, _ = text.split("---", 2)
        data = yaml.safe_load(fm) or {}
        return data.get(key, default)
    except Exception:  # noqa: BLE001 —— 解析失败取默认
        return default


__all__ = [
    "HARD_CONSTRAINT",
    "SESSION_GOAL",
    "STATE_TYPES",
    "TASK_TODO",
    "SessionStateStore",
]
