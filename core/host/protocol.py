"""host 控制通道的线协议：TCP loopback + 换行分隔 JSON。

**为什么不是 HTTP**（方案 C 的理由，见 spec）：与 `core/mcp/transport/` 的 stdio
传输同构（`Transport` 抽象 + 行协议），事件流天然是流式推送，且不引入任何新依赖。
代价是浏览器连不上——那留给二期，届时在核心协议之上加一层 HTTP 桥即可。

**帧格式**：一行一个 JSON 对象，`\\n` 结尾。`json.dumps` 会把字符串里的换行转义成
`\\n` 两个字面字符，所以一个对象永远只占一行——这是"换行分隔"能成立的前提。

三种帧靠**有无 `id` / `event`** 区分，不额外引入类型字段：

    请求  {"id": "1", "cmd": "list_runs", "args": {...}}
    应答  {"id": "1", "ok": true,  "data": {...}}
          {"id": "1", "ok": false, "error": "..."}
    事件  {"event": "ToolCallStarted", "data": {...}}     # 无 id，服务端主动推

**鉴权**：首帧必须是 `{"cmd": "hello", "token": "...", "protocol": 1}`；校验失败立即
断开，且**不返回任何 run 数据**。token 存 `<workspace>/.codeforge/host.token`。

**会合**：控制通道要在两个进程之间碰头，靠 `<workspace>/.codeforge/` 下两个文件：

    host.token   凭什么连 —— 凭据，每次启动换新，0600
    host.json    往哪连 —— 端口是内核分配的（`port=0`），不落盘客户端无从得知

不用固定端口（会撞），也不用"扫端口"（那是恶意软件的行为特征，也会被防火墙当异常
流量）。两者都是**进程私有**的，所以都在这里而不是各自的模块里。

token 文件的 0600 权限只在 POSIX 上真正生效——Windows 的 `os.chmod` 只切只读位，
设不了 ACL。本机 loopback + 单用户场景下这是可接受的降级，但不能假装它等于 0600；
要真正收紧 Windows 上的权限需要 ACL 操作（`pywin32` 或 ctypes），不在首期。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

# 协议版本：客户端与服务端各自携带，不匹配时服务端拒绝握手
PROTOCOL_VERSION = 1

# 只监听 loopback：不绑 0.0.0.0，避免把控制通道暴露到局域网
HOST_BIND_ADDRESS = "127.0.0.1"

# 端口 0 = 让内核分配随机端口（默认，避免端口冲突）。实际端口落 `host.json`。
DEFAULT_PORT = 0

# token 文件名（位于 <workspace>/.codeforge/ 下）
HOST_TOKEN_FILENAME = "host.token"

# host 会合信息文件名（位于 <workspace>/.codeforge/ 下）
HOST_INFO_FILENAME = "host.json"

# 会合文件的权限（POSIX 生效；Windows 见模块 docstring）
TOKEN_FILE_MODE = 0o600

# 单帧上限。超过即断开——不设上限的话，一个不发换行的客户端能把内存撑爆。
MAX_FRAME_BYTES = 1 << 20


class ProtocolError(Exception):
    """帧不合法（不是 JSON、不是对象、缺字段）。"""


def encode_frame(payload: dict[str, Any]) -> bytes:
    """把字典编码成一帧（单行 JSON + 换行）。"""
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def decode_frame(line: bytes | str) -> dict[str, Any]:
    """把一帧解成字典。

    Raises:
        ProtocolError: 不是合法 JSON，或解出来不是对象。
    """
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"非法 JSON 帧: {e}") from e
    if not isinstance(data, dict):
        raise ProtocolError(f"帧必须是 JSON 对象，实际是 {type(data).__name__}")
    return data


def to_jsonable(value: Any) -> Any:
    """把任意值转成可 JSON 序列化的形状。

    事件是 dataclass、`CompactPhase` 是 Enum、`CompactEvent.err` 是异常对象——
    客户端只做展示，不认识这些类型，统一在这里收口，别让 `json.dumps` 在
    推送途中炸掉整条连接。
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def event_frame(event: Any) -> dict[str, Any]:
    """把一个 Agent 事件包成事件帧。用类名当事件名，不需要维护映射表。"""
    return {"event": type(event).__name__, "data": to_jsonable(event)}


