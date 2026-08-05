"""Skill 远程安装 —— 从 GitHub URL 下载并安装 Skill。

支持三种 URL 格式：
- skills.sh/<owner>/<repo>
- github.com/<owner>/<repo>/tree/<ref>/<path>
- raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>/SKILL.md

通过 GitHub Contents API 递归拉取目录，验证 SKILL.md 存在后 atomic rename 安装。
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 限额常量
MAX_FILE_SIZE = 1 * 1024 * 1024  # 单文件 1 MiB
MAX_TOTAL_SIZE = 8 * 1024 * 1024  # 总大小 8 MiB
MAX_FILE_COUNT = 64  # 最大文件数
MAX_RECURSION_DEPTH = 4  # 最大递归深度

# URL 解析正则
_GITHUB_TREE_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$")
_RAW_GITHUB_RE = re.compile(
    r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$"
)
_SKILLS_SH_RE = re.compile(r"https?://skills\.sh/([^/]+)/([^/]+)$")


def parse_skill_url(url: str) -> tuple[str, str, str, str]:
    """解析 Skill URL，返回 (owner, repo, ref, subpath)。

    Raises:
        ValueError: URL 格式不被支持。
    """
    # raw.githubusercontent.com
    m = _RAW_GITHUB_RE.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        # 去掉尾部的 /SKILL.md
        if path.endswith("/SKILL.md"):
            path = path[:-9]
        elif path == "SKILL.md":
            path = ""
        return owner, repo, ref, path

    # github.com tree
    m = _GITHUB_TREE_RE.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        return owner, repo, ref, path

    # skills.sh
    m = _SKILLS_SH_RE.match(url)
    if m:
        owner, repo = m.groups()
        return owner, repo, "main", ""

    raise ValueError(
        f"Unsupported URL format: {url}. "
        f"Supported: github.com tree, raw.githubusercontent.com, skills.sh"
    )


async def install_from_url(url: str, install_root: Path) -> str:
    """从 URL 安装 Skill 到指定目录。

    Args:
        url: Skill 的 GitHub URL。
        install_root: 安装根目录（~/.codeforge/skills/）。

    Returns:
        Skill 目录名（skill_name）。

    Raises:
        ValueError: URL 格式错误或下载内容不合法。
        OSError: I/O 错误。
    """
    owner, repo, ref, subpath = parse_skill_url(url)

    # 下载到临时目录
    with tempfile.TemporaryDirectory(prefix="codeforge-skill-") as tmpdir:
        staging = Path(tmpdir) / "skill"
        staging.mkdir()

        total_size = 0
        file_count = 0

        await _download_recursive(
            owner,
            repo,
            ref,
            subpath,
            staging,
            0,
            total_size_ref=[total_size],
            file_count_ref=[file_count],
        )

        # 验证含 SKILL.md
        skill_md = staging / "SKILL.md"
        if not skill_md.is_file():
            raise ValueError(
                "Downloaded content does not contain a SKILL.md file. "
                "Expected at root of the skill directory."
            )

        # 确定 skill 目录名
        if subpath:
            skill_name = Path(subpath).name
        else:
            skill_name = repo

        # 验证目录名合法性
        if not re.match(r"^[a-z][a-z0-9\-]*$", skill_name):
            raise ValueError(
                f"Skill directory name '{skill_name}' is invalid. "
                f"Must match ^[a-z][a-z0-9\\-]*$"
            )

        # Atomic rename 到安装目录
        install_root.mkdir(parents=True, exist_ok=True)
        target = install_root / skill_name

        # 如果目标已存在，先移除
        if target.exists():
            import shutil

            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

        # 移动（同文件系统内是原子的）
        staging.rename(target)

        logger.info("Installed skill '%s' to %s", skill_name, target)
        return skill_name


async def _download_recursive(
    owner: str,
    repo: str,
    ref: str,
    path: str,
    dest: Path,
    depth: int,
    total_size_ref: list[int],
    file_count_ref: list[int],
) -> None:
    """递归从 GitHub Contents API 下载目录。

    Args:
        owner, repo, ref: GitHub 仓库信息。
        path: 仓库内相对路径。
        dest: 本地目标目录。
        depth: 当前递归深度。
        total_size_ref: 总大小累计（用列表传引用）。
        file_count_ref: 文件数累计。

    Raises:
        ValueError: 超过限额或路径不安全。
    """
    if depth > MAX_RECURSION_DEPTH:
        raise ValueError(f"Maximum recursion depth ({MAX_RECURSION_DEPTH}) exceeded")

    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"

    import httpx

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(api_url)
        resp.raise_for_status()
        entries = resp.json()

        if not isinstance(entries, list):
            entries = [entries]

        for entry in entries:
            entry_type = entry.get("type", "file")
            entry_name = entry.get("name", "")

            # 安全检查
            if ".." in entry_name or entry_name.startswith("/"):
                raise ValueError(f"Unsafe path component: {entry_name}")

            file_count_ref[0] += 1
            if file_count_ref[0] > MAX_FILE_COUNT:
                raise ValueError(f"Maximum file count ({MAX_FILE_COUNT}) exceeded")

            if entry_type == "dir":
                subdir = dest / entry_name
                subdir.mkdir(parents=True, exist_ok=True)
                sub_path = f"{path}/{entry_name}" if path else entry_name
                await _download_recursive(
                    owner,
                    repo,
                    ref,
                    sub_path,
                    subdir,
                    depth + 1,
                    total_size_ref,
                    file_count_ref,
                )

            elif entry_type == "file":
                size = entry.get("size", 0)
                if size > MAX_FILE_SIZE:
                    raise ValueError(
                        f"File '{entry_name}' ({size} bytes) exceeds "
                        f"maximum size ({MAX_FILE_SIZE} bytes)"
                    )
                total_size_ref[0] += size
                if total_size_ref[0] > MAX_TOTAL_SIZE:
                    raise ValueError(
                        f"Total size ({total_size_ref[0]} bytes) exceeds "
                        f"maximum ({MAX_TOTAL_SIZE} bytes)"
                    )

                # 下载文件
                download_url = entry.get("download_url")
                if not download_url:
                    continue

                file_resp = await client.get(download_url)
                file_resp.raise_for_status()

                file_path = dest / entry_name
                file_path.write_bytes(file_resp.content)
