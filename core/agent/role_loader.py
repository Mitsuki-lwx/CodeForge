"""Agent 角色目录 —— 多来源加载 + 同名优先级覆盖。

加载顺序：内置 → 用户级 → 项目级，后加载的高优先级覆盖前者。
插件级（Source.PLUGIN）本期恒为空，仅占位。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from core.agent.roles import AgentRole, Source, parse_role_file

# ── 内置定义 ──────────────────────────────────────────────────────


def builtin_roles() -> list[AgentRole]:
    """加载随包发布的内置角色定义。

    通过 importlib.resources 读取 core.agent.builtin 包下的 *.md 文件。
    解析失败 raise（代码 bug，启动期 fail-fast）。

    Returns:
        按 name 升序排列的内置角色列表。
    """
    pkg = files("core.agent.builtin")
    roles: list[AgentRole] = []

    for entry in sorted(pkg.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".md"):
            continue
        data = entry.read_bytes()
        file_path = f"builtin:{entry.name}"
        role = parse_role_bytes_raw(data, file_path, Source.BUILTIN)
        if role is None:
            raise ValueError(f"Builtin role {file_path}: parse failed")
        roles.append(role)

    if not roles:
        raise RuntimeError("No builtin agent roles found — package may be corrupted")

    roles.sort(key=lambda r: r.name)
    return roles


# ── 本模块内部解析（不依赖 parse_role_bytes 的 sys.stderr 输出）─────


def parse_role_bytes_raw(
    data: bytes,
    file_path: str,
    source: Source,
) -> AgentRole | None:
    """同 roles.parse_role_bytes，但不输出 stderr（供内置加载用）。"""
    from core.agent.roles import _build_role, _split_frontmatter

    raw = data.decode("utf-8")
    fm_dict, body = _split_frontmatter(raw, file_path)
    if fm_dict is None:
        return None
    return _build_role(fm_dict, body, file_path, source)


# ── Catalog ───────────────────────────────────────────────────────


class Catalog:
    """角色目录：持有全部已加载角色定义，按 name 索引。

    resolve(name) 返回优先级最高的定义；fork_role() 返回 Fork 路径专用虚拟定义。
    """

    def __init__(self) -> None:
        self._defs: dict[str, AgentRole] = {}
        self._by_source: dict[Source, list[AgentRole]] = {
            Source.BUILTIN: [],
            Source.USER: [],
            Source.PROJECT: [],
            Source.PLUGIN: [],
        }

    # ── 查询 ───────────────────────────────────────────────────

    def resolve(self, name: str) -> AgentRole | None:
        """按 name 查找角色，返回优先级最高的定义。"""
        return self._defs.get(name)

    def list(self) -> list[AgentRole]:
        """返回所有角色（按 name 升序）。"""
        return sorted(self._defs.values(), key=lambda r: r.name)

    def list_by_source(self, src: Source) -> list[AgentRole]:
        """返回指定来源的全部角色（保持插入序）。"""
        return list(self._by_source.get(src, []))

    def fork_role(self) -> AgentRole:
        """返回 Fork 路径专用的虚拟角色定义。

        Fork 角色：system_prompt 留空（继承父 Agent），
        tools/disallowed_tools 留空（继承父工具集），
        不设权限限制（由父对话权限决定）。
        """
        return AgentRole(
            name="__fork__",
            description="Fork-based subagent — inherits parent context and tools",
            model="inherit",
            max_turns=0,
            permission_mode=__import__(
                "core.permissions.modes", fromlist=["PermissionMode"]
            ).PermissionMode.DEFAULT,
            source=Source.BUILTIN,
        )

    # ── 加载 ───────────────────────────────────────────────────

    def _add_all(self, roles: list[AgentRole]) -> None:
        """批量添加角色。同名时后添加的覆盖先添加的（高优先级覆盖低优先级）。

        Args:
            roles: 待添加的角色列表。
        """
        for role in roles:
            self._defs[role.name] = role
            self._by_source[role.source].append(role)


# ── 目录加载 ──────────────────────────────────────────────────────


def _load_from_dir(
    directory: Path,
    source: Source,
) -> list[AgentRole]:
    """从一个目录加载全部 *.md 角色文件。

    文件解析失败 → stderr 警告并跳过，不阻断。
    目录不存在 → 返回空列表。

    Args:
        directory: 目录路径。
        source: 定义来源标签。

    Returns:
        解析成功的 AgentRole 列表。
    """
    if not directory.is_dir():
        return []

    roles: list[AgentRole] = []
    for md_file in sorted(directory.glob("*.md")):
        role = parse_role_file(str(md_file), source)
        if role is not None:
            roles.append(role)

    return roles


def load_catalog(root: str | Path) -> Catalog:
    """加载全部来源的角色定义，构造 Catalog。

    加载顺序：内置 → 用户级 → 项目级（后加载的覆盖先加载的同名角色）。
    插件级本期恒为空。

    Args:
        root: 项目根目录（用于项目级 .codeforge/agents/ 路径）。

    Returns:
        加载完成的 Catalog（即使无任何自定义角色也非空，至少含内置角色）。
    """
    catalog = Catalog()

    # 1. 内置
    catalog._add_all(builtin_roles())

    # 2. 用户级
    user_dir = Path.home() / ".codeforge" / "agents"
    catalog._add_all(_load_from_dir(user_dir, Source.USER))

    # 3. 插件级（占位，恒为空）
    # catalog._add_all(_load_plugin_roles())

    # 4. 项目级（最高优先级，最后加载）
    project_dir = Path(root) / ".codeforge" / "agents"
    catalog._add_all(_load_from_dir(project_dir, Source.PROJECT))

    return catalog