# ── token ───────────────────────────────────────────────────────


def token_path(workspace: str | Path) -> Path:
    """返回 `<workspace>/.codeforge/host.token`。"""
    return Path(workspace).resolve() / ".codeforge" / HOST_TOKEN_FILENAME


def issue_token(workspace: str | Path) -> str:
    """生成新 token 并落盘，返回 token 本身。

    每次 host 启动都换新 token：旧客户端会因 token 不匹配被拒，这正是想要的
    ——上一轮 host 的凭据不该继续有效。

    用 `os.open(..., 0o600)` 而不是"先 write 再 chmod"：后者有一段文件已存在但
    权限还没收紧的窗口。
    """
    token = secrets.token_urlsafe(32)
    path = token_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, TOKEN_FILE_MODE)
    try:
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)
    return token


def load_token(workspace: str | Path) -> str | None:
    """读 token；文件不存在或不可读时返回 `None`。"""
    try:
        return token_path(workspace).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def token_matches(expected: str | None, provided: object) -> bool:
    """恒定时间比对 token。

    没有 token 文件（`expected` 为空）一律拒绝，不做"本地就免鉴权"的假设。
    """
    if not expected or not isinstance(provided, str):
        return False
    return hmac.compare_digest(expected, provided)


# ── 会合：host 在哪 ─────────────────────────────────────────────


@dataclass(frozen=True)
class HostInfo:
    """`host.json` 的内容：客户端连上 host 所需的全部定位信息。"""

    port: int
    pid: int
    run_id: str
    protocol: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "pid": self.pid,
            "run_id": self.run_id,
            "protocol": self.protocol,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HostInfo | None:
        """从字典还原；缺字段或类型不对返回 `None`（把"坏文件"和"没文件"归一）。"""
        port = data.get("port")
        pid = data.get("pid")
        run_id = data.get("run_id")
        if not isinstance(port, int) or not isinstance(pid, int):
            return None
        if not isinstance(run_id, str):
            return None
        protocol = data.get("protocol")
        return cls(
            port=port,
            pid=pid,
            run_id=run_id,
            protocol=protocol if isinstance(protocol, int) else PROTOCOL_VERSION,
        )


def host_info_path(workspace: str | Path) -> Path:
    """返回 `<workspace>/.codeforge/host.json`。"""
    return Path(workspace).resolve() / ".codeforge" / HOST_INFO_FILENAME


def write_host_info(workspace: str | Path, info: HostInfo) -> None:
    """原子写 `host.json`。

    先写临时文件再 `os.replace`：客户端随时可能来读，半截 JSON 会让它把"host 在跑"
    误判成"没有 host"。`os.replace` 在同一文件系统内是原子的。
    """
    path = host_info_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, TOKEN_FILE_MODE)
    try:
        os.write(fd, json.dumps(info.to_dict(), ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def load_host_info(workspace: str | Path) -> HostInfo | None:
    """读 `host.json`；不存在 / 不可读 / 内容不合法一律返回 `None`。"""
    try:
        raw = host_info_path(workspace).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return HostInfo.from_dict(data) if isinstance(data, dict) else None


def clear_host_info(workspace: str | Path, *, pid: int) -> bool:
    """删掉 `host.json`，**但只在它还是 `pid` 写的这一份时**。

    不做这个校验就会踩到：旧 host 收尾时把刚启动的新 host 的会合信息删掉，客户端
    于是找不到一个明明在跑的 host。和单写者锁"只删自己的锁文件"是同一个道理。

    Returns:
        真的删掉了返回 `True`；文件不属于 `pid` 或删除失败返回 `False`。
    """
    info = load_host_info(workspace)
    if info is None or info.pid != pid:
        return False
    try:
        host_info_path(workspace).unlink()
    except OSError:
        return False
    return True
