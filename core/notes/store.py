"""笔记存储层。

四类笔记（user_preference / correction_feedback / project_knowledge /
reference_material），分项目级与用户级两级存放。每条笔记一个 Markdown 文件
（带 YAML frontmatter），每级一个 MEMORY.md 索引，索引由笔记文件重建派生。
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import yaml

# 笔记类型
NOTE_TYPES = frozenset(
    {
        "user_preference",
        "correction_feedback",
        "project_knowledge",
        "reference_material",
        # 会话状态（spec_session_state）：约束提升到 memory 时落 project/user
        "hard_constraint",
        "session_goal",
        "task_todo",
    }
)

# 索引文件名
INDEX_FILENAME = "MEMORY.md"

_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _now_iso() -> str:
    """本地时间 ISO 字符串（带时区偏移）。"""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _safe_slug(slug: str) -> str:
    """slug 仅保留小写字母/数字/下划线，防路径穿越。"""
    cleaned = _SLUG_RE.sub("_", (slug or "").lower())
    return cleaned.strip("_")


def _safe_filename(name: str) -> str:
    """文件名仅保留字母/数字/点/横线/下划线，防路径穿越。"""
    cleaned = _FILENAME_RE.sub("", name or "")
    if not cleaned or cleaned in ("..", ".") or ".." in cleaned:
        raise ValueError(f"非法笔记文件名: {name!r}")
    return cleaned


def _normalize_title(title: str) -> str:
    """标题归一：小写 + 去空白/符号，用于相似判定（对齐 slug 归一思路）。"""
    return re.sub(r"\s+", "", (title or "").lower()).strip("_")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _render_note(
    note_type: str, title: str, created: str, updated: str, content: str
) -> str:
    fm = yaml.safe_dump(
        {
            "type": note_type,
            "title": title,
            "created": created,
            "updated": updated,
        },
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{fm}---\n{content.rstrip()}\n"


def _parse_note(path: Path) -> dict:
    """解析单条笔记：frontmatter + body。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"笔记缺少 frontmatter: {path.name}")
    _, fm, body = text.split("---", 2)
    data = yaml.safe_load(fm) or {}
    return {
        "type": str(data.get("type", "")),
        "title": str(data.get("title", "")),
        "created": str(data.get("created", "")),
        "updated": str(data.get("updated", "")),
        "body": body.strip(),
    }


