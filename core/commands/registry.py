"""命令注册中心。

启动期注册全部内置命令；名字/别名冲突立即抛错，杜绝运行时静默失效。
"""

from __future__ import annotations

from core.commands.types import Command


class CommandRegistryError(RuntimeError):
    """命令注册冲突。"""


class Registry:
    """命令注册中心。

    - 主名与别名统一登记到 `_by_name`（key 已小写、不带 `/`）
    - `_visible` 按 name 字典序维护，供 /help 与补全
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._visible: list[Command] = []

    def register(self, cmd: Command) -> None:
        """注册命令；名/别名冲突即抛 CommandRegistryError。"""
        name = cmd.name
        if not name or name != name.lower():
            raise CommandRegistryError(f"命令名必须小写非空: {name!r}")
        for alias in cmd.aliases:
            if not alias or alias != alias.lower():
                raise CommandRegistryError(f"别名必须小写非空: {alias!r}")
            if alias == name:
                raise CommandRegistryError(f"别名与命令名重复: {alias}")

        for key in (name, *cmd.aliases):
            if key in self._by_name:
                other = self._by_name[key]
                raise CommandRegistryError(
                    f"command conflict: {key!r}（{other.name} 已占用）"
                )

        for key in (name, *cmd.aliases):
            self._by_name[key] = cmd

        if not cmd.hidden:
            self._visible.append(cmd)
            self._visible.sort(key=lambda c: c.name)

    def lookup(self, name: str) -> Command | None:
        """按名字/别名查找（大小写不敏感）。"""
        return self._by_name.get(name.lstrip("/").lower())

    def visible(self) -> list[Command]:
        """返回按字典序排序的可见命令拷贝。"""
        return list(self._visible)

    def prefix_match(self, prefix: str) -> list[Command]:
        """按命令名前缀匹配（strip '/'、小写），不匹配别名/描述。"""
        p = prefix.lstrip("/").lower()
        if p == "":
            return list(self._visible)
        return [c for c in self._visible if c.name.startswith(p)]

    def remove(self, name: str) -> None:
        """移除一条已注册的命令（含别名映射）。

        Args:
            name: 命令名（不带 / 前缀）。
        """
        key = name.strip("/").lower()
        cmd = self._by_name.pop(key, None)
        if cmd is None:
            return
        # 清理别名映射
        for alias in cmd.aliases:
            self._by_name.pop(alias, None)
        # 从 visible 列表移除
        if cmd in self._visible:
            self._visible.remove(cmd)
