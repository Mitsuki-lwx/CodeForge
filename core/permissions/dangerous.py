"""危险命令黑名单检测器。

在执行前扫描 shell 命令文本，识别高危操作模式：
  - 远程脚本下载即执行（curl/wget | bash/sh/python）
  - 破坏性文件操作（rm -rf /, chmod 777 /, mkfs, dd）
  - 提权操作（sudo 危险命令, chown 系统目录）
  - Fork bomb、反弹 shell
  - 编码混淆执行（base64 | bash, eval 等）
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DangerousMatch:
    is_dangerous: bool
    reason: str


class DangerousCommandDetector:
    """危险命令黑名单检测器。

    用编译后的正则模式列表匹配命令文本，返回匹配结果与原因。
    """

    def __init__(self) -> None:
        self._patterns: list[tuple[re.Pattern, str]] = [
            # ── 远程下载即执行 ──
            (re.compile(r"curl\s+\S+\s*\|\s*(ba)?sh\b", re.I), "curl pipe to shell interpreter"),
            (re.compile(r"curl\s+\S+\s*\|\s*(python|perl|ruby|node)\b", re.I), "curl pipe to script interpreter"),
            (re.compile(r"wget\s+\S+\s*-O\s*-\s*\|\s*(ba)?sh\b", re.I), "wget pipe to shell interpreter"),
            (re.compile(r"wget\s+\S+\s*-O\s*-\s*\|\s*(python|perl|ruby|node)\b", re.I), "wget pipe to script interpreter"),
            (re.compile(r"curl\s+\S+\s*>\s*\S+\.sh?\s*&&?\s*(ba)?sh\s+\S+", re.I), "curl download then execute"),
            (re.compile(r"wget\s+\S+\s*-O\s+\S+\s*&&?\s*(ba)?sh\s+\S+", re.I), "wget download then execute"),

            # ── 破坏性文件操作 ──
            (re.compile(r"rm\s+-rf?\s+/", re.I), "rm -rf on root directory"),
            (re.compile(r"rm\s+-rf?\s+\/\*", re.I), "rm -rf /*"),
            (re.compile(r"rm\s+-rf?\s+~", re.I), "rm -rf home directory"),
            (re.compile(r"rm\s+-rf?\s+\S*\*\s*\/", re.I), "rm -rf with path wildcard near root"),
            (re.compile(r"rm\s+-rf?\s+\/\S+\/\*", re.I), "rm -rf on top-level directory wildcard"),
            (re.compile(r"chmod\s+(-R\s+)?777\s+/", re.I), "chmod 777 on system path"),
            (re.compile(r"chown\s+(-R\s+)?\S+\s+/(etc|usr|bin|sbin|lib|boot|sys|proc|dev)\b", re.I), "chown on system directory"),
            (re.compile(r"mkfs\b", re.I), "mkfs (filesystem format)"),
            (re.compile(r"dd\s+if=\S+\s+of=/dev/", re.I), "dd writing to device"),
            (re.compile(r">\s*/dev/sd[a-z]", re.I), "redirect to raw disk device"),

            # ── Fork bomb ──
            (re.compile(r":\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I), "fork bomb (:(){ :|:& };:)"),
            (re.compile(r"\(\)\s*\{\s*\S+\s*\|\s*\S+\s*&\s*\}", re.I), "fork bomb pattern"),
            (re.compile(r"while\s*\(\s*1\s*\)\s*;\s*do\s*\S+\s*;?\s*done", re.I), "infinite fork loop"),

            # ── 反弹 Shell ──
            (re.compile(r"bash\s+-[ic]\s+.*>&\s*/dev/tcp/", re.I), "bash reverse shell via /dev/tcp"),
            (re.compile(r"nc\s+-[el]\s+\S+\s+\d+", re.I), "netcat reverse/bind shell"),
            (re.compile(r"ncat\s+-[el]\s+\S+\s+\d+", re.I), "ncat reverse/bind shell"),
            (re.compile(r"python\s+-c\s+.*socket\.connect", re.I), "python reverse shell"),
            (re.compile(r"perl\s+-e\s+.*socket", re.I), "perl reverse shell"),
            (re.compile(r"php\s+-r\s+.*fsockopen", re.I), "php reverse shell"),
            (re.compile(r"ruby\s+-e\s+.*TCPSocket", re.I), "ruby reverse shell"),

            # ── 编码混淆执行 ──
            (re.compile(r"base64\s+(-d|--decode)\s*\S*\s*\|\s*(ba)?sh\b", re.I), "base64 decode piped to shell"),
            (re.compile(r"base64\s+(-d|--decode)\s*\S*\s*\|\s*python\b", re.I), "base64 decode piped to python"),
            (re.compile(r"xxd\s+-r\s*\S*\s*\|\s*(ba)?sh\b", re.I), "xxd decode piped to shell"),
            (re.compile(r"echo\s+\S+\s*\|\s*base64\s+-d\s*\|\s*(ba)?sh\b", re.I), "echo base64 piped to shell"),
            (re.compile(r"eval\s+.*\$\(curl", re.I), "eval with curl substitution"),
            (re.compile(r"eval\s+.*\$\(wget", re.I), "eval with wget substitution"),
            (re.compile(r"\$\(\s*curl\s+\S+\s*\)\s*\|\s*(ba)?sh\b", re.I), "curl subshell piped to shell"),

            # ── 提权操作 ──
            (re.compile(r"sudo\s+rm\s+-rf?\s+/", re.I), "sudo rm on root"),
            (re.compile(r"sudo\s+chmod\s+(-R\s+)?777\s+/", re.I), "sudo chmod 777 on system path"),
            (re.compile(r"sudo\s+chown\s+(-R\s+)?\S+\s+/", re.I), "sudo chown on root"),
            (re.compile(r"sudo\s+bash\b", re.I), "sudo bash (interactive root shell)"),
            (re.compile(r"sudo\s+su\b", re.I), "sudo su (switch to root)"),
            (re.compile(r"sudo\s+-[iu]\s+root", re.I), "sudo -i/-u root"),

            # ── 破坏系统配置 ──
            (re.compile(r">\s*/etc/(passwd|shadow|sudoers|hosts|resolv\.conf|fstab)\b", re.I), "overwrite critical system config"),
            (re.compile(r"mv\s+/\S+\s+/etc/(passwd|shadow|sudoers)\b", re.I), "replace critical system config"),
            (re.compile(r"cp\s+/\S+\s+/etc/(passwd|shadow|sudoers)\b", re.I), "copy to critical system config"),

            # ── 网络隧道 / 隐蔽通信 ──
            (re.compile(r"ssh\s+-[fNRD]+\s", re.I), "SSH tunneling (port forwarding)"),
            (re.compile(r"socat\s+.*exec:", re.I), "socat with exec"),
            (re.compile(r"socat\s+.*connect:", re.I), "socat with connect"),

            # ── 资源耗尽 ──
            (re.compile(r":\s*>\s*\S+\s*&\s*while\s*:", re.I), "resource exhaustion loop"),
            (re.compile(r"yes\s*>\s*/dev/null\s*&", re.I), "CPU exhaustion (yes > /dev/null &)"),
        ]

    def detect(self, command: str) -> DangerousMatch:
        """扫描命令文本，返回危险匹配结果。

        返回首个匹配的危险模式；未匹配返回 is_dangerous=False。
        """
        if not command or not command.strip():
            return DangerousMatch(is_dangerous=False, reason="")

        for pattern, reason in self._patterns:
            if pattern.search(command):
                return DangerousMatch(is_dangerous=True, reason=reason)

        return DangerousMatch(is_dangerous=False, reason="")