class NoteStore:
    """两级笔记仓库（项目级 / 用户级）。写操作线程安全。"""

    def __init__(
        self, workspace: str | Path, user_home: str | Path | None = None
    ) -> None:
        self._project_dir = Path(workspace).resolve() / ".codeforge" / "memory"
        home = Path(user_home) if user_home is not None else Path.home()
        self._user_dir = home.resolve() / ".codeforge" / "memory"
        self._lock = threading.Lock()

    def _dir(self, level: str) -> Path:
        if level == "user":
            return self._user_dir
        if level == "project":
            return self._project_dir
        raise ValueError(f"未知笔记级别: {level!r}")

    def _note_path(self, level: str, filename: str) -> Path:
        return self._dir(level) / _safe_filename(filename)

    # ── 写操作 ─────────────────────────────────────────────────

    def create_note(
        self,
        level: str,
        note_type: str,
        title: str,
        slug: str,
        content: str,
    ) -> Path:
        if note_type not in NOTE_TYPES:
            raise ValueError(f"非法笔记类型: {note_type!r}")
        d = self._dir(level)
        filename = f"{note_type}_{_safe_slug(slug)}.md"
        now = _now_iso()
        with self._lock:
            d.mkdir(parents=True, exist_ok=True)
            path = d / filename
            if path.exists():
                raise FileExistsError(f"笔记已存在: {path}")
            path.write_text(
                _render_note(note_type, title, now, now, content), encoding="utf-8"
            )
            self._rebuild_index(level)
        return path

    def upsert_note(
        self,
        level: str,
        note_type: str,
        title: str,
        slug: str,
        content: str,
    ) -> Path:
        """写入笔记；遇重复则覆盖（update）而非新建（create）。

        确定性去重兜底（LLM 判断去重不可靠时兜住）：
          ① 同 level 同 type 下，title 归一相同 或 body 前 80 字符相同 → 覆盖那条；
          ② 同 slug 文件已存在 → 覆盖（保留原 created）；
          ③ 否则新建。
        返回命中的或新建的笔记文件路径。
        """
        if note_type not in NOTE_TYPES:
            raise ValueError(f"非法笔记类型: {note_type!r}")
        d = self._dir(level)
        with self._lock:
            d.mkdir(parents=True, exist_ok=True)

            # ① 内容/标题相似 → 覆盖那条
            existing = self._find_similar(d, note_type, title, content)
            if existing is not None:
                self._overwrite_note(existing, title, content)
                self._rebuild_index(level)
                return existing

            # ② 同 slug 文件已存在 → 覆盖
            path = d / f"{note_type}_{_safe_slug(slug)}.md"
            if path.exists():
                self._overwrite_note(path, title, content)
                self._rebuild_index(level)
                return path

            # ③ 新建
            now = _now_iso()
            path.write_text(
                _render_note(note_type, title, now, now, content), encoding="utf-8"
            )
            self._rebuild_index(level)
            return path

    def _find_similar(
        self, d: Path, note_type: str, title: str, content: str
    ) -> Path | None:
        """同目录同 type 下找「相似」笔记：title 归一相同 或 body 前 80 字符相同。"""
        norm_title = _normalize_title(title)
        head = (content or "").strip()[:80]
        for p in sorted(d.glob("*.md")):
            if p.name == INDEX_FILENAME:
                continue
            try:
                fm = _parse_note(p)
            except (OSError, ValueError):
                continue
            if fm["type"] != note_type:
                continue
            if norm_title and _normalize_title(fm["title"]) == norm_title:
                return p
            existing_head = fm["body"].strip()[:80]
            if head and existing_head and (
                head.startswith(existing_head) or existing_head.startswith(head)
            ):
                return p
        return None

    def _overwrite_note(self, path: Path, title: str, content: str) -> None:
        """覆盖已有笔记：保留原 created，更新 title/body/updated。"""
        parsed = _parse_note(path)
        path.write_text(
            _render_note(
                parsed["type"], title, parsed["created"], _now_iso(), content
            ),
            encoding="utf-8",
        )

    def update_note(self, level: str, filename: str, title: str, content: str) -> None:
        with self._lock:
            path = self._note_path(level, filename)
            if not path.exists():
                raise FileNotFoundError(f"笔记不存在: {path}")
            parsed = _parse_note(path)
            path.write_text(
                _render_note(
                    parsed["type"], title, parsed["created"], _now_iso(), content
                ),
                encoding="utf-8",
            )
            self._rebuild_index(level)

    def delete_note(self, level: str, filename: str) -> None:
        with self._lock:
            path = self._note_path(level, filename)
            if path.exists():
                path.unlink()
            self._rebuild_index(level)

    # ── 读操作 ─────────────────────────────────────────────────

    def read_note(self, level: str, filename: str) -> str:
        path = self._note_path(level, filename)
        if not path.exists():
            raise FileNotFoundError(f"笔记不存在: {path}")
        return path.read_text(encoding="utf-8")

    def list_notes(self, level: str) -> list[dict]:
        """列出该级别全部笔记（type/title/body/path 等）。"""
        d = self._dir(level)
        if not d.exists():
            return []
        out: list[dict] = []
        for p in sorted(d.glob("*.md")):
            if p.name == INDEX_FILENAME:
                continue
            try:
                fm = _parse_note(p)
            except (OSError, ValueError):
                continue
            fm["path"] = p
            out.append(fm)
        return out

    def clear_notes(
        self, level: str | None = None, note_type: str | None = None
    ) -> int:
        """清空笔记（可按级别/类型过滤），返回删除数量。"""
        levels = [level] if level else ["project", "user"]
        removed = 0
        with self._lock:
            for lv in levels:
                d = self._dir(lv)
                if not d.exists():
                    continue
                for p in list(d.glob("*.md")):
                    if p.name == INDEX_FILENAME:
                        continue
                    if note_type is not None:
                        try:
                            fm = _parse_note(p)
                        except (OSError, ValueError):
                            continue
                        if fm["type"] != note_type:
                            continue
                    p.unlink()
                    removed += 1
                self._rebuild_index(lv)
        return removed

    def list_index(self, level: str) -> list[str]:
        path = self._dir(level) / INDEX_FILENAME
        if not path.exists():
            return []
        return [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def list_files(self) -> tuple[list[str], list[str]]:
        """列出项目层 + 用户层 memory 目录下的 .md 文件名（含 MEMORY.md）。

        Returns:
            (project_files, user_files)，各自按字典序排序；目录不存在返回空。
        """

        def _scan(level: str) -> list[str]:
            d = self._dir(level)
            if not d.is_dir():
                return []
            try:
                return sorted(p.name for p in d.glob("*.md"))
            except OSError as e:
                import logging

                logging.getLogger(__name__).warning("记忆目录读取失败 %s: %s", d, e)
                return []

        return _scan("project"), _scan("user")

    def full_index(self) -> str:
        """项目级索引在前、用户级在后拼接（F32）。"""
        lines = self.list_index("project") + self.list_index("user")
        return "\n".join(lines) if lines else ""

    # ── 索引重建 ────────────────────────────────────────────────

    def _rebuild_index(self, level: str) -> None:
        """从笔记文件 frontmatter 重建 MEMORY.md（确定序，避免行匹配漂移）。"""
        d = self._dir(level)
        if not d.exists():
            return
        lines: list[str] = []
        for p in sorted(d.glob("*.md")):
            if p.name == INDEX_FILENAME:
                continue
            try:
                fm = _parse_note(p)
            except (OSError, ValueError):
                continue
            desc = _first_line(fm["body"])[:80]
            lines.append(f"- [{fm['type']}] {fm['title']} — {desc}")
        text = "\n".join(lines)
        d.joinpath(INDEX_FILENAME).write_text(
            (text + "\n") if text else "", encoding="utf-8"
        )
